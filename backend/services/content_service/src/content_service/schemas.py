import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

TaskMode = Literal["full", "copy", "image", "video"]
ContentLength = Literal["短", "中", "长"]
KnowledgeResourceId = Annotated[str, Field(min_length=1, max_length=36)]
StageKey = Literal[
    "configuration",
    "topic",
    "copy",
    "compliance",
    "image",
    "storyboard",
    "voice_subtitles",
    "video_preview",
    "export",
]
TaskStageState = Literal[
    "not_started",
    "in_progress",
    "awaiting_confirmation",
    "confirmed",
    "stale",
    "skipped",
    "failed",
]

# API input limits.  These limits are intentionally enforced at the schema
# boundary so oversized values never reach the database, provider adapters, or
# background jobs.  The values are generous for normal content authoring while
# bounding accidental/malicious memory and storage amplification.
MAX_METADATA_BYTES = 64 * 1024
MAX_METADATA_DEPTH = 8
MAX_METADATA_ARRAY_ITEMS = 100
MAX_METADATA_ARRAYS = 100
MAX_KEYWORDS = 50
MAX_TAGS = 100
MAX_FACTS = 100


def _validate_json_metadata(value: object, *, field_name: str = "metadata") -> object:
    """Validate bounded JSON metadata supplied by an API client.

    Pydantic's ``Any`` intentionally accepts arbitrary JSON values.  Walking
    the value here gives all metadata-bearing request models the same limits on
    serialized size, nesting and array fan-out, and produces a normal
    validation error (HTTP 422) instead of a later serialization/database
    failure.
    """

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是可 JSON 序列化的数据") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(f"{field_name} 总大小不能超过 {MAX_METADATA_BYTES} 字节")

    array_count = 0

    def walk(item: object, depth: int) -> None:
        nonlocal array_count
        if depth > MAX_METADATA_DEPTH:
            raise ValueError(f"{field_name} JSON 嵌套深度不能超过 {MAX_METADATA_DEPTH}")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{field_name} 对象键必须是字符串")
                walk(child, depth + 1)
        elif isinstance(item, list):
            array_count += 1
            if array_count > MAX_METADATA_ARRAYS:
                raise ValueError(f"{field_name} 最多允许 {MAX_METADATA_ARRAYS} 个数组")
            if len(item) > MAX_METADATA_ARRAY_ITEMS:
                raise ValueError(f"{field_name} 单个数组不能超过 {MAX_METADATA_ARRAY_ITEMS} 项")
            for child in item:
                walk(child, depth + 1)

    walk(value, 0)
    return value


def _non_blank(value: str, *, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} 不能为空或全为空格")
    return value


class ProviderInfo(BaseModel):
    provider_id: str
    name: str
    capability: str
    mode: str
    external: bool
    enabled: bool
    model: str | None = None


class LibraryItemCreate(BaseModel):
    resource_kind: Literal["document", "product", "logo", "image", "video", "audio", "spec"] = (
        "document"
    )
    category: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    text_content: str = Field(default="", max_length=1_000_000)
    data_level: Literal["public", "internal", "restricted"] = "internal"
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    parent_id: str | None = Field(default=None, max_length=36)
    source_url: str | None = Field(default=None, max_length=2048)
    crawled_at: datetime | None = None
    freshness: str | None = Field(default=None, max_length=64)
    facts: list[dict[str, Any] | str] = Field(default_factory=list, max_length=MAX_FACTS)
    notes: str = Field(default="", max_length=10_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _non_blank(value, field_name="资料名称")

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str) -> str:
        return _non_blank(value, field_name="资料分类")

    @field_validator("text_content", "notes")
    @classmethod
    def validate_text_fields(cls, value: str) -> str:
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_json_metadata(value)  # type: ignore[return-value]

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("标签不能为空或全为空格")
        if any(len(item) > 128 for item in value):
            raise ValueError("单个标签不能超过 128 个字符")
        return value

    source_type: str | None = Field(default=None, max_length=64)
    tenant_id: str | None = Field(default=None, max_length=128)
    department_id: str | None = Field(default=None, max_length=128)
    allowed_roles: list[str] = Field(default_factory=list, max_length=32)


class LibraryItemVersionCreate(LibraryItemCreate):
    expected_version: int = Field(ge=1)


class LibraryItemResponse(BaseModel):
    id: str
    owner_id: str | None = None
    category: str
    name: str
    text_content: str
    storage_key: str | None = None
    data_level: str
    version: int
    active: bool
    created_at: datetime
    updated_at: datetime
    resource_entry_id: str | None = None
    resource_version_id: str | None = None
    resource_kind: str
    tags: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    source_url: str | None = None
    crawled_at: datetime | None = None
    freshness: str | None = None
    facts: list[Any] = Field(default_factory=list)
    notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_type: str | None = None
    tenant_id: str | None = None
    department_id: str | None = None
    allowed_roles: list[str] = Field(default_factory=list)
    has_asset: bool = False
    download_url: str | None = None
    knowledge_sync: dict[str, Any] | None = None


class LibraryRelationSummary(BaseModel):
    item_id: str | None = None
    resource_entry_id: str
    name: str
    resource_kind: str
    category: str
    has_asset: bool = False
    download_url: str | None = None


class LibraryAssetSummary(BaseModel):
    id: str
    media_type: str
    size_bytes: int | None = None
    download_url: str


class LibraryExternalReferenceSummary(BaseModel):
    system: str
    external_id: str
    source_url: str | None = None


class LibraryItemDetailResponse(LibraryItemResponse):
    parent: LibraryRelationSummary | None = None
    children: list[LibraryRelationSummary] = Field(default_factory=list)
    assets: list[LibraryAssetSummary] = Field(default_factory=list)
    external_references: list[LibraryExternalReferenceSummary] = Field(default_factory=list)


class LibraryCategoryResponse(BaseModel):
    id: str
    name: str
    resource_count: int
    resource_kinds: list[str] = Field(default_factory=list)


class TaskCreate(BaseModel):
    mode: TaskMode = "full"
    name: str = Field(min_length=1, max_length=255)
    topic: str = Field(default="", max_length=2_000)
    product: str = Field(default="", max_length=2_000)
    product_resource_id: str | None = Field(default=None, max_length=36)
    knowledge_resource_ids: list[KnowledgeResourceId] = Field(
        default_factory=list,
        max_length=20,
    )
    keywords: list[str] = Field(default_factory=list, max_length=MAX_KEYWORDS)
    audience: str = Field(default="", max_length=1_000)
    content_type: str = Field(default="短视频", max_length=255)
    style: str = Field(default="品牌宣传型", max_length=255)
    length: ContentLength = "中"
    requirements: str = Field(default="", max_length=10_000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _non_blank(value, field_name="任务名称")

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("关键词不能为空或全为空格")
        if any(len(item) > 128 for item in value):
            raise ValueError("单个关键词不能超过 128 个字符")
        return value

    @field_validator("length", mode="before")
    @classmethod
    def normalize_legacy_length(cls, value: Any) -> Any:
        return {"短篇": "短", "中篇": "中", "中等": "中", "长篇": "长"}.get(value, value)


class TaskPatch(BaseModel):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    topic: str | None = Field(default=None, max_length=2_000)
    product: str | None = Field(default=None, max_length=2_000)
    product_resource_id: str | None = Field(default=None, max_length=36)
    knowledge_resource_ids: list[KnowledgeResourceId] | None = Field(
        default=None,
        max_length=20,
    )
    keywords: list[str] | None = Field(default=None, max_length=MAX_KEYWORDS)
    audience: str | None = Field(default=None, max_length=1_000)
    content_type: str | None = Field(default=None, max_length=255)
    style: str | None = Field(default=None, max_length=255)
    length: ContentLength | None = None
    requirements: str | None = Field(default=None, max_length=10_000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return None if value is None else _non_blank(value, field_name="任务名称")

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(not item.strip() for item in value):
            raise ValueError("关键词不能为空或全为空格")
        if any(len(item) > 128 for item in value):
            raise ValueError("单个关键词不能超过 128 个字符")
        return value

    @field_validator("length", mode="before")
    @classmethod
    def normalize_legacy_length(cls, value: Any) -> Any:
        return {"短篇": "短", "中篇": "中", "中等": "中", "长篇": "长"}.get(value, value)


class TaskResponse(BaseModel):
    id: str
    owner_id: str
    can_edit: bool = True
    mode: TaskMode
    name: str
    topic: str
    product: str
    product_resource_id: str | None
    knowledge_resource_ids: list[str]
    keywords: list[str]
    audience: str
    content_type: str
    style: str
    length: ContentLength
    requirements: str
    current_stage: str
    status: str
    workflow_status: str
    archived: bool
    archived_at: datetime | None
    is_edit_draft: bool = False
    source_task_id: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime


class TaskDeleteResponse(BaseModel):
    id: str
    archived: Literal[True] = True


class TaskPermanentDeleteResponse(BaseModel):
    id: str
    deleted: Literal[True] = True


class SubtitleStyleSpec(BaseModel):
    """Safe subset of libass styling exposed to content users.

    Raw FFmpeg/libass expressions are deliberately not accepted here.  Keeping the
    values structured prevents a subtitle style field from becoming a filter graph
    injection surface while still covering the lightweight editing requirements.
    """

    font_size: int = Field(default=36, ge=12, le=120)
    primary_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    outline_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    outline_width: float = Field(default=2, ge=0, le=10)
    shadow: float = Field(default=0, ge=0, le=10)
    alignment: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9] = 2
    margin_v: int = Field(default=36, ge=0, le=500)
    bold: bool = False


class VideoCompositionSpec(BaseModel):
    """Validated light-edit parameters for deterministic video composition."""

    width: int = Field(default=1280, ge=64, le=7680)
    height: int = Field(default=720, ge=64, le=4320)
    fps: int = Field(default=25, ge=1, le=60)
    fit_mode: Literal["contain", "cover"] = "contain"
    source_audio_policy: Literal["mute", "replace_by_voice"] = "mute"
    transition: Literal["none", "cut", "fade", "fadeblack", "fadewhite"] = "none"
    transition_duration_seconds: float = Field(default=0.4, ge=0, le=5)
    background_music_volume: float = Field(default=0.18, ge=0, le=2)
    logo_position: Literal["top_left", "top_right", "bottom_left", "bottom_right"] = "top_right"
    logo_width: int = Field(default=160, ge=16, le=2048)
    subtitle_style: SubtitleStyleSpec = Field(default_factory=SubtitleStyleSpec)
    intro_duration_seconds: float | None = Field(default=None, gt=0, le=600)
    outro_duration_seconds: float | None = Field(default=None, gt=0, le=600)


VideoTrackKind = Literal["video", "overlay", "subtitle", "voice", "music", "sfx"]
ProjectAssetRole = Literal[
    "video",
    "overlay",
    "subtitle",
    "voice",
    "music",
    "sfx",
    "logo",
    "intro",
    "outro",
]


class VideoCanvasSpec(BaseModel):
    width: int = Field(default=1280, ge=64, le=7680)
    height: int = Field(default=720, ge=64, le=4320)
    fps: int = Field(default=25, ge=1, le=60)
    aspect_ratio: str = Field(default="16:9", min_length=1, max_length=32)


class TimelineItemCreate(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=64)
    project_asset_id: str | None = Field(default=None, max_length=36)
    asset_ref_id: str | None = Field(default=None, max_length=36)
    artifact_id: str | None = Field(default=None, max_length=36)
    library_item_id: str | None = Field(default=None, max_length=36)
    resource_entry_id: str | None = Field(default=None, max_length=36)
    label: str = Field(default="", max_length=255)
    start_seconds: float = Field(default=0, ge=0, le=600)
    duration_seconds: float = Field(gt=0, le=600)
    trim_in_seconds: float = Field(default=0, ge=0, le=600)
    trim_out_seconds: float = Field(default=0, ge=0, le=600)
    volume: float = Field(default=1, ge=0, le=4)
    speed: float = Field(default=1, ge=0.1, le=8)
    enabled: bool = True
    content: str | None = Field(default=None, max_length=100000)
    transform: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("transform", "metadata")
    @classmethod
    def validate_json_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_json_metadata(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def one_source_reference(self):
        source_fields = (
            self.project_asset_id,
            self.asset_ref_id,
            self.artifact_id,
            self.library_item_id,
            self.resource_entry_id,
        )
        if sum(value is not None for value in source_fields) > 1:
            raise ValueError("一个时间线片段只能引用一个工程素材来源")
        if not any(source_fields) and not (self.content or "").strip():
            raise ValueError("时间线片段必须引用素材或提供文本内容")
        return self


class TimelineItemPatch(BaseModel):
    label: str | None = Field(default=None, max_length=255)
    start_seconds: float | None = Field(default=None, ge=0, le=600)
    duration_seconds: float | None = Field(default=None, gt=0, le=600)
    trim_in_seconds: float | None = Field(default=None, ge=0, le=600)
    trim_out_seconds: float | None = Field(default=None, ge=0, le=600)
    volume: float | None = Field(default=None, ge=0, le=4)
    speed: float | None = Field(default=None, ge=0.1, le=8)
    enabled: bool | None = None
    content: str | None = Field(default=None, max_length=100000)
    transform: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("transform", "metadata")
    @classmethod
    def validate_json_fields(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else _validate_json_metadata(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def at_least_one_change(self):
        if not self.model_fields_set:
            raise ValueError("update 操作至少需要一个变更字段")
        return self


class VideoCanvasPatch(BaseModel):
    width: int | None = Field(default=None, ge=64, le=7680)
    height: int | None = Field(default=None, ge=64, le=4320)
    fps: int | None = Field(default=None, ge=1, le=60)
    aspect_ratio: str | None = Field(
        default=None,
        min_length=3,
        max_length=32,
        pattern=r"^[1-9]\d{0,4}:[1-9]\d{0,4}$",
    )

    @model_validator(mode="after")
    def at_least_one_change(self):
        if not self.model_fields_set:
            raise ValueError("update_canvas 至少需要一个变更字段")
        return self


class TimelineTrackPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    muted: bool | None = None
    locked: bool | None = None

    @model_validator(mode="after")
    def at_least_one_change(self):
        if not self.model_fields_set:
            raise ValueError("update_track 至少需要一个变更字段")
        return self


class AddProjectOperation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    type: Literal["add"]
    track_id: str | None = Field(default=None, max_length=64)
    track_kind: VideoTrackKind | None = None
    index: int | None = Field(default=None, ge=0)
    item: TimelineItemCreate


class RemoveProjectOperation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    type: Literal["remove"]
    item_id: str = Field(min_length=1, max_length=64)


class MoveProjectOperation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    type: Literal["move"]
    item_id: str = Field(min_length=1, max_length=64)
    track_id: str | None = Field(default=None, max_length=64)
    index: int | None = Field(default=None, ge=0)
    start_seconds: float | None = Field(default=None, ge=0, le=600)


class TrimProjectOperation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    type: Literal["trim"]
    item_id: str = Field(min_length=1, max_length=64)
    trim_in_seconds: float = Field(ge=0, le=600)
    trim_out_seconds: float = Field(ge=0, le=600)
    duration_seconds: float | None = Field(default=None, gt=0, le=600)


class SplitProjectOperation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    type: Literal["split"]
    item_id: str = Field(min_length=1, max_length=64)
    at_seconds: float = Field(gt=0, le=600)
    new_item_id: str | None = Field(default=None, min_length=1, max_length=64)


class UpdateProjectOperation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    type: Literal["update"]
    item_id: str = Field(min_length=1, max_length=64)
    changes: TimelineItemPatch


class UpdateCanvasOperation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    type: Literal["update_canvas"]
    changes: VideoCanvasPatch


class UpdateTrackOperation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    type: Literal["update_track"]
    track_id: str = Field(min_length=1, max_length=64)
    changes: TimelineTrackPatch


class RestoreProjectOperation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=64)
    type: Literal["restore"]
    revision_id: str = Field(min_length=1, max_length=36)


VideoProjectOperation = Annotated[
    AddProjectOperation
    | RemoveProjectOperation
    | MoveProjectOperation
    | TrimProjectOperation
    | SplitProjectOperation
    | UpdateProjectOperation
    | UpdateCanvasOperation
    | UpdateTrackOperation
    | RestoreProjectOperation,
    Field(discriminator="type"),
]


class VideoProjectEditRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    operations: list[VideoProjectOperation] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_operation_ids_and_restore_isolated(self):
        operation_ids = [operation.op_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("同一请求中的 op_id 不能重复")
        if (
            any(operation.type == "restore" for operation in self.operations)
            and len(self.operations) != 1
        ):
            raise ValueError("restore 必须作为单独操作提交")
        return self


class AiEditProposalCreate(BaseModel):
    expected_revision: int = Field(ge=1)
    prompt: str = Field(min_length=1, max_length=10000)
    operations: list[VideoProjectOperation] = Field(default_factory=list, max_length=100)
    target_item_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_operation_ids(self):
        operation_ids = [operation.op_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("同一提案中的 op_id 不能重复")
        if len(self.target_item_ids) != len(set(self.target_item_ids)):
            raise ValueError("target_item_ids 不能重复")
        return self


class AiEditProposalAdoptRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    selected_operation_ids: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_selected_ids(self):
        if len(self.selected_operation_ids) != len(set(self.selected_operation_ids)):
            raise ValueError("selected_operation_ids 不能重复")
        return self


class AiEditProposalRejectRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class TimelineItemResponse(BaseModel):
    id: str
    project_asset_id: str | None = None
    label: str
    start_seconds: float
    duration_seconds: float
    trim_in_seconds: float
    trim_out_seconds: float
    volume: float
    speed: float
    enabled: bool
    content: str | None = None
    transform: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TimelineTrackResponse(BaseModel):
    id: str
    kind: VideoTrackKind
    name: str
    muted: bool = False
    locked: bool = False
    items: list[TimelineItemResponse] = Field(default_factory=list)


class VideoTimelineResponse(BaseModel):
    canvas: VideoCanvasSpec
    tracks: list[TimelineTrackResponse]


class MediaDerivativeResponse(BaseModel):
    id: str
    kind: str
    variant: str
    status: str
    download_url: str | None = None
    media_type: str | None = None
    checksum: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ProjectAssetResponse(BaseModel):
    id: str
    source_type: str
    source_id: str
    asset_ref_id: str | None = None
    artifact_id: str | None = None
    library_item_id: str | None = None
    resource_entry_id: str | None = None
    role: str
    label: str
    original_url: str
    stream_url: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    active: bool
    derivatives: list[MediaDerivativeResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class MusicGenerationCreateRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    prompt: str = Field(min_length=1, max_length=2000)
    duration_seconds: float = Field(default=15, ge=1, le=15)
    audio_format: Literal["mp3", "wav"] = "mp3"
    response_format: Literal["hex", "url"] | None = None
    label: str = Field(default="AI 背景音乐", min_length=1, max_length=255)


VideoProjectDeliverable = Literal[
    "mixed_video",
    "silent_video",
    "voice",
    "music",
    "sfx",
    "source_audio",
    "subtitles",
    "clips",
    "project_zip",
]


class VideoProjectRenderRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    profile: Literal["preview-480p", "final-480p", "final-720p", "final-1080p"] = "final-720p"
    fps: int = Field(default=30, ge=1, le=60)
    subtitle_mode: Literal["burn", "soft", "none"] = "burn"
    include_audio: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class VideoProjectExportRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    deliverables: list[VideoProjectDeliverable] = Field(
        default_factory=lambda: ["mixed_video"],
        min_length=1,
        max_length=9,
    )
    video_profile: Literal["480p", "720p", "1080p"] = "720p"
    fps: int = Field(default=30, ge=1, le=60)
    subtitle_mode: Literal["burn", "soft", "none"] = "burn"
    audio_format: Literal["wav", "mp3"] = "wav"
    subtitle_format: Literal["srt", "vtt"] = "srt"
    clip_item_ids: list[str] = Field(default_factory=list, max_length=500)
    project_track_kinds: list[Literal["video", "overlay", "subtitle", "voice", "music", "sfx"]] = (
        Field(default_factory=list, max_length=6)
    )
    project_asset_ids: list[str] | None = Field(default=None, max_length=500)
    include_original_assets: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def unique_deliverables(self):
        if len(self.deliverables) != len(set(self.deliverables)):
            raise ValueError("deliverables 不能重复")
        if len(self.clip_item_ids) != len(set(self.clip_item_ids)):
            raise ValueError("clip_item_ids 不能重复")
        if len(self.project_track_kinds) != len(set(self.project_track_kinds)):
            raise ValueError("project_track_kinds 不能重复")
        if self.project_asset_ids is not None and len(self.project_asset_ids) != len(
            set(self.project_asset_ids)
        ):
            raise ValueError("project_asset_ids 不能重复")
        return self


class MediaJobResponse(BaseModel):
    id: str
    task_id: str
    generation_run_id: str | None = None
    kind: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: int = Field(ge=0, le=100)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attempt: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    heartbeat_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProjectAssetPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=255)
    role: ProjectAssetRole | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def at_least_one_change(self):
        if not self.model_fields_set:
            raise ValueError("素材更新至少需要一个变更字段")
        return self


class VideoProjectRevisionResponse(BaseModel):
    id: str
    project_id: str
    revision: int | None
    parent_revision_id: str | None = None
    source: str
    timeline: VideoTimelineResponse
    operations: list[dict[str, Any]] = Field(default_factory=list)
    created_by: str
    created_at: datetime


class VideoProjectResponse(BaseModel):
    id: str
    task_id: str
    current_revision: int
    current_revision_id: str
    revision: VideoProjectRevisionResponse
    assets: list[ProjectAssetResponse] = Field(default_factory=list)
    created_by: str
    created_at: datetime
    updated_at: datetime


class AiEditProposalResponse(BaseModel):
    id: str
    project_id: str
    base_revision_id: str
    candidate_revision_id: str
    prompt: str
    operations: list[dict[str, Any]]
    status: Literal["pending", "adopted", "rejected"]
    selected_operation_ids: list[str]
    created_by: str
    resolved_by: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class AiEditProposalCompareResponse(BaseModel):
    proposal: AiEditProposalResponse
    base_revision: VideoProjectRevisionResponse
    candidate_revision: VideoProjectRevisionResponse
    differences: list[dict[str, Any]]


class AiEditProposalDecisionResponse(BaseModel):
    proposal: AiEditProposalResponse
    project: VideoProjectResponse


class GenerateRequest(BaseModel):
    count: int = Field(default=3, ge=1, le=6)
    instruction: str = Field(default="", max_length=10_000)
    source_artifact_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    composition_spec: VideoCompositionSpec | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_json_metadata(value, field_name="parameters")  # type: ignore[return-value]


class ArtifactPatch(BaseModel):
    content: dict[str, Any]
    expected_version: int = Field(ge=1)
    target_task_id: str | None = None


class ArtifactResponse(BaseModel):
    id: str
    task_id: str
    kind: str
    stage_key: str | None = None
    generation_run_id: str | None = None
    candidate_index: int | None = None
    version: int
    status: str
    content: dict[str, Any]
    storage_key: str | None = None
    parent_id: str | None = None
    created_by: str
    created_at: datetime
    linked: bool = False
    link_id: str | None = None
    source_task_id: str | None = None


class ArtifactLinkCreate(BaseModel):
    source_task_id: str
    artifact_ids: list[str] = Field(min_length=1)
    expected_version: int = Field(ge=1)


class ArtifactLinkItem(BaseModel):
    id: str
    target_task_id: str
    source_task_id: str
    artifact_id: str
    stage_key: StageKey
    created_by: str
    created_at: datetime


class ArtifactLinkResponse(BaseModel):
    links: list[ArtifactLinkItem]
    workflow: "WorkflowResponse"


class ArtifactDiscardResponse(BaseModel):
    id: str
    status: Literal["discarded"]
    discarded: Literal[True] = True
    already_discarded: bool = False


class TransformCopyRequest(BaseModel):
    operation: Literal["rewrite", "expand", "shorten", "summarize", "style"] = "rewrite"
    instruction: str = ""
    preview: bool = False
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    target_task_id: str | None = None
    draft_content: dict[str, Any] | None = None
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_preview_draft(self):
        if self.draft_content is not None:
            if not self.preview:
                raise ValueError("未保存的草稿仅可用于 AI 预览，请点击保存后再新增正式版本。")
            if self.expected_version is None:
                raise ValueError("预览草稿需要原内容版本号，请刷新内容后重试。")
        return self


class StageRequirementsUpdate(BaseModel):
    requirements: str
    expected_version: int = Field(ge=1)


class StageSelectRequest(BaseModel):
    # An empty list explicitly clears the current stage selection. This keeps
    # the persisted artifact status aligned with an unchecked UI selection.
    artifact_ids: list[str]
    expected_version: int = Field(ge=1)


class StageConfirmRequest(BaseModel):
    expected_version: int = Field(ge=1)
    artifact_ids: list[str] | None = Field(default=None, min_length=1)


class StageSkipRequest(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str = "用户选择跳过"


class StoryboardImportRequest(BaseModel):
    text: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


class CopyImportRequest(BaseModel):
    text: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


class WorkflowStageResponse(BaseModel):
    key: StageKey
    label: str
    order: int
    applicable: bool
    skippable: bool
    state: TaskStageState
    requirements: str
    selected_artifact_ids: dict[str, list[str]]
    confirmed_artifact_ids: dict[str, list[str]]
    skipped_reason: str | None = None
    version: int
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    updated_at: datetime


class WorkflowResponse(BaseModel):
    task_id: str
    task_version: int
    current_stage: str
    stages: list[WorkflowStageResponse]
    export_artifact_ids: list[str] = Field(default_factory=list)


class GenerationRunResponse(BaseModel):
    id: str
    task_id: str
    stage_key: StageKey
    status: Literal["running", "succeeded", "failed", "cancelled"]
    requested_count: int
    instruction: str
    parameters: dict[str, Any]
    artifact_ids: list[str]
    artifacts: list[ArtifactResponse]
    job_id: str | None = None
    job_ids: list[str] = Field(default_factory=list)
    provider_id: str | None = None
    model: str | None = None
    idempotency_key: str | None = None
    error: str | None = None
    created_by: str
    created_at: datetime
    completed_at: datetime | None = None


class ProviderCapability(BaseModel):
    capability: str
    provider_id: str
    enabled: bool
    mode: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class CapabilitiesResponse(BaseModel):
    capabilities: list[ProviderCapability]


class ServiceStatusItem(BaseModel):
    capability: str
    provider_id: str
    status: Literal["available", "degraded", "unavailable", "disabled"]
    checked_at: datetime
    message: str | None = None


class ServiceStatusResponse(BaseModel):
    overall: Literal["available", "degraded", "unavailable"]
    services: list[ServiceStatusItem]


class RuleCreate(BaseModel):
    phrase: str
    rule_type: Literal["keyword", "sensitive", "forbidden"] = "sensitive"
    level: Literal["warning", "forbidden"] | None = None
    replacement: str | None = None


class EgressGrantCreate(BaseModel):
    provider: str
    allowed_data_levels: list[Literal["public", "internal", "restricted"]]
    allowed_source_data_levels: list[Literal["public", "internal", "restricted"]] = Field(
        default_factory=list
    )
    allow_task_context: bool = False
    allow_compliance_terms: bool = False


class EgressStatusResponse(BaseModel):
    provider_id: str
    external: bool
    required: bool
    granted: bool
    allowed_data_levels: list[Literal["public", "internal", "restricted"]]
    allowed_source_data_levels: list[Literal["public", "internal", "restricted"]]
    allow_task_context: bool
    allow_compliance_terms: bool
    can_approve: bool


VideoUploadStatus = Literal["uploading", "completed", "cancelled", "failed"]


class VideoUploadSessionCreate(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    role: ProjectAssetRole
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=128)
    total_size: int = Field(gt=0, le=4 * 1024 * 1024 * 1024)
    chunk_size: int = Field(gt=0, le=64 * 1024 * 1024)
    checksum: str = Field(pattern=r"^[0-9A-Fa-f]{64}$")


class VideoUploadChunkResponse(BaseModel):
    upload_id: str
    index: int
    size_bytes: int
    checksum_sha256: str
    duplicate: bool


class VideoUploadSessionResponse(BaseModel):
    id: str
    project_id: str
    owner_id: str
    role: ProjectAssetRole
    filename: str
    media_type: str
    media_kind: Literal["image", "video", "audio", "subtitle"]
    total_size: int
    chunk_size: int
    total_chunks: int
    checksum_sha256: str
    status: VideoUploadStatus
    project_asset_id: str | None = None
    received_chunks: list[int] = Field(default_factory=list)
    missing_chunks: list[int] = Field(default_factory=list)
    received_size: int = 0
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class VideoUploadCompleteResponse(BaseModel):
    upload_id: str
    status: Literal["completed"]
    media_kind: Literal["image", "video", "audio", "subtitle"]
    media_type: str
    total_size: int
    checksum_sha256: str


class VideoUploadCancelResponse(BaseModel):
    upload_id: str
    status: Literal["cancelled"]


class VideoUploadFinalizedResponse(BaseModel):
    upload: VideoUploadSessionResponse
    asset: ProjectAssetResponse


class VideoProjectVoiceGenerationRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=100_000)
    label: str = Field(default="AI 配音", min_length=1, max_length=255)
    voice: str | None = Field(default=None, max_length=255)
    speed: float = Field(default=1, ge=0.1, le=4)
    volume: float = Field(default=1, ge=0, le=4)
    audio_format: Literal["wav", "mp3", "ogg_opus"] = "mp3"
    sample_rate: int = Field(default=24_000, ge=8_000, le=96_000)
    with_timestamps: bool = True
    instruction: str | None = Field(default=None, max_length=2_000)
    parent_artifact_id: str | None = Field(default=None, max_length=36)


class VideoProjectSubtitleGenerationRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    text: str | None = Field(default=None, max_length=100_000)
    label: str = Field(default="AI 字幕", min_length=1, max_length=255)
    source_project_asset_id: str | None = Field(default=None, max_length=36)
    duration_seconds: float | None = Field(default=None, gt=0, le=600)
    max_chars_per_line: int | None = Field(default=None, ge=1, le=200)
    max_lines: int | None = Field(default=None, ge=1, le=10)
    parent_artifact_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def require_text_or_audio_source(self):
        if not (self.text or "").strip() and self.source_project_asset_id is None:
            raise ValueError("生成工程字幕必须提供 text 或 source_project_asset_id")
        return self


class VideoProjectSubtitleSegmentResponse(BaseModel):
    sequence: int = Field(ge=1)
    text: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    timing_source: Literal["provider", "estimated"]


class VideoProjectSubtitleGenerationResponse(BaseModel):
    asset: ProjectAssetResponse
    artifact_id: str
    source_project_asset_id: str | None = None
    text: str
    duration_seconds: float = Field(gt=0, le=600)
    segments: list[VideoProjectSubtitleSegmentResponse]
    suggested_edit: VideoProjectEditRequest


class VideoProjectImageGenerationRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    prompt: str = Field(min_length=1, max_length=10_000)
    role: Literal["video", "overlay", "logo", "intro", "outro"] = "video"
    count: int = Field(default=1, ge=1, le=8)
    label: str = Field(default="AI 图片候选", min_length=1, max_length=255)
    size: str | None = Field(
        default=None,
        max_length=32,
        pattern=r"^(?:\d{2,5}[xX×]\d{2,5}|[1-9]\d?K)$",
    )
    ratio: str | None = Field(
        default=None,
        max_length=32,
        pattern=r"^(?:adaptive|[1-9]\d{0,4}:[1-9]\d{0,4})$",
    )
    watermark: bool = False
    seed: int | None = Field(default=None, ge=0)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=9)
    parent_artifact_id: str | None = Field(default=None, max_length=36)


class VideoProjectAiVideoGenerationRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    prompt: str = Field(min_length=1, max_length=10_000)
    label: str = Field(default="AI 生成视频", min_length=1, max_length=255)
    source_copy_artifact_id: str | None = Field(default=None, max_length=36)
    source_project_asset_id: str | None = Field(default=None, max_length=36)
    reference_image_asset_ids: list[str] = Field(default_factory=list, max_length=9)
    reference_video_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    duration_seconds: int | None = Field(default=None, ge=5, le=600)
    ratio: str | None = Field(
        default=None,
        max_length=32,
        pattern=r"^(?:adaptive|[1-9]\d{0,4}:[1-9]\d{0,4})$",
    )
    resolution: str | None = Field(default=None, min_length=1, max_length=64)
    generate_audio: bool = False
    return_last_frame: bool = False
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    parent_artifact_id: str | None = Field(default=None, max_length=36)

    @field_validator("duration_seconds", mode="before")
    @classmethod
    def validate_minimum_generation_duration(cls, value):
        if isinstance(value, (int, float)) and value < 5:
            raise ValueError("新生成的 AI 视频时长至少为 5 秒，请重新选择时长。")
        return value

    @model_validator(mode="after")
    def validate_reference_media(self):
        image_ids = [
            *((self.source_project_asset_id,) if self.source_project_asset_id else ()),
            *self.reference_image_asset_ids,
        ]
        if len(image_ids) > 9:
            raise ValueError("AI 视频单次最多允许 9 张参考图片")
        if len(image_ids) != len(set(image_ids)):
            raise ValueError("AI 视频参考图片不能重复")
        if len(self.reference_video_asset_ids) != len(set(self.reference_video_asset_ids)):
            raise ValueError("AI 视频参考视频不能重复")
        if set(image_ids).intersection(self.reference_video_asset_ids):
            raise ValueError("同一工程素材不能同时作为参考图片和参考视频")
        return self
