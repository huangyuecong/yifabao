import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import wave
import zipfile
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from docx import Document
from PIL import Image, ImageDraw, ImageOps
from sqlalchemy import func, select, union_all, update

from agent_core import JobStatus, LocalObjectStorage, ThreadTaskDispatcher

from .config import load_settings
from .db import SessionLocal
from .models import (
    AiEditProposal,
    Artifact,
    ArtifactLink,
    AssetRef,
    AuditLog,
    ContentTask,
    GenerationRun,
    Job,
    LibraryItem,
    MediaDerivative,
    ProjectAsset,
    ResourceEntry,
    ResourceVersion,
    TaskStage,
    VideoProject,
    VideoProjectRevision,
    VideoUploadChunk,
    VideoUploadSession,
    new_id,
)
from .provider_native_video import (
    provider_native_video_marker,
    resolve_provider_native_video_reference,
)
from .publishable_text import artifact_content
from .time_utils import ensure_utc

settings = load_settings()
storage = LocalObjectStorage(settings.data_dir / "objects")


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_float_env(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def create_task_dispatcher() -> ThreadTaskDispatcher:
    """Build the content worker pool; explicit environment settings take precedence."""
    return ThreadTaskDispatcher(
        max_workers=_positive_int_env("CONTENT_JOB_MAX_WORKERS", 4),
        max_queue_size=_positive_int_env("CONTENT_JOB_MAX_QUEUE", 1000),
        timeout_seconds=_positive_float_env("CONTENT_JOB_TIMEOUT_SECONDS"),
    )


dispatcher = create_task_dispatcher()


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def load(value: str):
    return json.loads(value)


def audit(
    session,
    owner_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    **details,
) -> None:
    session.add(
        AuditLog(
            owner_id=owner_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details_json=dump(details),
        )
    )


def can_access(task: ContentTask, user_id: str, department: str) -> bool:
    return (
        task.owner_id == user_id
        or user_id in load(task.allowed_users_json)
        or department in load(task.allowed_departments_json)
    )


def latest_task_update_times(
    session,
    tasks: Iterable[ContentTask],
) -> dict[str, datetime]:
    """Return the latest user-visible content change for each task.

    ``ContentTask.updated_at`` predates the video editor and immutable artifact
    history.  Those related records deliberately have their own lifecycle, so a
    read-time aggregate keeps the history API correct without introducing
    fragile cross-table write hooks.
    """

    task_rows = list(tasks)
    if not task_rows:
        return {}
    task_ids = [task.id for task in task_rows]
    event_queries = (
        select(
            ContentTask.id.label("task_id"),
            ContentTask.updated_at.label("event_at"),
        ),
        select(Artifact.task_id, Artifact.created_at),
        select(ArtifactLink.target_task_id, ArtifactLink.created_at),
        select(GenerationRun.task_id, GenerationRun.created_at),
        select(GenerationRun.task_id, GenerationRun.completed_at),
        select(TaskStage.task_id, TaskStage.updated_at),
        select(Job.task_id, Job.updated_at),
        select(VideoProject.task_id, VideoProject.updated_at),
        select(VideoProject.task_id, VideoProjectRevision.created_at).join(
            VideoProject,
            VideoProject.id == VideoProjectRevision.project_id,
        ),
        select(VideoProject.task_id, ProjectAsset.updated_at).join(
            ProjectAsset, ProjectAsset.project_id == VideoProject.id
        ),
        select(VideoProject.task_id, MediaDerivative.updated_at)
        .join(ProjectAsset, ProjectAsset.project_id == VideoProject.id)
        .join(MediaDerivative, MediaDerivative.project_asset_id == ProjectAsset.id),
        select(VideoProject.task_id, AiEditProposal.created_at).join(
            AiEditProposal,
            AiEditProposal.project_id == VideoProject.id,
        ),
        select(VideoProject.task_id, AiEditProposal.resolved_at).join(
            AiEditProposal,
            AiEditProposal.project_id == VideoProject.id,
        ),
        select(VideoProject.task_id, VideoUploadSession.updated_at).join(
            VideoUploadSession,
            VideoUploadSession.project_id == VideoProject.id,
        ),
        select(VideoProject.task_id, VideoUploadSession.completed_at).join(
            VideoUploadSession,
            VideoUploadSession.project_id == VideoProject.id,
        ),
        select(VideoProject.task_id, VideoUploadChunk.created_at)
        .join(VideoUploadSession, VideoUploadSession.project_id == VideoProject.id)
        .join(
            VideoUploadChunk,
            VideoUploadChunk.upload_session_id == VideoUploadSession.id,
        ),
        select(Artifact.task_id, AuditLog.created_at)
        .join(AuditLog, AuditLog.resource_id == Artifact.id)
        .where(AuditLog.resource_type == "artifact"),
        select(ArtifactLink.target_task_id, AuditLog.created_at)
        .join(AuditLog, AuditLog.resource_id == ArtifactLink.artifact_id)
        .where(AuditLog.resource_type == "artifact"),
        select(VideoProject.task_id, AuditLog.created_at)
        .join(AuditLog, AuditLog.resource_id == VideoProject.id)
        .where(AuditLog.resource_type == "video_project"),
        select(VideoProject.task_id, AuditLog.created_at)
        .join(ProjectAsset, ProjectAsset.project_id == VideoProject.id)
        .join(AuditLog, AuditLog.resource_id == ProjectAsset.id)
        .where(AuditLog.resource_type == "project_asset"),
        select(Job.task_id, AuditLog.created_at)
        .join(AuditLog, AuditLog.resource_id == Job.id)
        .where(AuditLog.resource_type == "job"),
        select(ContentTask.id, AuditLog.created_at)
        .join(AuditLog, AuditLog.resource_id == ContentTask.id)
        .where(AuditLog.resource_type == "content_task"),
    )
    events = union_all(*event_queries).subquery("task_update_events")
    rows = session.execute(
        select(events.c.task_id, func.max(events.c.event_at))
        .where(events.c.task_id.in_(task_ids))
        .group_by(events.c.task_id)
    )
    latest: dict[str, datetime] = {}
    for task_id, event_at in rows:
        if event_at is not None:
            latest[task_id] = ensure_utc(event_at)
    return latest


def next_version(session, task_id: str, kind: str) -> int:
    current = session.scalar(
        select(func.max(Artifact.version)).where(
            Artifact.task_id == task_id,
            Artifact.kind == kind,
        )
    )
    return (current or 0) + 1


class VideoProjectConflictError(RuntimeError):
    pass


class VideoProjectValidationError(ValueError):
    pass


VIDEO_TRACK_NAMES = {
    "video": "画面",
    "overlay": "叠加",
    "subtitle": "字幕",
    "voice": "配音",
    "music": "背景音乐",
    "sfx": "音效",
}
VIDEO_TRACK_ID_ALIASES = {f"track-{kind}": kind for kind in VIDEO_TRACK_NAMES}


def _project_now() -> datetime:
    return datetime.now(UTC)


def default_video_timeline(
    *,
    width: int = 1280,
    height: int = 720,
    fps: int = 25,
) -> dict:
    divisor = math.gcd(max(1, width), max(1, height))
    return {
        "canvas": {
            "width": width,
            "height": height,
            "fps": fps,
            "aspect_ratio": f"{width // divisor}:{height // divisor}",
        },
        "tracks": [
            {
                "id": f"{kind}-{uuid4()}",
                "kind": kind,
                "name": label,
                "muted": False,
                "locked": False,
                "items": [],
            }
            for kind, label in VIDEO_TRACK_NAMES.items()
        ],
    }


def _track_by_kind(timeline: dict, kind: str) -> dict:
    return next(track for track in timeline["tracks"] if track["kind"] == kind)


def _source_fields(session, source_id: str) -> dict:
    if session.get(Artifact, source_id) is not None:
        return {
            "source_type": "artifact",
            "source_id": source_id,
            "artifact_id": source_id,
        }
    if session.get(AssetRef, source_id) is not None:
        return {
            "source_type": "asset_ref",
            "source_id": source_id,
            "asset_ref_id": source_id,
        }
    if session.get(LibraryItem, source_id) is not None:
        return {
            "source_type": "library_item",
            "source_id": source_id,
            "library_item_id": source_id,
        }
    if session.get(ResourceEntry, source_id) is not None:
        return {
            "source_type": "resource_entry",
            "source_id": source_id,
            "resource_entry_id": source_id,
        }
    return {"source_type": "legacy", "source_id": source_id}


def ensure_project_asset(
    session,
    project: VideoProject,
    *,
    source_type: str,
    source_id: str,
    role: str,
    label: str,
    created_by: str,
    asset_ref_id: str | None = None,
    artifact_id: str | None = None,
    library_item_id: str | None = None,
    resource_entry_id: str | None = None,
    metadata: dict | None = None,
) -> ProjectAsset:
    existing = session.scalar(
        select(ProjectAsset).where(
            ProjectAsset.project_id == project.id,
            ProjectAsset.source_type == source_type,
            ProjectAsset.source_id == source_id,
        )
    )
    if existing is not None:
        if not existing.active:
            existing.active = True
        return existing
    item = ProjectAsset(
        project_id=project.id,
        source_type=source_type,
        source_id=source_id,
        asset_ref_id=asset_ref_id,
        artifact_id=artifact_id,
        library_item_id=library_item_id,
        resource_entry_id=resource_entry_id,
        role=role,
        label=label,
        metadata_json=dump(metadata or {}),
        created_by=created_by,
    )
    session.add(item)
    session.flush()
    return item


def _legacy_project_asset(
    session,
    project: VideoProject,
    source_id: str | None,
    *,
    role: str,
    label: str,
    created_by: str,
) -> ProjectAsset | None:
    normalized = str(source_id or "").strip()
    if not normalized:
        return None
    fields = _source_fields(session, normalized)
    return ensure_project_asset(
        session,
        project,
        role=role,
        label=label,
        created_by=created_by,
        **fields,
    )


def _timeline_item(
    *,
    project_asset_id: str | None,
    label: str,
    start_seconds: float,
    duration_seconds: float,
    content: str | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        "id": str(uuid4()),
        "project_asset_id": project_asset_id,
        "label": label,
        "start_seconds": round(max(0.0, start_seconds), 6),
        "duration_seconds": round(max(0.01, duration_seconds), 6),
        "trim_in_seconds": 0.0,
        "trim_out_seconds": 0.0,
        "volume": 1.0,
        "speed": 1.0,
        "enabled": True,
        "content": content,
        "transform": {},
        "metadata": metadata or {},
    }


def _latest_task_artifact(session, task_id: str, kind: str) -> Artifact | None:
    return session.scalar(
        select(Artifact)
        .where(
            Artifact.task_id == task_id,
            Artifact.kind == kind,
            Artifact.status.not_in(("discarded", "legacy_mock")),
        )
        .order_by(Artifact.created_at.desc())
    )


def _legacy_video_timeline(
    session,
    project: VideoProject,
    task: ContentTask,
    created_by: str,
) -> tuple[dict, str]:
    video = _latest_task_artifact(session, task.id, "video")
    video_content = load(video.content_json) if video is not None else {}
    composition = video_content.get("composition_spec")
    if not isinstance(composition, dict):
        composition = {}
    width = int(composition.get("width") or video_content.get("width") or 1280)
    height = int(composition.get("height") or video_content.get("height") or 720)
    fps = int(composition.get("fps") or 25)
    timeline = default_video_timeline(width=width, height=height, fps=fps)
    video_track = _track_by_kind(timeline, "video")
    current_time = 0.0

    def append_video_source(
        source_id: str | None,
        label: str,
        duration: float,
        metadata: dict | None = None,
    ) -> None:
        nonlocal current_time
        remaining = 600 - current_time
        if remaining <= 0:
            return
        original_duration = max(0.01, duration)
        duration = min(original_duration, remaining)
        item_metadata = deepcopy(metadata or {})
        if duration < original_duration:
            item_metadata["legacy_duration_truncated"] = True
        asset = _legacy_project_asset(
            session,
            project,
            source_id,
            role="video",
            label=label,
            created_by=created_by,
        )
        video_track["items"].append(
            _timeline_item(
                project_asset_id=asset.id if asset else None,
                label=label,
                start_seconds=current_time,
                duration_seconds=duration,
                content=None if asset else label,
                metadata=item_metadata,
            )
        )
        current_time += duration

    if composition:
        intro_id = composition.get("intro_asset_id")
        if intro_id:
            append_video_source(
                str(intro_id),
                "片头",
                float(composition.get("intro_duration_seconds") or 3),
                {"legacy_role": "intro"},
            )
        clips = composition.get("clips")
        if isinstance(clips, list):
            for index, clip in enumerate(clips, start=1):
                if not isinstance(clip, dict):
                    continue
                source_id = clip.get("source_artifact_id") or clip.get("source_asset_id")
                duration = float(clip.get("duration_seconds") or 3)
                append_video_source(
                    str(source_id) if source_id else None,
                    f"镜头 {index}",
                    duration,
                    {
                        "transition": clip.get("transition", "none"),
                        "transition_duration_seconds": float(
                            clip.get("transition_duration_seconds") or 0
                        ),
                        "fit_mode": clip.get("fit_mode")
                        or composition.get("fit_mode")
                        or "contain",
                    },
                )
        outro_id = composition.get("outro_asset_id")
        if outro_id:
            append_video_source(
                str(outro_id),
                "片尾",
                float(composition.get("outro_duration_seconds") or 3),
                {"legacy_role": "outro"},
            )

        full_duration = min(
            600,
            max(
                current_time,
                float(video_content.get("duration_seconds") or 0),
                0.01,
            ),
        )
        supporting_assets = (
            (
                "voice",
                "配音",
                composition.get("voice_artifact_id") or composition.get("voice_asset_id"),
            ),
            ("subtitle", "字幕", composition.get("subtitle_artifact_id")),
            ("music", "背景音乐", composition.get("background_music_asset_id")),
            ("overlay", "Logo", composition.get("logo_asset_id")),
        )
        for track_kind, label, source_id in supporting_assets:
            if not source_id:
                continue
            asset = _legacy_project_asset(
                session,
                project,
                str(source_id),
                role=track_kind,
                label=label,
                created_by=created_by,
            )
            metadata = {}
            if track_kind == "overlay":
                metadata = {
                    "position": composition.get("logo_position", "top_right"),
                    "width": composition.get("logo_width", 160),
                }
            _track_by_kind(timeline, track_kind)["items"].append(
                _timeline_item(
                    project_asset_id=asset.id if asset else None,
                    label=label,
                    start_seconds=0,
                    duration_seconds=full_duration,
                    metadata=metadata,
                )
            )
        return timeline, "legacy_import"

    storyboard = _latest_task_artifact(session, task.id, "storyboard")
    if storyboard is None:
        return timeline, "initial"
    storyboard_content = load(storyboard.content_json)
    shots = storyboard_content.get("shots", [])
    if not isinstance(shots, list):
        return timeline, "initial"
    for index, shot in enumerate(shots, start=1):
        if current_time >= 600:
            break
        if not isinstance(shot, dict):
            continue
        original_duration = float(shot.get("duration") or shot.get("duration_seconds") or 3)
        duration = min(max(0.01, original_duration), 600 - current_time)
        source_ids = shot.get("source_ids") or []
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        source_id = str(source_ids[0]) if source_ids else None
        asset = _legacy_project_asset(
            session,
            project,
            source_id,
            role="video",
            label=f"镜头 {index}",
            created_by=created_by,
        )
        visual = str(shot.get("visual") or shot.get("description") or f"镜头 {index}")
        video_track["items"].append(
            _timeline_item(
                project_asset_id=asset.id if asset else None,
                label=f"镜头 {index}",
                start_seconds=current_time,
                duration_seconds=duration,
                content=None if asset else visual,
                metadata={
                    "storyboard_artifact_id": storyboard.id,
                    "sequence": shot.get("sequence", index),
                    "transition": shot.get("transition", "none"),
                    "legacy_duration_truncated": duration < original_duration,
                },
            )
        )
        subtitle = str(shot.get("subtitle") or "").strip()
        if subtitle:
            _track_by_kind(timeline, "subtitle")["items"].append(
                _timeline_item(
                    project_asset_id=None,
                    label=f"字幕 {index}",
                    start_seconds=current_time,
                    duration_seconds=duration,
                    content=subtitle,
                )
            )
        current_time += duration
    return timeline, "legacy_import" if video_track["items"] else "initial"


def sync_confirmed_workflow_images_to_project(
    session,
    project: VideoProject,
    task: ContentTask,
    created_by: str,
) -> list[ProjectAsset]:
    """Expose confirmed workflow images in the video's project asset library.

    The image stage's confirmed selection is authoritative.  Synchronization is
    deliberately additive and idempotent: an asset the user archived remains
    archived instead of being silently brought back on the next project load.
    """

    stage = session.scalar(
        select(TaskStage).where(
            TaskStage.task_id == task.id,
            TaskStage.stage_key == "image",
        )
    )
    if stage is None or stage.state != "confirmed":
        return []
    try:
        confirmed = load(stage.confirmed_artifact_ids_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(confirmed, dict):
        return []
    raw_ids = confirmed.get("image", [])
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    if not isinstance(raw_ids, list):
        return []

    synced: list[ProjectAsset] = []
    for artifact_id in dict.fromkeys(item for item in raw_ids if isinstance(item, str)):
        artifact = session.get(Artifact, artifact_id)
        if (
            artifact is None
            or artifact.kind != "image"
            or artifact.status != "confirmed"
            or not artifact.storage_key
        ):
            continue
        if artifact.task_id != task.id:
            linked = session.scalar(
                select(ArtifactLink.id).where(
                    ArtifactLink.target_task_id == task.id,
                    ArtifactLink.artifact_id == artifact.id,
                    ArtifactLink.stage_key == "image",
                )
            )
            if linked is None:
                continue
        try:
            content = load(artifact.content_json or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(content, dict) or content.get("legacy_mock"):
            continue
        media_type = str(content.get("media_type") or "").strip().lower()
        guessed_media_type = mimetypes.guess_type(artifact.storage_key)[0] or ""
        if not media_type.startswith("image/") and not guessed_media_type.startswith("image/"):
            continue
        metadata = {
            "source": "workflow-confirmed",
            "workflow_stage": "image",
            "source_task_id": artifact.task_id,
            "artifact_version": artifact.version,
            "media_kind": "image",
            "media_type": media_type or guessed_media_type,
        }
        existing = session.scalar(
            select(ProjectAsset).where(
                ProjectAsset.project_id == project.id,
                ProjectAsset.source_type == "artifact",
                ProjectAsset.source_id == artifact.id,
            )
        )
        if existing is not None:
            # An explicit archive is a user decision, not a cache miss.
            if existing.active:
                existing_metadata = load(existing.metadata_json or "{}")
                existing_metadata = existing_metadata if isinstance(existing_metadata, dict) else {}
                updated_metadata = {**existing_metadata, **metadata}
                if existing.metadata_json != dump(updated_metadata):
                    existing.metadata_json = dump(updated_metadata)
                # The first sync defaults the image to the main video track,
                # but a later role change (overlay/logo/intro/outro) is an
                # explicit editing decision and must survive project reloads.
                existing.artifact_id = artifact.id
            synced.append(existing)
            continue
        synced.append(
            ensure_project_asset(
                session,
                project,
                source_type="artifact",
                source_id=artifact.id,
                artifact_id=artifact.id,
                role="video",
                label=f"已确认图片 v{artifact.version}",
                created_by=created_by,
                metadata=metadata,
            )
        )
    session.flush()
    return synced


def create_or_get_video_project(
    session,
    task: ContentTask,
    created_by: str,
) -> tuple[VideoProject, bool]:
    existing = session.scalar(select(VideoProject).where(VideoProject.task_id == task.id))
    if existing is not None:
        sync_confirmed_workflow_images_to_project(session, existing, task, created_by)
        return existing, False
    revision_id = new_id()
    project = VideoProject(
        task_id=task.id,
        current_revision=1,
        current_revision_id=revision_id,
        created_by=created_by,
    )
    session.add(project)
    session.flush()
    sync_confirmed_workflow_images_to_project(session, project, task, created_by)
    timeline, source = _legacy_video_timeline(session, project, task, created_by)
    session.add(
        VideoProjectRevision(
            id=revision_id,
            project_id=project.id,
            revision=1,
            parent_revision_id=None,
            source=source,
            timeline_json=dump(timeline),
            operations_json="[]",
            created_by=created_by,
        )
    )
    session.flush()
    return project, True


def _find_timeline_item(timeline: dict, item_id: str) -> tuple[dict, int, dict]:
    for track in timeline.get("tracks", []):
        for index, item in enumerate(track.get("items", [])):
            if item.get("id") == item_id:
                return track, index, item
    raise VideoProjectValidationError(f"时间线片段不存在：{item_id}")


def _find_timeline_track(
    timeline: dict,
    *,
    track_id: str | None,
    track_kind: str | None = None,
) -> dict:
    tracks = timeline.get("tracks", [])
    if track_id:
        track = next((item for item in tracks if item.get("id") == track_id), None)
        if track is None:
            # A brand-new editor starts with deterministic local IDs before the
            # project exists.  The persisted timeline deliberately uses unique
            # IDs, so accept only those six well-known provisional aliases and
            # resolve them to the matching server track.
            alias_kind = VIDEO_TRACK_ID_ALIASES.get(track_id)
            if alias_kind is not None:
                if track_kind and track_kind != alias_kind:
                    raise VideoProjectValidationError("track_id 与 track_kind 不匹配")
                track = next(
                    (item for item in tracks if item.get("kind") == alias_kind),
                    None,
                )
        if track is None:
            raise VideoProjectValidationError(f"时间线轨道不存在：{track_id}")
        if track_kind and track.get("kind") != track_kind:
            raise VideoProjectValidationError("track_id 与 track_kind 不匹配")
        return track
    desired_kind = track_kind or "video"
    track = next((item for item in tracks if item.get("kind") == desired_kind), None)
    if track is None:
        raise VideoProjectValidationError(f"时间线没有 {desired_kind} 轨道")
    return track


def _timeline_item_ids(timeline: dict) -> set[str]:
    return {
        str(item.get("id"))
        for track in timeline.get("tracks", [])
        for item in track.get("items", [])
        if item.get("id")
    }


def _validate_timeline_duration(timeline: dict) -> None:
    maximum_end = max(
        (
            float(item.get("start_seconds") or 0) + float(item.get("duration_seconds") or 0)
            for track in timeline.get("tracks", [])
            for item in track.get("items", [])
        ),
        default=0,
    )
    if maximum_end > 600.000001:
        raise VideoProjectValidationError("视频工程时间线最长不能超过 600 秒")


def _validate_timeline_project_assets(
    session,
    project: VideoProject,
    timeline: dict,
) -> None:
    project_asset_ids = {
        str(item["project_asset_id"])
        for track in timeline.get("tracks", [])
        for item in track.get("items", [])
        if item.get("project_asset_id")
    }
    if not project_asset_ids:
        return
    assets = {
        item.id: item
        for item in session.scalars(
            select(ProjectAsset).where(ProjectAsset.id.in_(project_asset_ids))
        ).all()
    }
    invalid = sorted(
        asset_id
        for asset_id in project_asset_ids
        if asset_id not in assets
        or assets[asset_id].project_id != project.id
        or not assets[asset_id].active
    )
    if invalid:
        raise VideoProjectValidationError("时间线引用了不存在、已归档或属于其他工程的素材")


def _canonical_timeline_item(raw: dict) -> dict:
    return {
        "id": str(raw.get("id") or uuid4()),
        "project_asset_id": raw.get("project_asset_id"),
        "label": str(raw.get("label") or ""),
        "start_seconds": round(float(raw.get("start_seconds") or 0), 6),
        "duration_seconds": round(float(raw["duration_seconds"]), 6),
        "trim_in_seconds": round(float(raw.get("trim_in_seconds") or 0), 6),
        "trim_out_seconds": round(float(raw.get("trim_out_seconds") or 0), 6),
        "volume": round(float(raw.get("volume", 1)), 6),
        "speed": round(float(raw.get("speed", 1)), 6),
        "enabled": bool(raw.get("enabled", True)),
        "content": raw.get("content"),
        "transform": deepcopy(raw.get("transform") or {}),
        "metadata": deepcopy(raw.get("metadata") or {}),
    }


def apply_video_project_operations(
    session,
    project: VideoProject,
    base_timeline: dict,
    operations: list[dict],
) -> tuple[dict, list[dict]]:
    timeline = deepcopy(base_timeline)
    normalized_operations: list[dict] = []
    if any(operation.get("type") == "restore" for operation in operations) and len(operations) != 1:
        raise VideoProjectValidationError("restore 必须作为单独操作提交")

    for raw_operation in operations:
        operation = deepcopy(raw_operation)
        operation_type = operation.get("type")
        if operation_type == "restore":
            revision = session.get(VideoProjectRevision, operation.get("revision_id"))
            if revision is None or revision.project_id != project.id or revision.revision is None:
                raise VideoProjectValidationError("只能恢复本工程的已提交版本")
            timeline = deepcopy(load(revision.timeline_json))
            normalized_operations.append(operation)
            continue

        if operation_type == "update_canvas":
            changes = operation.get("changes") or {}
            if not changes:
                raise VideoProjectValidationError("update_canvas 至少需要一个变更字段")
            canvas = timeline.setdefault("canvas", {})
            for field, value in changes.items():
                canvas[field] = deepcopy(value)
            if {"width", "height"} & set(changes) and "aspect_ratio" not in changes:
                width = int(canvas["width"])
                height = int(canvas["height"])
                divisor = math.gcd(width, height)
                canvas["aspect_ratio"] = f"{width // divisor}:{height // divisor}"
            normalized_operations.append(operation)
            continue

        if operation_type == "update_track":
            changes = operation.get("changes") or {}
            if not changes:
                raise VideoProjectValidationError("update_track 至少需要一个变更字段")
            track = _find_timeline_track(
                timeline,
                track_id=operation.get("track_id"),
            )
            for field, value in changes.items():
                track[field] = deepcopy(value)
            operation["track_id"] = track["id"]
            normalized_operations.append(operation)
            continue

        if operation_type == "add":
            track = _find_timeline_track(
                timeline,
                track_id=operation.get("track_id"),
                track_kind=operation.get("track_kind"),
            )
            item = _canonical_timeline_item(operation.get("item") or {})
            if item["id"] in _timeline_item_ids(timeline):
                raise VideoProjectValidationError(f"时间线片段 ID 已存在：{item['id']}")
            index = operation.get("index")
            if index is None:
                index = len(track["items"])
            if not 0 <= int(index) <= len(track["items"]):
                raise VideoProjectValidationError("add.index 超出轨道范围")
            track["items"].insert(int(index), item)
            operation["track_id"] = track["id"]
            operation["track_kind"] = track["kind"]
            operation["index"] = int(index)
            operation["item"] = deepcopy(item)
            normalized_operations.append(operation)
            continue

        if operation_type == "remove":
            track, index, _ = _find_timeline_item(timeline, operation["item_id"])
            track["items"].pop(index)
            normalized_operations.append(operation)
            continue

        if operation_type == "move":
            source_track, source_index, item = _find_timeline_item(timeline, operation["item_id"])
            source_track["items"].pop(source_index)
            target_track = _find_timeline_track(
                timeline,
                track_id=operation.get("track_id") or source_track["id"],
            )
            index = operation.get("index")
            if index is None:
                index = len(target_track["items"])
            if not 0 <= int(index) <= len(target_track["items"]):
                raise VideoProjectValidationError("move.index 超出轨道范围")
            if operation.get("start_seconds") is not None:
                item["start_seconds"] = round(float(operation["start_seconds"]), 6)
            target_track["items"].insert(int(index), item)
            operation["track_id"] = target_track["id"]
            operation["index"] = int(index)
            normalized_operations.append(operation)
            continue

        if operation_type == "trim":
            _, _, item = _find_timeline_item(timeline, operation["item_id"])
            old_trim_in = float(item.get("trim_in_seconds") or 0)
            old_trim_out = float(item.get("trim_out_seconds") or 0)
            speed = float(item.get("speed") or 1)
            source_span = float(item["duration_seconds"]) * speed + old_trim_in + old_trim_out
            trim_in = float(operation["trim_in_seconds"])
            trim_out = float(operation["trim_out_seconds"])
            duration = operation.get("duration_seconds")
            if duration is None:
                duration = (source_span - trim_in - trim_out) / speed
            if float(duration) <= 0:
                raise VideoProjectValidationError("裁剪后片段时长必须大于 0")
            item["trim_in_seconds"] = round(trim_in, 6)
            item["trim_out_seconds"] = round(trim_out, 6)
            item["duration_seconds"] = round(float(duration), 6)
            operation["duration_seconds"] = item["duration_seconds"]
            normalized_operations.append(operation)
            continue

        if operation_type == "split":
            track, index, item = _find_timeline_item(timeline, operation["item_id"])
            split_at = float(operation["at_seconds"])
            old_duration = float(item["duration_seconds"])
            if not 0 < split_at < old_duration:
                raise VideoProjectValidationError("拆分点必须位于片段有效时长内")
            new_item_id = str(operation.get("new_item_id") or uuid4())
            if new_item_id in _timeline_item_ids(timeline):
                raise VideoProjectValidationError(f"时间线片段 ID 已存在：{new_item_id}")
            speed = float(item.get("speed") or 1)
            second = deepcopy(item)
            second["id"] = new_item_id
            second["start_seconds"] = round(float(item["start_seconds"]) + split_at, 6)
            second["duration_seconds"] = round(old_duration - split_at, 6)
            second["trim_in_seconds"] = round(
                float(item.get("trim_in_seconds") or 0) + split_at * speed,
                6,
            )
            item["duration_seconds"] = round(split_at, 6)
            item["trim_out_seconds"] = round(
                float(item.get("trim_out_seconds") or 0) + (old_duration - split_at) * speed,
                6,
            )
            track["items"].insert(index + 1, second)
            operation["new_item_id"] = new_item_id
            normalized_operations.append(operation)
            continue

        if operation_type == "update":
            _, _, item = _find_timeline_item(timeline, operation["item_id"])
            changes = operation.get("changes") or {}
            if not changes:
                raise VideoProjectValidationError("update 操作至少需要一个变更字段")
            for field, value in changes.items():
                item[field] = deepcopy(value)
            normalized_operations.append(operation)
            continue

        raise VideoProjectValidationError(f"不支持的编辑操作：{operation_type}")

    _validate_timeline_project_assets(session, project, timeline)
    _validate_timeline_duration(timeline)
    return timeline, normalized_operations


def _current_video_revision(session, project: VideoProject) -> VideoProjectRevision:
    revision = session.get(VideoProjectRevision, project.current_revision_id)
    if revision is None or revision.project_id != project.id:
        raise RuntimeError("视频工程当前版本数据不完整")
    return revision


def commit_video_project_revision(
    session,
    project: VideoProject,
    *,
    expected_revision: int,
    timeline: dict,
    operations: list[dict],
    source: str,
    created_by: str,
) -> VideoProjectRevision:
    if project.current_revision != expected_revision:
        raise VideoProjectConflictError(
            f"版本冲突：当前版本为 {project.current_revision}，请求基于版本 {expected_revision}"
        )
    parent_revision_id = project.current_revision_id
    revision_id = new_id()
    next_revision = expected_revision + 1
    changed_at = _project_now()
    result = session.execute(
        update(VideoProject)
        .where(
            VideoProject.id == project.id,
            VideoProject.current_revision == expected_revision,
            VideoProject.current_revision_id == parent_revision_id,
        )
        .values(
            current_revision=next_revision,
            current_revision_id=revision_id,
            updated_at=changed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        session.rollback()
        raise VideoProjectConflictError("视频工程已被其他操作更新，请刷新后重试")
    revision = VideoProjectRevision(
        id=revision_id,
        project_id=project.id,
        revision=next_revision,
        parent_revision_id=parent_revision_id,
        source=source,
        timeline_json=dump(timeline),
        operations_json=dump(operations),
        created_by=created_by,
    )
    session.add(revision)
    session.flush()
    project.current_revision = next_revision
    project.current_revision_id = revision_id
    project.updated_at = changed_at
    return revision


def edit_video_project(
    session,
    project: VideoProject,
    *,
    expected_revision: int,
    operations: list[dict],
    created_by: str,
) -> VideoProjectRevision:
    base = _current_video_revision(session, project)
    timeline, normalized = apply_video_project_operations(
        session,
        project,
        load(base.timeline_json),
        operations,
    )
    source = "restore" if normalized[0].get("type") == "restore" else "human"
    return commit_video_project_revision(
        session,
        project,
        expected_revision=expected_revision,
        timeline=timeline,
        operations=normalized,
        source=source,
        created_by=created_by,
    )


def create_ai_edit_proposal(
    session,
    project: VideoProject,
    *,
    expected_revision: int,
    prompt: str,
    operations: list[dict],
    created_by: str,
) -> AiEditProposal:
    if project.current_revision != expected_revision:
        raise VideoProjectConflictError(
            f"版本冲突：当前版本为 {project.current_revision}，请求基于版本 {expected_revision}"
        )
    base = _current_video_revision(session, project)
    timeline, normalized = apply_video_project_operations(
        session,
        project,
        load(base.timeline_json),
        operations,
    )
    candidate = VideoProjectRevision(
        project_id=project.id,
        revision=None,
        parent_revision_id=base.id,
        source="ai_candidate",
        timeline_json=dump(timeline),
        operations_json=dump(normalized),
        created_by=created_by,
    )
    session.add(candidate)
    session.flush()
    proposal = AiEditProposal(
        project_id=project.id,
        base_revision_id=base.id,
        candidate_revision_id=candidate.id,
        prompt=prompt,
        operations_json=dump(normalized),
        created_by=created_by,
    )
    session.add(proposal)
    session.flush()
    return proposal


def adopt_ai_edit_proposal(
    session,
    project: VideoProject,
    proposal: AiEditProposal,
    *,
    expected_revision: int,
    selected_operation_ids: list[str],
    resolved_by: str,
) -> VideoProjectRevision:
    if proposal.status != "pending":
        raise VideoProjectConflictError("AI 修改提案已经处理")
    if (
        project.current_revision != expected_revision
        or project.current_revision_id != proposal.base_revision_id
    ):
        raise VideoProjectConflictError("视频工程已变化，该 AI 修改提案需要重新生成")
    operations = load(proposal.operations_json)
    available_ids = {operation.get("op_id") for operation in operations}
    selected_ids = set(selected_operation_ids)
    unknown = sorted(selected_ids - available_ids)
    if unknown:
        raise VideoProjectValidationError(f"提案中不存在这些操作：{', '.join(unknown)}")
    selected = [operation for operation in operations if operation.get("op_id") in selected_ids]
    base = session.get(VideoProjectRevision, proposal.base_revision_id)
    if base is None:
        raise RuntimeError("AI 修改提案的基础版本不存在")
    timeline, normalized = apply_video_project_operations(
        session,
        project,
        load(base.timeline_json),
        selected,
    )
    revision = commit_video_project_revision(
        session,
        project,
        expected_revision=expected_revision,
        timeline=timeline,
        operations=normalized,
        source="ai_adopt",
        created_by=resolved_by,
    )
    proposal.status = "adopted"
    proposal.selected_operation_ids_json = dump(selected_operation_ids)
    proposal.resolved_by = resolved_by
    proposal.resolved_at = _project_now()
    return revision


def reject_ai_edit_proposal(
    project: VideoProject,
    proposal: AiEditProposal,
    *,
    expected_revision: int,
    resolved_by: str,
) -> None:
    if proposal.status != "pending":
        raise VideoProjectConflictError("AI 修改提案已经处理")
    if (
        project.current_revision != expected_revision
        or project.current_revision_id != proposal.base_revision_id
    ):
        raise VideoProjectConflictError("视频工程已变化，该 AI 修改提案不能再拒绝")
    proposal.status = "rejected"
    proposal.selected_operation_ids_json = "[]"
    proposal.resolved_by = resolved_by
    proposal.resolved_at = _project_now()


def video_project_revision_json(revision: VideoProjectRevision) -> dict:
    return {
        "id": revision.id,
        "project_id": revision.project_id,
        "revision": revision.revision,
        "parent_revision_id": revision.parent_revision_id,
        "source": revision.source,
        "timeline": load(revision.timeline_json),
        "operations": load(revision.operations_json),
        "created_by": revision.created_by,
        "created_at": revision.created_at,
    }


def ai_edit_proposal_json(proposal: AiEditProposal) -> dict:
    return {
        "id": proposal.id,
        "project_id": proposal.project_id,
        "base_revision_id": proposal.base_revision_id,
        "candidate_revision_id": proposal.candidate_revision_id,
        "prompt": proposal.prompt,
        "operations": load(proposal.operations_json),
        "status": proposal.status,
        "selected_operation_ids": load(proposal.selected_operation_ids_json),
        "created_by": proposal.created_by,
        "resolved_by": proposal.resolved_by,
        "created_at": proposal.created_at,
        "resolved_at": proposal.resolved_at,
    }


def _positive_metadata_number(value: object) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _project_asset_timing_sources(
    session,
    item: ProjectAsset,
    metadata: dict,
) -> list[dict]:
    sources = [metadata]
    if item.asset_ref_id:
        asset_ref = session.get(AssetRef, item.asset_ref_id)
        if asset_ref is not None:
            asset_metadata = load(asset_ref.metadata_json)
            if isinstance(asset_metadata, dict):
                sources.append(asset_metadata)
    if item.artifact_id:
        artifact = session.get(Artifact, item.artifact_id)
        if artifact is not None:
            content = artifact_content(artifact.kind, load(artifact.content_json))
            if isinstance(content, dict):
                sources.append(content)
    if item.library_item_id:
        library_item = session.get(LibraryItem, item.library_item_id)
        if library_item is not None:
            entry = (
                session.get(ResourceEntry, library_item.resource_entry_id)
                if library_item.resource_entry_id
                else None
            )
            version = (
                session.scalar(
                    select(ResourceVersion).where(
                        ResourceVersion.entry_id == entry.id,
                        ResourceVersion.version == library_item.version,
                    )
                )
                if entry is not None
                else None
            )
            if version is not None:
                version_metadata = load(version.metadata_json)
                if isinstance(version_metadata, dict):
                    sources.append(version_metadata)
            if library_item.storage_key:
                asset_ref = session.scalar(
                    select(AssetRef).where(AssetRef.storage_key == library_item.storage_key)
                )
                if asset_ref is not None:
                    asset_metadata = load(asset_ref.metadata_json)
                    if isinstance(asset_metadata, dict):
                        sources.append(asset_metadata)
    if item.resource_entry_id:
        entry = session.get(ResourceEntry, item.resource_entry_id)
        version = (
            session.get(ResourceVersion, entry.current_version_id)
            if entry is not None and entry.current_version_id
            else None
        )
        if version is not None:
            version_metadata = load(version.metadata_json)
            if isinstance(version_metadata, dict):
                sources.append(version_metadata)
    return sources


def _normalize_project_asset_timing_metadata(
    session,
    item: ProjectAsset,
    metadata: dict,
) -> None:
    sources = _project_asset_timing_sources(session, item, metadata)
    if _positive_metadata_number(metadata.get("duration_seconds")) is None:
        for source in sources:
            probe = source.get("probe")
            provenance = source.get("provenance")
            request = provenance.get("request") if isinstance(provenance, dict) else None
            candidates = [source.get("duration_seconds")]
            if isinstance(probe, dict):
                candidates.append(probe.get("duration_seconds"))
            if isinstance(request, dict):
                candidates.append(request.get("duration_seconds"))
            duration = next(
                (
                    value
                    for candidate in candidates
                    if (value := _positive_metadata_number(candidate)) is not None
                ),
                None,
            )
            if duration is not None:
                metadata["duration_seconds"] = duration
                break
    if _positive_metadata_number(metadata.get("speed")) is None:
        for source in sources:
            provenance = source.get("provenance")
            request = provenance.get("request") if isinstance(provenance, dict) else None
            speed = _positive_metadata_number(source.get("speed"))
            if speed is None and isinstance(request, dict):
                speed = _positive_metadata_number(request.get("speed"))
            if speed is not None:
                metadata["speed"] = speed
                break


def project_asset_json(session, item: ProjectAsset) -> dict:
    derivatives = session.scalars(
        select(MediaDerivative)
        .where(MediaDerivative.project_asset_id == item.id)
        .order_by(MediaDerivative.created_at)
    ).all()
    raw_metadata = load(item.metadata_json)
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    _normalize_project_asset_timing_metadata(session, item, metadata)
    # Never echo a stored eligibility claim.  Derive it from the linked
    # Artifact on each response so historical AI outputs gain the capability
    # without a migration and stale/spoofed metadata cannot grant it.
    metadata.pop("provider_native_reference", None)
    native_reference = resolve_provider_native_video_reference(session, item)
    if native_reference is not None:
        metadata["provider_native_reference"] = provider_native_video_marker(native_reference)
        if native_reference.duration_seconds is not None:
            metadata["duration_seconds"] = native_reference.duration_seconds
    return {
        "id": item.id,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "asset_ref_id": item.asset_ref_id,
        "artifact_id": item.artifact_id,
        "library_item_id": item.library_item_id,
        "resource_entry_id": item.resource_entry_id,
        "role": item.role,
        "label": item.label,
        "original_url": (f"/api/v1/video-projects/{item.project_id}/assets/{item.id}/download"),
        "stream_url": (f"/api/v1/video-projects/{item.project_id}/assets/{item.id}/stream"),
        "metadata": metadata,
        "active": item.active,
        "derivatives": [
            {
                "id": derivative.id,
                "kind": derivative.kind,
                "variant": derivative.variant,
                "status": derivative.status,
                "download_url": (
                    f"/api/v1/media-derivatives/{derivative.id}/download"
                    if derivative.storage_key
                    else None
                ),
                "media_type": derivative.media_type,
                "checksum": derivative.checksum,
                "metadata": load(derivative.metadata_json),
                "created_at": derivative.created_at,
                "updated_at": derivative.updated_at,
            }
            for derivative in derivatives
        ],
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def project_asset_is_referenced(
    session,
    project: VideoProject,
    project_asset_id: str,
) -> bool:
    revision = _current_video_revision(session, project)
    timeline = load(revision.timeline_json)
    return any(
        item.get("project_asset_id") == project_asset_id
        for track in timeline.get("tracks", [])
        for item in track.get("items", [])
    )


def archive_project_asset(
    session,
    project: VideoProject,
    item: ProjectAsset,
) -> None:
    if project_asset_is_referenced(session, project, item.id):
        raise VideoProjectConflictError("素材仍被当前视频工程时间线引用，不能归档")
    item.active = False


def project_asset_storage(
    session,
    item: ProjectAsset,
) -> tuple[str, str, str]:
    storage_key: str | None = None
    media_type: str | None = None
    if item.asset_ref_id:
        asset_ref = session.get(AssetRef, item.asset_ref_id)
        if asset_ref is not None:
            storage_key = asset_ref.storage_key
            media_type = asset_ref.media_type
    elif item.artifact_id:
        artifact = session.get(Artifact, item.artifact_id)
        if artifact is not None:
            storage_key = artifact.storage_key
            media_type = str(load(artifact.content_json).get("media_type") or "")
    elif item.library_item_id:
        library_item = session.get(LibraryItem, item.library_item_id)
        if library_item is not None:
            storage_key = library_item.storage_key
            asset_ref = (
                session.scalar(select(AssetRef).where(AssetRef.storage_key == storage_key))
                if storage_key
                else None
            )
            media_type = asset_ref.media_type if asset_ref is not None else None
    elif item.resource_entry_id:
        entry = session.get(ResourceEntry, item.resource_entry_id)
        if entry is not None:
            statement = select(AssetRef).where(AssetRef.entry_id == entry.id)
            asset_ref = (
                session.scalar(
                    statement.where(AssetRef.version_id == entry.current_version_id).order_by(
                        AssetRef.created_at.desc()
                    )
                )
                if entry.current_version_id
                else None
            )
            if asset_ref is None:
                asset_ref = session.scalar(statement.order_by(AssetRef.created_at.desc()))
            if asset_ref is not None:
                storage_key = asset_ref.storage_key
                media_type = asset_ref.media_type
    if not storage_key:
        raise VideoProjectValidationError("工程素材没有可下载的原始文件")
    filename = Path(storage_key).name
    resolved_type = media_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return storage_key, resolved_type, filename


def video_project_json(session, project: VideoProject) -> dict:
    revision = _current_video_revision(session, project)
    assets = session.scalars(
        select(ProjectAsset)
        .where(ProjectAsset.project_id == project.id)
        .order_by(ProjectAsset.created_at)
    ).all()
    return {
        "id": project.id,
        "task_id": project.task_id,
        "current_revision": project.current_revision,
        "current_revision_id": project.current_revision_id,
        "revision": video_project_revision_json(revision),
        "assets": [project_asset_json(session, item) for item in assets],
        "created_by": project.created_by,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def ai_edit_compare_json(session, proposal: AiEditProposal) -> dict:
    base = session.get(VideoProjectRevision, proposal.base_revision_id)
    candidate = session.get(VideoProjectRevision, proposal.candidate_revision_id)
    if base is None or candidate is None:
        raise RuntimeError("AI 修改提案的对比版本不存在")
    operations = load(proposal.operations_json)
    return {
        "proposal": ai_edit_proposal_json(proposal),
        "base_revision": video_project_revision_json(base),
        "candidate_revision": video_project_revision_json(candidate),
        "differences": [
            {
                "op_id": operation.get("op_id"),
                "type": operation.get("type"),
                "item_id": operation.get("item_id") or (operation.get("item") or {}).get("id"),
                "track_id": operation.get("track_id"),
            }
            for operation in operations
        ],
    }


def create_artifact(
    session,
    task: ContentTask,
    kind: str,
    content: dict,
    user_id: str,
    storage_key: str | None = None,
) -> Artifact:
    item = Artifact(
        task_id=task.id,
        kind=kind,
        version=next_version(session, task.id, kind),
        content_json=dump(artifact_content(kind, content)),
        storage_key=storage_key,
        created_by=user_id,
    )
    session.add(item)
    session.flush()
    return item


def generate_mock_image(
    task: ContentTask,
    index: int,
    parameters: dict | None = None,
) -> tuple[str, dict]:
    parameters = parameters or {}
    width, height = 1280, 720
    size = str(parameters.get("size", ""))
    if match := re.fullmatch(r"(\d{2,4})[xX×](\d{2,4})", size):
        width, height = (int(match.group(1)), int(match.group(2)))
        if not 64 <= width <= 4096 or not 64 <= height <= 4096:
            raise ValueError("图片宽高必须在 64 到 4096 像素之间")
    elif size:
        raise ValueError("图片尺寸格式应为 宽x高，例如 1080x1920")
    image = Image.new("RGB", (width, height), "#f5f7fa")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 90), fill="#197465")
    draw.rectangle((80, 160, 520, 600), fill="#ffffff", outline="#cddbd8", width=3)
    draw.rectangle((580, 160, 1200, 600), fill="#e8f5f2")
    draw.ellipse((770, 235, 1010, 475), fill="#12b58e")
    draw.text((80, 32), "YIFABAO CONTENT MOCK", fill="white")
    draw.text((110, 190), f"TASK: {task.id[:8]}", fill="#222222")
    draw.text((110, 230), f"VARIANT: {index + 1}", fill="#666666")
    key = f"tasks/{task.id}/images/{uuid4().hex}-mock-{index + 1}.png"
    path = storage.path_for(key)
    image.save(path, "PNG")
    ratio = str(parameters.get("ratio", f"{width}:{height}"))
    return key, {
        "width": width,
        "height": height,
        "ratio": ratio,
        "provider": "deterministic-image",
    }


def create_mock_voice(task_id: str, text: str) -> tuple[str, float]:
    duration = min(max(len(text) / 8.0, 3.0), 30.0)
    sample_rate = 16000
    frames = int(duration * sample_rate)
    key = f"tasks/{task_id}/audio/{uuid4().hex}-voice.wav"
    path = storage.path_for(key)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        audio = bytearray()
        for frame in range(frames):
            value = int(900 * math.sin(2 * math.pi * 220 * frame / sample_rate))
            audio.extend(struct.pack("<h", value))
        wav.writeframes(audio)
    return key, duration


def create_srt(task_id: str, text: str, duration: float) -> str:
    key = f"tasks/{task_id}/subtitles/{uuid4().hex}-subtitles.srt"
    path = storage.path_for(key)
    end_ms = int(duration * 1000)
    end = (
        f"{end_ms // 3600000:02}:{(end_ms // 60000) % 60:02}:"
        f"{(end_ms // 1000) % 60:02},{end_ms % 1000:03}"
    )
    path.write_text(f"1\n00:00:00,000 --> {end}\n{text}\n", encoding="utf-8")
    return key


def ffmpeg_executable() -> str:
    if found := shutil.which("ffmpeg"):
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def start_video_job(
    task_id: str,
    owner_id: str,
    image_key: str,
    voice_key: str,
    subtitle_key: str,
    parameters: dict | None = None,
) -> Job:
    parameters = parameters or {}
    with SessionLocal() as session:
        job = Job(task_id=task_id, owner_id=owner_id, kind="video_composition")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    def work() -> None:
        with SessionLocal() as session:
            current = session.get(Job, job_id)
            current.status = JobStatus.RUNNING
            current.progress = 10
            session.commit()
        try:
            image_path = storage.path_for(image_key)
            voice_path = storage.path_for(voice_key)
            subtitle_path = storage.path_for(subtitle_key)
            output_key = f"tasks/{task_id}/video/{job_id}.mp4"
            output_path = storage.path_for(output_key)
            command = [
                ffmpeg_executable(),
                "-y",
                "-loop",
                "1",
                "-i",
                str(image_path),
                "-i",
                str(voice_path),
                "-i",
                str(subtitle_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-map",
                "2:s:0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-tune",
                "stillimage",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-c:s",
                "mov_text",
                "-shortest",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
            with SessionLocal() as session:
                task = session.get(ContentTask, task_id)
                artifact = create_artifact(
                    session,
                    task,
                    "video",
                    {
                        "format": "mp4",
                        "subtitle_track": True,
                        "provider": "ffmpeg",
                        "parameters": parameters,
                    },
                    owner_id,
                    output_key,
                )
                task.current_stage = "video_preview"
                current = session.get(Job, job_id)
                current.status = JobStatus.SUCCEEDED
                current.progress = 100
                current.result_json = dump({"artifact_id": artifact.id, "storage_key": output_key})
                audit(session, owner_id, "video.composed", "artifact", artifact.id)
                session.commit()
        except Exception as exc:
            with SessionLocal() as session:
                current = session.get(Job, job_id)
                current.status = JobStatus.FAILED
                current.error = str(exc)
                session.commit()

    dispatcher.submit(work)
    return job


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_exportable_artifact(item: Artifact) -> bool:
    if item.status != "confirmed":
        return False
    content = load(item.content_json)
    return item.kind != "compliance" and not bool(content.get("legacy_mock"))


def _safe_task_name(task: ContentTask) -> str:
    return re.sub(r"[^\w\-.\u4e00-\u9fff]+", "-", task.name).strip("-_") or task.id


def _copy_candidates(artifact: Artifact) -> list[dict]:
    content = artifact_content(artifact.kind, load(artifact.content_json))
    raw_candidates = content.get("candidates") or [content.get("candidate", content)]
    candidates: list[dict] = []
    for candidate in raw_candidates:
        if isinstance(candidate, dict):
            candidates.append(candidate)
        elif candidate is not None:
            candidates.append({"body": str(candidate)})
    return candidates


def _selected_topic_title(artifact: Artifact) -> str:
    content = artifact_content(artifact.kind, load(artifact.content_json))
    candidates = content.get("candidates")
    selected_index = content.get("selected_candidate_index")
    candidate = None
    if (
        isinstance(candidates, list)
        and isinstance(selected_index, int)
        and not isinstance(selected_index, bool)
        and 0 <= selected_index < len(candidates)
    ):
        candidate = candidates[selected_index]
    elif isinstance(content.get("selected_candidate"), (dict, str)):
        candidate = content["selected_candidate"]
    elif isinstance(candidates, list) and candidates:
        candidate = candidates[0]
    if isinstance(candidate, dict):
        title = str(candidate.get("title") or candidate.get("text") or "").strip()
    else:
        title = str(candidate or "").strip()
    if not title:
        raise ValueError("当前确认标题内容为空")
    return title


def export_topic_artifact(
    task: ContentTask,
    artifact: Artifact,
    file_format: str,
) -> Path:
    """Export only the user-selected title, without workflow metadata."""
    if artifact.kind != "topic":
        raise ValueError("只有标题版本可以导出为标题文档")
    normalized = file_format.strip().lower()
    if normalized not in {"docx", "txt", "md"}:
        raise ValueError("标题导出格式仅支持 DOCX、TXT 或 Markdown")

    export_dir = settings.data_dir / "exports" / task.id / "direct"
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"{_safe_task_name(task)}-标题-v{artifact.version}.{normalized}"
    title = _selected_topic_title(artifact)
    if normalized == "docx":
        document = Document()
        document.add_heading(title, level=1)
        document.save(path)
    elif normalized == "md":
        path.write_text(f"# {title}\n", encoding="utf-8")
    else:
        path.write_text(f"{title}\n", encoding="utf-8")
    return path


def export_copy_artifact(
    task: ContentTask,
    artifact: Artifact,
    file_format: str,
) -> Path:
    """Export a clean, user-facing copy document without internal metadata."""
    if artifact.kind != "copy":
        raise ValueError("只有文案版本可以导出为文档")
    normalized = file_format.strip().lower()
    if normalized not in {"docx", "txt", "md"}:
        raise ValueError("文案导出格式仅支持 DOCX、TXT 或 Markdown")

    export_dir = settings.data_dir / "exports" / task.id / "direct"
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"{_safe_task_name(task)}-文案-v{artifact.version}.{normalized}"
    candidates = _copy_candidates(artifact)

    if normalized == "docx":
        document = Document()
        document.core_properties.title = task.name
        for index, candidate in enumerate(candidates, start=1):
            title = str(candidate.get("title") or "").strip()
            body = str(candidate.get("body") or "").strip()
            document.add_heading(title or task.name, level=1 if index == 1 else 2)
            if body:
                for paragraph_text in re.split(r"\n\s*\n", body):
                    document.add_paragraph(paragraph_text.strip())
        document.save(path)
        return path

    lines: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        title = str(candidate.get("title") or "").strip()
        body = str(candidate.get("body") or "").strip()
        if normalized == "md":
            heading = (
                f"# {title or task.name}" if index == 1 else f"## {title or f'文案版本 {index}'}"
            )
            lines.extend(
                [
                    heading,
                    "",
                    body,
                    "",
                ]
            )
        else:
            if title:
                lines.extend([title, ""])
            lines.extend([body, ""])
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def export_image_artifact(
    task: ContentTask,
    artifact: Artifact,
    file_format: str,
) -> Path:
    """Convert one generated image to a common end-user format."""
    if artifact.kind != "image" or not artifact.storage_key:
        raise ValueError("当前版本不是可导出的图片")
    normalized = file_format.strip().lower()
    if normalized == "jpeg":
        normalized = "jpg"
    if normalized not in {"png", "jpg", "webp"}:
        raise ValueError("图片导出格式仅支持 PNG、JPG 或 WebP")

    source_path = storage.path_for(artifact.storage_key)
    if not source_path.is_file():
        raise FileNotFoundError("图片源文件不存在")
    export_dir = settings.data_dir / "exports" / task.id / "direct"
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"{_safe_task_name(task)}-图片-v{artifact.version}.{normalized}"
    with Image.open(source_path) as source:
        image = ImageOps.exif_transpose(source)
        if normalized == "jpg":
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                flattened = Image.new("RGB", rgba.size, "white")
                flattened.paste(rgba, mask=rgba.getchannel("A"))
                image = flattened
            else:
                image = image.convert("RGB")
            image.save(path, "JPEG", quality=95, optimize=True)
        elif normalized == "webp":
            image.save(path, "WEBP", quality=95, method=6)
        else:
            image.save(path, "PNG", optimize=True)
    return path


def _markdown_text_artifact(task: ContentTask, artifact: Artifact) -> Path:
    export_dir = settings.data_dir / "exports" / task.id / "artifacts"
    export_dir.mkdir(parents=True, exist_ok=True)
    content = artifact_content(artifact.kind, load(artifact.content_json))
    path = export_dir / f"{artifact.id}.md"
    lines = [
        f"# {task.name}",
        "",
        f"- 内容类型：{artifact.kind}",
        f"- 版本：{artifact.version}",
        f"- 内容 ID：{artifact.id}",
        "",
    ]
    if artifact.kind == "topic":
        lines.extend(
            f"{index}. {candidate}"
            for index, candidate in enumerate(content.get("candidates", []), start=1)
        )
    elif artifact.kind == "copy":
        candidates = content.get("candidates") or [content.get("candidate", content)]
        for index, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, dict):
                lines.extend([f"## 文案版本 {index}", "", str(candidate), ""])
                continue
            title = str(candidate.get("title", "")).strip()
            body = str(candidate.get("body", "")).strip()
            lines.extend(
                [
                    f"## {title or f'文案版本 {index}'}",
                    "",
                    body,
                    "",
                ]
            )
    else:
        lines.extend(["```json", json.dumps(content, ensure_ascii=False, indent=2), "```"])
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _embedded_subtitle_srt(video_project_snapshot: dict) -> str:
    timeline = video_project_snapshot.get("timeline")
    tracks = timeline.get("tracks") if isinstance(timeline, dict) else None
    if not isinstance(tracks, list):
        return ""
    cues: list[tuple[float, int, float, str]] = []
    ordinal = 0
    for track in tracks:
        if (
            not isinstance(track, dict)
            or track.get("kind") != "subtitle"
            or bool(track.get("muted", False))
        ):
            continue
        items = track.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            ordinal += 1
            if (
                not isinstance(item, dict)
                or not bool(item.get("enabled", True))
                or str(item.get("project_asset_id") or "").strip()
            ):
                continue
            text = (
                str(item.get("content") or "")
                .replace("\x00", "")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
                .strip()
            )
            if not text:
                continue
            start = float(item.get("start_seconds") or 0)
            duration = float(item.get("duration_seconds") or 0)
            if not math.isfinite(start) or not math.isfinite(duration):
                raise VideoProjectValidationError("最终成片的内嵌字幕时间无效")
            if start < 0 or duration <= 0 or start + duration > 600.000001:
                raise VideoProjectValidationError("最终成片的内嵌字幕时间超出有效范围")
            cues.append((start, ordinal, duration, text))
    cues.sort(key=lambda cue: (cue[0], cue[1]))
    blocks = [
        "\n".join(
            [
                str(index),
                f"{_srt_timestamp(start)} --> {_srt_timestamp(start + duration)}",
                text,
            ]
        )
        for index, (start, _, duration, text) in enumerate(cues, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def export_task_bundle(
    task: ContentTask,
    artifacts: list[Artifact],
    referenced_assets: list[dict] | None = None,
    video_project_snapshot: dict | None = None,
    workflow_confirmed_selection: dict | None = None,
    resolved_artifact_ids: set[str] | None = None,
) -> Path:
    """Package the selected deliverables, not the editable project or audit data."""
    artifacts = [
        item
        for item in artifacts
        if _is_exportable_artifact(item)
        or (
            resolved_artifact_ids is not None
            and item.id in resolved_artifact_ids
            and item.kind in {"topic", "copy", "image", "video", "voice", "subtitle"}
            and item.status not in {"discarded", "deleted", "legacy_mock"}
            and not bool(load(item.content_json).get("legacy_mock"))
        )
    ]
    export_dir = settings.data_dir / "exports" / task.id
    export_dir.mkdir(parents=True, exist_ok=True)
    task_name = _safe_task_name(task)
    composed_video = next((item for item in reversed(artifacts) if item.kind == "video"), None)
    archive_files: list[tuple[Path | bytes, str]] = []
    archived_names: set[str] = set()
    delivered_checksums: set[str] = set()
    delivered_ids: set[str] = set()
    media_labels = {"image": "图片", "video": "成片", "voice": "配音", "subtitle": "字幕"}

    def add_file(source: Path | bytes, filename: str) -> None:
        # Linked artifacts can share a version number. Keep both without exposing IDs.
        base = Path(filename)
        candidate = filename
        duplicate = 2
        while candidate.casefold() in archived_names:
            candidate = f"{base.stem}-{duplicate}{base.suffix}"
            duplicate += 1
        archived_names.add(candidate.casefold())
        archive_files.append((source, candidate))

    for item in artifacts:
        if item.id in delivered_ids:
            continue
        delivered_ids.add(item.id)
        if item.kind == "topic":
            path = export_topic_artifact(task, item, "docx")
            add_file(path.read_bytes(), path.name)
        elif item.kind == "copy":
            path = export_copy_artifact(task, item, "docx")
            add_file(path.read_bytes(), path.name)
        elif item.kind in media_labels:
            label = media_labels[item.kind]
            if not item.storage_key:
                raise FileNotFoundError(f"所选{label}文件缺失，请重新生成后下载")
            source_path = storage.path_for(item.storage_key)
            if not source_path.is_file():
                raise FileNotFoundError(f"所选{label}文件缺失，请重新生成后下载")
            add_file(
                source_path,
                f"{task_name}-{label}-v{item.version}{source_path.suffix.lower()}",
            )
            delivered_checksums.add(_sha256_file(source_path))

    # Only useful audio/subtitle sidecars from the rendered revision accompany
    # a final video. Source clips, logos and reference images stay in the project.
    sidecar_counts: dict[str, int] = {}
    for dependency in referenced_assets or []:
        if task.mode not in {"full", "video"} or composed_video is None:
            continue
        source_path = Path(dependency["path"])
        media_type = str(dependency.get("media_type") or "").lower()
        suffix = source_path.suffix.lower()
        if suffix in {".srt", ".vtt"}:
            label = "字幕"
        elif media_type.startswith("audio/") and suffix in {
            ".mp3",
            ".wav",
            ".m4a",
            ".aac",
            ".ogg",
            ".flac",
            ".opus",
        }:
            role = " ".join(str(value) for value in dependency.get("roles", []))
            role += " " + str(dependency.get("role") or "")
            label = (
                "背景音乐"
                if "背景音乐" in role or "music" in role
                else ("配音" if "配音" in role or "voice" in role else "音频")
            )
        else:
            continue
        if not source_path.is_file():
            raise FileNotFoundError(f"成片对应的{label}文件缺失，请重新生成后下载")
        checksum = _sha256_file(source_path)
        if checksum in delivered_checksums:
            continue
        delivered_checksums.add(checksum)
        sidecar_counts[label] = sidecar_counts.get(label, 0) + 1
        add_file(source_path, f"{task_name}-{label}-{sidecar_counts[label]}{suffix}")

    if video_project_snapshot is not None and composed_video is not None:
        embedded_subtitles = _embedded_subtitle_srt(video_project_snapshot)
        if embedded_subtitles:
            subtitle_path = export_dir / f"{task_name}-成片字幕-v{composed_video.version}.srt"
            subtitle_path.write_text(embedded_subtitles, encoding="utf-8", newline="\n")
            if _sha256_file(subtitle_path) not in delivered_checksums:
                add_file(subtitle_path, subtitle_path.name)

    bundle_label = {
        "full": "完整创作素材包",
        "copy": "完整文案素材包",
        "image": "完整图片素材包",
        "video": "完整视频素材包",
    }.get(task.mode, "完整创作素材包")
    zip_path = export_dir / f"{_safe_task_name(task)}-{bundle_label}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for source, archive_path in archive_files:
            if isinstance(source, bytes):
                archive.writestr(archive_path, source)
            else:
                archive.write(source, archive_path)
    return zip_path


def export_text_artifact(task: ContentTask, artifact: Artifact) -> Path:
    if artifact.kind == "copy":
        # Card downloads have no format parameter. Reuse the readable document
        # writer rather than the legacy diagnostic dump, including old content.
        return export_copy_artifact(task, artifact, "txt")
    export_dir = settings.data_dir / "exports" / task.id / "artifacts"
    export_dir.mkdir(parents=True, exist_ok=True)
    content = artifact_content(artifact.kind, load(artifact.content_json))
    if artifact.kind == "storyboard":
        path = export_dir / f"{artifact.id}.json"
        path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    if artifact.kind == "topic":
        path = export_dir / f"{_safe_task_name(task)}-标题-v{artifact.version}.txt"
        candidates = content.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            candidates = [content.get("candidate") or content.get("selected_candidate") or content]
        readable: list[tuple[str, str]] = []
        for candidate in candidates:
            if isinstance(candidate, str):
                readable.append((candidate.strip(), ""))
            elif isinstance(candidate, dict):
                fields = (
                    candidate.get("title") or candidate.get("text"),
                    candidate.get("direction"),
                )
                title, direction = (
                    value.strip() if isinstance(value, str) else "" for value in fields
                )
                readable.append((title, direction))
        readable = [item for item in readable if any(item)]
        if not readable:
            raise ValueError("当前标题没有可下载的文字内容")
        lines: list[str] = []
        for index, (title, direction) in enumerate(readable, start=1):
            if title:
                lines.extend([f"{index}. {title}" if len(readable) > 1 else title, ""])
            if direction:
                lines.extend([f"创作方向：{direction}", ""])
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return path

    path = export_dir / f"{artifact.id}.txt"
    lines = [f"任务：{task.name}", f"内容类型：{artifact.kind}", f"版本：{artifact.version}", ""]
    lines.append(json.dumps(content, ensure_ascii=False, indent=2))
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path
