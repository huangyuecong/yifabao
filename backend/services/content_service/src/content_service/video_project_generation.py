from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import math
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from .generation_errors import describe_generation_error
from .job_leases import enqueue_job
from .llm_adapters import ProviderCallError
from .media_adapters import download_generated_video, render_srt
from .media_ingest import (
    MediaIngestPolicy,
    MediaValidationError,
    probe_media,
    read_upload_head,
    validate_upload_header,
)
from .models import Artifact, ContentTask, Job, ProjectAsset, ResourceEntry, VideoProject
from .provider_native_video import (
    PROVIDER_NATIVE_VIDEO_TRANSPORT,
    ProviderNativeVideoReference,
    resolve_provider_native_video_reference,
)
from .providers import (
    AiVideoGenerationPort,
    AiVideoGenerationRequest,
    AiVideoTask,
    AssetStorePort,
    AssetWriteResult,
    GeneratedAsset,
    ImageGenerationPort,
    ImageGenerationRequest,
    ProviderMediaPublication,
    ProviderMediaPublisherPort,
    ProviderStatus,
    SpeechSynthesisPort,
    SpeechSynthesisRequest,
    SubtitleAlignmentPort,
    SubtitleAlignmentRequest,
    SubtitleSegment,
    WordTiming,
)
from .services import (
    VideoProjectValidationError,
    create_artifact,
    dump,
    ensure_project_asset,
    load,
    project_asset_storage,
)
from .workflow import adopt_revision

logger = logging.getLogger(__name__)

ProjectImageRole = Literal["video", "overlay", "logo", "intro", "outro"]
ProjectAudioFormat = Literal["wav", "mp3", "ogg_opus"]
VIDEO_PROJECT_AI_JOB_KIND = "video_project_ai_generation"
# A submission reservation may become visible before its caller commits on
# SQLite. It must never be claimed while the provider request is still in flight.
_AI_VIDEO_SUBMISSION_HOLD_UNTIL = datetime(9999, 1, 1, tzinfo=UTC)
_AI_VIDEO_TASK_NOT_CREATED_ERROR = "AI 视频服务未能创建生成任务，请重新提交或更换参考素材"
_SENSITIVE_METADATA_KEYS = re.compile(
    r"(^|_)(api_?key|authorization|bearer|cookie|password|secret|token)($|_)",
    re.IGNORECASE,
)
_AUDIO_EXTENSION_BY_FORMAT = {
    "wav": "wav",
    "mp3": "mp3",
    "ogg_opus": "ogg",
}
_PROJECT_IMAGE_ROLES = frozenset({"video", "overlay", "logo", "intro", "outro"})
_IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp"})
_VIDEO_EXTENSIONS = frozenset({"mp4", "mov", "m4v", "webm", "mkv"})
_AI_VIDEO_REFERENCE_IMAGE_MAX_COUNT = 9
_AI_VIDEO_REFERENCE_VIDEO_MAX_COUNT = 3
_AI_VIDEO_REFERENCE_IMAGE_MAX_BYTES = 30 * 1024 * 1024
_AI_VIDEO_REFERENCE_VIDEO_MAX_BYTES = 200 * 1024 * 1024
_AI_VIDEO_REFERENCE_VIDEO_MIN_SECONDS = 2.0
_AI_VIDEO_REFERENCE_VIDEO_MAX_SECONDS = 15.0
_AI_VIDEO_REFERENCE_VIDEO_MAX_TOTAL_SECONDS = 15.0
_AI_VIDEO_REQUEST_BODY_MAX_BYTES = 64 * 1024 * 1024
_AI_VIDEO_PROVIDER_CONTENT_INDEX = re.compile(r"\bcontent\[(\d+)]", re.IGNORECASE)
_AI_VIDEO_PRIVACY_ERROR_CODES = frozenset(
    {
        "InputImageSensitiveContentDetected.PrivacyInformation",
        "input_image_privacy_information",
    }
)
PROVIDER_MEDIA_PUBLICATIONS_RESULT_KEY = "_provider_media_publications"


class VideoProjectGenerationError(RuntimeError):
    pass


class VideoProjectGenerationValidationError(VideoProjectGenerationError):
    pass


class VideoProjectGenerationConflictError(VideoProjectGenerationError):
    pass


class VideoProjectGenerationSafetyError(VideoProjectGenerationValidationError):
    """A stable, actionable input-safety rejection safe to return to clients."""

    def __init__(self, message: str, *, details: Mapping[str, object]):
        super().__init__(message)
        self.details = dict(details)


@dataclass(frozen=True, slots=True)
class ProjectVoiceGenerationSpec:
    text: str
    label: str = "AI 配音"
    voice: str | None = None
    speed: float = 1.0
    volume: float = 1.0
    audio_format: ProjectAudioFormat = "mp3"
    sample_rate: int = 24_000
    with_timestamps: bool = True
    instruction: str | None = None
    parent_artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectSubtitleGenerationSpec:
    text: str | None = None
    label: str = "AI 字幕"
    source_project_asset_id: str | None = None
    duration_seconds: float | None = None
    max_chars_per_line: int | None = None
    max_lines: int | None = None
    parent_artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectImageGenerationSpec:
    prompt: str
    role: ProjectImageRole = "video"
    count: int = 1
    label: str = "AI 图片候选"
    size: str | None = None
    ratio: str | None = None
    watermark: bool = False
    seed: int | None = None
    reference_images: tuple[bytes, ...] = ()
    reference_media_types: tuple[str, ...] = ()
    reference_asset_ids: tuple[str, ...] = ()
    knowledge_resource_ids: tuple[str, ...] = ()
    visual_reference_resource_ids: tuple[str, ...] = ()
    parent_artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectAiVideoGenerationSpec:
    prompt: str
    label: str = "AI 生成视频"
    source_copy_artifact_id: str | None = None
    reference_images: tuple[bytes, ...] = ()
    reference_media_types: tuple[str, ...] = ()
    source_project_asset_id: str | None = None
    reference_image_asset_ids: tuple[str, ...] = ()
    reference_video_asset_ids: tuple[str, ...] = ()
    knowledge_resource_ids: tuple[str, ...] = ()
    visual_reference_resource_ids: tuple[str, ...] = ()
    duration_seconds: int | None = None
    ratio: str | None = None
    resolution: str | None = None
    generate_audio: bool = False
    return_last_frame: bool = False
    idempotency_key: str | None = None
    parent_artifact_id: str | None = None
    # Keep a reference-video edit instruction separate from old copy/knowledge.
    supporting_context: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedProjectAsset:
    artifact: Artifact
    project_asset: ProjectAsset
    provenance: dict[str, object]


@dataclass(frozen=True, slots=True)
class GeneratedProjectSubtitle:
    artifact: Artifact
    project_asset: ProjectAsset
    provenance: dict[str, object]
    text: str
    duration_seconds: float
    source_project_asset_id: str | None
    segments: tuple[SubtitleSegment, ...]


@dataclass(frozen=True, slots=True)
class ProjectImageCandidates:
    candidates: tuple[GeneratedProjectAsset, ...]


@dataclass(frozen=True, slots=True)
class ProjectAiVideoEnqueueResult:
    job: Job
    provider_task_id: str | None
    created: bool


@dataclass(frozen=True, slots=True)
class ProjectAiVideoArchiveResult:
    generated: GeneratedProjectAsset
    job_result: dict[str, object]


VideoTransfer = Callable[..., AssetWriteResult]


def _json_safe(value: object) -> object:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError) as exc:
        raise VideoProjectGenerationValidationError("生成元数据无法安全序列化") from exc


def _redact_metadata(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_METADATA_KEYS.search(str(key))
                else _redact_metadata(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_metadata(item) for item in value]
    return _json_safe(value)


def normalize_ai_video_provider_failure(
    error_message: Exception | str | None,
    *,
    error_code: str | None = None,
    request: Mapping[str, object] | None = None,
    retryable: bool = False,
    classify: bool = True,
) -> tuple[str, dict[str, object]]:
    """Convert provider failures into stable client-safe error information."""

    preserved = getattr(error_message, "generation_error_detail", None)
    if classify and isinstance(preserved, dict) and isinstance(preserved.get("message"), str):
        return preserved["message"], dict(preserved)
    raw_message = " ".join(str(error_message or "").split())
    normalized_code = str(error_code or "").strip()
    is_privacy_rejection = (
        normalized_code in _AI_VIDEO_PRIVACY_ERROR_CODES
        or "InputImageSensitiveContentDetected.PrivacyInformation" in raw_message
        or "may contain real person" in raw_message.lower()
    )
    if not is_privacy_rejection:
        if classify:
            details = describe_generation_error(
                error_message or "",
                error_code=error_code,
                retryable=retryable,
            )
            return str(details["message"]), details
        message = "AI 视频服务暂时无法完成请求，请稍后重试；若重复失败，请调整参考素材或提示词。"
        return message, {
            "category": "provider_failure",
            "code": "ai_video_generation_failed",
            "retryable": retryable,
            "message": message,
        }

    provider_content_index = None
    index_match = _AI_VIDEO_PROVIDER_CONTENT_INDEX.search(raw_message)
    if index_match is not None:
        provider_content_index = int(index_match.group(1))
    manifest = request.get("reference_image_manifest", []) if request is not None else []
    matched_reference = next(
        (
            item
            for item in manifest
            if isinstance(item, dict)
            and item.get("provider_content_index") == provider_content_index
        ),
        None,
    )
    sequence = (
        matched_reference.get("sequence")
        if isinstance(matched_reference, dict)
        and isinstance(matched_reference.get("sequence"), int)
        else None
    )
    subject = f"第 {sequence} 张参考图" if isinstance(sequence, int) else "某张参考图"
    details: dict[str, object] = {
        "category": "input_content_safety",
        "code": "reference_image_real_person",
        "retryable": False,
    }
    if classify:
        classified = describe_generation_error(error_message or "", error_code=error_code)
        details.update(
            {key: value for key, value in classified.items() if key.startswith("provider_")}
        )
    if provider_content_index is not None:
        details["provider_content_index"] = provider_content_index
    if isinstance(sequence, int):
        details["reference_image_sequence"] = sequence
    if isinstance(matched_reference, dict):
        project_asset_id = matched_reference.get("project_asset_id")
        resource_entry_id = matched_reference.get("resource_entry_id")
        label = matched_reference.get("label")
        if isinstance(label, str) and label.strip():
            subject += f"“{' '.join(label.split())}”"
        if isinstance(project_asset_id, str) and project_asset_id:
            details["project_asset_id"] = project_asset_id
        elif isinstance(resource_entry_id, str) and resource_entry_id:
            details["resource_entry_id"] = resource_entry_id
    message = (
        f"AI 视频生成未通过隐私安全检查：{subject}可能包含真实人物。请移除或更换该图片后重试。"
    )
    details["message"] = message
    return message, details


def _provider_provenance(
    *,
    status: ProviderStatus,
    provider_id: str,
    model: str | None,
    request: Mapping[str, object],
    metadata: Mapping[str, object] | None,
) -> dict[str, object]:
    safe_metadata = _redact_metadata(metadata or {})
    assert isinstance(safe_metadata, dict)
    usage = safe_metadata.get("usage", {})
    cost = safe_metadata.get("cost")
    if cost is None:
        cost = safe_metadata.get("billing")
    return {
        "provider_id": provider_id,
        "model": model,
        "provider_mode": status.mode,
        "external_provider": status.external,
        "request": _redact_metadata(request),
        "usage": usage,
        "cost": cost,
        "cost_status": "reported" if cost is not None else "not_reported",
        "provider_metadata": safe_metadata,
        "recorded_at": datetime.now(UTC).isoformat(),
    }


def _validate_context(
    project: VideoProject,
    task: ContentTask,
    created_by: str,
) -> None:
    if project.task_id != task.id:
        raise VideoProjectGenerationValidationError("视频工程与内容任务不匹配")
    if not created_by.strip() or len(created_by) > 128:
        raise VideoProjectGenerationValidationError("素材创建人不能为空")


def _validate_label(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise VideoProjectGenerationValidationError("素材名称长度必须在 1 到 255 之间")
    return normalized


def _validate_ratio(value: str | None, *, field_name: str) -> None:
    if value is not None and not re.fullmatch(r"[1-9]\d{0,4}:[1-9]\d{0,4}", value):
        raise VideoProjectGenerationValidationError(f"{field_name} 必须采用 宽:高 格式")


def _image_extension_for_media_type(media_type: str) -> str:
    normalized = media_type.split(";", 1)[0].strip().lower()
    mapping = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }
    extension = mapping.get(normalized)
    if extension is None:
        raise VideoProjectGenerationValidationError("参考素材必须是 JPEG、PNG 或 WebP 图片")
    return extension


def _project_image_bytes(
    session: Session,
    *,
    project: VideoProject,
    project_asset_id: str,
    asset_store: AssetStorePort,
) -> tuple[bytes, str]:
    item = session.get(ProjectAsset, project_asset_id)
    if item is None or item.project_id != project.id or not item.active:
        raise VideoProjectGenerationValidationError("参考图片不存在或已经归档")
    try:
        storage_key, media_type, filename = project_asset_storage(session, item)
        content = asset_store.read_bytes(storage_key)
    except (VideoProjectValidationError, OSError) as exc:
        raise VideoProjectGenerationValidationError("参考图片文件不可用") from exc
    media_kind, canonical_media_type = _validate_generated_media(
        content=content,
        filename=filename,
        media_type=media_type,
    )
    if media_kind != "image":
        raise VideoProjectGenerationValidationError("参考素材必须是图片")
    return content, canonical_media_type


def _project_image_reference_descriptor(
    session: Session,
    *,
    project_asset_id: str,
    source: str,
) -> dict[str, object]:
    descriptor: dict[str, object] = {
        "source": source,
        "project_asset_id": project_asset_id,
    }
    item = session.get(ProjectAsset, project_asset_id)
    if item is not None:
        label = " ".join(str(item.label or "").split())
        if label:
            descriptor["label"] = label
    return descriptor


def _knowledge_image_reference_descriptor(
    session: Session,
    *,
    resource_entry_id: str,
) -> dict[str, object]:
    descriptor: dict[str, object] = {
        "source": "knowledge_visual",
        "resource_entry_id": resource_entry_id,
    }
    item = session.get(ResourceEntry, resource_entry_id)
    if item is not None:
        label = " ".join(str(item.title or "").split())
        if label:
            descriptor["label"] = label
    return descriptor


def _validate_parent_artifact(
    session: Session,
    task: ContentTask,
    parent_artifact_id: str | None,
    *,
    allowed_kinds: frozenset[str],
) -> Artifact | None:
    if parent_artifact_id is None:
        return None
    parent = session.get(Artifact, parent_artifact_id)
    if parent is None:
        raise VideoProjectGenerationValidationError("父版本素材不存在")
    if parent.task_id != task.id or parent.kind not in allowed_kinds:
        raise VideoProjectGenerationValidationError("父版本素材不属于当前任务或类型不匹配")
    return parent


def _validate_generated_media(
    *,
    content: bytes,
    filename: str,
    media_type: str,
) -> tuple[str, str]:
    if not content:
        raise VideoProjectGenerationValidationError("模型返回了空素材")
    try:
        kind, canonical_media_type = validate_upload_header(
            filename=filename,
            declared_media_type=media_type,
            head=content[:8192],
            size_bytes=len(content),
            policy=MediaIngestPolicy(),
        )
    except MediaValidationError as exc:
        raise VideoProjectGenerationValidationError(str(exc)) from exc
    return kind, canonical_media_type


def _project_asset_metadata(
    *,
    role: str,
    stored: AssetWriteResult,
    provenance: Mapping[str, object],
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "source": "ai-generated",
        "role": role,
        "media_type": stored.media_type,
        "size_bytes": stored.size,
        "checksum_sha256": stored.checksum_sha256,
        "provenance": dict(provenance),
        **dict(extra or {}),
    }


def generate_project_voice_asset(
    session: Session,
    *,
    project: VideoProject,
    task: ContentTask,
    created_by: str,
    provider: SpeechSynthesisPort,
    asset_store: AssetStorePort,
    spec: ProjectVoiceGenerationSpec,
) -> GeneratedProjectAsset:
    _validate_context(project, task, created_by)
    label = _validate_label(spec.label)
    text = spec.text.strip()
    if not text or len(text) > 100_000:
        raise VideoProjectGenerationValidationError("配音文本长度必须在 1 到 100000 之间")
    if not 0.1 <= spec.speed <= 4 or not 0 <= spec.volume <= 4:
        raise VideoProjectGenerationValidationError("配音语速或音量超出允许范围")
    if not 8_000 <= spec.sample_rate <= 96_000:
        raise VideoProjectGenerationValidationError("配音采样率超出允许范围")
    if spec.audio_format not in _AUDIO_EXTENSION_BY_FORMAT:
        raise VideoProjectGenerationValidationError("不支持的配音输出格式")
    if spec.voice is not None and len(spec.voice) > 255:
        raise VideoProjectGenerationValidationError("voice 不能超过 255 个字符")
    if spec.instruction is not None and len(spec.instruction) > 2_000:
        raise VideoProjectGenerationValidationError("配音指令不能超过 2000 个字符")
    parent = _validate_parent_artifact(
        session,
        task,
        spec.parent_artifact_id,
        allowed_kinds=frozenset({"voice"}),
    )
    request = SpeechSynthesisRequest(
        text=text,
        voice=spec.voice,
        speed=spec.speed,
        volume=spec.volume,
        audio_format=spec.audio_format,
        sample_rate=spec.sample_rate,
        with_timestamps=spec.with_timestamps,
        user_id=created_by,
    )
    generated = provider.synthesize(request)
    extension = generated.extension.lower().lstrip(".")
    if extension == "ogg_opus":
        extension = "ogg"
    if extension not in set(_AUDIO_EXTENSION_BY_FORMAT.values()):
        raise VideoProjectGenerationValidationError("配音模型返回了不支持的音频格式")
    media_kind, canonical_media_type = _validate_generated_media(
        content=generated.audio,
        filename=f"generated.{extension}",
        media_type=generated.media_type,
    )
    if media_kind != "audio":
        raise VideoProjectGenerationValidationError("配音模型没有返回音频")
    request_provenance = {
        "text": text,
        "instruction": spec.instruction,
        "voice": spec.voice,
        "speed": spec.speed,
        "volume": spec.volume,
        "audio_format": spec.audio_format,
        "sample_rate": spec.sample_rate,
        "with_timestamps": spec.with_timestamps,
        "parent_artifact_id": spec.parent_artifact_id,
    }
    provenance = _provider_provenance(
        status=provider.status,
        provider_id=generated.provider_id,
        model=provider.status.model,
        request=request_provenance,
        metadata=generated.metadata,
    )
    storage_key = (
        f"tasks/{task.id}/video-projects/{project.id}/generated/voice/{uuid4().hex}.{extension}"
    )
    stored = asset_store.put_bytes(storage_key, generated.audio, canonical_media_type)
    probe_result = None
    probe_status = "unavailable"
    probe_error = None
    try:
        probe_result = probe_media(
            asset_store.path_for(stored.key),
            kind="audio",
            media_type=canonical_media_type,
            timeout_seconds=MediaIngestPolicy().max_probe_seconds,
        )
        probe_status = "succeeded"
    except MediaValidationError as exc:
        asset_store.path_for(stored.key).unlink(missing_ok=True)
        raise VideoProjectGenerationValidationError(f"配音模型返回的音频无法读取：{exc}") from exc
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        probe_error = str(exc)[:500]
    probed_duration = (
        _duration_value(probe_result.duration_seconds) if probe_result is not None else None
    )
    fallback_durations = [
        value
        for value in (
            _duration_value(generated.duration_seconds),
            _duration_value(
                max(
                    (timing.end_seconds for timing in generated.word_timings),
                    default=0,
                )
            ),
        )
        if value is not None
    ]
    duration_seconds = probed_duration or (max(fallback_durations) if fallback_durations else None)
    if duration_seconds is None:
        asset_store.path_for(stored.key).unlink(missing_ok=True)
        raise VideoProjectGenerationValidationError("无法获取生成配音的实际时长")
    probe_metadata = asdict(probe_result) if probe_result is not None else None
    artifact = create_artifact(
        session,
        task,
        "voice",
        {
            "project_id": project.id,
            "source": "ai-generated",
            "text": text,
            "instruction": spec.instruction,
            "voice": generated.voice,
            "speed": spec.speed,
            "volume": spec.volume,
            "duration_seconds": duration_seconds,
            "provider_duration_seconds": generated.duration_seconds,
            "probe_status": probe_status,
            "probe_error": probe_error,
            "probe": probe_metadata,
            "media_type": stored.media_type,
            "size_bytes": stored.size,
            "checksum_sha256": stored.checksum_sha256,
            "word_timings": [
                {
                    "text": timing.text,
                    "start_seconds": timing.start_seconds,
                    "end_seconds": timing.end_seconds,
                }
                for timing in generated.word_timings
            ],
            "provenance": provenance,
        },
        created_by,
        stored.key,
    )
    artifact.parent_id = parent.id if parent is not None else None
    metadata = _project_asset_metadata(
        role="voice",
        stored=stored,
        provenance=provenance,
        extra={
            "media_kind": "audio",
            "duration_seconds": duration_seconds,
            "provider_duration_seconds": generated.duration_seconds,
            "speed": spec.speed,
            "volume": spec.volume,
            "voice": generated.voice,
            "text": text,
            "parent_artifact_id": spec.parent_artifact_id,
            "probe_status": probe_status,
            "probe_error": probe_error,
            "probe": probe_metadata,
        },
    )
    project_asset = ensure_project_asset(
        session,
        project,
        source_type="artifact",
        source_id=artifact.id,
        artifact_id=artifact.id,
        role="voice",
        label=label,
        created_by=created_by,
        metadata=metadata,
    )
    return GeneratedProjectAsset(artifact, project_asset, provenance)


def _duration_value(value: object) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def _source_duration(*values: object) -> float | None:
    for value in values:
        if isinstance(value, Mapping):
            duration = _source_duration(
                value.get("duration_seconds"),
                value.get("duration"),
                value.get("probe"),
            )
        else:
            duration = _duration_value(value)
        if duration is not None:
            return duration
    return None


def _source_word_timings(value: object) -> tuple[WordTiming, ...]:
    if not isinstance(value, list):
        return ()
    timings: list[WordTiming] = []
    for item in value:
        if not isinstance(item, Mapping):
            return ()
        text = str(item.get("text") or "").strip()
        start = _duration_value(item.get("start_seconds"))
        raw_start = item.get("start_seconds")
        if raw_start == 0 or raw_start == 0.0 or raw_start == "0":
            start = 0.0
        end = _duration_value(item.get("end_seconds"))
        if not text or start is None or end is None or end <= start:
            return ()
        timings.append(WordTiming(text, start, end))
    return tuple(timings)


def generate_project_subtitle_asset(
    session: Session,
    *,
    project: VideoProject,
    task: ContentTask,
    created_by: str,
    provider: SubtitleAlignmentPort,
    asset_store: AssetStorePort,
    spec: ProjectSubtitleGenerationSpec,
) -> GeneratedProjectSubtitle:
    """Generate one project-owned SRT asset without mutating the timeline.

    A generated voice asset is the preferred source because it carries provider word
    timings. Uploaded audio is also accepted, but this alignment provider is not an
    ASR service: callers must supply text when the audio asset has no persisted text.
    """

    _validate_context(project, task, created_by)
    label = _validate_label(spec.label)
    if spec.max_chars_per_line is not None and not 1 <= spec.max_chars_per_line <= 200:
        raise VideoProjectGenerationValidationError("字幕每行最大字符数必须在 1 到 200 之间")
    if spec.max_lines is not None and not 1 <= spec.max_lines <= 10:
        raise VideoProjectGenerationValidationError("字幕每段最大行数必须在 1 到 10 之间")
    parent = _validate_parent_artifact(
        session,
        task,
        spec.parent_artifact_id,
        allowed_kinds=frozenset({"subtitle"}),
    )

    source_asset: ProjectAsset | None = None
    source_artifact: Artifact | None = None
    source_content: dict[str, object] = {}
    source_metadata: dict[str, object] = {}
    if spec.source_project_asset_id is not None:
        source_asset = session.get(ProjectAsset, spec.source_project_asset_id)
        if source_asset is None or source_asset.project_id != project.id or not source_asset.active:
            raise VideoProjectGenerationValidationError("字幕来源音频不存在或已经归档")
        try:
            source_storage_key, source_media_type, _ = project_asset_storage(
                session,
                source_asset,
            )
        except VideoProjectValidationError as exc:
            raise VideoProjectGenerationValidationError("字幕来源音频文件不可用") from exc
        if not source_media_type.lower().startswith("audio/"):
            raise VideoProjectGenerationValidationError("字幕来源工程素材必须是音频")
        if not asset_store.exists(source_storage_key):
            raise VideoProjectGenerationValidationError("字幕来源音频文件不存在")
        source_metadata = load(source_asset.metadata_json)
        if source_asset.artifact_id is not None:
            candidate = session.get(Artifact, source_asset.artifact_id)
            if candidate is None or candidate.task_id != task.id:
                raise VideoProjectGenerationValidationError("字幕来源音频版本不可用")
            source_artifact = candidate
            source_content = load(candidate.content_json)

    explicit_text = (spec.text or "").strip()
    persisted_text = str(source_content.get("text") or source_metadata.get("text") or "").strip()
    text = explicit_text or persisted_text
    if not text:
        if source_asset is not None:
            raise VideoProjectGenerationValidationError(
                "该音频没有可用文本；当前字幕能力不执行语音转写，请同时传入 text"
            )
        raise VideoProjectGenerationValidationError("字幕文本不能为空")
    if len(text) > 100_000:
        raise VideoProjectGenerationValidationError("字幕文本不能超过 100000 个字符")

    source_timings = _source_word_timings(source_content.get("word_timings"))
    # Provider timings describe one exact utterance. A caller text override must not
    # silently reuse timings for different words.
    word_timings = (
        source_timings
        if source_timings and (not explicit_text or explicit_text == persisted_text)
        else ()
    )
    requested_duration = _duration_value(spec.duration_seconds)
    if spec.duration_seconds is not None and requested_duration is None:
        raise VideoProjectGenerationValidationError("字幕时长必须大于 0")
    duration_seconds = requested_duration or _source_duration(
        source_content,
        source_metadata,
        max((item.end_seconds for item in word_timings), default=None),
    )
    if duration_seconds is None:
        duration_seconds = min(max(len(text) / 8.0, 0.5), 300.0)
    if duration_seconds > 600:
        raise VideoProjectGenerationValidationError("字幕时长不能超过 600 秒")
    latest_timing = max((item.end_seconds for item in word_timings), default=0.0)
    if latest_timing > duration_seconds + 0.001:
        raise VideoProjectGenerationValidationError("字幕时长不能短于来源配音的字词时间戳")

    segments = tuple(
        provider.align(
            SubtitleAlignmentRequest(
                text=text,
                duration_seconds=duration_seconds,
                word_timings=word_timings,
                max_chars_per_line=spec.max_chars_per_line,
                max_lines=spec.max_lines,
            )
        )
    )
    if not segments:
        raise VideoProjectGenerationValidationError("字幕对齐没有产生可用字幕片段")
    for segment in segments:
        if (
            segment.sequence < 1
            or not segment.text.strip()
            or not math.isfinite(segment.start_seconds)
            or not math.isfinite(segment.end_seconds)
            or segment.start_seconds < 0
            or segment.end_seconds <= segment.start_seconds
            or segment.end_seconds > duration_seconds + 0.001
            or segment.timing_source not in {"provider", "estimated"}
        ):
            raise VideoProjectGenerationValidationError("字幕对齐返回了无效的字幕片段")
    timing_sources = sorted({segment.timing_source for segment in segments})
    request_provenance = {
        "text": text,
        "source_project_asset_id": source_asset.id if source_asset is not None else None,
        "source_artifact_id": source_artifact.id if source_artifact is not None else None,
        "duration_seconds": duration_seconds,
        "max_chars_per_line": spec.max_chars_per_line,
        "max_lines": spec.max_lines,
        "word_timings_used": bool(word_timings),
        "parent_artifact_id": spec.parent_artifact_id,
    }
    provenance = _provider_provenance(
        status=provider.status,
        provider_id=provider.status.provider_id,
        model=provider.status.model,
        request=request_provenance,
        metadata={
            "segment_count": len(segments),
            "timing_sources": timing_sources,
        },
    )
    subtitle_payload = render_srt(segments).encode("utf-8")
    media_kind, canonical_media_type = _validate_generated_media(
        content=subtitle_payload,
        filename="generated.srt",
        media_type="application/x-subrip",
    )
    if media_kind != "subtitle":
        raise VideoProjectGenerationValidationError("字幕对齐没有返回 SRT 文件")
    storage_key = (
        f"tasks/{task.id}/video-projects/{project.id}/generated/subtitle/{uuid4().hex}.srt"
    )
    stored = asset_store.put_bytes(storage_key, subtitle_payload, canonical_media_type)
    segment_content = [
        {
            "sequence": segment.sequence,
            "text": segment.text,
            "start_seconds": segment.start_seconds,
            "end_seconds": segment.end_seconds,
            "timing_source": segment.timing_source,
        }
        for segment in segments
    ]
    artifact = create_artifact(
        session,
        task,
        "subtitle",
        {
            "project_id": project.id,
            "source": "ai-generated",
            "text": text,
            "format": "srt",
            "timed": True,
            "duration_seconds": duration_seconds,
            "source_project_asset_id": (source_asset.id if source_asset is not None else None),
            "source_artifact_id": (source_artifact.id if source_artifact is not None else None),
            "parameters": {
                "max_chars_per_line": spec.max_chars_per_line,
                "max_lines": spec.max_lines,
            },
            "segments": segment_content,
            "media_type": stored.media_type,
            "size_bytes": stored.size,
            "checksum_sha256": stored.checksum_sha256,
            "provenance": provenance,
        },
        created_by,
        stored.key,
    )
    artifact.parent_id = parent.id if parent is not None else None
    metadata = _project_asset_metadata(
        role="subtitle",
        stored=stored,
        provenance=provenance,
        extra={
            "media_kind": "subtitle",
            "format": "srt",
            "text": text,
            "duration_seconds": duration_seconds,
            "segment_count": len(segments),
            "segments": segment_content,
            "timing_sources": timing_sources,
            "source_project_asset_id": (source_asset.id if source_asset is not None else None),
            "source_artifact_id": (source_artifact.id if source_artifact is not None else None),
            "parent_artifact_id": spec.parent_artifact_id,
        },
    )
    project_asset = ensure_project_asset(
        session,
        project,
        source_type="artifact",
        source_id=artifact.id,
        artifact_id=artifact.id,
        role="subtitle",
        label=label,
        created_by=created_by,
        metadata=metadata,
    )
    return GeneratedProjectSubtitle(
        artifact=artifact,
        project_asset=project_asset,
        provenance=provenance,
        text=text,
        duration_seconds=duration_seconds,
        source_project_asset_id=(source_asset.id if source_asset is not None else None),
        segments=segments,
    )


def _validate_project_image_options(
    provider: ImageGenerationPort,
    *,
    size: str | None,
    ratio: str | None,
) -> None:
    """Validate only video-editor image options against the active model.

    In particular, reject undersized custom dimensions before the shared Ark
    adapter can apply its legacy upscaling fallback. Leaving a field empty means
    model default, not a locally guessed resolution or the video canvas ratio.
    """
    capabilities = provider.capabilities
    sizes = capabilities.get("sizes", [])
    ratios = capabilities.get("ratios", [])
    size_options = tuple(sizes) if isinstance(sizes, (list, tuple)) else ()
    ratio_options = tuple(ratios) if isinstance(ratios, (list, tuple)) else ()
    legacy_size = capabilities.get("size", {})
    legacy_size = legacy_size if isinstance(legacy_size, dict) else {}
    custom_size = capabilities.get("custom_size") is True or legacy_size.get("mode") == "custom"
    dimensions = re.fullmatch(r"(\d{2,5})[xX×](\d{2,5})", size or "")
    if size is not None and size not in size_options and not (custom_size and dimensions):
        detail = "、".join(str(value) for value in size_options) or "请选择模型默认"
        raise VideoProjectGenerationValidationError(
            f"当前图片模型不支持清晰度或尺寸“{size}”；{detail}。请重新选择或清空后重试。"
        )
    if (
        ratio is not None
        and ratio not in ratio_options
        and not (capabilities.get("custom_ratio") is True and ratio != "adaptive")
    ):
        detail = "、".join(str(value) for value in ratio_options) or "请选择模型默认"
        raise VideoProjectGenerationValidationError(
            f"当前图片模型不支持画面比例“{ratio}”；{detail}。请重新选择或清空后重试。"
        )
    ratio_range = capabilities.get("aspect_ratio_range", {})
    ratio_range = ratio_range if isinstance(ratio_range, dict) else {}

    def validate_aspect(value: float) -> None:
        minimum, maximum = ratio_range.get("min"), ratio_range.get("max")
        if (isinstance(minimum, (int, float)) and value < minimum) or (
            isinstance(maximum, (int, float)) and value > maximum
        ):
            raise VideoProjectGenerationValidationError(
                "所选画面比例超出当前图片模型支持的范围，请重新选择模型支持的比例。"
            )

    if ratio is not None and ratio != "adaptive":
        _validate_ratio(ratio, field_name="图片比例")
        ratio_width, ratio_height = (int(value) for value in ratio.split(":"))
        validate_aspect(ratio_width / ratio_height)
    if dimensions:
        width, height = (int(value) for value in dimensions.groups())
        if width <= 0 or height <= 0:
            raise VideoProjectGenerationValidationError("图片宽度和高度必须大于 0。")
        for boundary, comparator in (("min", min), ("max", max)):
            bound = legacy_size.get(boundary)
            dimension = comparator(width, height)
            if isinstance(bound, (int, float)) and (
                (boundary == "min" and dimension < bound)
                or (boundary == "max" and dimension > bound)
            ):
                raise VideoProjectGenerationValidationError(
                    "自定义图片尺寸超出当前模型支持的宽高范围，请重新选择或清空后重试。"
                )
        pixels = width * height
        minimum, maximum = capabilities.get("minimum_pixels"), capabilities.get("maximum_pixels")
        if isinstance(minimum, (int, float)) and pixels < minimum:
            raise VideoProjectGenerationValidationError(
                f"自定义图片尺寸过小，当前模型至少需要 {int(minimum)} 像素。请选用支持的清晰度或增大尺寸。"
            )
        if isinstance(maximum, (int, float)) and pixels > maximum:
            raise VideoProjectGenerationValidationError(
                f"自定义图片尺寸过大，当前模型最多支持 {int(maximum)} 像素。请选用支持的清晰度或减小尺寸。"
            )
        validate_aspect(width / height)
        if (
            ratio is not None
            and ratio != "adaptive"
            and width * ratio_height != height * ratio_width
        ):
            raise VideoProjectGenerationValidationError(
                "自定义图片尺寸与所选画面比例不一致。请修改尺寸，或清空比例后重试。"
            )


def generate_project_image_candidates(
    session: Session,
    *,
    project: VideoProject,
    task: ContentTask,
    created_by: str,
    provider: ImageGenerationPort,
    asset_store: AssetStorePort,
    spec: ProjectImageGenerationSpec,
) -> ProjectImageCandidates:
    _validate_context(project, task, created_by)
    label = _validate_label(spec.label)
    prompt = spec.prompt.strip()
    if not prompt or len(prompt) > 10_000:
        raise VideoProjectGenerationValidationError("图片提示词长度必须在 1 到 10000 之间")
    if spec.role not in _PROJECT_IMAGE_ROLES:
        raise VideoProjectGenerationValidationError("不支持的图片素材用途")
    if not 1 <= spec.count <= 8:
        raise VideoProjectGenerationValidationError("图片候选数量必须在 1 到 8 之间")
    if spec.size is not None and not re.fullmatch(r"(?:\d{2,5}[xX×]\d{2,5}|[1-9]\d?K)", spec.size):
        raise VideoProjectGenerationValidationError("图片尺寸格式无效")
    _validate_project_image_options(provider, size=spec.size, ratio=spec.ratio)
    provider_prompt = prompt
    if spec.ratio is not None and spec.ratio != "adaptive":
        # Ark accepts resolution in `size`, not a separate ratio parameter.
        # Match the task-image entry point: retain the selected resolution and
        # explicitly include the composition ratio in the generation prompt.
        provider_prompt += f"；画面比例：{spec.ratio}"
        if len(provider_prompt) > 10_000:
            raise VideoProjectGenerationValidationError(
                "图片提示词与画面比例合计超过 10000 个字符，请缩短提示词后重试。"
            )
    if spec.seed is not None and spec.seed < 0:
        raise VideoProjectGenerationValidationError("图片随机种子不能为负数")
    if len(spec.reference_images) != len(spec.reference_media_types):
        raise VideoProjectGenerationValidationError("参考图片与媒体类型数量不一致")
    reference_asset_ids = tuple(dict.fromkeys(spec.reference_asset_ids))
    reference_images = spec.reference_images
    reference_media_types = spec.reference_media_types
    if reference_asset_ids:
        resolved_references = tuple(
            _project_image_bytes(
                session,
                project=project,
                project_asset_id=project_asset_id,
                asset_store=asset_store,
            )
            for project_asset_id in reference_asset_ids
        )
        reference_images = reference_images + tuple(item[0] for item in resolved_references)
        reference_media_types = reference_media_types + tuple(
            item[1] for item in resolved_references
        )
    unique_references: list[tuple[bytes, str]] = []
    seen_reference_checksums: set[str] = set()
    for content, media_type in zip(reference_images, reference_media_types, strict=True):
        checksum = hashlib.sha256(content).hexdigest()
        if checksum in seen_reference_checksums:
            continue
        seen_reference_checksums.add(checksum)
        unique_references.append((content, media_type))
    if len(unique_references) > 9:
        raise VideoProjectGenerationValidationError("最多允许 9 张参考图片")
    reference_images = tuple(item[0] for item in unique_references)
    reference_media_types = tuple(item[1] for item in unique_references)
    for reference, media_type in zip(
        reference_images,
        reference_media_types,
        strict=True,
    ):
        extension = _image_extension_for_media_type(media_type)
        media_kind, _ = _validate_generated_media(
            content=reference,
            filename=f"reference.{extension}",
            media_type=media_type,
        )
        if media_kind != "image":
            raise VideoProjectGenerationValidationError("参考素材必须是图片")
    parent = _validate_parent_artifact(
        session,
        task,
        spec.parent_artifact_id,
        allowed_kinds=frozenset({"image"}),
    )
    request = ImageGenerationRequest(
        prompt=provider_prompt,
        count=spec.count,
        size=spec.size,
        ratio=spec.ratio,
        reference_images=reference_images,
        reference_media_types=reference_media_types,
        watermark=spec.watermark,
        seed=spec.seed,
    )
    generated_assets = tuple(provider.generate(request))
    if len(generated_assets) != spec.count:
        raise VideoProjectGenerationValidationError(
            f"图片模型应返回 {spec.count} 个候选，实际返回 {len(generated_assets)} 个"
        )

    validated: list[tuple[GeneratedAsset, str, str]] = []
    for generated in generated_assets:
        extension = generated.extension.lower().lstrip(".")
        if extension not in _IMAGE_EXTENSIONS:
            raise VideoProjectGenerationValidationError("图片模型返回了不支持的扩展名")
        media_kind, canonical_media_type = _validate_generated_media(
            content=generated.content,
            filename=f"generated.{extension}",
            media_type=generated.media_type,
        )
        if media_kind != "image":
            raise VideoProjectGenerationValidationError("图片模型没有返回图片")
        validated.append((generated, extension, canonical_media_type))

    results: list[GeneratedProjectAsset] = []
    for index, (generated, extension, canonical_media_type) in enumerate(validated):
        request_provenance = {
            "prompt": prompt,
            "role": spec.role,
            "candidate_index": index,
            "count": spec.count,
            "size": spec.size,
            "ratio": spec.ratio,
            "watermark": spec.watermark,
            "seed": spec.seed,
            "reference_asset_ids": list(reference_asset_ids),
            "knowledge_resource_ids": list(spec.knowledge_resource_ids),
            "visual_reference_resource_ids": list(spec.visual_reference_resource_ids),
            "parent_artifact_id": spec.parent_artifact_id,
        }
        provenance = _provider_provenance(
            status=provider.status,
            provider_id=generated.provider_id,
            model=generated.model,
            request=request_provenance,
            metadata=generated.metadata,
        )
        storage_key = (
            f"tasks/{task.id}/video-projects/{project.id}/generated/images/"
            f"{uuid4().hex}.{extension}"
        )
        stored = asset_store.put_bytes(
            storage_key,
            generated.content,
            canonical_media_type,
        )
        brand_metadata = (
            {
                "pending_brand_confirmation": True,
                "brand_asset_status": "pending_confirmation",
                "official_brand_asset": False,
            }
            if spec.role == "logo"
            else {}
        )
        artifact = create_artifact(
            session,
            task,
            "image",
            {
                "project_id": project.id,
                "source": "ai-generated",
                "role": spec.role,
                "prompt": prompt,
                "candidate_index": index,
                "media_type": stored.media_type,
                "size_bytes": stored.size,
                "checksum_sha256": stored.checksum_sha256,
                "knowledge_resource_ids": list(spec.knowledge_resource_ids),
                "visual_reference_resource_ids": list(spec.visual_reference_resource_ids),
                "provenance": provenance,
                **brand_metadata,
            },
            created_by,
            stored.key,
        )
        artifact.parent_id = parent.id if parent is not None else None
        metadata = _project_asset_metadata(
            role=spec.role,
            stored=stored,
            provenance=provenance,
            extra={
                "media_kind": "image",
                "prompt": prompt,
                "candidate_index": index,
                "knowledge_resource_ids": list(spec.knowledge_resource_ids),
                "visual_reference_resource_ids": list(spec.visual_reference_resource_ids),
                "parent_artifact_id": spec.parent_artifact_id,
                **brand_metadata,
            },
        )
        candidate_label = label if spec.count == 1 else f"{label[:250]} {index + 1}"
        project_asset = ensure_project_asset(
            session,
            project,
            source_type="artifact",
            source_id=artifact.id,
            artifact_id=artifact.id,
            role=spec.role,
            label=candidate_label,
            created_by=created_by,
            metadata=metadata,
        )
        results.append(GeneratedProjectAsset(artifact, project_asset, provenance))
    return ProjectImageCandidates(tuple(results))


def _source_image_data_url(
    session: Session,
    *,
    project: VideoProject,
    project_asset_id: str | None,
    asset_store: AssetStorePort,
) -> str | None:
    if project_asset_id is None:
        return None
    content, media_type = _project_image_bytes(
        session,
        project=project,
        project_asset_id=project_asset_id,
        asset_store=asset_store,
    )
    return f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}"


def _reference_image_data_url(
    session: Session,
    *,
    project: VideoProject,
    project_asset_id: str,
    asset_store: AssetStorePort,
) -> str:
    content, media_type = _project_image_bytes(
        session,
        project=project,
        project_asset_id=project_asset_id,
        asset_store=asset_store,
    )
    if len(content) >= _AI_VIDEO_REFERENCE_IMAGE_MAX_BYTES:
        raise VideoProjectGenerationValidationError("单张 AI 视频参考图片必须小于 30MB")
    return f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}"


def _duration_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result > 0 and result < float("inf") else None


def _known_project_video_duration(
    session: Session,
    item: ProjectAsset,
    *,
    metadata: Mapping[str, object],
) -> float | None:
    probe = metadata.get("probe")
    provenance = metadata.get("provenance")
    request = provenance.get("request") if isinstance(provenance, dict) else None
    candidates: list[object] = [metadata.get("duration_seconds")]
    if isinstance(probe, dict):
        candidates.append(probe.get("duration_seconds"))
    if isinstance(request, dict):
        candidates.append(request.get("duration_seconds"))
    if item.artifact_id:
        artifact = session.get(Artifact, item.artifact_id)
        if artifact is not None:
            content = load(artifact.content_json)
            if isinstance(content, dict):
                candidates.append(content.get("duration_seconds"))
    return next(
        (duration for value in candidates if (duration := _duration_value(value)) is not None),
        None,
    )


@dataclass(frozen=True, slots=True)
class _ProjectVideoReference:
    project_asset_id: str
    storage_key: str
    source_path: Path
    media_type: str
    duration_seconds: float
    provider_native_reference: ProviderNativeVideoReference | None


def _project_video_reference(
    session: Session,
    *,
    project: VideoProject,
    project_asset_id: str,
    asset_store: AssetStorePort,
) -> _ProjectVideoReference:
    item = session.get(ProjectAsset, project_asset_id)
    if item is None or item.project_id != project.id or not item.active:
        raise VideoProjectGenerationValidationError("参考视频不存在或已经归档")
    try:
        storage_key, media_type, filename = project_asset_storage(session, item)
        path = asset_store.path_for(storage_key)
        size_bytes = path.stat().st_size
        with path.open("rb") as source:
            head = read_upload_head(source)
        media_kind, canonical_media_type = validate_upload_header(
            filename=filename,
            declared_media_type=media_type,
            head=head,
            size_bytes=size_bytes,
            policy=MediaIngestPolicy(),
        )
    except (VideoProjectValidationError, MediaValidationError, OSError) as exc:
        raise VideoProjectGenerationValidationError("参考视频文件不可用") from exc
    if media_kind != "video" or canonical_media_type not in {"video/mp4", "video/quicktime"}:
        raise VideoProjectGenerationValidationError("参考视频必须是 MP4 或 MOV")
    if Path(filename).suffix.lower() not in {".mp4", ".mov"}:
        raise VideoProjectGenerationValidationError("参考视频文件扩展名必须是 .mp4 或 .mov")
    if size_bytes > _AI_VIDEO_REFERENCE_VIDEO_MAX_BYTES:
        raise VideoProjectGenerationValidationError("单段 AI 视频参考视频不能超过 200MB")

    metadata = load(item.metadata_json)
    duration = _known_project_video_duration(
        session,
        item,
        metadata=metadata if isinstance(metadata, dict) else {},
    )
    if duration is None:
        try:
            duration = probe_media(
                path,
                kind="video",
                media_type=canonical_media_type,
                timeout_seconds=MediaIngestPolicy().max_probe_seconds,
            ).duration_seconds
        except (MediaValidationError, RuntimeError, OSError) as exc:
            raise VideoProjectGenerationValidationError(
                "无法安全获取参考视频时长，请先完成媒体探测或重新上传"
            ) from exc
    if duration is None:
        raise VideoProjectGenerationValidationError(
            "无法安全获取参考视频时长，请先完成媒体探测或重新上传"
        )
    if not (
        _AI_VIDEO_REFERENCE_VIDEO_MIN_SECONDS <= duration <= _AI_VIDEO_REFERENCE_VIDEO_MAX_SECONDS
    ):
        raise VideoProjectGenerationValidationError("每段参考视频时长必须在 2 到 15 秒之间")

    return _ProjectVideoReference(
        project_asset_id=project_asset_id,
        storage_key=storage_key,
        source_path=path,
        media_type=canonical_media_type,
        duration_seconds=float(duration),
        provider_native_reference=resolve_provider_native_video_reference(session, item),
    )


def _provider_accepts_local_video_upload(provider: AiVideoGenerationPort) -> bool:
    capability = provider.capabilities.get("reference_videos")
    return isinstance(capability, Mapping) and capability.get("local_file_upload") is True


def _video_fast_retry_payload(metadata: Mapping[str, object] | None) -> dict[str, object] | None:
    if not isinstance(metadata, Mapping):
        return None
    candidates: list[object] = [metadata.get("retry_payload")]
    nested = metadata.get("provider_metadata")
    if isinstance(nested, Mapping):
        candidates.append(nested.get("retry_payload"))
    provenance = metadata.get("provenance")
    if isinstance(provenance, Mapping):
        nested = provenance.get("provider_metadata")
        if isinstance(nested, Mapping):
            candidates.append(nested.get("retry_payload"))
    for payload in candidates:
        if isinstance(payload, dict) and str(payload.get("Prompt") or "").strip():
            return payload
    return None


def maybe_retry_ai_video_on_fast(
    provider: AiVideoGenerationPort,
    *,
    failed: AiVideoTask,
    metadata: Mapping[str, object] | None,
) -> AiVideoTask | None:
    """Pro-first create can succeed and then fail. Retry the same Files on Fast."""

    retry = getattr(provider, "submit_fast_retry", None)
    if not callable(retry) or failed.status != "failed":
        return None
    payload = _video_fast_retry_payload(metadata)
    if payload is None:
        return None
    classified = describe_generation_error(
        failed.error_message or "",
        error_code=failed.error_code,
    )
    if classified.get("category") == "content_safety":
        return None
    try:
        retried = retry(payload)
    except (ProviderCallError, ValueError, TypeError):
        return None
    if retried is None or not str(getattr(retried, "provider_task_id", "") or "").strip():
        return None
    return retried


def _validated_provider_https_url(value: object) -> str:
    url = str(value or "")
    parsed_url = urlsplit(url)
    if (
        parsed_url.scheme.lower() != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.fragment
    ):
        raise VideoProjectGenerationValidationError(
            "参考视频需要配置私有 TOS 临时发布或可供模型拉取的短时签名 HTTPS 媒体网关；"
            "本地视频不会转为 Base64"
        )
    return url


def _provider_native_video_url(
    provider: AiVideoGenerationPort,
    reference: ProviderNativeVideoReference,
) -> str:
    video_capability = provider.capabilities.get("reference_videos")
    if not (
        isinstance(video_capability, Mapping)
        and video_capability.get("provider_native_reference_supported") is True
    ):
        raise VideoProjectGenerationValidationError(
            "当前 AI 视频 Provider 不支持复用其历史生成视频；请配置私有 TOS 或签名媒体网关"
        )
    if reference.provider_id != provider.status.provider_id:
        raise VideoProjectGenerationValidationError(
            "所选 AI 生成视频来自其他 Provider，不能通过当前 Provider 原生复用；"
            "请配置私有 TOS 或签名媒体网关"
        )
    try:
        provider_task = provider.get(reference.provider_task_id)
    except ProviderCallError as exc:
        raise VideoProjectGenerationValidationError(
            "所选 AI 生成视频已过期或当前 Provider 无法访问；请配置私有 TOS 或签名媒体网关"
        ) from exc
    if (
        provider_task.provider_task_id != reference.provider_task_id
        or provider_task.provider_id != reference.provider_id
    ):
        raise VideoProjectGenerationValidationError("AI 生成参考视频的 Provider 任务身份校验失败")
    if provider_task.status != "succeeded" or not provider_task.video_url:
        raise VideoProjectGenerationValidationError(
            "所选 AI 生成视频的 Provider 输出已不可用；请配置私有 TOS 或签名媒体网关"
        )
    return _validated_provider_https_url(provider_task.video_url)


def _publication_record(publication: ProviderMediaPublication) -> dict[str, str]:
    if not publication.cleanup_token:
        return {}
    return {
        "transport": publication.transport,
        "cleanup_token": publication.cleanup_token,
        "expires_at": publication.expires_at.astimezone(UTC).isoformat(),
    }


def release_provider_media_publications(
    publisher: ProviderMediaPublisherPort,
    job_result: Mapping[str, object],
) -> int:
    """Best-effort, idempotent release of internal publication records."""

    records = job_result.get(PROVIDER_MEDIA_PUBLICATIONS_RESULT_KEY)
    if not isinstance(records, list):
        return 0
    released = 0
    for record in records:
        if not isinstance(record, dict) or record.get("transport") != publisher.transport:
            continue
        cleanup_token = record.get("cleanup_token")
        if not isinstance(cleanup_token, str) or not cleanup_token:
            continue
        try:
            publisher.release(cleanup_token)
        except Exception:
            # The fixed-prefix expiry sweep and TOS object lifecycle are the
            # fallback; cleanup failures must not overwrite provider status.
            continue
        released += 1
    return released


def _ai_video_task_context(task: ContentTask) -> dict[str, str]:
    fields = (
        ("task_name", task.name),
        ("topic", task.topic),
        ("product", task.product),
        ("audience", task.audience),
        ("content_type", task.content_type),
        ("style", task.style),
        ("requirements", task.requirements),
    )
    return {key: str(value).strip() for key, value in fields if str(value or "").strip()}


def _ai_video_provider_prompt(
    task_context: dict[str, str],
    user_prompt: str,
    *,
    supporting_context: str | None = None,
    reference_image_manifest: list[dict[str, object]] | None = None,
    reference_video_count: int = 0,
) -> str:
    labels = {
        "task_name": "任务名称",
        "topic": "主题",
        "product": "产品",
        "audience": "目标人群",
        "content_type": "内容类型",
        "style": "表达风格",
        "requirements": "补充要求",
    }
    brief = "\n".join(
        f"{labels[key]}：{value}" for key, value in task_context.items() if key in labels
    )
    if reference_video_count:
        # The numbering must match the submitted, deduplicated media order, not
        # project/timeline order (automatic product images may come first).
        references = [
            f"@视频{index}：本次选中的第 {index} 段参考视频。"
            for index in range(1, reference_video_count + 1)
        ]
        for item in reference_image_manifest or []:
            role = (
                "真实产品参考，仅用于瓶型、包装、Logo 和产品标签保真，不是字幕样式或字幕内容参考"
                if item.get("source") == "knowledge_visual"
                else "本次选中的视觉参考，按本次用户要求用于主体、场景或构图"
            )
            references.append(f"@图片{item['sequence']}：{role}。")
        instructions = (
            "【本次用户要求（最高优先级）】\n"
            f"{user_prompt.strip()}\n"
            "【参考素材绑定】\n" + "\n".join(references) + "\n【参考视频处理规则】\n"
            "直接理解以上参考视频的连续画面、主体、动作与镜头，不要忽略视频只看产品图片。"
            "用户要求修改‘这个视频’时，以 @视频1 为编辑对象；多段参考按上述编号及用户指定用途使用。"
            "明确完成本次要求的变化，未要求修改的部分保持连贯，不能直接复制原视频作为新结果。"
            "如果本次要求不要字幕或去除字幕，必须去除参考视频画面中已烧录的字幕、标题及后加文案，"
            "恢复对应背景，不只是停止添加新字幕；产品实体包装、Logo 和标签上的真实文字应保留。"
            "如果本次要求添加或修改字幕，则按本次要求处理，不能套用旧字幕。"
            "下方任务简报、旧标题、文案和知识摘要仅供事实背景参考，不是待显示的字幕或必须照搬的脚本；"
            "旧创作要求与本次要求冲突时，以本次要求为准，但不得虚构产品事实。\n"
        )
        background_heading = "【背景资料（不得覆盖本次要求）】\n"
        available = 10_000 - len(instructions) - len(background_heading)
        if available < 0:
            raise VideoProjectGenerationValidationError(
                "视频提示词与参考素材说明合计不能超过 10000 个字符，请缩短本次要求"
            )
        background = "\n".join(
            value for value in (brief, (supporting_context or "").strip()) if value
        )
        # Bound background only. Never silently truncate the user's edit.
        return instructions + background_heading + background[:available]
    prompt = (
        f"请结合以下任务简报和用户镜头要求生成视频。\n{brief}\n用户镜头要求：{user_prompt.strip()}"
    )
    if len(prompt) > 10_000:
        raise VideoProjectGenerationValidationError("视频提示词与任务信息合计不能超过 10000 个字符")
    return prompt


def _resolve_ai_video_option(
    provider: AiVideoGenerationPort,
    *,
    capability_key: str,
    requested: object | None,
    preferred: object,
    field_name: str,
) -> object:
    raw_values = provider.capabilities.get(capability_key, [])
    allowed = tuple(raw_values) if isinstance(raw_values, (list, tuple)) else ()
    if not allowed:
        raise VideoProjectGenerationValidationError(
            f"当前 AI 视频模型未声明支持的{field_name}，请先完成服务能力配置"
        )
    if requested is not None:
        resolved = requested
    elif preferred in allowed:
        resolved = preferred
    else:
        resolved = allowed[0]
    if resolved not in allowed:
        options = "、".join(str(value) for value in allowed)
        raise VideoProjectGenerationValidationError(
            f"当前 AI 视频模型不支持{field_name}“{resolved}”；可选值：{options}"
        )
    return resolved


def restore_misclassified_project_ai_video_job(
    session: Session,
    job_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Reopen only the old submission race, retaining the existing provider task.

    This never submits to a provider or commits. Callers choose the exact job,
    authorize it, commit, and then dispatch the ordinary polling worker.
    """
    current_time = now or datetime.now(UTC)
    job = session.get(Job, job_id, populate_existing=True)
    if (
        job is None
        or job.kind != VIDEO_PROJECT_AI_JOB_KIND
        or job.status != "failed"
        or job.cancel_requested_at is not None
        or (job.lease_expires_at is not None and job.lease_expires_at > current_time)
    ):
        return False
    result = load(job.result_json)
    detail = result.get("error_detail")
    detail_code = detail.get("code") if isinstance(detail, dict) else None
    if (
        not str(result.get("provider_task_id") or "").strip()
        or result.get("provider_status") in {"failed", "cancelled", "expired"}
        or detail_code not in {None, "provider_task_not_created"}
        or not (
            detail_code == "provider_task_not_created"
            or job.error == _AI_VIDEO_TASK_NOT_CREATED_ERROR
        )
    ):
        return False
    previous_result = job.result_json
    result.pop("error_detail", None)
    result["submission_state"] = "submitted"
    result["submission_recovery"] = {
        "previous_error": job.error,
        "recovered_at": current_time.isoformat(),
    }
    restored = session.execute(
        update(Job)
        .where(
            Job.id == job.id,
            Job.kind == VIDEO_PROJECT_AI_JOB_KIND,
            Job.status == "failed",
            Job.error == job.error,
            Job.result_json == previous_result,
            Job.cancel_requested_at.is_(None),
            or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= current_time),
        )
        .values(
            status="queued",
            result_json=dump(result),
            error=None,
            attempt=max(0, job.attempt - 1),
            available_at=current_time,
            completed_at=None,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            updated_at=current_time,
        )
        .execution_options(synchronize_session=False)
    )
    session.expire(job)
    return restored.rowcount == 1


def enqueue_project_ai_video(
    session: Session,
    *,
    project: VideoProject,
    task: ContentTask,
    created_by: str,
    provider: AiVideoGenerationPort,
    asset_store: AssetStorePort,
    spec: ProjectAiVideoGenerationSpec,
    provider_media_publisher: ProviderMediaPublisherPort | None = None,
) -> ProjectAiVideoEnqueueResult:
    _validate_context(project, task, created_by)
    label = _validate_label(spec.label)
    prompt = spec.prompt.strip()
    if not prompt or len(prompt) > 10_000:
        raise VideoProjectGenerationValidationError("视频提示词长度必须在 1 到 10000 之间")
    if spec.duration_seconds is not None and not 5 <= spec.duration_seconds <= 600:
        raise VideoProjectGenerationValidationError(
            "新生成的 AI 视频时长至少为 5 秒，请重新选择时长。"
        )
    if spec.ratio != "adaptive":
        _validate_ratio(spec.ratio, field_name="视频比例")
    if spec.resolution is not None and not 1 <= len(spec.resolution.strip()) <= 64:
        raise VideoProjectGenerationValidationError("视频分辨率长度必须在 1 到 64 之间")
    if len(spec.reference_images) != len(spec.reference_media_types):
        raise VideoProjectGenerationValidationError("参考图片与媒体类型数量不一致")
    project_image_asset_ids = (
        *((spec.source_project_asset_id,) if spec.source_project_asset_id else ()),
        *spec.reference_image_asset_ids,
    )
    video_asset_ids = spec.reference_video_asset_ids
    if len(video_asset_ids) > _AI_VIDEO_REFERENCE_VIDEO_MAX_COUNT:
        raise VideoProjectGenerationValidationError("AI 视频单次最多允许 3 段参考视频")
    if len(project_image_asset_ids) != len(set(project_image_asset_ids)):
        raise VideoProjectGenerationValidationError("AI 视频参考图片不能重复")
    if len(video_asset_ids) != len(set(video_asset_ids)):
        raise VideoProjectGenerationValidationError("AI 视频参考视频不能重复")
    if set(project_image_asset_ids).intersection(video_asset_ids):
        raise VideoProjectGenerationValidationError("同一工程素材不能同时作为参考图片和参考视频")
    source_references = tuple(
        _project_image_bytes(
            session,
            project=project,
            project_asset_id=project_asset_id,
            asset_store=asset_store,
        )
        for project_asset_id in (
            (spec.source_project_asset_id,) if spec.source_project_asset_id else ()
        )
    )
    manual_references = tuple(
        _project_image_bytes(
            session,
            project=project,
            project_asset_id=project_asset_id,
            asset_store=asset_store,
        )
        for project_asset_id in spec.reference_image_asset_ids
    )
    raw_references: list[tuple[bytes, str, dict[str, object]]] = []
    for reference, project_asset_id in zip(
        source_references,
        ((spec.source_project_asset_id,) if spec.source_project_asset_id else ()),
        strict=True,
    ):
        raw_references.append(
            (
                *reference,
                _project_image_reference_descriptor(
                    session,
                    project_asset_id=project_asset_id,
                    source="source_project_asset",
                ),
            )
        )
    for index, reference in enumerate(
        zip(spec.reference_images, spec.reference_media_types, strict=True)
    ):
        descriptor: dict[str, object] = {"source": "embedded_reference"}
        if index < len(spec.visual_reference_resource_ids):
            descriptor = _knowledge_image_reference_descriptor(
                session,
                resource_entry_id=spec.visual_reference_resource_ids[index],
            )
        raw_references.append((*reference, descriptor))
    for reference, project_asset_id in zip(
        manual_references,
        spec.reference_image_asset_ids,
        strict=True,
    ):
        raw_references.append(
            (
                *reference,
                _project_image_reference_descriptor(
                    session,
                    project_asset_id=project_asset_id,
                    source="project_asset",
                ),
            )
        )
    unique_references: list[tuple[bytes, str, dict[str, object]]] = []
    seen_reference_checksums: set[str] = set()
    for content, media_type, descriptor in raw_references:
        if len(content) >= _AI_VIDEO_REFERENCE_IMAGE_MAX_BYTES:
            raise VideoProjectGenerationValidationError("单张 AI 视频参考图片必须小于 30MB")
        extension = _image_extension_for_media_type(media_type)
        media_kind, canonical_media_type = _validate_generated_media(
            content=content,
            filename=f"reference.{extension}",
            media_type=media_type,
        )
        if media_kind != "image":
            raise VideoProjectGenerationValidationError("参考素材必须是图片")
        checksum = hashlib.sha256(content).hexdigest()
        if checksum in seen_reference_checksums:
            continue
        seen_reference_checksums.add(checksum)
        unique_references.append((content, canonical_media_type, descriptor))
    if len(unique_references) > _AI_VIDEO_REFERENCE_IMAGE_MAX_COUNT:
        raise VideoProjectGenerationValidationError("AI 视频单次最多允许 9 张参考图片")
    image_urls = tuple(
        f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}"
        for content, media_type, _descriptor in unique_references
    )
    reference_image_manifest = [
        {
            "sequence": sequence,
            # Ark places the text prompt in content[0], followed by images.
            "provider_content_index": sequence,
            **descriptor,
        }
        for sequence, (_content, _media_type, descriptor) in enumerate(
            unique_references,
            start=1,
        )
    ]
    _validate_parent_artifact(
        session,
        task,
        spec.parent_artifact_id,
        allowed_kinds=frozenset({"ai_video_clip", "video"}),
    )
    if (
        not image_urls
        and not video_asset_ids
        and provider.capabilities.get("text_to_video") is not True
    ):
        raise VideoProjectGenerationValidationError("当前 AI 视频模型不支持文生视频")
    if image_urls:
        image_capability = provider.capabilities.get("reference_images")
        legacy_single_image = (
            len(image_urls) == 1 and provider.capabilities.get("image_to_video") is True
        )
        if not image_capability and not legacy_single_image:
            raise VideoProjectGenerationValidationError("当前 AI 视频模型不支持参考图片")
        if isinstance(image_capability, dict):
            provider_max_images = image_capability.get("max")
            if isinstance(provider_max_images, int) and len(image_urls) > provider_max_images:
                raise VideoProjectGenerationValidationError(
                    f"当前 AI 视频模型最多允许 {provider_max_images} 张参考图片"
                )
    if video_asset_ids:
        video_capability = provider.capabilities.get("reference_videos")
        if not video_capability:
            raise VideoProjectGenerationValidationError("当前 AI 视频模型不支持参考视频")
        if isinstance(video_capability, dict):
            provider_max_videos = video_capability.get("max")
            if isinstance(provider_max_videos, int) and len(video_asset_ids) > provider_max_videos:
                raise VideoProjectGenerationValidationError(
                    f"当前 AI 视频模型最多允许 {provider_max_videos} 段参考视频"
                )
    if spec.generate_audio and provider.capabilities.get("generate_audio") is not True:
        raise VideoProjectGenerationValidationError("当前 AI 视频模型不支持同步生成音频")
    if spec.return_last_frame and provider.capabilities.get("return_last_frame") is not True:
        raise VideoProjectGenerationValidationError("当前 AI 视频模型不支持返回尾帧")
    declared_durations = provider.capabilities.get("durations", [])
    selectable_durations = (
        [
            value
            for value in declared_durations
            if isinstance(value, int) and not isinstance(value, bool) and value >= 5
        ]
        if isinstance(declared_durations, (list, tuple))
        else []
    )
    preferred_duration = 5 if 5 in selectable_durations else next(iter(selectable_durations), 5)
    duration_seconds = int(
        _resolve_ai_video_option(
            provider,
            capability_key="durations",
            requested=spec.duration_seconds,
            preferred=preferred_duration,
            field_name="视频时长",
        )
    )
    if duration_seconds < 5:
        raise VideoProjectGenerationValidationError(
            "当前 AI 视频模型没有可用的 5 秒及以上视频时长，请选择支持该时长的视频模型。"
        )
    ratio = str(
        _resolve_ai_video_option(
            provider,
            capability_key="ratios",
            requested=spec.ratio,
            preferred="16:9",
            field_name="画面比例",
        )
    )
    resolution = str(
        _resolve_ai_video_option(
            provider,
            capability_key="resolutions",
            requested=spec.resolution,
            preferred="720p",
            field_name="清晰度",
        )
    )
    task_context = _ai_video_task_context(task)
    provider_prompt = _ai_video_provider_prompt(
        task_context,
        prompt,
        supporting_context=spec.supporting_context,
        reference_image_manifest=reference_image_manifest,
        reference_video_count=len(video_asset_ids),
    )
    video_references = tuple(
        _project_video_reference(
            session,
            project=project,
            project_asset_id=project_asset_id,
            asset_store=asset_store,
        )
        for project_asset_id in video_asset_ids
    )
    video_durations = tuple(item.duration_seconds for item in video_references)
    if sum(video_durations) > _AI_VIDEO_REFERENCE_VIDEO_MAX_TOTAL_SECONDS:
        raise VideoProjectGenerationValidationError("参考视频总时长不能超过 15 秒")
    request_payload: dict[str, object] = {
        "project_id": project.id,
        "prompt": prompt,
        "label": label,
        "source_copy_artifact_id": spec.source_copy_artifact_id,
        "source_project_asset_id": spec.source_project_asset_id,
        "reference_image_asset_ids": list(spec.reference_image_asset_ids),
        "reference_video_asset_ids": list(spec.reference_video_asset_ids),
        "knowledge_resource_ids": list(spec.knowledge_resource_ids),
        "visual_reference_resource_ids": list(spec.visual_reference_resource_ids),
        "effective_reference_image_count": len(image_urls),
        "reference_image_manifest": reference_image_manifest,
        "reference_video_durations_seconds": list(video_durations),
        "duration_seconds": duration_seconds,
        "ratio": ratio,
        "resolution": resolution,
        "generate_audio": spec.generate_audio,
        "return_last_frame": spec.return_last_frame,
        "parent_artifact_id": spec.parent_artifact_id,
        "task_context": task_context,
        "context_included": True,
    }
    if spec.supporting_context is not None:
        request_payload["supporting_context"] = spec.supporting_context
        request_payload["provider_prompt"] = provider_prompt
    enqueued = enqueue_job(
        session,
        task_id=task.id,
        owner_id=created_by,
        kind=VIDEO_PROJECT_AI_JOB_KIND,
        result_json={
            "project_id": project.id,
            "request": request_payload,
            "submission_state": "submitting",
        },
        idempotency_key=spec.idempotency_key,
        max_attempts=3,
        available_at=_AI_VIDEO_SUBMISSION_HOLD_UNTIL,
    )
    if not enqueued.created:
        existing_result = load(enqueued.job.result_json)
        existing_request = existing_result.get("request")
        if not isinstance(existing_request, dict) or any(
            existing_request.get(key) != value for key, value in request_payload.items()
        ):
            raise VideoProjectGenerationConflictError(
                "该幂等键已经用于不同的 AI 视频生成请求，请刷新后重试"
            )
        return ProjectAiVideoEnqueueResult(
            job=enqueued.job,
            provider_task_id=existing_result.get("provider_task_id"),
            created=False,
        )
    publications: list[ProviderMediaPublication] = []
    publication_records: list[dict[str, str]] = []
    video_urls: list[str] = []
    video_transports: list[str] = []
    try:
        for reference in video_references:
            if not provider.status.external:
                video_urls.append(f"asset://project-assets/{reference.project_asset_id}")
                video_transports.append("internal-direct")
                continue
            if provider_media_publisher is not None and provider_media_publisher.enabled:
                publication = provider_media_publisher.publish(
                    reference.source_path,
                    media_type=reference.media_type,
                    publication_id=f"{enqueued.job.id}:{reference.project_asset_id}",
                )
                publications.append(publication)
                record = _publication_record(publication)
                if record:
                    publication_records.append(record)
                video_urls.append(_validated_provider_https_url(publication.url))
                video_transports.append(publication.transport)
                continue
            provider_url_factory = getattr(asset_store, "provider_url", None)
            provider_url = (
                provider_url_factory(reference.storage_key)
                if callable(provider_url_factory)
                else None
            )
            if provider_url:
                video_urls.append(_validated_provider_https_url(provider_url))
                video_transports.append("provider-signed-https-gateway")
                continue
            native_used = False
            if reference.provider_native_reference is not None:
                try:
                    video_urls.append(
                        _provider_native_video_url(
                            provider,
                            reference.provider_native_reference,
                        )
                    )
                    video_transports.append(PROVIDER_NATIVE_VIDEO_TRANSPORT)
                    native_used = True
                except VideoProjectGenerationValidationError:
                    if not _provider_accepts_local_video_upload(provider):
                        raise
            if native_used:
                continue
            if _provider_accepts_local_video_upload(provider):
                video_urls.append(reference.source_path.resolve().as_uri())
                video_transports.append("hiagent-runtime-upload")
                continue
            video_urls.append(_validated_provider_https_url(None))

        estimated_provider_payload = {
            "content": [
                {"type": "text", "text": provider_prompt},
                *(
                    {
                        "type": "image_url",
                        "image_url": {"url": reference_url},
                        "role": "reference_image",
                    }
                    for reference_url in image_urls
                ),
                *(
                    {
                        "type": "video_url",
                        "video_url": {"url": reference_url},
                        "role": "reference_video",
                    }
                    for reference_url in video_urls
                ),
            ],
            "duration": duration_seconds,
            "ratio": ratio,
            "resolution": resolution,
        }
        if (
            len(
                json.dumps(
                    estimated_provider_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            > _AI_VIDEO_REQUEST_BODY_MAX_BYTES
        ):
            raise VideoProjectGenerationValidationError(
                "AI 视频请求体不能超过 64MB，请减少或压缩参考图片"
            )
        request_payload["reference_video_transports"] = video_transports
        request = AiVideoGenerationRequest(
            prompt=provider_prompt,
            image_url=(
                image_urls[0]
                if image_urls and (spec.reference_images or spec.source_project_asset_id)
                else None
            ),
            reference_image_urls=(
                image_urls[1:]
                if image_urls and (spec.reference_images or spec.source_project_asset_id)
                else image_urls
            ),
            reference_video_urls=tuple(video_urls),
            reference_video_durations_seconds=video_durations,
            duration_seconds=duration_seconds,
            ratio=ratio,
            resolution=resolution,
            generate_audio=spec.generate_audio,
            return_last_frame=spec.return_last_frame,
            idempotency_key=spec.idempotency_key,
        )
        session.refresh(enqueued.job)
        if enqueued.job.status == "cancelled" or enqueued.job.cancel_requested_at is not None:
            # Publishing reference media can be slow. Honour a cancellation
            # received in that phase before creating a billable provider task.
            if provider_media_publisher is not None:
                release_provider_media_publications(
                    provider_media_publisher,
                    {PROVIDER_MEDIA_PUBLICATIONS_RESULT_KEY: publication_records},
                )
            return ProjectAiVideoEnqueueResult(
                job=enqueued.job,
                provider_task_id=None,
                created=True,
            )
        try:
            submitted = provider.submit(request)
        except ProviderCallError as exc:
            logger.warning(
                "AI video provider submit failed: code=%s error=%s",
                exc.code,
                exc,
                exc_info=True,
            )
            message, details = normalize_ai_video_provider_failure(
                exc,
                error_code=exc.code,
                request=request_payload,
                retryable=exc.retryable,
            )
            if details["category"] == "input_content_safety":
                raise VideoProjectGenerationSafetyError(
                    message,
                    details=details,
                ) from exc
            failure = ProviderCallError(
                message,
                code="ai_video_generation_failed",
                retryable=exc.retryable,
                status_code=exc.status_code,
            )
            failure.generation_error_detail = details
            raise failure from exc
        except Exception as exc:
            logger.warning("AI video provider submit failed: %s", exc, exc_info=True)
            message, details = normalize_ai_video_provider_failure(exc)
            failure = ProviderCallError(message, retryable=False)
            failure.generation_error_detail = details
            raise failure from exc
        if submitted.status in {"failed", "expired"}:
            logger.warning(
                "AI video provider rejected submit: status=%s code=%s error=%s",
                submitted.status,
                submitted.error_code,
                submitted.error_message,
            )
            message, details = normalize_ai_video_provider_failure(
                submitted.error_message,
                error_code=submitted.error_code,
                request=request_payload,
            )
            if details["category"] == "input_content_safety":
                raise VideoProjectGenerationSafetyError(
                    message,
                    details=details,
                )
            failure = ProviderCallError(message, retryable=False)
            failure.generation_error_detail = details
            raise failure
        if not str(submitted.provider_task_id or "").strip():
            logger.warning(
                "AI video provider submit returned no task id: status=%s code=%s error=%s",
                submitted.status,
                submitted.error_code,
                submitted.error_message,
            )
            message, details = normalize_ai_video_provider_failure(
                submitted.error_message,
                error_code=submitted.error_code,
                request=request_payload,
            )
            if details["category"] == "input_content_safety":
                raise VideoProjectGenerationSafetyError(
                    message,
                    details=details,
                )
            failure = ProviderCallError(message, retryable=False)
            failure.generation_error_detail = details
            raise failure
        provenance = _provider_provenance(
            status=provider.status,
            provider_id=submitted.provider_id,
            model=submitted.model,
            request=request_payload,
            metadata=submitted.metadata,
        )
        internal_result: dict[str, object] = {
            "project_id": project.id,
            "provider_task_id": submitted.provider_task_id,
            "provider_status": submitted.status,
            "submission_state": "submitted",
            "request": request_payload,
            "provenance": provenance,
            "retry_payload": (submitted.metadata or {}).get("retry_payload"),
        }
        if publication_records:
            internal_result[PROVIDER_MEDIA_PUBLICATIONS_RESULT_KEY] = publication_records
        completed_at = datetime.now(UTC)
        opened = session.execute(
            update(Job)
            .where(
                Job.id == enqueued.job.id,
                Job.status == "queued",
                Job.cancel_requested_at.is_(None),
            )
            .values(
                result_json=dump(internal_result),
                progress=submitted.progress or 0,
                available_at=completed_at,
                **(
                    {"status": "cancelled", "completed_at": completed_at}
                    if submitted.status == "cancelled"
                    else {}
                ),
            )
            .execution_options(synchronize_session=False)
        )
        session.refresh(enqueued.job)
        if opened.rowcount != 1:
            # A cancellation can arrive while publish/submit is in flight.
            # Retain the returned ID, but never revive that cancelled local job.
            if enqueued.job.status != "cancelled" and enqueued.job.cancel_requested_at is None:
                raise VideoProjectGenerationConflictError("视频任务状态已变化，请刷新查看结果")
            try:
                cancelled = provider.cancel(submitted.provider_task_id)
                internal_result["provider_status"] = cancelled.status
            except Exception:
                # Keep the cancellation and returned ID even if remote cancel is unavailable.
                logger.exception("Could not cancel submitted AI video task %s", enqueued.job.id)
            enqueued.job.result_json = dump(internal_result)
            session.flush()
            if (
                internal_result["provider_status"]
                in {"succeeded", "failed", "cancelled", "expired"}
                and provider_media_publisher is not None
            ):
                release_provider_media_publications(provider_media_publisher, internal_result)
    except Exception as exc:
        if provider_media_publisher is not None:
            release_provider_media_publications(
                provider_media_publisher,
                {PROVIDER_MEDIA_PUBLICATIONS_RESULT_KEY: publication_records},
            )
        # SQLite legacy transaction control can commit the enqueue SAVEPOINT
        # before the request fails.  The API boundary uses this opaque marker
        # to roll back first and then remove only a provider-less ghost row.
        exc.ai_video_submission_job_id = enqueued.job.id
        raise
    if submitted.status == "cancelled" and provider_media_publisher is not None:
        release_provider_media_publications(
            provider_media_publisher,
            {PROVIDER_MEDIA_PUBLICATIONS_RESULT_KEY: publication_records},
        )
    return ProjectAiVideoEnqueueResult(
        job=enqueued.job,
        provider_task_id=submitted.provider_task_id,
        created=True,
    )


def _video_extension(task: AiVideoTask) -> str:
    if not task.video_url:
        raise VideoProjectGenerationValidationError("AI 视频结果缺少下载地址")
    suffix = Path(urlsplit(task.video_url).path).suffix.lower().lstrip(".")
    return suffix if suffix in _VIDEO_EXTENSIONS else "mp4"


def _validate_stored_video(
    asset_store: AssetStorePort,
    stored: AssetWriteResult,
    *,
    extension: str,
) -> str:
    path = asset_store.path_for(stored.key)
    try:
        with path.open("rb") as source:
            head = read_upload_head(source)
        media_kind, canonical_media_type = validate_upload_header(
            filename=f"generated.{extension}",
            declared_media_type=stored.media_type,
            head=head,
            size_bytes=stored.size,
            policy=MediaIngestPolicy(),
        )
    except (MediaValidationError, OSError) as exc:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        raise VideoProjectGenerationValidationError(f"AI 视频归档文件校验失败：{exc}") from exc
    if media_kind != "video":
        raise VideoProjectGenerationValidationError("AI 视频任务没有返回视频文件")
    return canonical_media_type


def archive_project_ai_video_result(
    session: Session,
    *,
    project: VideoProject,
    task: ContentTask,
    job: Job,
    provider_task: AiVideoTask,
    asset_store: AssetStorePort,
    created_by: str,
    video_transfer: VideoTransfer = download_generated_video,
) -> ProjectAiVideoArchiveResult:
    """Archive one completed provider task; polling and lease completion stay external."""

    _validate_context(project, task, created_by)
    if job.task_id != task.id or job.kind != VIDEO_PROJECT_AI_JOB_KIND:
        raise VideoProjectGenerationValidationError("本地任务不是当前工程的 AI 视频任务")
    job_result = load(job.result_json)
    if job_result.get("project_id") != project.id:
        raise VideoProjectGenerationValidationError("AI 视频任务与工程不匹配")
    existing_asset_id = job_result.get("project_asset_id")
    existing_artifact_id = job_result.get("artifact_id")
    if existing_asset_id and existing_artifact_id:
        existing_asset = session.get(ProjectAsset, existing_asset_id)
        existing_artifact = session.get(Artifact, existing_artifact_id)
        if (
            existing_asset is not None
            and existing_artifact is not None
            and existing_asset.project_id == project.id
        ):
            provenance = load(existing_artifact.content_json).get("provenance", {})
            return ProjectAiVideoArchiveResult(
                GeneratedProjectAsset(
                    existing_artifact,
                    existing_asset,
                    provenance,
                ),
                job_result,
            )
    expected_provider_task_id = job_result.get("provider_task_id")
    if provider_task.provider_task_id != expected_provider_task_id:
        raise VideoProjectGenerationConflictError("Provider 任务 ID 与本地任务不一致")
    if provider_task.status != "succeeded" or not provider_task.video_url:
        raise VideoProjectGenerationConflictError("只有成功的 AI 视频任务可以归档")
    request_payload = job_result.get("request", {})
    if not isinstance(request_payload, dict):
        raise VideoProjectGenerationValidationError("AI 视频任务请求记录损坏")
    extension = _video_extension(provider_task)
    storage_key = (
        f"tasks/{task.id}/video-projects/{project.id}/generated/videos/{uuid4().hex}.{extension}"
    )
    stored = video_transfer(
        provider_task,
        asset_store=asset_store,
        storage_key=storage_key,
    )
    canonical_media_type = _validate_stored_video(
        asset_store,
        stored,
        extension=extension,
    )
    submission_provenance = job_result.get("provenance")
    submission_provenance = submission_provenance if isinstance(submission_provenance, dict) else {}
    provenance = _provider_provenance(
        status=ProviderStatus(
            provider_id=provider_task.provider_id,
            name=provider_task.provider_id,
            capability="ai-video-generation",
            mode=str(submission_provenance.get("provider_mode", "unknown")),
            external=bool(submission_provenance.get("external_provider", True)),
            enabled=True,
            model=provider_task.model,
        ),
        provider_id=provider_task.provider_id,
        model=provider_task.model,
        request=request_payload,
        metadata=provider_task.metadata,
    )
    artifact = create_artifact(
        session,
        task,
        "ai_video_clip",
        {
            "project_id": project.id,
            "source": "ai-generated",
            "prompt": request_payload.get("prompt"),
            "source_copy_artifact_id": request_payload.get("source_copy_artifact_id"),
            "source_project_asset_id": request_payload.get("source_project_asset_id"),
            "reference_image_asset_ids": request_payload.get("reference_image_asset_ids", []),
            "reference_video_asset_ids": request_payload.get("reference_video_asset_ids", []),
            "knowledge_resource_ids": request_payload.get("knowledge_resource_ids", []),
            "visual_reference_resource_ids": request_payload.get(
                "visual_reference_resource_ids",
                [],
            ),
            "provider_task_id": provider_task.provider_task_id,
            "duration_seconds": request_payload.get("duration_seconds"),
            "ratio": request_payload.get("ratio"),
            "resolution": request_payload.get("resolution"),
            "media_type": canonical_media_type,
            "size_bytes": stored.size,
            "checksum_sha256": stored.checksum_sha256,
            "last_frame_url": provider_task.last_frame_url,
            "provenance": provenance,
        },
        created_by,
        stored.key,
    )
    parent_artifact_id = request_payload.get("parent_artifact_id")
    if parent_artifact_id:
        parent = _validate_parent_artifact(
            session,
            task,
            str(parent_artifact_id),
            allowed_kinds=frozenset({"ai_video_clip", "video"}),
        )
        artifact.parent_id = parent.id if parent is not None else None
    metadata = _project_asset_metadata(
        role="video",
        stored=stored,
        provenance=provenance,
        extra={
            "media_kind": "video",
            "prompt": request_payload.get("prompt"),
            "provider_task_id": provider_task.provider_task_id,
            "duration_seconds": request_payload.get("duration_seconds"),
            "source_copy_artifact_id": request_payload.get("source_copy_artifact_id"),
            "source_project_asset_id": request_payload.get("source_project_asset_id"),
            "reference_image_asset_ids": request_payload.get("reference_image_asset_ids", []),
            "reference_video_asset_ids": request_payload.get("reference_video_asset_ids", []),
            "knowledge_resource_ids": request_payload.get("knowledge_resource_ids", []),
            "visual_reference_resource_ids": request_payload.get(
                "visual_reference_resource_ids",
                [],
            ),
            "parent_artifact_id": parent_artifact_id,
        },
    )
    project_asset = ensure_project_asset(
        session,
        project,
        source_type="artifact",
        source_id=artifact.id,
        artifact_id=artifact.id,
        role="video",
        label=_validate_label(str(request_payload.get("label") or "AI 视频候选")),
        created_by=created_by,
        metadata=metadata,
    )
    adopt_revision(session, task, artifact)
    job_result = {
        **job_result,
        "provider_status": provider_task.status,
        "artifact_id": artifact.id,
        "project_asset_id": project_asset.id,
        "media_type": canonical_media_type,
        "size_bytes": stored.size,
        "checksum_sha256": stored.checksum_sha256,
    }
    job.result_json = dump(job_result)
    session.flush()
    return ProjectAiVideoArchiveResult(
        GeneratedProjectAsset(artifact, project_asset, provenance),
        job_result,
    )
