from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Self

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Artifact,
    AssetRef,
    LibraryItem,
    ProjectAsset,
    ResourceEntry,
    VideoProject,
    VideoProjectRevision,
)
from .providers import AssetStorePort
from .timeline_media import (
    TimelineAudioTrack,
    TimelineClip,
    TimelineDucking,
    TimelineLogo,
    TimelineRenderRequest,
    TimelineSubtitle,
    _atempo_filter_chain,
    _ffprobe_executable,
    _probe,
)

type ProjectTrackKind = Literal[
    "video",
    "overlay",
    "subtitle",
    "voice",
    "music",
    "sfx",
]
type StemKind = Literal["voice", "music", "sfx", "source_audio"]

_TRACK_KINDS = {"video", "overlay", "subtitle", "voice", "music", "sfx"}
_AUDIO_TRACK_KINDS = {"voice", "music", "sfx"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}
_SUBTITLE_EXTENSIONS = {".srt", ".vtt"}
_ITEM_METADATA_KEYS = {
    "transition",
    "transition_duration_seconds",
    "fit_mode",
    "loop",
    "fade_in_seconds",
    "fade_out_seconds",
    "position",
    "opacity",
    "width",
    "legacy_role",
    "source_audio",
    "subtitle_style",
}
_TRANSFORM_KEYS = {"x", "y", "scale", "rotation", "opacity"}
_SUBTITLE_STYLE_KEYS = {
    "font_size",
    "primary_color",
    "outline_color",
    "outline_width",
    "shadow",
    "alignment",
    "margin_v",
    "bold",
}
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


class VideoProjectRenderingError(ValueError):
    """A current project cannot be compiled without guessing unsafe data."""


@dataclass(frozen=True, slots=True)
class ResolvedProjectAsset:
    project_asset_id: str
    source_type: str
    source_id: str
    role: str
    label: str
    storage_key: str = field(repr=False)
    path: Path = field(repr=False)
    media_type: str
    media_kind: Literal["image", "video", "audio", "subtitle", "unknown"]
    size_bytes: int
    declared_checksum: str | None = None
    duration_seconds: float | None = None
    has_audio: bool | None = None


@dataclass(frozen=True, slots=True)
class PreparedProjectItem:
    item_id: str
    track_kind: ProjectTrackKind
    project_asset_id: str
    asset: ResolvedProjectAsset


@dataclass(slots=True)
class PreparedVideoProjectRender:
    project_id: str
    revision_id: str
    revision: int
    request: TimelineRenderRequest
    item_sources: tuple[PreparedProjectItem, ...]
    _temporary_directory: tempfile.TemporaryDirectory[str] | None = field(
        default=None,
        repr=False,
    )

    def cleanup(self) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.cleanup()


@dataclass(frozen=True, slots=True)
class ProjectRenderOptions:
    default_source_audio: Literal["keep", "mute"] = "keep"
    automatic_music_ducking: bool = True
    ducking_target_kinds: tuple[Literal["source", "music", "sfx"], ...] = ("music",)
    background_color: str = "#000000"
    video_bitrate: str = "4M"
    audio_bitrate: str = "192k"
    overwrite: bool = False


@dataclass(frozen=True, slots=True)
class ProjectBundleExportOptions:
    track_kinds: frozenset[ProjectTrackKind] = frozenset(_TRACK_KINDS)
    project_asset_ids: frozenset[str] | None = None
    include_original_assets: bool = True
    include_inline_subtitles: bool = True
    max_assets: int = 500
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    overwrite: bool = False


@dataclass(frozen=True, slots=True)
class ProjectBundleExportResult:
    output_path: Path
    project_id: str
    revision_id: str
    asset_count: int
    inline_subtitle_count: int
    checksum_count: int


@dataclass(frozen=True, slots=True)
class DirectStemSource:
    item_id: str
    project_asset_id: str
    path: Path
    media_type: str
    filename: str
    size_bytes: int
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class StemExportPlan:
    kind: StemKind
    output_path: Path
    command: tuple[str, ...]
    duration_seconds: float
    source_count: int


@dataclass(frozen=True, slots=True)
class StemExportResult:
    kind: StemKind
    output_path: Path
    duration_seconds: float
    source_count: int


def _load_json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise VideoProjectRenderingError(f"{label} JSON 无效") from exc
    if not isinstance(parsed, dict):
        raise VideoProjectRenderingError(f"{label} 必须是 JSON 对象")
    return parsed


def _safe_number(value: Any, label: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise VideoProjectRenderingError(f"{label} 必须是数值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise VideoProjectRenderingError(f"{label} 必须是数值") from exc
    if not minimum <= number <= maximum:
        raise VideoProjectRenderingError(f"{label} 超出允许范围")
    return number


def _safe_item_metadata(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("metadata")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise VideoProjectRenderingError("时间线 metadata 必须是 JSON 对象")
    result = {key: raw[key] for key in _ITEM_METADATA_KEYS if key in raw}
    if "subtitle_style" in result:
        result["subtitle_style"] = _safe_subtitle_style(result["subtitle_style"])
    return result


def _safe_subtitle_style(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise VideoProjectRenderingError("字幕样式必须是 JSON 对象")
    unknown = set(value).difference(_SUBTITLE_STYLE_KEYS)
    if unknown:
        raise VideoProjectRenderingError(f"字幕样式包含不支持的字段：{', '.join(sorted(unknown))}")
    return dict(value)


def _media_kind(media_type: str, path: Path, fallback: str = "") -> str:
    normalized = media_type.lower().split(";", 1)[0].strip()
    if normalized.startswith("image/") or path.suffix.lower() in _IMAGE_EXTENSIONS:
        return "image"
    if normalized.startswith("video/") or path.suffix.lower() in _VIDEO_EXTENSIONS:
        return "video"
    if normalized.startswith("audio/") or path.suffix.lower() in _AUDIO_EXTENSIONS:
        return "audio"
    if path.suffix.lower() in _SUBTITLE_EXTENSIONS or fallback == "subtitle":
        return "subtitle"
    return "unknown"


def _project_revision(
    session: Session,
    project: VideoProject,
    revision_id: str | None = None,
) -> VideoProjectRevision:
    revision = session.get(
        VideoProjectRevision,
        revision_id or project.current_revision_id,
    )
    if revision is None or revision.project_id != project.id or revision.revision is None:
        raise VideoProjectRenderingError("视频工程当前版本不存在或不是已提交版本")
    if revision_id is None and revision.revision != project.current_revision:
        raise VideoProjectRenderingError("视频工程当前版本指针不一致")
    return revision


class ProjectAssetResolver:
    """Resolve project-owned opaque IDs through the content asset boundary."""

    def __init__(self, session: Session, project: VideoProject, asset_store: AssetStorePort):
        self.session = session
        self.project = project
        self.asset_store = asset_store
        self._cache: dict[str, ResolvedProjectAsset] = {}

    def _asset_ref_for_resource(self, entry: ResourceEntry) -> AssetRef | None:
        statement = select(AssetRef).where(AssetRef.entry_id == entry.id)
        if entry.current_version_id:
            current = self.session.scalar(
                statement.where(AssetRef.version_id == entry.current_version_id).order_by(
                    AssetRef.created_at.desc()
                )
            )
            if current is not None:
                return current
        return self.session.scalar(statement.order_by(AssetRef.created_at.desc()))

    def resolve(self, project_asset_id: str) -> ResolvedProjectAsset:
        if project_asset_id in self._cache:
            return self._cache[project_asset_id]
        item = self.session.get(ProjectAsset, project_asset_id)
        if item is None or item.project_id != self.project.id:
            raise VideoProjectRenderingError("时间线引用了其他工程或不存在的素材")
        project_metadata = _load_json_object(
            item.metadata_json,
            "ProjectAsset metadata",
        )
        if item.role == "logo" and project_metadata.get("pending_brand_confirmation") is True:
            raise VideoProjectRenderingError("AI Logo 候选未经人工确认，禁止参与渲染")
        references = [
            value
            for value in (
                item.asset_ref_id,
                item.artifact_id,
                item.library_item_id,
                item.resource_entry_id,
            )
            if value
        ]
        if len(references) != 1:
            raise VideoProjectRenderingError("工程素材必须且只能绑定一个原始来源")

        storage_key: str | None = None
        media_type = ""
        declared_checksum: str | None = None
        source_metadata: dict[str, Any] = {}
        fallback_kind = item.role
        if item.asset_ref_id:
            if item.source_type != "asset_ref" or item.source_id != item.asset_ref_id:
                raise VideoProjectRenderingError("工程素材来源标识与 AssetRef 不一致")
            source = self.session.get(AssetRef, item.asset_ref_id)
            if source is None:
                raise VideoProjectRenderingError("工程素材绑定的 AssetRef 不存在")
            entry = self.session.get(ResourceEntry, source.entry_id)
            if entry is None or not entry.active:
                raise VideoProjectRenderingError("工程素材绑定的资源不存在或已经停用")
            storage_key = source.storage_key
            media_type = source.media_type
            declared_checksum = source.checksum
            source_metadata = _load_json_object(source.metadata_json, "AssetRef metadata")
        elif item.artifact_id:
            if item.source_type != "artifact" or item.source_id != item.artifact_id:
                raise VideoProjectRenderingError("工程素材来源标识与 Artifact 不一致")
            source = self.session.get(Artifact, item.artifact_id)
            if source is None or source.status in {"discarded", "legacy_mock", "stale"}:
                raise VideoProjectRenderingError("工程素材绑定的生成结果不存在或已经失效")
            storage_key = source.storage_key
            source_metadata = _load_json_object(source.content_json, "Artifact content")
            media_type = str(source_metadata.get("media_type") or "")
            fallback_kind = source.kind
        elif item.library_item_id:
            if item.source_type != "library_item" or item.source_id != item.library_item_id:
                raise VideoProjectRenderingError("工程素材来源标识与 LibraryItem 不一致")
            source = self.session.get(LibraryItem, item.library_item_id)
            if source is None or not source.active:
                raise VideoProjectRenderingError("工程素材绑定的企业素材不存在或已经停用")
            storage_key = source.storage_key
            if storage_key:
                asset_ref = self.session.scalar(
                    select(AssetRef).where(AssetRef.storage_key == storage_key)
                )
                if asset_ref is not None:
                    media_type = asset_ref.media_type
                    declared_checksum = asset_ref.checksum
                    source_metadata = _load_json_object(
                        asset_ref.metadata_json,
                        "Library AssetRef metadata",
                    )
            elif source.resource_entry_id:
                entry = self.session.get(ResourceEntry, source.resource_entry_id)
                if entry is None or not entry.active:
                    raise VideoProjectRenderingError("企业素材绑定的资源不存在或已经停用")
                asset_ref = self._asset_ref_for_resource(entry)
                if asset_ref is not None:
                    storage_key = asset_ref.storage_key
                    media_type = asset_ref.media_type
                    declared_checksum = asset_ref.checksum
                    source_metadata = _load_json_object(
                        asset_ref.metadata_json,
                        "Library Resource metadata",
                    )
        else:
            if item.source_type != "resource_entry" or item.source_id != item.resource_entry_id:
                raise VideoProjectRenderingError("工程素材来源标识与 ResourceEntry 不一致")
            entry = self.session.get(ResourceEntry, item.resource_entry_id)
            if entry is None or not entry.active:
                raise VideoProjectRenderingError("工程素材绑定的资源不存在或已经停用")
            asset_ref = self._asset_ref_for_resource(entry)
            if asset_ref is None:
                raise VideoProjectRenderingError("工程资源当前版本没有可用媒体文件")
            storage_key = asset_ref.storage_key
            media_type = asset_ref.media_type
            declared_checksum = asset_ref.checksum
            source_metadata = _load_json_object(asset_ref.metadata_json, "Resource metadata")
            fallback_kind = entry.resource_kind

        if not storage_key:
            raise VideoProjectRenderingError("工程素材没有本地存储文件")
        try:
            path = self.asset_store.path_for(storage_key).resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise VideoProjectRenderingError("工程素材存储 key 无效或文件不存在") from exc
        if not path.is_file():
            raise VideoProjectRenderingError("工程素材本地文件不存在")
        guessed_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        media_type = media_type or guessed_type

        probe = project_metadata.get("probe")
        if not isinstance(probe, dict):
            probe = source_metadata.get("probe")
        if not isinstance(probe, dict):
            probe = {}
        audio_codec = probe.get("audio_codec")
        has_audio: bool | None
        if isinstance(audio_codec, str):
            has_audio = bool(audio_codec.strip())
        elif isinstance(project_metadata.get("has_audio"), bool):
            has_audio = project_metadata["has_audio"]
        elif isinstance(source_metadata.get("has_audio"), bool):
            has_audio = source_metadata["has_audio"]
        else:
            has_audio = None
        raw_duration = probe.get("duration_seconds")
        if raw_duration is None:
            raw_duration = source_metadata.get("duration_seconds")
        duration = None
        if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool):
            duration = float(raw_duration)
            if duration <= 0 or duration > 86_400:
                duration = None

        resolved = ResolvedProjectAsset(
            project_asset_id=item.id,
            source_type=item.source_type,
            source_id=item.source_id,
            role=item.role,
            label=item.label,
            storage_key=storage_key,
            path=path,
            media_type=media_type,
            media_kind=_media_kind(media_type, path, fallback_kind),
            size_bytes=path.stat().st_size,
            declared_checksum=declared_checksum,
            duration_seconds=duration,
            has_audio=has_audio,
        )
        self._cache[item.id] = resolved
        return resolved


def _tracks(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    raw_tracks = timeline.get("tracks")
    if not isinstance(raw_tracks, list):
        raise VideoProjectRenderingError("工程时间线缺少 tracks 数组")
    result: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    for raw in raw_tracks:
        if not isinstance(raw, dict):
            raise VideoProjectRenderingError("工程时间线轨道必须是 JSON 对象")
        kind = str(raw.get("kind") or "")
        if kind not in _TRACK_KINDS or kind in seen_kinds:
            raise VideoProjectRenderingError("工程时间线轨道类型无效或重复")
        items = raw.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise VideoProjectRenderingError("工程时间线 items 必须是对象数组")
        seen_kinds.add(kind)
        result.append(raw)
    return result


def _item_number(
    item: dict[str, Any],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    return _safe_number(item.get(name, default), f"时间线 {name}", minimum=minimum, maximum=maximum)


def _item_id(item: dict[str, Any]) -> str:
    value = str(item.get("id") or "").strip()
    if not value or len(value) > 128 or any(character in value for character in "/\\\x00"):
        raise VideoProjectRenderingError("时间线 item ID 无效")
    return value


def _item_asset(
    item: dict[str, Any],
    resolver: ProjectAssetResolver,
) -> ResolvedProjectAsset:
    asset_id = str(item.get("project_asset_id") or "").strip()
    if not asset_id:
        raise VideoProjectRenderingError("可渲染的时间线素材必须绑定 project_asset_id")
    return resolver.resolve(asset_id)


def _assert_video_speed(item: dict[str, Any]) -> None:
    speed = _item_number(item, "speed", 1, minimum=0.1, maximum=8)
    if abs(speed - 1) > 0.000001:
        raise VideoProjectRenderingError("视频轨道当前尚不支持非 1.0 倍速")


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _inline_subtitle_srt(items: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for sequence, item in enumerate(items, start=1):
        text = str(item.get("content") or "").replace("\x00", "").strip()
        if not text:
            continue
        start = _item_number(item, "start_seconds", 0, minimum=0, maximum=600)
        duration = _item_number(item, "duration_seconds", 0, minimum=0.001, maximum=600)
        blocks.append(
            "\n".join(
                [
                    str(sequence),
                    f"{_srt_timestamp(start)} --> {_srt_timestamp(start + duration)}",
                    text,
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _project_trimmed_duration(
    item: dict[str, Any],
    asset: ResolvedProjectAsset,
    duration: float,
    source_in: float,
    *,
    speed: float = 1,
    loop: bool = False,
    source_durations: dict[Path, float | None],
) -> tuple[float, float | None]:
    """Honor source-tail trims even for legacy edits carrying an old duration.

    Only timed media uses source trims. Static images and inline captions never
    enter this function. Probe the actual local source before trusting its range.
    """
    trim_out = _item_number(item, "trim_out_seconds", 0, minimum=0, maximum=86_400)
    if not trim_out and not (loop and source_in):
        return duration, None
    if asset.path not in source_durations:
        ffmpeg = _ffmpeg_executable()
        probed, _ = _probe(asset.path, _ffprobe_executable(ffmpeg), ffmpeg)
        source_durations[asset.path] = probed or asset.duration_seconds
    source_duration = source_durations[asset.path]
    if source_duration is None:
        raise VideoProjectRenderingError("无法读取素材时长，暂不能裁剪结尾，请重新导入该素材")
    available = source_duration - source_in - trim_out
    if available <= 0:
        raise VideoProjectRenderingError("裁入与裁出合计已超过素材时长，请至少保留一段有效内容")
    if loop:
        return duration, available
    return min(duration, available / speed), None


def _safe_component(value: str, fallback: str) -> str:
    normalized = _SAFE_COMPONENT.sub("-", value).strip(" .-_")
    return (normalized or fallback)[:100]


def _is_legacy_subtitle_file_placeholder(
    session: Session,
    item: dict[str, Any],
    asset: ResolvedProjectAsset,
) -> bool:
    """Recognize the old editor's file-label fallback without discarding edited text."""

    if (
        asset.role != "subtitle"
        or asset.media_kind != "subtitle"
        or asset.path.suffix.lower() not in _SUBTITLE_EXTENSIONS
    ):
        return False
    metadata = item.get("metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("asset_role") != "subtitle"
        or metadata.get("media_kind") not in ("file", "subtitle")
        or metadata.get("resource_kind") != metadata.get("media_kind")
    ):
        return False
    source = session.get(ProjectAsset, asset.project_asset_id)
    if source is None:
        return False
    source_metadata = _load_json_object(source.metadata_json, "ProjectAsset metadata")
    original_text = str(source_metadata.get("text") or "").strip()
    copied_text = str(metadata.get("text") or "").strip()
    if copied_text != original_text:
        return False
    placeholder = original_text or asset.label.strip()
    return bool(placeholder) and str(item.get("content") or "").strip() == placeholder


def prepare_video_project_render(
    session: Session,
    project: VideoProject,
    asset_store: AssetStorePort,
    output_path: Path,
    *,
    work_root: Path,
    revision_id: str | None = None,
    options: ProjectRenderOptions | None = None,
) -> PreparedVideoProjectRender:
    """Compile the current committed project into an internal render request.

    The caller must perform project/identity authorization before invoking this
    function. Only project-owned opaque asset IDs are resolved here; timeline
    metadata can choose from explicit enums/numeric fields and cannot provide a
    filesystem path or FFmpeg expression.
    """

    options = options or ProjectRenderOptions()
    revision = _project_revision(session, project, revision_id)
    timeline = _load_json_object(revision.timeline_json, "VideoProject timeline")
    tracks = _tracks(timeline)
    resolver = ProjectAssetResolver(session, project, asset_store)
    canvas = timeline.get("canvas")
    if not isinstance(canvas, dict):
        raise VideoProjectRenderingError("工程时间线缺少 canvas")
    width = int(_safe_number(canvas.get("width"), "canvas.width", minimum=64, maximum=7680))
    height = int(_safe_number(canvas.get("height"), "canvas.height", minimum=64, maximum=4320))
    fps = int(_safe_number(canvas.get("fps"), "canvas.fps", minimum=1, maximum=120))

    prepared_items: list[PreparedProjectItem] = []
    clips: list[TimelineClip] = []
    audio_tracks: list[TimelineAudioTrack] = []
    logos: list[TimelineLogo] = []
    subtitles: list[TimelineSubtitle] = []
    inline_groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    source_durations: dict[Path, float | None] = {}
    non_audio_extent = 0.0
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    for track in tracks:
        kind = str(track["kind"])
        if bool(track.get("muted", False)):
            continue
        for item in track["items"]:
            if not bool(item.get("enabled", True)):
                continue
            item_id = _item_id(item)
            metadata = _safe_item_metadata(item)
            if kind == "video":
                _assert_video_speed(item)
                asset = _item_asset(item, resolver)
                if asset.media_kind not in {"image", "video"}:
                    raise VideoProjectRenderingError("video 轨道只能引用图片或视频素材")
                duration = _item_number(
                    item,
                    "duration_seconds",
                    0,
                    minimum=0.001,
                    maximum=600,
                )
                source_in = 0.0
                if asset.media_kind == "video":
                    source_in = _item_number(
                        item,
                        "trim_in_seconds",
                        0,
                        minimum=0,
                        maximum=86_400,
                    )
                    duration, _ = _project_trimmed_duration(
                        item,
                        asset,
                        duration,
                        source_in,
                        source_durations=source_durations,
                    )
                source_audio = str(
                    metadata.get("source_audio") or options.default_source_audio
                ).lower()
                if source_audio not in {"keep", "mute"}:
                    raise VideoProjectRenderingError("source_audio 仅支持 keep 或 mute")
                if asset.media_kind == "image" or asset.has_audio is False:
                    source_audio = "mute"
                transition = str(metadata.get("transition") or "none").lower()
                if not clips:
                    transition = "none"
                transition_duration = _safe_number(
                    metadata.get("transition_duration_seconds", 0.4),
                    "transition_duration_seconds",
                    minimum=0,
                    maximum=5,
                )
                fit_mode = str(metadata.get("fit_mode") or "contain").lower()
                volume = _item_number(item, "volume", 1, minimum=0, maximum=4)
                clips.append(
                    TimelineClip(
                        asset_id=asset.project_asset_id,
                        source_path=asset.path,
                        kind=asset.media_kind,
                        source_in_seconds=source_in,
                        source_out_seconds=(
                            source_in + duration if asset.media_kind == "video" else None
                        ),
                        duration_seconds=duration,
                        transition=transition,
                        transition_duration_seconds=transition_duration,
                        fit_mode=fit_mode,
                        source_audio=source_audio,
                        source_audio_volume=volume,
                        has_source_audio=asset.has_audio,
                    )
                )
                prepared_items.append(
                    PreparedProjectItem(item_id, "video", asset.project_asset_id, asset)
                )
                continue

            if kind in _AUDIO_TRACK_KINDS:
                asset = _item_asset(item, resolver)
                if asset.media_kind != "audio":
                    raise VideoProjectRenderingError(f"{kind} 轨道只能引用音频素材")
                offset = _item_number(
                    item,
                    "start_seconds",
                    0,
                    minimum=0,
                    maximum=600,
                )
                duration = _item_number(
                    item,
                    "duration_seconds",
                    0,
                    minimum=0.001,
                    maximum=600,
                )
                source_in = _item_number(
                    item,
                    "trim_in_seconds",
                    0,
                    minimum=0,
                    maximum=86_400,
                )
                speed = _item_number(item, "speed", 1, minimum=0.1, maximum=8)
                volume = _item_number(item, "volume", 1, minimum=0, maximum=4)
                fade_in = _safe_number(
                    metadata.get("fade_in_seconds", 0),
                    "fade_in_seconds",
                    minimum=0,
                    maximum=600,
                )
                fade_out = _safe_number(
                    metadata.get("fade_out_seconds", 0),
                    "fade_out_seconds",
                    minimum=0,
                    maximum=600,
                )
                loop = metadata.get("loop", False)
                if not isinstance(loop, bool):
                    raise VideoProjectRenderingError("loop 必须是布尔值")
                duration, loop_span = _project_trimmed_duration(
                    item,
                    asset,
                    duration,
                    source_in,
                    speed=speed,
                    loop=loop,
                    source_durations=source_durations,
                )
                audio_tracks.append(
                    TimelineAudioTrack(
                        asset_id=asset.project_asset_id,
                        source_path=asset.path,
                        kind=kind,
                        offset_seconds=offset,
                        source_in_seconds=source_in,
                        source_out_seconds=(None if loop else source_in + duration * speed),
                        duration_seconds=duration,
                        speed=speed,
                        volume=volume,
                        fade_in_seconds=fade_in,
                        fade_out_seconds=fade_out,
                        loop=loop,
                        loop_source_duration_seconds=loop_span,
                    )
                )
                prepared_items.append(
                    PreparedProjectItem(item_id, kind, asset.project_asset_id, asset)
                )
                continue

            if kind == "overlay":
                asset = _item_asset(item, resolver)
                if asset.media_kind != "image":
                    raise VideoProjectRenderingError("overlay 轨道当前只支持图片 Logo")
                raw_transform = item.get("transform")
                if raw_transform is None:
                    transform: dict[str, Any] = {}
                elif isinstance(raw_transform, dict):
                    transform = raw_transform
                else:
                    raise VideoProjectRenderingError("时间线 transform 必须是 JSON 对象")
                start = _item_number(item, "start_seconds", 0, minimum=0, maximum=600)
                duration = _item_number(
                    item,
                    "duration_seconds",
                    0,
                    minimum=0.001,
                    maximum=600,
                )
                non_audio_extent = max(non_audio_extent, start + duration)
                position = str(metadata.get("position") or "top_right").lower()
                base_width = _safe_number(
                    metadata.get("width", 160),
                    "Logo width",
                    minimum=16,
                    maximum=2048,
                )
                scale = _safe_number(
                    transform.get("scale", 1),
                    "Logo scale",
                    minimum=0.1,
                    maximum=10,
                )
                width_value = int(
                    round(
                        _safe_number(
                            base_width * scale,
                            "Logo scaled width",
                            minimum=16,
                            maximum=2048,
                        )
                    )
                )
                opacity = _safe_number(
                    transform.get("opacity", metadata.get("opacity", 1)),
                    "Logo opacity",
                    minimum=0,
                    maximum=1,
                )
                x_percent: float | None = None
                y_percent: float | None = None
                has_x = "x" in transform
                has_y = "y" in transform
                if has_x != has_y:
                    raise VideoProjectRenderingError("Logo transform 必须同时提供 x 和 y")
                if has_x and has_y:
                    x_percent = _safe_number(
                        transform["x"],
                        "Logo x",
                        minimum=0,
                        maximum=100,
                    )
                    y_percent = _safe_number(
                        transform["y"],
                        "Logo y",
                        minimum=0,
                        maximum=100,
                    )
                logos.append(
                    TimelineLogo(
                        asset_id=asset.project_asset_id,
                        source_path=asset.path,
                        start_seconds=start,
                        end_seconds=start + duration,
                        width=width_value,
                        position=position,
                        opacity=opacity,
                        x_percent=x_percent,
                        y_percent=y_percent,
                    )
                )
                prepared_items.append(
                    PreparedProjectItem(item_id, "overlay", asset.project_asset_id, asset)
                )
                continue

            if kind == "subtitle":
                content = str(item.get("content") or "").strip()
                project_asset_id = str(item.get("project_asset_id") or "").strip()
                style = _safe_subtitle_style(metadata.get("subtitle_style"))
                start = _item_number(item, "start_seconds", 0, minimum=0, maximum=600)
                duration = _item_number(item, "duration_seconds", 0, minimum=0.001, maximum=600)
                non_audio_extent = max(non_audio_extent, start + duration)
                if project_asset_id:
                    asset = resolver.resolve(project_asset_id)
                    if asset.media_kind != "subtitle" or asset.path.suffix.lower() not in (
                        _SUBTITLE_EXTENSIONS
                    ):
                        raise VideoProjectRenderingError("字幕轨道仅支持本地 SRT 或 VTT")
                    # Old editor revisions copied the subtitle asset's display
                    # label/text into content while retaining its file ID. Use
                    # that file only for the exact known placeholder shape;
                    # genuine competing text sources must still be rejected.
                    if content and not _is_legacy_subtitle_file_placeholder(session, item, asset):
                        raise VideoProjectRenderingError("字幕 item 不能同时提供内联文本和素材")
                    start = _item_number(
                        item,
                        "start_seconds",
                        0,
                        minimum=0,
                        maximum=600,
                    )
                    if start != 0:
                        raise VideoProjectRenderingError(
                            "外部 SRT/VTT 必须使用文件内时间戳且从 0 秒放置"
                        )
                    subtitles.append(
                        TimelineSubtitle(
                            asset.project_asset_id,
                            asset.path,
                            style,
                            width,
                            height,
                        )
                    )
                    prepared_items.append(
                        PreparedProjectItem(
                            item_id,
                            "subtitle",
                            asset.project_asset_id,
                            asset,
                        )
                    )
                elif content:
                    style_key = json.dumps(style, ensure_ascii=False, sort_keys=True)
                    group = inline_groups.setdefault(style_key, (style, []))
                    group[1].append(item)
                else:
                    raise VideoProjectRenderingError("字幕 item 必须提供文本或 SRT/VTT 素材")

    if not clips:
        raise VideoProjectRenderingError("当前工程没有可渲染的视频/图片镜头")
    if inline_groups:
        work_root = work_root.expanduser().resolve()
        work_root.mkdir(parents=True, exist_ok=True)
        temporary_directory = tempfile.TemporaryDirectory(
            prefix=f"project-{project.id[:12]}-",
            dir=work_root,
        )
        subtitle_root = Path(temporary_directory.name).resolve()
        for index, (style, items) in enumerate(inline_groups.values(), start=1):
            srt = _inline_subtitle_srt(items)
            if not srt:
                continue
            path = subtitle_root / f"inline-{index}.srt"
            path.write_text(srt, encoding="utf-8")
            subtitles.append(
                TimelineSubtitle(
                    f"inline-subtitle-{index}",
                    path,
                    style,
                    width,
                    height,
                )
            )

    has_voice = any(track.kind == "voice" for track in audio_tracks)
    target_kinds = tuple(options.ducking_target_kinds)
    has_duck_target = any(track.kind in target_kinds for track in audio_tracks) or (
        "source" in target_kinds and any(clip.source_audio == "keep" for clip in clips)
    )
    request = TimelineRenderRequest(
        clips=tuple(clips),
        output_path=output_path,
        width=width,
        height=height,
        fps=fps,
        audio_tracks=tuple(audio_tracks),
        logos=tuple(logos),
        subtitles=tuple(subtitles),
        ducking=TimelineDucking(
            enabled=options.automatic_music_ducking and has_voice and has_duck_target,
            target_kinds=target_kinds,
        ),
        background_color=options.background_color,
        video_bitrate=options.video_bitrate,
        audio_bitrate=options.audio_bitrate,
        overwrite=options.overwrite,
        duration_seconds=non_audio_extent or None,
        extend_to_audio=True,
    )
    return PreparedVideoProjectRender(
        project_id=project.id,
        revision_id=revision.id,
        revision=revision.revision,
        request=request,
        item_sources=tuple(prepared_items),
        _temporary_directory=temporary_directory,
    )


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _safe_output_path(
    output_path: Path,
    *,
    suffixes: frozenset[str],
    allowed_output_roots: tuple[Path, ...],
    overwrite: bool,
) -> Path:
    if not isinstance(output_path, Path):
        raise TypeError("输出路径必须是 pathlib.Path")
    if not allowed_output_roots:
        raise VideoProjectRenderingError("必须配置至少一个受控输出目录")
    roots = tuple(root.expanduser().resolve() for root in allowed_output_roots)
    path = output_path.expanduser().resolve()
    if path.suffix.lower() not in suffixes:
        raise VideoProjectRenderingError("输出文件类型不在允许范围内")
    if not _is_within(path, roots):
        raise VideoProjectRenderingError("输出路径超出内容服务受控目录")
    if path.exists() and not overwrite:
        raise VideoProjectRenderingError("输出文件已存在；覆盖必须由服务端显式启用")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_export_item(item: dict[str, Any], kind: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": _item_id(item),
        "label": str(item.get("label") or "")[:255],
        "start_seconds": _item_number(
            item,
            "start_seconds",
            0,
            minimum=0,
            maximum=600,
        ),
        "duration_seconds": _item_number(
            item,
            "duration_seconds",
            0,
            minimum=0,
            maximum=600,
        ),
        "trim_in_seconds": _item_number(
            item,
            "trim_in_seconds",
            0,
            minimum=0,
            maximum=86_400,
        ),
        "trim_out_seconds": _item_number(
            item,
            "trim_out_seconds",
            0,
            minimum=0,
            maximum=86_400,
        ),
        "volume": _item_number(item, "volume", 1, minimum=0, maximum=4),
        "speed": _item_number(item, "speed", 1, minimum=0.1, maximum=8),
        "enabled": bool(item.get("enabled", True)),
    }
    project_asset_id = str(item.get("project_asset_id") or "").strip()
    if project_asset_id:
        result["project_asset_id"] = project_asset_id
    if kind == "subtitle" and not project_asset_id:
        result["content"] = str(item.get("content") or "").replace("\x00", "")
    metadata = _safe_item_metadata(item)
    if metadata:
        result["metadata"] = metadata
    transform = item.get("transform")
    if transform is not None:
        if not isinstance(transform, dict):
            raise VideoProjectRenderingError("时间线 transform 必须是 JSON 对象")
        controlled_transform = {key: transform[key] for key in _TRANSFORM_KEYS if key in transform}
        if controlled_transform:
            result["transform"] = controlled_transform
    return result


def _filtered_export_timeline(
    timeline: dict[str, Any],
    options: ProjectBundleExportOptions,
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]], list[dict[str, Any]]]:
    unknown_kinds = set(options.track_kinds).difference(_TRACK_KINDS)
    if unknown_kinds:
        raise VideoProjectRenderingError("工程导出包含未知轨道类型")
    if not 1 <= options.max_assets <= 10_000 or not options.max_total_bytes >= 1:
        raise VideoProjectRenderingError("工程导出容量限制无效")
    canvas = timeline.get("canvas")
    if not isinstance(canvas, dict):
        raise VideoProjectRenderingError("工程时间线缺少 canvas")
    safe_canvas = {
        "width": int(_safe_number(canvas.get("width"), "canvas.width", minimum=64, maximum=7680)),
        "height": int(
            _safe_number(canvas.get("height"), "canvas.height", minimum=64, maximum=4320)
        ),
        "fps": int(_safe_number(canvas.get("fps"), "canvas.fps", minimum=1, maximum=120)),
        "aspect_ratio": str(canvas.get("aspect_ratio") or "")[:32],
    }
    selected_assets: list[tuple[str, dict[str, Any]]] = []
    inline_subtitles: list[dict[str, Any]] = []
    selected_asset_ids: set[str] = set()
    safe_tracks: list[dict[str, Any]] = []
    for raw_track in _tracks(timeline):
        kind = str(raw_track["kind"])
        if kind not in options.track_kinds:
            continue
        safe_items: list[dict[str, Any]] = []
        for item in raw_track["items"]:
            project_asset_id = str(item.get("project_asset_id") or "").strip()
            if (
                project_asset_id
                and options.project_asset_ids is not None
                and project_asset_id not in options.project_asset_ids
            ):
                continue
            safe_item = _safe_export_item(item, kind)
            safe_items.append(safe_item)
            if project_asset_id:
                selected_assets.append((kind, item))
                selected_asset_ids.add(project_asset_id)
            elif kind == "subtitle" and str(item.get("content") or "").strip():
                inline_subtitles.append(item)
        safe_tracks.append(
            {
                "id": str(raw_track.get("id") or kind)[:128],
                "kind": kind,
                "name": str(raw_track.get("name") or "")[:255],
                "muted": bool(raw_track.get("muted", False)),
                "locked": bool(raw_track.get("locked", False)),
                "items": safe_items,
            }
        )
    if options.project_asset_ids is not None:
        missing = set(options.project_asset_ids).difference(selected_asset_ids)
        if missing:
            raise VideoProjectRenderingError("指定导出的工程素材不在所选当前时间线中")
    return {"canvas": safe_canvas, "tracks": safe_tracks}, selected_assets, inline_subtitles


def _zip_write_bytes(
    archive: zipfile.ZipFile,
    archive_path: str,
    payload: bytes,
    checksums: dict[str, str],
) -> None:
    archive.writestr(archive_path, payload)
    checksums[archive_path] = _sha256_bytes(payload)


def export_video_project_bundle(
    session: Session,
    project: VideoProject,
    asset_store: AssetStorePort,
    output_path: Path,
    *,
    allowed_output_roots: tuple[Path, ...],
    revision_id: str | None = None,
    options: ProjectBundleExportOptions | None = None,
) -> ProjectBundleExportResult:
    """Export a current project subset without leaking storage keys or server paths."""

    options = options or ProjectBundleExportOptions()
    output = _safe_output_path(
        output_path,
        suffixes=frozenset({".zip"}),
        allowed_output_roots=allowed_output_roots,
        overwrite=options.overwrite,
    )
    revision = _project_revision(session, project, revision_id)
    timeline = _load_json_object(revision.timeline_json, "VideoProject timeline")
    safe_timeline, selected_items, inline_items = _filtered_export_timeline(
        timeline,
        options,
    )
    resolver = ProjectAssetResolver(session, project, asset_store)
    unique_assets: dict[str, tuple[str, dict[str, Any], ResolvedProjectAsset]] = {}
    for kind, item in selected_items:
        asset = _item_asset(item, resolver)
        unique_assets.setdefault(asset.project_asset_id, (kind, item, asset))
    if len(unique_assets) > options.max_assets:
        raise VideoProjectRenderingError("工程导出的素材数量超过服务端限制")
    total_bytes = sum(value[2].size_bytes for value in unique_assets.values())
    if options.include_original_assets and total_bytes > options.max_total_bytes:
        raise VideoProjectRenderingError("工程导出的原始素材总容量超过服务端限制")

    asset_manifest: list[dict[str, Any]] = []
    archive_sources: list[tuple[Path, str, str]] = []
    used_archive_paths: set[str] = set()
    for index, (project_asset_id, (kind, item, asset)) in enumerate(
        unique_assets.items(),
        start=1,
    ):
        label = str(item.get("label") or asset.label or asset.path.stem)
        suffix = asset.path.suffix.lower()
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
            suffix = ".bin"
        folder = "subtitles" if asset.media_kind == "subtitle" else "assets"
        archive_name = (
            f"{folder}/{index:03d}-{_safe_component(label, 'asset')}-{project_asset_id[:8]}{suffix}"
        )
        while archive_name in used_archive_paths:
            archive_name = f"{folder}/{index:03d}-{project_asset_id}{suffix}"
        used_archive_paths.add(archive_name)
        checksum = _sha256_file(asset.path)
        asset_manifest.append(
            {
                "project_asset_id": project_asset_id,
                "source_type": asset.source_type,
                "source_id": asset.source_id,
                "role": asset.role,
                "label": asset.label,
                "track_kind": kind,
                "media_type": asset.media_type,
                "media_kind": asset.media_kind,
                "size_bytes": asset.size_bytes,
                "sha256": checksum,
                "archive_path": archive_name if options.include_original_assets else None,
            }
        )
        if options.include_original_assets:
            archive_sources.append((asset.path, archive_name, checksum))

    project_payload = json.dumps(
        {
            "format": "content-video-project/v1",
            "project_id": project.id,
            "task_id": project.task_id,
            "revision_id": revision.id,
            "revision": revision.revision,
            "timeline": safe_timeline,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    manifest_payload = json.dumps(
        {
            "format": "content-video-project-manifest/v1",
            "project_id": project.id,
            "revision_id": revision.id,
            "selection": {
                "track_kinds": sorted(options.track_kinds),
                "project_asset_ids": (
                    sorted(options.project_asset_ids)
                    if options.project_asset_ids is not None
                    else None
                ),
            },
            "assets": asset_manifest,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    checksum_count = 0
    inline_count = 0
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        checksums: dict[str, str] = {}
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            _zip_write_bytes(archive, "project.json", project_payload, checksums)
            _zip_write_bytes(archive, "manifest.json", manifest_payload, checksums)
            for source_path, archive_path, checksum in archive_sources:
                archive.write(source_path, archive_path)
                checksums[archive_path] = checksum
            if options.include_inline_subtitles and inline_items:
                inline_payload = _inline_subtitle_srt(inline_items).encode("utf-8")
                if inline_payload:
                    _zip_write_bytes(
                        archive,
                        "subtitles/inline-subtitles.srt",
                        inline_payload,
                        checksums,
                    )
                    inline_count = len(inline_items)
            checksum_payload = "".join(
                f"{checksum}  {archive_path}\n"
                for archive_path, checksum in sorted(checksums.items())
            ).encode("utf-8")
            archive.writestr("checksums.sha256", checksum_payload)
            checksum_count = len(checksums)
        if output.exists() and not options.overwrite:
            raise VideoProjectRenderingError("输出文件已存在；覆盖必须由服务端显式启用")
        temporary_path.replace(output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return ProjectBundleExportResult(
        output_path=output,
        project_id=project.id,
        revision_id=revision.id,
        asset_count=len(unique_assets),
        inline_subtitle_count=inline_count,
        checksum_count=checksum_count,
    )


def _format_seconds(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _ffmpeg_executable(configured: str | None = None) -> str:
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise VideoProjectRenderingError("FFmpeg 可执行文件不存在")
        return str(path)
    if found := shutil.which("ffmpeg"):
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError) as exc:
        raise VideoProjectRenderingError("没有可用的 FFmpeg 可执行文件") from exc


def _project_schedule(
    clips: tuple[TimelineClip, ...],
) -> tuple[list[float], list[float], float]:
    if not clips:
        raise VideoProjectRenderingError("视频工程没有可计算时长的画面素材")
    durations: list[float] = []
    for clip in clips:
        duration = clip.duration_seconds
        if duration is None and clip.source_out_seconds is not None:
            duration = clip.source_out_seconds - clip.source_in_seconds
        if duration is None or duration <= 0:
            raise VideoProjectRenderingError("视频镜头缺少可验证时长")
        durations.append(duration)
    starts = [0.0]
    overlaps = [0.0]
    total_duration = durations[0]
    for index in range(1, len(clips)):
        transition = clips[index].transition.lower()
        overlap = 0.0
        if transition not in {"", "none", "cut"}:
            overlap = min(
                clips[index].transition_duration_seconds,
                durations[index - 1] / 2,
                durations[index] / 2,
            )
        starts.append(total_duration - overlap)
        overlaps.append(overlap)
        total_duration += durations[index] - overlap
    return starts, overlaps, total_duration


def _prepared_pairs(
    prepared: PreparedVideoProjectRender,
    track_kind: ProjectTrackKind,
) -> list[tuple[PreparedProjectItem, TimelineClip | TimelineAudioTrack]]:
    items = [item for item in prepared.item_sources if item.track_kind == track_kind]
    if track_kind == "video":
        values: list[TimelineClip | TimelineAudioTrack] = list(prepared.request.clips)
    elif track_kind in _AUDIO_TRACK_KINDS:
        values = [track for track in prepared.request.audio_tracks if track.kind == track_kind]
    else:
        values = []
    if len(items) != len(values):
        raise VideoProjectRenderingError("工程素材索引与渲染请求不一致")
    return list(zip(items, values, strict=True))


def select_direct_stem_source(
    prepared: PreparedVideoProjectRender,
    kind: StemKind,
    *,
    item_id: str | None = None,
) -> DirectStemSource:
    """Return a trusted original source for a caller that wants raw-source export."""

    if kind == "source_audio":
        candidates = [
            (item, clip)
            for item, clip in _prepared_pairs(prepared, "video")
            if isinstance(clip, TimelineClip)
            and clip.source_audio == "keep"
            and clip.has_source_audio is not False
        ]
    elif kind in _AUDIO_TRACK_KINDS:
        candidates = _prepared_pairs(prepared, kind)
    else:
        raise VideoProjectRenderingError("stem 类型无效")
    if item_id is not None:
        candidates = [candidate for candidate in candidates if candidate[0].item_id == item_id]
    if not candidates:
        raise VideoProjectRenderingError("当前工程没有匹配的原始 stem 素材")
    if len(candidates) != 1:
        raise VideoProjectRenderingError("存在多个 stem 素材，请指定 item_id")
    item, _ = candidates[0]
    path = item.asset.path.resolve(strict=True)
    return DirectStemSource(
        item_id=item.item_id,
        project_asset_id=item.project_asset_id,
        path=path,
        media_type=item.asset.media_type,
        filename=path.name,
        size_bytes=path.stat().st_size,
        checksum_sha256=_sha256_file(path),
    )


@dataclass(frozen=True, slots=True)
class _StemEntry:
    item_id: str
    path: Path
    source_in_seconds: float
    duration_seconds: float
    offset_seconds: float
    speed: float
    volume: float
    fade_in_seconds: float
    fade_out_seconds: float
    loop: bool
    loop_source_duration_seconds: float | None = None


def _stem_entries(
    prepared: PreparedVideoProjectRender,
    kind: StemKind,
    item_id: str | None,
) -> tuple[list[_StemEntry], float]:
    starts, overlaps, total_duration = _project_schedule(prepared.request.clips)
    total_duration = max(total_duration, prepared.request.duration_seconds or 0)
    if prepared.request.extend_to_audio and prepared.request.audio_tracks:
        total_duration = max(
            total_duration,
            *(
                value.offset_seconds + (value.duration_seconds or 0)
                for value in prepared.request.audio_tracks
            ),
        )
    entries: list[_StemEntry] = []
    if kind == "source_audio":
        video_pairs = _prepared_pairs(prepared, "video")
        for index, (item, value) in enumerate(video_pairs):
            if not isinstance(value, TimelineClip):
                continue
            if item_id is not None and item.item_id != item_id:
                continue
            if value.source_audio != "keep" or value.has_source_audio is False:
                continue
            duration = value.duration_seconds
            if duration is None and value.source_out_seconds is not None:
                duration = value.source_out_seconds - value.source_in_seconds
            if duration is None:
                raise VideoProjectRenderingError("源音轨镜头缺少时长")
            fade_in = overlaps[index]
            fade_out = overlaps[index + 1] if index + 1 < len(overlaps) else 0
            entries.append(
                _StemEntry(
                    item_id=item.item_id,
                    path=value.source_path,
                    source_in_seconds=value.source_in_seconds,
                    duration_seconds=duration,
                    offset_seconds=starts[index],
                    speed=1,
                    volume=value.source_audio_volume,
                    fade_in_seconds=fade_in,
                    fade_out_seconds=fade_out,
                    loop=False,
                )
            )
    elif kind in _AUDIO_TRACK_KINDS:
        for item, value in _prepared_pairs(prepared, kind):
            if not isinstance(value, TimelineAudioTrack):
                continue
            if item_id is not None and item.item_id != item_id:
                continue
            duration = value.duration_seconds
            if duration is None and value.source_out_seconds is not None:
                duration = (value.source_out_seconds - value.source_in_seconds) / value.speed
            if duration is None:
                raise VideoProjectRenderingError("附加音轨缺少时长")
            duration = min(duration, total_duration - value.offset_seconds)
            if duration <= 0:
                raise VideoProjectRenderingError("附加音轨必须位于视频时间线内")
            entries.append(
                _StemEntry(
                    item_id=item.item_id,
                    path=value.source_path,
                    source_in_seconds=value.source_in_seconds,
                    duration_seconds=duration,
                    offset_seconds=value.offset_seconds,
                    speed=value.speed,
                    volume=value.volume,
                    fade_in_seconds=value.fade_in_seconds,
                    fade_out_seconds=value.fade_out_seconds,
                    loop=value.loop,
                    loop_source_duration_seconds=value.loop_source_duration_seconds,
                )
            )
    else:
        raise VideoProjectRenderingError("stem 类型无效")
    if not entries:
        raise VideoProjectRenderingError("当前工程没有可导出的 stem")
    if item_id is not None and len(entries) != 1:
        raise VideoProjectRenderingError("指定的 stem item_id 不唯一")
    return entries, total_duration


def build_stem_export_plan(
    prepared: PreparedVideoProjectRender,
    kind: StemKind,
    output_path: Path,
    *,
    allowed_input_roots: tuple[Path, ...],
    allowed_output_roots: tuple[Path, ...],
    item_id: str | None = None,
    ffmpeg_path: str | None = None,
    overwrite: bool = False,
) -> StemExportPlan:
    """Build an allowlist-only, shell-free FFmpeg plan for one project stem bus."""

    if not allowed_input_roots:
        raise VideoProjectRenderingError("必须配置至少一个受控素材目录")
    input_roots = tuple(root.expanduser().resolve() for root in allowed_input_roots)
    output = _safe_output_path(
        output_path,
        suffixes=frozenset({".mp3", ".wav"}),
        allowed_output_roots=allowed_output_roots,
        overwrite=overwrite,
    )
    entries, total_duration = _stem_entries(prepared, kind, item_id)
    resolved_entries: list[_StemEntry] = []
    for entry in entries:
        path = entry.path.expanduser().resolve(strict=True)
        if not path.is_file() or not _is_within(path, input_roots):
            raise VideoProjectRenderingError("stem 素材超出内容服务对象存储目录")
        if output == path:
            raise VideoProjectRenderingError("stem 输出不能覆盖输入素材")
        if entry.source_in_seconds < 0 or entry.duration_seconds <= 0:
            raise VideoProjectRenderingError("stem 裁剪范围无效")
        try:
            _atempo_filter_chain(entry.speed)
        except ValueError as exc:
            raise VideoProjectRenderingError(str(exc)) from exc
        if not 0 <= entry.offset_seconds < total_duration:
            raise VideoProjectRenderingError("stem 偏移必须位于视频时间线内")
        if not 0 <= entry.volume <= 4:
            raise VideoProjectRenderingError("stem 音量超出允许范围")
        if (
            entry.fade_in_seconds < 0
            or entry.fade_out_seconds < 0
            or entry.fade_in_seconds > entry.duration_seconds
            or entry.fade_out_seconds > entry.duration_seconds
        ):
            raise VideoProjectRenderingError("stem 淡入淡出不能超过素材时长")
        resolved_entries.append(
            _StemEntry(
                item_id=entry.item_id,
                path=path,
                source_in_seconds=entry.source_in_seconds,
                duration_seconds=entry.duration_seconds,
                offset_seconds=entry.offset_seconds,
                speed=entry.speed,
                volume=entry.volume,
                fade_in_seconds=entry.fade_in_seconds,
                fade_out_seconds=entry.fade_out_seconds,
                loop=entry.loop,
                loop_source_duration_seconds=entry.loop_source_duration_seconds,
            )
        )

    command = [
        _ffmpeg_executable(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y" if overwrite else "-n",
    ]
    for entry in resolved_entries:
        if entry.loop and entry.loop_source_duration_seconds is None:
            command.extend(["-stream_loop", "-1"])
        command.extend(["-protocol_whitelist", "file,pipe", "-i", str(entry.path)])
    filters: list[str] = []
    for index, entry in enumerate(resolved_entries):
        parts = [
            (
                f"[{index}:a]atrim=start={_format_seconds(entry.source_in_seconds)}:"
                f"duration={_format_seconds(entry.loop_source_duration_seconds or entry.duration_seconds * entry.speed)}"
            ),
            "asetpts=PTS-STARTPTS",
            "aresample=48000",
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
        ]
        if entry.loop_source_duration_seconds is not None:
            samples = max(1, round(entry.loop_source_duration_seconds * 48_000))
            parts.append(f"aloop=loop=-1:size={samples}")
        parts.extend(_atempo_filter_chain(entry.speed))
        parts.extend(
            [
                f"apad=pad_dur={_format_seconds(entry.duration_seconds)}",
                f"atrim=duration={_format_seconds(entry.duration_seconds)}",
                f"volume={entry.volume:.4f}",
            ]
        )
        if entry.fade_in_seconds:
            parts.append(f"afade=t=in:st=0:d={_format_seconds(entry.fade_in_seconds)}")
        if entry.fade_out_seconds:
            parts.append(
                "afade=t=out:st="
                f"{_format_seconds(entry.duration_seconds - entry.fade_out_seconds)}:"
                f"d={_format_seconds(entry.fade_out_seconds)}"
            )
        delay = round(entry.offset_seconds * 1000)
        parts.extend(
            [
                f"adelay={delay}:all=1",
                f"apad=pad_dur={_format_seconds(total_duration)}",
                f"atrim=duration={_format_seconds(total_duration)}[stem{index}]",
            ]
        )
        filters.append(",".join(parts))
    if len(resolved_entries) == 1:
        filters.append(
            f"[stem0]atrim=duration={_format_seconds(total_duration)},alimiter=limit=0.95[stemout]"
        )
    else:
        inputs = "".join(f"[stem{index}]" for index in range(len(resolved_entries)))
        filters.append(
            f"{inputs}amix=inputs={len(resolved_entries)}:duration=longest:"
            f"dropout_transition=0:normalize=0,atrim=duration={_format_seconds(total_duration)},"
            "alimiter=limit=0.95[stemout]"
        )
    command.extend(["-filter_complex", ";".join(filters), "-map", "[stemout]", "-vn"])
    if output.suffix.lower() == ".wav":
        command.extend(["-c:a", "pcm_s16le"])
    else:
        command.extend(["-c:a", "libmp3lame", "-b:a", "192k"])
    command.extend(
        [
            "-ar",
            "48000",
            "-t",
            _format_seconds(total_duration),
            str(output),
        ]
    )
    return StemExportPlan(
        kind=kind,
        output_path=output,
        command=tuple(command),
        duration_seconds=total_duration,
        source_count=len(resolved_entries),
    )


def render_stem_export(
    plan: StemExportPlan,
    *,
    timeout_seconds: float = 600,
) -> StemExportResult:
    """Execute a plan returned by :func:`build_stem_export_plan` without a shell."""

    if not 0 < timeout_seconds <= 3_600:
        raise VideoProjectRenderingError("stem 渲染超时时间必须在 0 到 3600 秒之间")
    plan.output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            list(plan.command),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg stem 导出超时") from exc
    except subprocess.CalledProcessError as exc:
        stderr = " ".join((exc.stderr or "").split())[-1600:]
        raise RuntimeError(f"FFmpeg stem 导出失败：{stderr}") from exc
    if not plan.output_path.is_file() or plan.output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg 未生成有效 stem 文件")
    return StemExportResult(
        kind=plan.kind,
        output_path=plan.output_path,
        duration_seconds=plan.duration_seconds,
        source_count=plan.source_count,
    )
