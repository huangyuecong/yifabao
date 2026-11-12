from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, Literal, Protocol
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    provider_id: str
    name: str
    capability: str
    mode: str
    external: bool
    enabled: bool
    model: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    id: str
    title: str
    text: str
    category: str
    data_level: str
    version: int = 1
    source_uri: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    text: str
    requester_id: str
    task_id: str
    categories: tuple[str, ...]
    allowed_data_levels: frozenset[str]
    limit: int
    requester_department: str = ""
    requester_roles: frozenset[str] = frozenset()
    requester_tenant: str | None = None


class KnowledgeSearchPort(Protocol):
    status: ProviderStatus

    def search(self, query: KnowledgeQuery) -> Sequence[KnowledgeChunk]: ...


# Kept as a compatibility alias for the existing generation layer.
KnowledgeProvider = KnowledgeSearchPort


@dataclass(frozen=True, slots=True)
class KnowledgeCategory:
    id: str
    name: str
    document_count: int = 0


@dataclass(frozen=True, slots=True)
class KnowledgeIngestRequest:
    source_path: Path
    owner_id: str
    title: str = ""
    category: str = "document"
    data_level: str = "internal"
    source_uri: str | None = None
    source_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KnowledgeIngestResult:
    document_id: str
    title: str
    category: str
    text: str
    chunks: tuple[KnowledgeChunk, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


class KnowledgeCatalogPort(Protocol):
    status: ProviderStatus

    def ingest(self, request: KnowledgeIngestRequest) -> KnowledgeIngestResult: ...

    def categories(self) -> Sequence[KnowledgeCategory]: ...


@dataclass(frozen=True, slots=True)
class AssetWriteResult:
    key: str
    size: int
    media_type: str
    checksum_sha256: str


class AssetStorePort(Protocol):
    def put_bytes(self, key: str, content: bytes, media_type: str) -> AssetWriteResult: ...

    def put_stream(
        self,
        key: str,
        stream: BinaryIO,
        media_type: str,
    ) -> AssetWriteResult: ...

    def read_bytes(self, key: str) -> bytes: ...

    def path_for(self, key: str) -> Path: ...

    def exists(self, key: str) -> bool: ...

    def provider_url(self, key: str) -> str | None:
        """Return a short-lived provider-fetchable HTTPS URL when configured."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderMediaPublication:
    """One short-lived, provider-fetchable copy of a local media object.

    ``url`` is a bearer credential and must remain in memory only.  The opaque
    ``cleanup_token`` may be persisted in an internal job field so a later
    worker can release the private object without persisting the signed URL.
    """

    url: str
    transport: str
    expires_at: datetime
    cleanup_token: str | None = None


class ProviderMediaPublisherPort(Protocol):
    enabled: bool
    transport: str
    url_ttl_seconds: int | None

    def publish(
        self,
        source_path: Path,
        *,
        media_type: str,
        publication_id: str,
    ) -> ProviderMediaPublication: ...

    def release(self, cleanup_token: str) -> None: ...

    def purge_expired(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ContentIdentity:
    user_id: str
    department: str = ""
    roles: frozenset[str] = frozenset()


class IdentityPort(Protocol):
    """Boundary for a future main-system identity adapter."""

    def current(self) -> ContentIdentity: ...


class JobDispatcherPort(Protocol):
    """Persistent or local background execution boundary."""

    def submit(self, operation: Callable[[], None]) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ImageGenerationRequest:
    prompt: str
    count: int = 1
    size: str | None = None
    ratio: str | None = None
    reference_images: tuple[bytes, ...] = ()
    reference_media_types: tuple[str, ...] = ()
    watermark: bool = False
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GeneratedAsset:
    content: bytes
    media_type: str
    extension: str
    provider_id: str
    model: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ImageGenerationPort(Protocol):
    status: ProviderStatus
    capabilities: dict[str, Any]

    def generate(self, request: ImageGenerationRequest) -> Sequence[GeneratedAsset]: ...


@dataclass(frozen=True, slots=True)
class WordTiming:
    text: str
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True, slots=True)
class SpeechSynthesisRequest:
    text: str
    voice: str | None = None
    speed: float = 1.0
    volume: float = 1.0
    audio_format: Literal["wav", "mp3", "pcm", "ogg_opus"] = "mp3"
    sample_rate: int = 24000
    with_timestamps: bool = True
    user_id: str = "content-agent"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SpeechSynthesisResult:
    audio: bytes
    media_type: str
    extension: str
    duration_seconds: float | None
    provider_id: str
    voice: str
    word_timings: tuple[WordTiming, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class SpeechSynthesisPort(Protocol):
    status: ProviderStatus
    capabilities: dict[str, Any]

    def synthesize(self, request: SpeechSynthesisRequest) -> SpeechSynthesisResult: ...


@dataclass(frozen=True, slots=True)
class MusicGenerationRequest:
    """Provider-neutral request for instrumental background music.

    ``duration_seconds`` is a timeline target. Some suppliers, including
    MiniMax Music 2.6, do not expose exact duration control; callers must use
    the returned duration and let the timeline renderer trim or loop the
    material to this target.
    """

    prompt: str
    duration_seconds: float
    audio_format: Literal["mp3", "wav"] = "mp3"
    response_format: Literal["hex", "url"] | None = None
    sample_rate: int = 44100
    bitrate: int = 256000


@dataclass(frozen=True, slots=True)
class MusicGenerationResult:
    audio: bytes
    media_type: str
    extension: Literal["mp3", "wav"]
    duration_seconds: float | None
    requested_duration_seconds: float
    provider_id: str
    model: str
    metadata: dict[str, Any] = field(default_factory=dict)


class MusicGenerationPort(Protocol):
    status: ProviderStatus
    capabilities: dict[str, Any]

    def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult: ...


@dataclass(frozen=True, slots=True)
class SubtitleAlignmentRequest:
    text: str
    duration_seconds: float
    word_timings: tuple[WordTiming, ...] = ()
    max_chars_per_line: int | None = None
    max_lines: int | None = None


@dataclass(frozen=True, slots=True)
class SubtitleSegment:
    sequence: int
    text: str
    start_seconds: float
    end_seconds: float
    timing_source: Literal["provider", "estimated"]


class SubtitleAlignmentPort(Protocol):
    status: ProviderStatus
    capabilities: dict[str, Any]

    def align(self, request: SubtitleAlignmentRequest) -> Sequence[SubtitleSegment]: ...


AiVideoTaskStatus = Literal[
    "queued",
    "running",
    "cancelled",
    "succeeded",
    "failed",
    "expired",
]


@dataclass(frozen=True, slots=True)
class AiVideoGenerationRequest:
    prompt: str
    # Kept for compatibility with the original single-image request. New
    # callers should use the plural reference fields below.
    image_url: str | None = None
    reference_image_urls: tuple[str, ...] = ()
    reference_video_urls: tuple[str, ...] = ()
    reference_video_durations_seconds: tuple[float | None, ...] = ()
    duration_seconds: float | None = None
    ratio: str | None = None
    resolution: str | None = None
    callback_url: str | None = None
    generate_audio: bool = False
    return_last_frame: bool = False
    idempotency_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # With exactly one image and no reference video, start the clip from that
    # image (image-to-video) instead of using it as a loose reference.
    first_frame: bool = False


@dataclass(frozen=True, slots=True)
class AiVideoTask:
    provider_task_id: str
    status: AiVideoTaskStatus
    provider_id: str
    model: str
    progress: int | None = None
    video_url: str | None = None
    last_frame_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AiVideoGenerationPort(Protocol):
    status: ProviderStatus
    capabilities: dict[str, Any]

    def submit(self, request: AiVideoGenerationRequest) -> AiVideoTask: ...

    def get(self, provider_task_id: str) -> AiVideoTask: ...

    def cancel(self, provider_task_id: str) -> AiVideoTask: ...


@dataclass(frozen=True, slots=True)
class VideoClip:
    path: Path
    duration_seconds: float
    transition: str = "none"
    transition_duration_seconds: float = 0.4
    is_image: bool | None = None
    fit_mode: Literal["contain", "cover"] = "contain"


@dataclass(frozen=True, slots=True)
class VideoCompositionRequest:
    clips: tuple[VideoClip, ...]
    output_path: Path
    width: int
    height: int
    fps: int = 25
    fit_mode: Literal["contain", "cover"] = "contain"
    source_audio_policy: Literal["mute", "replace_by_voice"] = "mute"
    voice_path: Path | None = None
    subtitle_path: Path | None = None
    subtitle_style: dict[str, Any] = field(default_factory=dict)
    background_music_path: Path | None = None
    background_music_volume: float = 0.18
    logo_path: Path | None = None
    logo_width: int = 160
    logo_position: Literal["top_left", "top_right", "bottom_left", "bottom_right"] = "top_right"
    intro_path: Path | None = None
    intro_duration_seconds: float | None = None
    outro_path: Path | None = None
    outro_duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class VideoCompositionResult:
    output_path: Path
    duration_seconds: float
    width: int
    height: int
    clip_count: int
    transitions_applied: tuple[str, ...]
    subtitle_burned: bool
    voice_applied: bool
    background_music_applied: bool
    logo_applied: bool


class VideoComposerPort(Protocol):
    status: ProviderStatus
    capabilities: dict[str, Any]

    def compose(self, request: VideoCompositionRequest) -> VideoCompositionResult: ...


@dataclass(frozen=True, slots=True)
class ContentGenerationInput:
    task_name: str
    topic: str
    product: str
    keywords: tuple[str, ...]
    audience: str
    content_type: str
    style: str
    length: str
    requirements: str
    instruction: str
    knowledge: tuple[KnowledgeChunk, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    source_content: dict[str, Any] | None = None


def selected_text_candidate(source_content: dict[str, Any] | None) -> dict[str, Any]:
    """Return the candidate the user actually selected, preferring its live list value."""
    if not isinstance(source_content, dict):
        return {}
    candidates = source_content.get("candidates")
    selected_index = source_content.get("selected_candidate_index")
    if (
        isinstance(candidates, list)
        and isinstance(selected_index, int)
        and not isinstance(selected_index, bool)
        and 0 <= selected_index < len(candidates)
        and isinstance(candidates[selected_index], dict)
    ):
        return candidates[selected_index]
    selected = source_content.get("selected_candidate")
    if isinstance(selected, dict):
        return selected
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict):
                return candidate
    return source_content


def transform_required_fields(
    source_content: dict[str, Any] | None,
    instruction: str = "",
) -> tuple[str, ...]:
    """Resolve which user-visible fields a transform must materially change."""
    source = selected_text_candidate(source_content)
    if "body" not in source:
        return ("title",)
    normalized_instruction = " ".join(instruction.casefold().split())
    title_change_markers = (
        "修改标题",
        "改标题",
        "标题改",
        "标题更",
        "标题要",
        "题目改",
        "题目更",
        "优化标题",
        "调整标题",
        "重写标题",
        "改写标题",
        "精简标题",
        "缩短标题",
        "换个标题",
        "换一个标题",
        "标题换",
        "重新起标题",
        "重起标题",
        "另拟标题",
        "另起标题",
        "重新拟标题",
        "change title",
        "change the title",
        "edit title",
        "rewrite title",
        "shorten title",
        "replace title",
        "different title",
        "new title",
        "retitle",
        "change headline",
        "rewrite heading",
    )
    preserve_title_markers = (
        "标题不变",
        "标题保持不变",
        "保留标题",
        "不要修改标题",
        "不要改标题",
        "不改标题",
        "keep the title",
    )
    change_title = any(
        marker in normalized_instruction for marker in title_change_markers
    ) and not any(marker in normalized_instruction for marker in preserve_title_markers)
    return ("title", "body") if change_title else ("body",)


def unchanged_transform_fields(
    source_content: dict[str, Any] | None,
    transformed: dict[str, Any] | None,
    *,
    instruction: str = "",
) -> tuple[str, ...]:
    """List user-visible fields that an AI transform returned unchanged."""
    source = selected_text_candidate(source_content)
    required_fields = transform_required_fields(source, instruction)
    if not isinstance(transformed, dict):
        return required_fields

    def normalized(value: Any) -> str:
        return " ".join(str(value or "").split())

    return tuple(
        field_name
        for field_name in required_fields
        if normalized(transformed.get(field_name)) == normalized(source.get(field_name))
    )


@dataclass(frozen=True, slots=True)
class TextGenerationResult:
    content: Any
    provider_id: str
    model: str
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    prompt_version: str = "content-grounded-v1"


class TextGenerationPort(Protocol):
    status: ProviderStatus
    capabilities: dict[str, Any]

    def topics(self, request: ContentGenerationInput, count: int) -> TextGenerationResult: ...

    def copies(self, request: ContentGenerationInput, count: int) -> TextGenerationResult: ...

    def transform_copy(
        self,
        request: ContentGenerationInput,
        operation: str,
    ) -> TextGenerationResult: ...

    def storyboard(self, request: ContentGenerationInput, count: int) -> TextGenerationResult: ...

    def timeline_edits(self, request: ContentGenerationInput) -> TextGenerationResult: ...


# Existing routes import TextProvider; keep the name while exposing the stable port.
TextProvider = TextGenerationPort


class MockTextProvider:
    status = ProviderStatus(
        provider_id="mock-text",
        name="deterministic-text",
        capability="text-generation",
        mode="mock",
        external=False,
        enabled=True,
        model="deterministic-text",
    )
    capabilities: ClassVar[dict[str, Any]] = {
        "operations": [
            "topics",
            "copies",
            "transform_copy",
            "storyboard",
            "timeline_edits",
        ],
        "structured_output": True,
        "mock": True,
    }

    @staticmethod
    def _source_ids(request: ContentGenerationInput) -> list[str]:
        return [item.id for item in request.knowledge]

    def topics(self, request: ContentGenerationInput, count: int) -> TextGenerationResult:
        base = request.topic or request.product or "品牌内容"
        directions = ["品牌故事", "产品场景", "文化表达", "节日传播", "用户体验", "知识科普"]
        values = [
            {
                "direction": directions[index % len(directions)],
                "title": f"{base}：{directions[index % len(directions)]}",
                "source_ids": self._source_ids(request),
            }
            for index in range(count)
        ]
        return TextGenerationResult(values, self.status.provider_id, self.status.model or "")

    def copies(self, request: ContentGenerationInput, count: int) -> TextGenerationResult:
        source_candidate = selected_text_candidate(request.source_content)
        selected_title = str(source_candidate.get("title") or "")
        subject = selected_title or request.topic or request.product or "雅酿品牌"
        source_ids = self._source_ids(request)
        values = [
            {
                "title": f"{subject}｜内容版本 {index + 1}",
                "body": (
                    f"从真实场景出发，讲述{subject}。内容坚持准确、克制和可核验，"
                    "最终表述需由业务人员确认。"
                ),
                "source_ids": source_ids,
            }
            for index in range(count)
        ]
        return TextGenerationResult(values, self.status.provider_id, self.status.model or "")

    def transform_copy(
        self,
        request: ContentGenerationInput,
        operation: str,
    ) -> TextGenerationResult:
        candidate = dict(selected_text_candidate(request.source_content))
        candidate.pop("summary", None)
        instruction = request.instruction.strip()
        operation_labels = {
            "rewrite": "改写",
            "expand": "扩写",
            "shorten": "精简",
            "summarize": "摘要",
            "style": "风格调整",
        }
        change_label = instruction or operation_labels.get(operation, "调整")
        source_title = str(candidate.get("title") or candidate.get("text") or "").strip()
        if "body" not in candidate:
            candidate["title"] = f"{source_title}（{change_label}）".strip()
            candidate["source_ids"] = self._source_ids(request)
            return TextGenerationResult(candidate, self.status.provider_id, self.status.model or "")
        if "title" in transform_required_fields(candidate, instruction):
            candidate["title"] = f"{source_title}（{change_label}）".strip()
        source_body = str(candidate.get("body", "")).strip()
        body = source_body
        if operation == "shorten":
            shortened = body[: max(1, min(len(body), 80))].rstrip("，。！？； ")
            body = shortened if shortened != source_body else f"{shortened}（精简版）"
        elif operation == "expand":
            body = f"{body} 进一步结合实际使用场景展开说明。".strip()
        elif operation == "summarize":
            summarized = body[:80].strip()
            body = summarized if summarized != source_body else f"{summarized}（摘要版）"
        else:
            body = f"{body}（已按要求调整：{change_label}）".strip()
        candidate["body"] = body
        candidate["source_ids"] = self._source_ids(request)
        return TextGenerationResult(candidate, self.status.provider_id, self.status.model or "")

    def storyboard(self, request: ContentGenerationInput, count: int) -> TextGenerationResult:
        subject = request.topic or request.product or request.task_name
        shots = [
            {
                "sequence": index + 1,
                "visual": f"围绕{subject}的品牌场景镜头 {index + 1}",
                "narration": f"{subject}，从真实场景展开。",
                "subtitle": f"镜头 {index + 1}：{subject}",
                "duration_seconds": 3,
                "transition": "fade" if index else "none",
                "source_ids": self._source_ids(request),
            }
            for index in range(max(2, count))
        ]
        return TextGenerationResult(shots, self.status.provider_id, self.status.model or "")

    def timeline_edits(self, request: ContentGenerationInput) -> TextGenerationResult:
        """Return deterministic, reviewable timeline operations for local development.

        The mock deliberately uses the same constrained operation vocabulary as the
        real provider. It never invents file paths or mutates the active revision.
        """

        source = request.source_content if isinstance(request.source_content, dict) else {}
        timeline = source.get("timeline") if isinstance(source.get("timeline"), dict) else {}
        tracks = timeline.get("tracks") if isinstance(timeline.get("tracks"), list) else []
        canvas = timeline.get("canvas") if isinstance(timeline.get("canvas"), dict) else {}
        selected = {item for item in source.get("target_item_ids", []) if isinstance(item, str)}
        items: list[tuple[str, dict[str, Any]]] = []
        track_ids: dict[str, str] = {}
        for track in tracks:
            if not isinstance(track, dict):
                continue
            kind = str(track.get("kind") or "")
            track_id = track.get("id")
            if isinstance(track_id, str):
                track_ids[kind] = track_id
            track_items = track.get("items")
            if not isinstance(track_items, list):
                continue
            for item in track_items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                if selected and item["id"] not in selected:
                    continue
                items.append((kind, item))

        prompt = request.instruction.strip()
        operations: list[dict[str, Any]] = []

        def operation_id(label: str) -> str:
            return f"ai-{label}-{uuid4().hex[:10]}"

        if any(value in prompt.lower() for value in ("9:16", "竖屏", "vertical")):
            operations.append(
                {
                    "op_id": operation_id("vertical"),
                    "type": "update_canvas",
                    "changes": {"width": 1080, "height": 1920, "aspect_ratio": "9:16"},
                }
            )
        elif any(value in prompt.lower() for value in ("16:9", "横屏", "landscape")):
            operations.append(
                {
                    "op_id": operation_id("landscape"),
                    "type": "update_canvas",
                    "changes": {"width": 1920, "height": 1080, "aspect_ratio": "16:9"},
                }
            )
        elif any(value in prompt for value in ("方形", "正方形", "1:1")):
            operations.append(
                {
                    "op_id": operation_id("square"),
                    "type": "update_canvas",
                    "changes": {"width": 1080, "height": 1080, "aspect_ratio": "1:1"},
                }
            )

        if any(value in prompt for value in ("静音", "关闭声音", "去掉声音")):
            target = next((item for _, item in items), None)
            if target is not None:
                operations.append(
                    {
                        "op_id": operation_id("mute"),
                        "type": "update",
                        "item_id": target["id"],
                        "changes": {"volume": 0},
                    }
                )

        if any(value in prompt for value in ("缩短", "快节奏", "加快节奏")):
            target = next((item for kind, item in items if kind == "video"), None)
            if target is not None:
                current_duration = float(target.get("duration_seconds") or 1)
                operations.append(
                    {
                        "op_id": operation_id("shorten"),
                        "type": "update",
                        "item_id": target["id"],
                        "changes": {"duration_seconds": max(0.1, round(current_duration * 0.8, 3))},
                    }
                )

        if any(value in prompt for value in ("转场", "叠化", "淡入")):
            target = next((item for kind, item in items if kind == "video"), None)
            if target is not None:
                metadata = dict(target.get("metadata") or {})
                metadata.update({"transition": "fade", "transition_duration_seconds": 0.4})
                operations.append(
                    {
                        "op_id": operation_id("transition"),
                        "type": "update",
                        "item_id": target["id"],
                        "changes": {"metadata": metadata},
                    }
                )

        if any(value in prompt for value in ("恢复音乐", "开启音乐")) and track_ids.get("music"):
            operations.append(
                {
                    "op_id": operation_id("music"),
                    "type": "update_track",
                    "track_id": track_ids["music"],
                    "changes": {"muted": False},
                }
            )

        if not operations:
            target = next((item for _, item in items), None)
            if target is not None:
                metadata = dict(target.get("metadata") or {})
                metadata["ai_revision_note"] = prompt[:500]
                operations.append(
                    {
                        "op_id": operation_id("revision"),
                        "type": "update",
                        "item_id": target["id"],
                        "changes": {"metadata": metadata},
                    }
                )
            else:
                operations.append(
                    {
                        "op_id": operation_id("preview"),
                        "type": "update_canvas",
                        "changes": {"fps": int(canvas.get("fps") or 30)},
                    }
                )
        return TextGenerationResult(
            operations,
            self.status.provider_id,
            self.status.model or "",
            prompt_version="timeline-edit-v1",
        )
