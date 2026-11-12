from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .generation_errors import describe_generation_error
from .job_leases import (
    JobLease,
    cancel_job,
    claim_job,
    complete_job,
    fail_job,
    heartbeat_job,
)
from .models import Artifact, ContentTask, Job, VideoProject
from .providers import AssetStorePort
from .timeline_media import (
    TimelineClip,
    TimelineDucking,
    TimelineRenderRequest,
    TimelineRenderResult,
)
from .video_project_rendering import (
    PreparedVideoProjectRender,
    ProjectBundleExportOptions,
    ProjectRenderOptions,
    VideoProjectRenderingError,
    build_stem_export_plan,
    export_video_project_bundle,
    prepare_video_project_render,
    render_stem_export,
)

type RenderProfile = Literal[
    "preview-480p",
    "final-480p",
    "final-720p",
    "final-1080p",
]
type SubtitleMode = Literal["burn", "soft", "none"]
type AudioFormat = Literal["wav", "mp3"]
type ExportDeliverable = Literal[
    "mixed_mp4",
    "silent_mp4",
    "voice_stem",
    "music_stem",
    "sfx_stem",
    "source_audio_stem",
    "subtitles_srt",
    "subtitles_vtt",
    "clips",
    "project_zip",
]

RENDER_JOB_KIND = "video_project_render"
EXPORT_JOB_KIND = "video_project_export"
EXPORT_DELIVERABLES: frozenset[str] = frozenset(
    {
        "mixed_mp4",
        "silent_mp4",
        "voice_stem",
        "music_stem",
        "sfx_stem",
        "source_audio_stem",
        "subtitles_srt",
        "subtitles_vtt",
        "clips",
        "project_zip",
    }
)
_TRACK_KINDS = frozenset({"video", "overlay", "subtitle", "voice", "music", "sfx"})
_ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")
_PROFILE_SETTINGS = {
    "preview-480p": (480, "1M", "96k"),
    "final-480p": (480, "3M", "128k"),
    "final-720p": (720, "5M", "192k"),
    "final-1080p": (1080, "8M", "256k"),
}
_PROFILE_ALIASES = {"preview": "preview-480p", "final": "final-1080p"}
_PUBLIC_DELIVERABLES = {
    "mixed_video": ("mixed_mp4",),
    "silent_video": ("silent_mp4",),
    "voice": ("voice_stem",),
    "music": ("music_stem",),
    "sfx": ("sfx_stem",),
    "source_audio": ("source_audio_stem",),
    "clips": ("clips",),
    "project_zip": ("project_zip",),
}
_WORKER_LEASE_SECONDS = 2 * 60 * 60


class TimelineRendererPort(Protocol):
    def render(self, request: TimelineRenderRequest) -> TimelineRenderResult: ...


class VideoProjectJobError(RuntimeError):
    """An internal video-project job payload or lifecycle is invalid."""


class _JobCancelled(VideoProjectJobError):
    pass


class _LeaseLost(VideoProjectJobError):
    pass


@dataclass(frozen=True, slots=True)
class RenderJobSpec:
    project_id: str
    revision_id: str
    profile: RenderProfile
    fps: int | None
    subtitle_mode: SubtitleMode
    include_audio: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "revision_id": self.revision_id,
            "profile": self.profile,
            "fps": self.fps,
            "subtitle_mode": self.subtitle_mode,
            "include_audio": self.include_audio,
        }


@dataclass(frozen=True, slots=True)
class ExportJobSpec:
    project_id: str
    revision_id: str
    deliverables: tuple[ExportDeliverable, ...]
    profile: RenderProfile
    fps: int | None
    subtitle_mode: SubtitleMode
    subtitle_format: Literal["srt", "vtt", "both"]
    audio_format: AudioFormat
    clip_item_ids: tuple[str, ...]
    project_track_kinds: frozenset[str]
    project_asset_ids: frozenset[str] | None
    include_original_assets: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "revision_id": self.revision_id,
            "deliverables": list(self.deliverables),
            "profile": self.profile,
            "fps": self.fps,
            "subtitle_mode": self.subtitle_mode,
            "subtitle_format": self.subtitle_format,
            "audio_format": self.audio_format,
            "clip_item_ids": list(self.clip_item_ids),
            "project_track_kinds": sorted(self.project_track_kinds),
            "project_asset_ids": (
                sorted(self.project_asset_ids) if self.project_asset_ids is not None else None
            ),
            "include_original_assets": self.include_original_assets,
        }


@dataclass(frozen=True, slots=True)
class VideoProjectJobResult:
    job_id: str
    status: Literal["succeeded", "failed", "cancelled", "skipped", "missing"]
    artifact_id: str | None = None
    storage_key: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _WorkerPaths:
    data_root: Path
    object_root: Path
    work_root: Path


@dataclass(frozen=True, slots=True)
class _ExportedFile:
    path: Path
    archive_path: str
    media_type: str
    kind: str
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class _CaptionCue:
    start_seconds: float
    end_seconds: float
    text: str


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise VideoProjectJobError(f"{label} JSON 无效") from exc
    if not isinstance(payload, dict):
        raise VideoProjectJobError(f"{label} 必须是 JSON 对象")
    return payload


def _internal_id(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    try:
        parsed = UUID(normalized)
    except (ValueError, TypeError, AttributeError) as exc:
        raise VideoProjectJobError(f"{label} 不是有效的内部 ID") from exc
    if str(parsed) != normalized.lower():
        raise VideoProjectJobError(f"{label} 不是规范内部 ID")
    return str(parsed)


def _enum(value: Any, label: str, allowed: frozenset[str], default: str) -> str:
    normalized = str(value if value is not None else default).strip().lower()
    if normalized not in allowed:
        raise VideoProjectJobError(f"{label} 不在允许枚举中")
    return normalized


def _optional_fps(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VideoProjectJobError("fps 必须是整数")
    fps = int(value)
    if fps != value or not 1 <= fps <= 120:
        raise VideoProjectJobError("fps 必须是 1 到 120 的整数")
    return fps


def _render_profile(value: Any, default: RenderProfile) -> RenderProfile:
    normalized = str(value if value is not None else default).strip().lower()
    normalized = _PROFILE_ALIASES.get(normalized, normalized)
    if normalized not in _PROFILE_SETTINGS:
        raise VideoProjectJobError("profile 不在允许枚举中")
    return normalized


def _boolean(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise VideoProjectJobError(f"{label} 必须是布尔值")
    return value


def parse_render_job_spec(payload: dict[str, Any]) -> RenderJobSpec:
    """Read only the allowlisted immutable inputs needed by a render worker."""

    profile = _render_profile(payload.get("profile"), "preview-480p")
    fps = _optional_fps(payload.get("fps"))
    if profile == "final-1080p" and fps is not None and fps > 60:
        raise VideoProjectJobError("final-1080p 的 fps 不能超过 60")
    return RenderJobSpec(
        project_id=_internal_id(payload.get("project_id"), "project_id"),
        revision_id=_internal_id(payload.get("revision_id"), "revision_id"),
        profile=profile,
        fps=fps,
        subtitle_mode=_enum(
            payload.get("subtitle_mode"),
            "subtitle_mode",
            frozenset({"burn", "soft", "none"}),
            "burn",
        ),
        include_audio=_boolean(payload.get("include_audio"), "include_audio", True),
    )


def _string_list(value: Any, label: str, *, maximum: int = 500) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise VideoProjectJobError(f"{label} 必须是受限数组")
    if any(not isinstance(item, str) for item in value):
        raise VideoProjectJobError(f"{label} 只能包含字符串")
    return value


def parse_export_job_spec(payload: dict[str, Any]) -> ExportJobSpec:
    """Read only IDs, booleans, numbers, and documented deliverable enums."""

    subtitle_format = _enum(
        payload.get("subtitle_format"),
        "subtitle_format",
        frozenset({"srt", "vtt", "both"}),
        "srt",
    )
    raw_deliverables = _string_list(payload.get("deliverables"), "deliverables", maximum=20)
    deliverables: list[ExportDeliverable] = []
    for value in raw_deliverables:
        normalized = value.strip().lower()
        if normalized == "subtitles":
            mapped = (
                ("subtitles_srt", "subtitles_vtt")
                if subtitle_format == "both"
                else (f"subtitles_{subtitle_format}",)
            )
        elif normalized in _PUBLIC_DELIVERABLES:
            mapped = _PUBLIC_DELIVERABLES[normalized]
        elif normalized in EXPORT_DELIVERABLES:
            mapped = (normalized,)
        else:
            raise VideoProjectJobError("deliverables 包含不支持的导出类型")
        for internal_name in mapped:
            if internal_name not in deliverables:
                deliverables.append(internal_name)
    if not deliverables:
        raise VideoProjectJobError("deliverables 至少需要一个导出类型")

    clip_item_ids: list[str] = []
    for value in _string_list(payload.get("clip_item_ids"), "clip_item_ids"):
        normalized = value.strip()
        if not _ITEM_ID.fullmatch(normalized):
            raise VideoProjectJobError("clip_item_ids 包含无效内部 ID")
        if normalized not in clip_item_ids:
            clip_item_ids.append(normalized)

    raw_track_kinds = payload.get("project_track_kinds")
    if raw_track_kinds is None:
        track_kinds = set(_TRACK_KINDS)
    else:
        track_kinds = {
            item.strip().lower()
            for item in _string_list(raw_track_kinds, "project_track_kinds", maximum=6)
        }
        if not track_kinds or track_kinds.difference(_TRACK_KINDS):
            raise VideoProjectJobError("project_track_kinds 包含无效轨道")

    raw_asset_ids = payload.get("project_asset_ids")
    project_asset_ids: frozenset[str] | None = None
    if raw_asset_ids is not None:
        project_asset_ids = frozenset(
            _internal_id(value, "project_asset_ids")
            for value in _string_list(raw_asset_ids, "project_asset_ids")
        )
        if not project_asset_ids:
            raise VideoProjectJobError("project_asset_ids 不能是空数组")

    profile = _render_profile(payload.get("profile"), "final-1080p")
    fps = _optional_fps(payload.get("fps"))
    if profile == "final-1080p" and fps is not None and fps > 60:
        raise VideoProjectJobError("final-1080p 的 fps 不能超过 60")
    return ExportJobSpec(
        project_id=_internal_id(payload.get("project_id"), "project_id"),
        revision_id=_internal_id(payload.get("revision_id"), "revision_id"),
        deliverables=tuple(deliverables),
        profile=profile,
        fps=fps,
        subtitle_mode=_enum(
            payload.get("subtitle_mode"),
            "subtitle_mode",
            frozenset({"burn", "soft", "none"}),
            "burn",
        ),
        subtitle_format=subtitle_format,
        audio_format=_enum(
            payload.get("audio_format"),
            "audio_format",
            frozenset({"wav", "mp3"}),
            "wav",
        ),
        clip_item_ids=tuple(clip_item_ids),
        project_track_kinds=frozenset(track_kinds),
        project_asset_ids=project_asset_ids,
        include_original_assets=_boolean(
            payload.get("include_original_assets"),
            "include_original_assets",
            True,
        ),
    )


def _resolve_worker_paths(
    asset_store: AssetStorePort,
    data_root: Path | Any,
) -> _WorkerPaths:
    configured = getattr(data_root, "data_dir", data_root)
    if not isinstance(configured, Path):
        configured = Path(configured)
    resolved_data_root = configured.expanduser().resolve()
    object_root = asset_store.path_for(".video-project-worker-root").parent.resolve()
    if not (
        object_root == resolved_data_root
        or resolved_data_root in object_root.parents
        or object_root in resolved_data_root.parents
    ):
        raise VideoProjectJobError("素材存储目录不属于配置的内容数据目录")
    work_root = object_root / ".video-project-work"
    work_root.mkdir(parents=True, exist_ok=True)
    return _WorkerPaths(resolved_data_root, object_root, work_root)


def _provider_id(renderer: TimelineRendererPort) -> str:
    status = getattr(renderer, "status", None)
    value = str(getattr(status, "provider_id", "local-ffmpeg-timeline"))
    return value[:128]


def _safe_error(exc: Exception, paths: _WorkerPaths | None) -> str:
    value = " ".join(str(exc).replace("\x00", "").split())
    if paths is not None:
        for path in (paths.object_root, paths.data_root, paths.work_root):
            # Keep the redaction marker as prose, not an HTML-looking tag:
            # the shared error formatter rejects HTML bodies in their entirety.
            value = value.replace(str(path), "[路径已隐藏]")
            value = value.replace(str(path).replace("\\", "/"), "[路径已隐藏]")
    return value[:2000] or exc.__class__.__name__


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _job_payload_or_empty(value: str) -> dict[str, Any]:
    try:
        return _json_object(value, "Job result")
    except VideoProjectJobError:
        return {}


def _load_job(session: Session, job_id: str, expected_kind: str) -> Job | None:
    job = session.get(Job, job_id)
    if job is not None and job.kind != expected_kind:
        raise VideoProjectJobError("Job 类型与 worker 不匹配")
    return job


def _claim_worker_job(
    job_id: str,
    expected_kind: str,
    session_factory: Callable[[], Session],
) -> tuple[JobLease, str, str] | VideoProjectJobResult:
    worker_id = f"{expected_kind}:{uuid4().hex[:20]}"
    with session_factory() as session:
        lease = claim_job(
            session,
            worker_id=worker_id,
            lease_seconds=_WORKER_LEASE_SECONDS,
            job_id=job_id,
            kinds=(expected_kind,),
        )
        session.commit()
        if lease is None:
            job = session.get(Job, job_id)
            if job is None:
                return VideoProjectJobResult(job_id, "missing")
            if job.status == "cancelled" or job.cancel_requested_at is not None:
                return VideoProjectJobResult(job_id, "cancelled")
            return VideoProjectJobResult(job_id, "skipped")
        job = _load_job(session, job_id, expected_kind)
        if job is None:
            raise _LeaseLost("已领取的 Job 不存在")
        task_id = job.task_id
        return lease, task_id, job.result_json


def _write_fenced_result(
    session: Session,
    lease: JobLease,
    payload: dict[str, Any],
) -> None:
    updated = session.execute(
        update(Job)
        .where(
            Job.id == lease.job_id,
            Job.status == "running",
            Job.lease_owner == lease.worker_id,
            Job.lease_token == lease.token,
            Job.attempt == lease.attempt,
        )
        .values(result_json=_json_dump(payload))
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:
        raise _LeaseLost("Job 租约 fencing 校验失败")


def _job_checkpoint(
    lease: JobLease,
    session_factory: Callable[[], Session],
    progress: int,
) -> None:
    with session_factory() as session:
        heartbeat = heartbeat_job(
            session,
            lease,
            lease_seconds=_WORKER_LEASE_SECONDS,
            progress=min(progress, 99),
        )
        if heartbeat.renewed:
            session.commit()
            return
        if heartbeat.cancel_requested or heartbeat.status == "cancelled":
            job = session.get(Job, lease.job_id)
            if job is not None and job.status == "running":
                payload = _job_payload_or_empty(job.result_json)
                _write_fenced_result(
                    session,
                    lease,
                    {**payload, "worker_state": "cancelled"},
                )
            cancel_job(session, lease.job_id, lease=lease)
            session.commit()
            raise _JobCancelled("视频工程任务已经取消")
        session.commit()
        raise _LeaseLost("Job 租约已经过期或被其他 attempt 接管")


def _store_running_spec(
    lease: JobLease,
    session_factory: Callable[[], Session],
    payload: dict[str, Any],
    progress: int,
) -> None:
    _job_checkpoint(lease, session_factory, progress)
    with session_factory() as session:
        _write_fenced_result(session, lease, {**payload, "worker_state": "running"})
        session.commit()


def _project_for_job(
    session: Session,
    project_id: str,
    task_id: str,
) -> tuple[VideoProject, ContentTask]:
    project = session.get(VideoProject, project_id)
    task = session.get(ContentTask, task_id)
    if project is None or project.task_id != task_id or task is None:
        raise VideoProjectJobError("视频工程与 Job 内容任务不匹配")
    return project, task


def _apply_render_controls(
    request: TimelineRenderRequest,
    *,
    profile: RenderProfile,
    fps: int | None,
    subtitle_mode: SubtitleMode,
    include_audio: bool,
    output_path: Path,
) -> TimelineRenderRequest:
    short_edge, video_bitrate, audio_bitrate = _PROFILE_SETTINGS[profile]
    ratio = request.width / request.height
    if request.width >= request.height:
        height = short_edge
        width = max(64, round((short_edge * ratio) / 2) * 2)
    else:
        width = short_edge
        height = max(64, round((short_edge / ratio) / 2) * 2)
    if width > 7680 or height > 4320:
        raise VideoProjectJobError("profile 与锁定画布比例生成的分辨率超出安全范围")
    effective_fps = fps or request.fps
    if profile == "final-1080p" and effective_fps > 60:
        raise VideoProjectJobError("final-1080p 的 fps 不能超过 60")
    clips = request.clips
    audio_tracks = request.audio_tracks
    ducking = request.ducking
    duration = request.duration_seconds
    if request.extend_to_audio and audio_tracks:
        duration = max(
            duration or 0,
            *(track.offset_seconds + (track.duration_seconds or 0) for track in audio_tracks),
        )
    if not include_audio:
        clips = tuple(replace(clip, source_audio="mute") for clip in clips)
        audio_tracks = ()
        ducking = replace(ducking, enabled=False)
    return replace(
        request,
        output_path=output_path,
        width=width,
        height=height,
        fps=effective_fps,
        clips=clips,
        audio_tracks=audio_tracks,
        logos=tuple(
            replace(logo, width=max(1, round(logo.width * width / request.width)))
            for logo in request.logos
        ),
        subtitles=request.subtitles if subtitle_mode == "burn" else (),
        duration_seconds=duration,
        ducking=ducking,
        video_bitrate=video_bitrate,
        audio_bitrate=audio_bitrate,
        overwrite=True,
    )


def _next_artifact_version(session: Session, task_id: str, kind: str) -> int:
    current = session.scalar(
        select(func.max(Artifact.version)).where(
            Artifact.task_id == task_id,
            Artifact.kind == kind,
        )
    )
    return int(current or 0) + 1


def _create_render_artifact(
    session: Session,
    *,
    job: Job,
    spec: RenderJobSpec,
    prepared: PreparedVideoProjectRender,
    result: TimelineRenderResult,
    storage_key: str,
    provider_id: str,
    soft_subtitle_track: bool,
) -> Artifact:
    output = result.output_path
    artifact = Artifact(
        task_id=job.task_id,
        kind="video",
        stage_key="video_preview",
        version=_next_artifact_version(session, job.task_id, "video"),
        status="draft",
        content_json=_json_dump(
            {
                "format": "mp4",
                "media_type": "video/mp4",
                "purpose": (
                    "exact_project_preview"
                    if spec.profile.startswith("preview-")
                    else "final_project_render"
                ),
                "timeline_composition_version": 2,
                "render_profile": spec.profile,
                "project_id": spec.project_id,
                "revision_id": spec.revision_id,
                "revision": prepared.revision,
                "job_id": job.id,
                "provider": provider_id,
                "duration_seconds": result.duration_seconds,
                "width": result.width,
                "height": result.height,
                "fps": result.fps,
                "clip_count": result.clip_count,
                "audio_track_count": result.audio_track_count,
                "transitions_applied": list(result.transitions_applied),
                "source_audio_clip_count": result.source_audio_clip_count,
                "ducking_applied": result.ducking_applied,
                "logos_applied": result.logos_applied,
                "subtitles_burned": result.subtitles_burned,
                "soft_subtitle_track": soft_subtitle_track,
                "subtitle_mode": spec.subtitle_mode,
                "include_audio": spec.include_audio,
                "size_bytes": output.stat().st_size,
                "checksum_sha256": _sha256_file(output),
            }
        ),
        storage_key=storage_key,
        created_by=job.owner_id,
    )
    session.add(artifact)
    session.flush()
    return artifact


def _finish_failed_job(
    lease: JobLease,
    session_factory: Callable[[], Session],
    error: str,
    error_detail: dict[str, object] | None = None,
) -> VideoProjectJobResult:
    with session_factory() as session:
        job = session.get(Job, lease.job_id)
        if job is not None:
            payload = _job_payload_or_empty(job.result_json)
            state = "cancelled" if job.cancel_requested_at is not None else "failed"
            _write_fenced_result(
                session,
                lease,
                {
                    **payload,
                    "worker_state": state,
                    **({"error_detail": error_detail} if error_detail is not None else {}),
                },
            )
        transition = fail_job(
            session,
            lease,
            error=error,
            retryable=False,
        )
        session.commit()
        if transition is None:
            job = session.get(Job, lease.job_id)
            if job is None:
                return VideoProjectJobResult(lease.job_id, "missing", error=error)
            if job.status == "cancelled" or job.cancel_requested_at is not None:
                return VideoProjectJobResult(lease.job_id, "cancelled")
            return VideoProjectJobResult(lease.job_id, "skipped", error=error)
        return VideoProjectJobResult(
            lease.job_id,
            transition.status,
            error=error if transition.status == "failed" else None,
        )


def _finish_cancelled_job(
    lease: JobLease,
    session_factory: Callable[[], Session],
) -> VideoProjectJobResult:
    with session_factory() as session:
        job = session.get(Job, lease.job_id)
        if job is not None and job.status == "running":
            payload = _job_payload_or_empty(job.result_json)
            _write_fenced_result(
                session,
                lease,
                {**payload, "worker_state": "cancelled"},
            )
        transition = cancel_job(session, lease.job_id, lease=lease)
        session.commit()
    return VideoProjectJobResult(
        lease.job_id,
        "cancelled" if transition.status == "cancelled" else "skipped",
    )


def run_video_project_render_job(
    job_id: str,
    session_factory: Callable[[], Session],
    asset_store: AssetStorePort,
    renderer: TimelineRendererPort,
    data_root: Path | Any,
) -> VideoProjectJobResult:
    """Render the exact committed revision pinned in ``Job.result_json``."""

    paths: _WorkerPaths | None = None
    lease: JobLease | None = None
    output_path: Path | None = None
    intermediate_path: Path | None = None
    try:
        claimed = _claim_worker_job(job_id, RENDER_JOB_KIND, session_factory)
        if isinstance(claimed, VideoProjectJobResult):
            return claimed
        lease, task_id, raw_payload = claimed
        spec = parse_render_job_spec(_json_object(raw_payload, "Job result"))
        _store_running_spec(lease, session_factory, spec.as_dict(), 5)
        paths = _resolve_worker_paths(asset_store, data_root)
        # Attempt/token fencing prevents an expired worker from overwriting the
        # next attempt's media even if its FFmpeg process finishes late.
        output_key = f"video-jobs/{job_id}/a{lease.attempt}-{lease.token[:8]}-{spec.profile}.mp4"
        output_path = asset_store.path_for(output_key).resolve()
        intermediate_path = (
            output_path.with_name(f".{output_path.stem}.video-only.mp4")
            if spec.subtitle_mode == "soft"
            else output_path
        )
        with session_factory() as session:
            project, _ = _project_for_job(session, spec.project_id, task_id)
            prepared = prepare_video_project_render(
                session,
                project,
                asset_store,
                intermediate_path,
                work_root=paths.work_root,
                revision_id=spec.revision_id,
                options=ProjectRenderOptions(overwrite=True),
            )
        with prepared:
            request = _apply_render_controls(
                prepared.request,
                profile=spec.profile,
                fps=spec.fps,
                subtitle_mode=spec.subtitle_mode,
                include_audio=spec.include_audio,
                output_path=intermediate_path,
            )
            _job_checkpoint(lease, session_factory, 20)
            result = renderer.render(request)
            if result.output_path.resolve() != intermediate_path or not intermediate_path.is_file():
                raise VideoProjectJobError("时间线 renderer 返回了非预期输出")
            _job_checkpoint(lease, session_factory, 78)
            soft_subtitle_track = False
            if spec.subtitle_mode == "soft":
                soft_subtitle_track = _mux_soft_subtitles(
                    renderer,
                    prepared,
                    intermediate_path,
                    output_path,
                    allowed_root=paths.object_root,
                    work_root=paths.work_root,
                )
                result = replace(
                    result,
                    output_path=output_path,
                    subtitles_burned=0,
                )
                _job_checkpoint(lease, session_factory, 90)
            with session_factory() as session:
                job = _load_job(session, job_id, RENDER_JOB_KIND)
                if job is None or job.cancel_requested_at is not None:
                    raise _JobCancelled("视频工程渲染任务已经取消")
                if (
                    job.status != "running"
                    or job.lease_token != lease.token
                    or job.lease_owner != lease.worker_id
                    or job.attempt != lease.attempt
                ):
                    raise _LeaseLost("Job attempt fencing 校验失败")
                artifact = _create_render_artifact(
                    session,
                    job=job,
                    spec=spec,
                    prepared=prepared,
                    result=result,
                    storage_key=output_key,
                    provider_id=_provider_id(renderer),
                    soft_subtitle_track=soft_subtitle_track,
                )
                completion_payload = {
                    **spec.as_dict(),
                    "worker_state": "succeeded",
                    "artifact_id": artifact.id,
                    "timeline_composition_version": 2,
                    "storage_key": output_key,
                    "size_bytes": output_path.stat().st_size,
                    "checksum_sha256": _sha256_file(output_path),
                }
                transition = complete_job(
                    session,
                    lease,
                    result_json=completion_payload,
                )
                if transition is None:
                    session.rollback()
                    raise _LeaseLost("完成 Job 时 attempt fencing 校验失败")
                session.commit()
                return VideoProjectJobResult(
                    job_id,
                    "succeeded",
                    artifact_id=artifact.id,
                    storage_key=output_key,
                )
    except _JobCancelled:
        if output_path is not None:
            output_path.unlink(missing_ok=True)
        if intermediate_path is not None and intermediate_path != output_path:
            intermediate_path.unlink(missing_ok=True)
        if lease is None:
            return VideoProjectJobResult(job_id, "cancelled")
        return _finish_cancelled_job(lease, session_factory)
    except _LeaseLost as exc:
        if output_path is not None:
            output_path.unlink(missing_ok=True)
        if intermediate_path is not None and intermediate_path != output_path:
            intermediate_path.unlink(missing_ok=True)
        return VideoProjectJobResult(job_id, "skipped", error=_safe_error(exc, paths))
    except Exception as exc:
        if output_path is not None:
            output_path.unlink(missing_ok=True)
        if intermediate_path is not None and intermediate_path != output_path:
            intermediate_path.unlink(missing_ok=True)
        details = describe_generation_error(_safe_error(exc, paths))
        error = str(details["message"])
        if lease is None:
            return VideoProjectJobResult(job_id, "skipped", error=error)
        return _finish_failed_job(lease, session_factory, error, details)


def _timestamp(value: str) -> float:
    normalized = value.strip().replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise VideoProjectJobError("字幕时间戳格式无效")
    try:
        result = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError as exc:
        raise VideoProjectJobError("字幕时间戳格式无效") from exc
    if result < 0 or result > 86_400:
        raise VideoProjectJobError("字幕时间戳超出允许范围")
    return result


def _caption_cues(path: Path) -> list[_CaptionCue]:
    if path.stat().st_size > 10 * 1024 * 1024:
        raise VideoProjectJobError("字幕文件超过服务端大小限制")
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    cues: list[_CaptionCue] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line == "WEBVTT":
            index += 1
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        if "-->" not in line and index + 1 < len(lines) and "-->" in lines[index + 1]:
            index += 1
            line = lines[index].strip()
        if "-->" not in line:
            index += 1
            continue
        start_text, end_text = line.split("-->", 1)
        end_token = end_text.strip().split()[0]
        start = _timestamp(start_text)
        end = _timestamp(end_token)
        if end <= start:
            raise VideoProjectJobError("字幕结束时间必须晚于开始时间")
        index += 1
        content: list[str] = []
        while index < len(lines) and lines[index].strip():
            content.append(lines[index].replace("\x00", ""))
            index += 1
        value = "\n".join(content).strip()
        if value:
            cues.append(_CaptionCue(start, end, value))
    return cues


def _srt_time(seconds: float) -> str:
    milliseconds = round(max(0, seconds) * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _vtt_time(seconds: float) -> str:
    return _srt_time(seconds).replace(",", ".")


def _write_consolidated_subtitles(
    prepared: PreparedVideoProjectRender,
    output_path: Path,
    subtitle_format: Literal["srt", "vtt"],
) -> int:
    cues = sorted(
        (
            cue
            for subtitle in prepared.request.subtitles
            for cue in _caption_cues(subtitle.source_path.resolve(strict=True))
        ),
        key=lambda cue: (cue.start_seconds, cue.end_seconds, cue.text),
    )
    if subtitle_format == "srt":
        blocks = [
            "\n".join(
                [
                    str(index),
                    f"{_srt_time(cue.start_seconds)} --> {_srt_time(cue.end_seconds)}",
                    cue.text,
                ]
            )
            for index, cue in enumerate(cues, start=1)
        ]
        payload = "\n\n".join(blocks) + ("\n" if blocks else "")
    else:
        blocks = [
            "\n".join(
                [
                    f"{_vtt_time(cue.start_seconds)} --> {_vtt_time(cue.end_seconds)}",
                    cue.text,
                ]
            )
            for cue in cues
        ]
        payload = "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")
    output_path.write_text(payload, encoding="utf-8")
    return len(cues)


def _ffmpeg_executable(renderer: TimelineRendererPort) -> str:
    configured = getattr(renderer, "ffmpeg", None)
    if configured:
        path = Path(str(configured)).expanduser().resolve()
        if not path.is_file():
            raise VideoProjectJobError("renderer 的 FFmpeg 可执行文件不存在")
        return str(path)
    if found := shutil.which("ffmpeg"):
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError) as exc:
        raise VideoProjectJobError("没有可用的 FFmpeg 进行软字幕封装") from exc


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _mux_soft_subtitles(
    renderer: TimelineRendererPort,
    prepared: PreparedVideoProjectRender,
    source_path: Path,
    output_path: Path,
    *,
    allowed_root: Path,
    work_root: Path,
) -> bool:
    source = source_path.resolve(strict=True)
    output = output_path.resolve()
    allowed = allowed_root.resolve()
    work = work_root.resolve()
    if not _within(source, allowed) or not _within(output, allowed) or not _within(work, allowed):
        raise VideoProjectJobError("软字幕输入或输出超出受控目录")
    if source == output or output.suffix.lower() != ".mp4":
        raise VideoProjectJobError("软字幕输出路径无效")
    subtitle = work / f"soft-subtitle-{uuid4().hex}.srt"
    cue_count = _write_consolidated_subtitles(prepared, subtitle, "srt")
    if cue_count == 0:
        source.replace(output)
        return False
    command = [
        _ffmpeg_executable(renderer),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(subtitle),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "1:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-c:s",
        "mov_text",
        "-metadata:s:s:0",
        "language=chi",
        "-movflags",
        "+faststart",
        str(output),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg 软字幕封装超时") from exc
    except subprocess.CalledProcessError as exc:
        stderr = " ".join((exc.stderr or "").split())[-1600:]
        raise RuntimeError(f"FFmpeg 软字幕封装失败：{stderr}") from exc
    finally:
        subtitle.unlink(missing_ok=True)
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg 未生成有效软字幕 MP4")
    source.unlink(missing_ok=True)
    return True


def _safe_name(value: str, fallback: str) -> str:
    normalized = _SAFE_NAME.sub("-", value).strip(" .-_")
    return (normalized or fallback)[:100]


def _clip_item_pairs(
    prepared: PreparedVideoProjectRender,
) -> list[tuple[str, TimelineClip]]:
    items = [item for item in prepared.item_sources if item.track_kind == "video"]
    if len(items) != len(prepared.request.clips):
        raise VideoProjectRenderingError("画面素材索引与渲染请求不一致")
    return [(item.item_id, clip) for item, clip in zip(items, prepared.request.clips, strict=True)]


def _render_export_video(
    renderer: TimelineRendererPort,
    prepared: PreparedVideoProjectRender,
    spec: ExportJobSpec,
    output_path: Path,
    *,
    include_audio: bool,
    allowed_root: Path,
    work_root: Path,
) -> TimelineRenderResult:
    render_path = (
        output_path.with_name(f".{output_path.stem}.video-only.mp4")
        if spec.subtitle_mode == "soft"
        else output_path
    )
    request = _apply_render_controls(
        prepared.request,
        profile=spec.profile,
        fps=spec.fps,
        subtitle_mode=spec.subtitle_mode,
        include_audio=include_audio,
        output_path=render_path,
    )
    result = renderer.render(request)
    if result.output_path.resolve() != render_path.resolve() or not render_path.is_file():
        raise VideoProjectJobError("时间线 renderer 返回了非预期导出文件")
    if spec.subtitle_mode == "soft":
        _mux_soft_subtitles(
            renderer,
            prepared,
            render_path,
            output_path,
            allowed_root=allowed_root,
            work_root=work_root,
        )
        result = replace(result, output_path=output_path, subtitles_burned=0)
    return result


def _export_clips(
    renderer: TimelineRendererPort,
    prepared: PreparedVideoProjectRender,
    spec: ExportJobSpec,
    workspace: Path,
) -> list[_ExportedFile]:
    pairs = _clip_item_pairs(prepared)
    if spec.clip_item_ids:
        by_id = dict(pairs)
        missing = set(spec.clip_item_ids).difference(by_id)
        if missing:
            raise VideoProjectJobError("clip_item_ids 不属于锁定版本的画面轨道")
        pairs = [(item_id, by_id[item_id]) for item_id in spec.clip_item_ids]
    results: list[_ExportedFile] = []
    for index, (item_id, clip) in enumerate(pairs, start=1):
        output = workspace / "clips" / f"{index:03d}-{_safe_name(item_id, 'clip')}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        controlled = _apply_render_controls(
            prepared.request,
            profile=spec.profile,
            fps=spec.fps,
            subtitle_mode="none",
            include_audio=True,
            output_path=output,
        )
        clip_request = replace(
            controlled,
            clips=(replace(clip, transition="none"),),
            audio_tracks=(),
            logos=(),
            subtitles=(),
            ducking=TimelineDucking(enabled=False),
            duration_seconds=None,
            extend_to_audio=False,
        )
        result = renderer.render(clip_request)
        if result.output_path.resolve() != output.resolve() or not output.is_file():
            raise VideoProjectJobError("时间线 renderer 返回了非预期裁切片段")
        results.append(
            _ExportedFile(
                output,
                f"clips/{output.name}",
                "video/mp4",
                "clip",
                item_id,
            )
        )
    return results


def _deliverable_entry(value: _ExportedFile) -> dict[str, Any]:
    return {
        "kind": value.kind,
        "item_id": value.item_id,
        "path": value.archive_path,
        "media_type": value.media_type,
        "size_bytes": value.path.stat().st_size,
        "checksum_sha256": _sha256_file(value.path),
    }


def _write_export_archive(
    output_path: Path,
    *,
    job_id: str,
    task_id: str,
    spec: ExportJobSpec,
    prepared: PreparedVideoProjectRender,
    files: list[_ExportedFile],
) -> dict[str, Any]:
    deliverable_entries = [_deliverable_entry(value) for value in files]
    manifest = {
        "format": "content-video-project-export/v1",
        "job_id": job_id,
        "task_id": task_id,
        "project_id": spec.project_id,
        "revision_id": spec.revision_id,
        "revision": prepared.revision,
        "requested_deliverables": list(spec.deliverables),
        "profile": spec.profile,
        "fps": spec.fps or prepared.request.fps,
        "subtitle_mode": spec.subtitle_mode,
        "audio_format": spec.audio_format,
        "deliverables": deliverable_entries,
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    checksums = {
        value.archive_path: entry["checksum_sha256"]
        for value, entry in zip(files, deliverable_entries, strict=True)
    }
    checksums["manifest.json"] = hashlib.sha256(manifest_bytes).hexdigest()
    checksum_bytes = "".join(
        f"{checksum}  {archive_path}\n" for archive_path, checksum in sorted(checksums.items())
    ).encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        with zipfile.ZipFile(
            temporary_path,
            "w",
            zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            archive.writestr("checksums.sha256", checksum_bytes)
            for value in files:
                archive.write(value.path, value.archive_path)
        temporary_path.replace(output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return manifest


def _export_file(
    path: Path,
    archive_path: str,
    kind: str,
    media_type: str | None = None,
) -> _ExportedFile:
    return _ExportedFile(
        path,
        archive_path,
        media_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        kind,
    )


def run_video_project_export_job(
    job_id: str,
    session_factory: Callable[[], Session],
    asset_store: AssetStorePort,
    renderer: TimelineRendererPort,
    data_root: Path | Any,
) -> VideoProjectJobResult:
    """Render the locked revision; return MP4 directly for a normal single-video export."""

    paths: _WorkerPaths | None = None
    lease: JobLease | None = None
    final_path: Path | None = None
    try:
        claimed = _claim_worker_job(job_id, EXPORT_JOB_KIND, session_factory)
        if isinstance(claimed, VideoProjectJobResult):
            return claimed
        lease, task_id, raw_payload = claimed
        spec = parse_export_job_spec(_json_object(raw_payload, "Job result"))
        _store_running_spec(lease, session_factory, spec.as_dict(), 3)
        paths = _resolve_worker_paths(asset_store, data_root)
        direct_mixed_video = spec.deliverables == ("mixed_mp4",)
        final_extension = "mp4" if direct_mixed_video else "zip"
        final_key = f"video-jobs/{job_id}/a{lease.attempt}-{lease.token[:8]}.{final_extension}"
        final_path = asset_store.path_for(final_key).resolve()
        with tempfile.TemporaryDirectory(
            prefix=f"export-{job_id[:12]}-",
            dir=paths.work_root,
        ) as temporary_directory:
            workspace = Path(temporary_directory).resolve()
            placeholder = workspace / "render-placeholder.mp4"
            with session_factory() as session:
                project, _ = _project_for_job(session, spec.project_id, task_id)
                prepared = prepare_video_project_render(
                    session,
                    project,
                    asset_store,
                    placeholder,
                    work_root=workspace,
                    revision_id=spec.revision_id,
                    options=ProjectRenderOptions(overwrite=True),
                )
            with prepared:
                files: list[_ExportedFile] = []
                total = len(spec.deliverables)
                for completed, deliverable in enumerate(spec.deliverables):
                    _job_checkpoint(
                        lease,
                        session_factory,
                        5 + int(completed / max(total, 1) * 80),
                    )
                    if deliverable == "mixed_mp4":
                        output = workspace / "video" / "mixed.mp4"
                        output.parent.mkdir(parents=True, exist_ok=True)
                        _render_export_video(
                            renderer,
                            prepared,
                            spec,
                            output,
                            include_audio=True,
                            allowed_root=paths.object_root,
                            work_root=workspace,
                        )
                        files.append(_export_file(output, "video/mixed.mp4", deliverable))
                    elif deliverable == "silent_mp4":
                        output = workspace / "video" / "silent.mp4"
                        output.parent.mkdir(parents=True, exist_ok=True)
                        _render_export_video(
                            renderer,
                            prepared,
                            spec,
                            output,
                            include_audio=False,
                            allowed_root=paths.object_root,
                            work_root=workspace,
                        )
                        files.append(_export_file(output, "video/silent.mp4", deliverable))
                    elif deliverable.endswith("_stem"):
                        stem_kind = deliverable.removesuffix("_stem")
                        output = workspace / "audio" / f"{stem_kind}.{spec.audio_format}"
                        output.parent.mkdir(parents=True, exist_ok=True)
                        plan = build_stem_export_plan(
                            prepared,
                            stem_kind,
                            output,
                            allowed_input_roots=(paths.object_root,),
                            allowed_output_roots=(workspace,),
                            overwrite=True,
                        )
                        render_stem_export(plan)
                        media_type = "audio/wav" if spec.audio_format == "wav" else "audio/mpeg"
                        files.append(
                            _export_file(
                                output,
                                f"audio/{output.name}",
                                deliverable,
                                media_type,
                            )
                        )
                    elif deliverable in {"subtitles_srt", "subtitles_vtt"}:
                        subtitle_format = deliverable.removeprefix("subtitles_")
                        output = workspace / "subtitles" / f"subtitles.{subtitle_format}"
                        output.parent.mkdir(parents=True, exist_ok=True)
                        _write_consolidated_subtitles(prepared, output, subtitle_format)
                        files.append(
                            _export_file(
                                output,
                                f"subtitles/{output.name}",
                                deliverable,
                                "application/x-subrip" if subtitle_format == "srt" else "text/vtt",
                            )
                        )
                    elif deliverable == "clips":
                        files.extend(_export_clips(renderer, prepared, spec, workspace))
                    elif deliverable == "project_zip":
                        output = workspace / "project" / "project.zip"
                        output.parent.mkdir(parents=True, exist_ok=True)
                        with session_factory() as session:
                            project, _ = _project_for_job(session, spec.project_id, task_id)
                            export_video_project_bundle(
                                session,
                                project,
                                asset_store,
                                output,
                                allowed_output_roots=(workspace,),
                                revision_id=spec.revision_id,
                                options=ProjectBundleExportOptions(
                                    track_kinds=spec.project_track_kinds,
                                    project_asset_ids=spec.project_asset_ids,
                                    include_original_assets=spec.include_original_assets,
                                    overwrite=True,
                                ),
                            )
                        files.append(
                            _export_file(
                                output,
                                "project/project.zip",
                                deliverable,
                                "application/zip",
                            )
                        )
                    _job_checkpoint(
                        lease,
                        session_factory,
                        5 + int((completed + 1) / max(total, 1) * 80),
                    )
                _job_checkpoint(lease, session_factory, 88)
                if direct_mixed_video:
                    mixed_video = next(
                        (item for item in files if item.kind == "mixed_mp4"),
                        None,
                    )
                    if mixed_video is None:
                        raise VideoProjectJobError("成片视频导出结果缺失")
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(mixed_video.path, final_path)
                    manifest = {
                        "format": "content-video-export/v1",
                        "job_id": job_id,
                        "task_id": task_id,
                        "project_id": spec.project_id,
                        "revision_id": spec.revision_id,
                        "deliverables": [_deliverable_entry(mixed_video)],
                    }
                else:
                    manifest = _write_export_archive(
                        final_path,
                        job_id=job_id,
                        task_id=task_id,
                        spec=spec,
                        prepared=prepared,
                        files=files,
                    )
                _job_checkpoint(lease, session_factory, 96)
                with session_factory() as session:
                    job = _load_job(session, job_id, EXPORT_JOB_KIND)
                    if job is None or job.cancel_requested_at is not None:
                        raise _JobCancelled("视频工程导出任务已经取消")
                    if (
                        job.status != "running"
                        or job.lease_token != lease.token
                        or job.lease_owner != lease.worker_id
                        or job.attempt != lease.attempt
                    ):
                        raise _LeaseLost("Job attempt fencing 校验失败")
                    transition = complete_job(
                        session,
                        lease,
                        result_json={
                            **spec.as_dict(),
                            "worker_state": "succeeded",
                            "storage_key": final_key,
                            "media_type": (
                                "video/mp4" if direct_mixed_video else "application/zip"
                            ),
                            "download_filename": (
                                "成片视频.mp4" if direct_mixed_video else "视频素材包.zip"
                            ),
                            "size_bytes": final_path.stat().st_size,
                            "checksum_sha256": _sha256_file(final_path),
                            "manifest": manifest,
                        },
                    )
                    if transition is None:
                        session.rollback()
                        raise _LeaseLost("完成 Job 时 attempt fencing 校验失败")
                    session.commit()
                    return VideoProjectJobResult(
                        job_id,
                        "succeeded",
                        storage_key=final_key,
                    )
    except _JobCancelled:
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        if lease is None:
            return VideoProjectJobResult(job_id, "cancelled")
        return _finish_cancelled_job(lease, session_factory)
    except _LeaseLost as exc:
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        return VideoProjectJobResult(job_id, "skipped", error=_safe_error(exc, paths))
    except Exception as exc:
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        details = describe_generation_error(_safe_error(exc, paths))
        error = str(details["message"])
        if lease is None:
            return VideoProjectJobResult(job_id, "skipped", error=error)
        return _finish_failed_job(lease, session_factory, error, details)
