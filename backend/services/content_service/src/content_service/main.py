import asyncio
import base64
import hashlib
import json
import logging
import subprocess
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, or_, select, true
from sqlalchemy.orm import Session, object_session

from agent_core import Identity, install_request_context
from agent_core.web import install_cors, install_utc_datetime_responses

from . import login_monitor, remote_browser, trial_accounts
from .access_control import can_view_all_tasks, has_permission
from .access_control import get_content_identity as get_identity
from .access_routes import router as local_access_router
from .advertising_law_rules import SEED_ID, advertising_law_seed_enabled, seed_advertising_law_rules
from .ai_model_config import router as ai_model_configuration_router
from .android_routes import router as android_router
from .archive_drafts import (
    archive_snapshot_time,
    edit_draft_fields,
    hidden_edit_draft_ids,
    is_archived_snapshot,
    legacy_archived_task_ids,
    promote_edit_draft,
    restore_archive_as_draft,
)
from .automation_browser_session_routes import router as browser_session_router
from .automation_jobs import recover_interrupted as recover_automation_runs
from .automation_routes import router as automation_router
from .catalog_routes import router as catalog_router
from .chat_routes import router as chat_router
from .config import load_settings
from .db import SessionLocal, get_session, init_database
from .demo_quota import check as demo_quota_check
from .demo_quota import record as demo_quota_record
from .demo_quota import tag_visitor as demo_tag_visitor
from .demo_quota import use_videos as demo_use_videos
from .export_selection import resolve_task_export_artifacts
from .generation import (
    EgressDeniedError,
    KnowledgeProviderContractError,
    KnowledgeSelectionError,
    compliance_result,
    draft_content_data_level,
    prepare_generation_input,
    provider_metadata,
    reference_metadata,
    selected_knowledge_chunks,
    source_reference_metadata,
    used_reference_ids,
)
from .generation_errors import describe_generation_error
from .job_leases import cancel_job as request_durable_job_cancel
from .job_leases import claim_job as claim_durable_job
from .job_leases import complete_job as complete_durable_job
from .job_leases import enqueue_job
from .job_leases import fail_job as fail_durable_job
from .job_leases import heartbeat_job as heartbeat_durable_job
from .job_leases import recover_jobs as recover_durable_jobs
from .job_leases import retry_job as retry_durable_job
from .knowledge_ingestion import load_official_jsonl_bundle
from .library_deletion import LibraryDeletionConflict, delete_library_records, library_cleanup_loop
from .library_resources import is_product_resource
from .llm_adapters import ProviderCallError
from .mcp_server import router as mcp_router
from .media_adapters import (
    download_generated_video,
    provider_capability_document,
    render_srt,
    wait_for_ai_video_task,
)
from .media_ingest import (
    MediaIngestPolicy,
    MediaValidationError,
    generate_derivatives,
    probe_media,
    read_upload_head,
    safe_filename,
    validate_upload_header,
)
from .media_security import MalwareScanError, scan_media_file
from .models import (
    AiEditProposal,
    Artifact,
    ArtifactLink,
    AssetRef,
    AuditLog,
    ContentTask,
    EgressGrant,
    ExternalRef,
    GenerationRun,
    Job,
    LibraryItem,
    MediaDerivative,
    ProjectAsset,
    ResourceChunk,
    ResourceEntry,
    ResourceVersion,
    SensitiveRule,
    TaskStage,
    VideoProject,
    VideoProjectRevision,
    VideoUploadSession,
)
from .original_archives import ArchiveUnavailableError, CacheMaintenanceLock, OriginalArchiveRunner
from .provider_dependencies import (
    get_ai_video_generation_port,
    get_asset_store_port,
    get_image_generation_port,
    get_knowledge_catalog_port,
    get_knowledge_provider,
    get_music_generation_port,
    get_provider_media_publisher,
    get_provider_media_url_signer,
    get_speech_synthesis_port,
    get_subtitle_alignment_port,
    get_text_provider,
    get_timeline_renderer,
    get_video_composer_port,
)
from .provider_health import current_health
from .provider_media_gateway import ProviderMediaUrlSigner
from .provider_media_publishers import ProviderMediaPublicationError
from .providers import (
    AiVideoGenerationPort,
    AiVideoGenerationRequest,
    AssetStorePort,
    ContentGenerationInput,
    ImageGenerationPort,
    ImageGenerationRequest,
    KnowledgeCatalogPort,
    KnowledgeChunk,
    KnowledgeIngestRequest,
    KnowledgeProvider,
    MusicGenerationPort,
    MusicGenerationRequest,
    ProviderMediaPublisherPort,
    SpeechSynthesisPort,
    SpeechSynthesisRequest,
    SubtitleAlignmentPort,
    SubtitleAlignmentRequest,
    TextGenerationResult,
    TextProvider,
    VideoClip,
    VideoComposerPort,
    VideoCompositionRequest,
    WordTiming,
    selected_text_candidate,
    unchanged_transform_fields,
)
from .providers import (
    SubtitleSegment as AlignedSubtitleSegment,
)
from .publishable_text import artifact_content
from .schemas import (
    AiEditProposalAdoptRequest,
    AiEditProposalCompareResponse,
    AiEditProposalCreate,
    AiEditProposalDecisionResponse,
    AiEditProposalRejectRequest,
    AiEditProposalResponse,
    ArtifactDiscardResponse,
    ArtifactLinkCreate,
    ArtifactLinkResponse,
    ArtifactPatch,
    ArtifactResponse,
    CapabilitiesResponse,
    CopyImportRequest,
    EgressGrantCreate,
    EgressStatusResponse,
    GenerateRequest,
    GenerationRunResponse,
    LibraryCategoryResponse,
    LibraryItemCreate,
    LibraryItemDetailResponse,
    LibraryItemResponse,
    LibraryItemVersionCreate,
    MediaJobResponse,
    MusicGenerationCreateRequest,
    ProjectAssetPatch,
    ProjectAssetResponse,
    ProviderInfo,
    RuleCreate,
    ServiceStatusResponse,
    StageConfirmRequest,
    StageRequirementsUpdate,
    StageSelectRequest,
    StageSkipRequest,
    StoryboardImportRequest,
    TaskCreate,
    TaskDeleteResponse,
    TaskPatch,
    TaskPermanentDeleteResponse,
    TaskResponse,
    TransformCopyRequest,
    VideoCompositionSpec,
    VideoProjectAiVideoGenerationRequest,
    VideoProjectEditRequest,
    VideoProjectExportRequest,
    VideoProjectImageGenerationRequest,
    VideoProjectOperation,
    VideoProjectRenderRequest,
    VideoProjectResponse,
    VideoProjectRevisionResponse,
    VideoProjectSubtitleGenerationRequest,
    VideoProjectSubtitleGenerationResponse,
    VideoProjectVoiceGenerationRequest,
    VideoUploadCancelResponse,
    VideoUploadChunkResponse,
    VideoUploadFinalizedResponse,
    VideoUploadSessionCreate,
    VideoUploadSessionResponse,
    WorkflowResponse,
)
from .services import (
    VIDEO_TRACK_ID_ALIASES,
    VideoProjectConflictError,
    VideoProjectValidationError,
    adopt_ai_edit_proposal,
    ai_edit_compare_json,
    ai_edit_proposal_json,
    archive_project_asset,
    audit,
    can_access,
    create_ai_edit_proposal,
    create_artifact,
    create_or_get_video_project,
    dispatcher,
    dump,
    edit_video_project,
    ensure_project_asset,
    export_copy_artifact,
    export_image_artifact,
    export_task_bundle,
    export_text_artifact,
    export_topic_artifact,
    latest_task_update_times,
    load,
    project_asset_json,
    project_asset_storage,
    reject_ai_edit_proposal,
    storage,
    video_project_json,
    video_project_revision_json,
)
from .task_deletion import (
    TaskDeletionConflictError,
    delete_content_task_records,
)
from .task_image_prompt import task_image_prompt_parts
from .video_project_generation import (
    PROVIDER_MEDIA_PUBLICATIONS_RESULT_KEY,
    VIDEO_PROJECT_AI_JOB_KIND,
    ProjectAiVideoGenerationSpec,
    ProjectImageGenerationSpec,
    ProjectSubtitleGenerationSpec,
    ProjectVoiceGenerationSpec,
    VideoProjectGenerationConflictError,
    VideoProjectGenerationSafetyError,
    VideoProjectGenerationValidationError,
    archive_project_ai_video_result,
    enqueue_project_ai_video,
    generate_project_image_candidates,
    generate_project_subtitle_asset,
    generate_project_voice_asset,
    maybe_retry_ai_video_on_fast,
    normalize_ai_video_provider_failure,
    release_provider_media_publications,
    restore_misclassified_project_ai_video_job,
)
from .video_project_jobs import (
    EXPORT_JOB_KIND,
    RENDER_JOB_KIND,
    run_video_project_export_job,
    run_video_project_render_job,
)
from .video_uploads import (
    VideoUploadConflictError,
    VideoUploadIncompleteError,
    VideoUploadNotFoundError,
    VideoUploadValidationError,
    cancel_upload_session,
    complete_upload_session,
    create_upload_session,
    get_upload_session,
    upload_session_json,
    write_upload_chunk,
)
from .weknora_routes import knowledge_sync_status
from .weknora_routes import router as knowledge_sync_router
from .workflow import (
    ARTIFACT_STAGE,
    WorkflowConflictError,
    WorkflowValidationError,
    activate_copy_selection,
    adopt_revision,
    applicable_stages,
    artifact_is_currently_confirmed,
    artifact_is_currently_selected,
    attach_artifact,
    begin_generation_run,
    complete_generation_run,
    confirm_stage,
    ensure_workflow,
    fail_generation_run,
    generation_run_json,
    get_stage,
    initialize_workflow,
    mark_configuration_changed,
    operable_stages,
    select_artifacts,
    select_linked_artifacts,
    skip_stage,
    stage_json,
    update_requirements,
    workflow_json,
)

logger = logging.getLogger(__name__)
settings = load_settings()
RESOURCE_KINDS = {
    "document",
    "product",
    "logo",
    "image",
    "video",
    "audio",
    "subtitle",
    "spec",
}
DATA_LEVELS = {"public", "internal", "restricted"}
PROJECT_ASSET_ROLES = {
    "video",
    "overlay",
    "subtitle",
    "voice",
    "music",
    "sfx",
    "logo",
    "intro",
    "outro",
}
PROJECT_ASSET_ROLES_BY_KIND = {
    "image": {"video", "overlay", "logo", "intro", "outro"},
    # Phase one renders timed overlays as still images.  Reject animated
    # overlay/logo uploads up front instead of accepting an edit that can only
    # fail later in the FFmpeg worker.
    "video": {"video", "intro", "outro"},
    "audio": {"voice", "music", "sfx"},
    "subtitle": {"subtitle"},
}
# Per-request upload guardrails.  The knowledge ingestion pipeline has its own
# 50 MiB document parser limit; media uploads use MediaIngestPolicy's stricter
# type-specific limits.  A project is also bounded so a single task cannot
# accumulate an unbounded number of originals/derivatives.
LIBRARY_DOCUMENT_MAX_BYTES = 50 * 1024 * 1024
MAX_PROJECT_ASSETS_PER_TASK = 100
LIBRARY_DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".txt", ".md", ".markdown"})
LIBRARY_MEDIA_SUFFIXES = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".mp4",
        ".mov",
        ".m4v",
        ".webm",
        ".mkv",
        ".mp3",
        ".wav",
        ".m4a",
        ".aac",
        ".ogg",
        ".flac",
        ".srt",
        ".vtt",
    }
)
DISCARDED_ARTIFACT_STATUS = "discarded"
# Deprecated injection seam retained for existing in-process integrations.
start_video_job = None
VIDEO_PROJECT_OPERATION_ADAPTER = TypeAdapter(list[VideoProjectOperation])
PROVIDER_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}


def split_metadata_values(value: str) -> list[str]:
    return list(
        dict.fromkeys(
            item.strip()
            for item in value.replace(",", "\n").replace("，", "\n").splitlines()
            if item.strip()
        )
    )


def parse_fact_values(value: str) -> list:
    normalized = value.strip()
    if not normalized:
        return []
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return split_metadata_values(normalized)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, (dict, str)):
        return [parsed]
    raise HTTPException(422, "事实记录必须是字符串、对象或数组")


def library_item_json(
    item: LibraryItem,
    entry: ResourceEntry | None = None,
    version: ResourceVersion | None = None,
) -> dict:
    """Return the stable, unified library representation used by the Web client."""
    return {
        "id": item.id,
        "owner_id": item.owner_id,
        "category": item.category,
        "name": item.name,
        "text_content": item.text_content,
        "storage_key": item.storage_key,
        "data_level": item.data_level,
        "version": item.version,
        "active": item.active,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "resource_entry_id": entry.id if entry else item.resource_entry_id,
        "resource_version_id": version.id if version else None,
        "resource_kind": entry.resource_kind if entry else "document",
        "tags": load(entry.tags_json) if entry else [],
        "parent_id": entry.parent_id if entry else None,
        "source_url": version.source_url if version else None,
        "crawled_at": version.crawled_at if version else None,
        "freshness": version.freshness if version else None,
        "facts": load(version.facts_json) if version else [],
        "notes": version.notes if version else "",
        "metadata": load(version.metadata_json) if version else {},
        "source_type": (load(version.metadata_json).get("source_type") if version else None),
        "tenant_id": entry.tenant_id if entry else None,
        "department_id": entry.department_id if entry else None,
        "allowed_roles": load(entry.allowed_roles_json) if entry else [],
        "has_asset": bool(item.storage_key),
        "download_url": (f"/api/v1/library/items/{item.id}/download" if item.storage_key else None),
        "knowledge_sync": (
            knowledge_sync_status(object_session(item), entry, item) if object_session(item) else None
        ),
    }


def _positive_duration_seconds(value: object) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 and duration < float("inf") else None


def _metadata_duration_seconds(metadata: object) -> float | None:
    if not isinstance(metadata, dict):
        return None
    direct = _positive_duration_seconds(metadata.get("duration_seconds"))
    if direct is not None:
        return direct
    probe = metadata.get("probe")
    if isinstance(probe, dict):
        return _positive_duration_seconds(probe.get("duration_seconds"))
    return None


def _probe_timed_media_metadata(
    path: Path,
    *,
    media_kind: str,
    media_type: str,
) -> dict:
    """Return canonical persisted metadata for one local audio/video object."""

    try:
        result = probe_media(
            path,
            kind=media_kind,
            media_type=media_type,
            timeout_seconds=MediaIngestPolicy().max_probe_seconds,
        )
    except MediaValidationError:
        raise
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return {
            "media_kind": media_kind,
            "media_type": media_type,
            "probe_status": "unavailable",
            "probe_error": str(exc)[:500],
            "probe": None,
        }
    probe = asdict(result)
    duration = _positive_duration_seconds(result.duration_seconds)
    if duration is None:
        raise MediaValidationError("音视频文件没有可用时长")
    return {
        "media_kind": media_kind,
        "media_type": media_type,
        "duration_seconds": duration,
        "probe_status": "succeeded",
        "probe_error": None,
        "probe": probe,
    }


def _uploaded_library_metadata(
    *,
    asset_store: AssetStorePort,
    storage_key: str,
    resource_kind: str,
    media_type: str,
    metadata: dict,
) -> dict:
    result = {**metadata, "media_type": media_type}
    if resource_kind not in {"audio", "video"}:
        return result
    path = asset_store.path_for(storage_key)
    try:
        result.update(
            _probe_timed_media_metadata(
                path,
                media_kind=resource_kind,
                media_type=media_type,
            )
        )
    except MediaValidationError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, f"媒体探测失败：{exc}") from exc
    return result


def _library_asset_ref(
    session: Session,
    item: LibraryItem | None,
    entry: ResourceEntry | None,
    version: ResourceVersion | None,
) -> AssetRef | None:
    if item is not None and item.storage_key:
        asset = session.scalar(select(AssetRef).where(AssetRef.storage_key == item.storage_key))
        if asset is not None:
            return asset
    if version is not None:
        return session.scalar(
            select(AssetRef)
            .where(AssetRef.version_id == version.id)
            .order_by(AssetRef.created_at.desc())
        )
    if entry is not None:
        return session.scalar(
            select(AssetRef)
            .where(AssetRef.entry_id == entry.id)
            .order_by(AssetRef.created_at.desc())
        )
    return None


def _ensure_library_media_metadata(
    session: Session,
    *,
    item: LibraryItem,
    entry: ResourceEntry | None,
    version: ResourceVersion | None,
    asset_store: AssetStorePort,
) -> bool:
    """Backfill duration metadata for legacy local enterprise audio/video assets."""

    if entry is None or entry.resource_kind not in {"audio", "video"}:
        return False
    asset = _library_asset_ref(session, item, entry, version)
    metadata_sources = [
        load(version.metadata_json) if version is not None else {},
        load(asset.metadata_json) if asset is not None else {},
        load(entry.metadata_json),
    ]
    duration = next(
        (
            value
            for metadata in metadata_sources
            if (value := _metadata_duration_seconds(metadata)) is not None
        ),
        None,
    )
    proven_metadata = next(
        (
            metadata
            for metadata in metadata_sources
            if isinstance(metadata, dict)
            and _metadata_duration_seconds(metadata) is not None
            and (
                isinstance(metadata.get("probe"), dict)
                or metadata.get("probe_status") == "succeeded"
            )
        ),
        None,
    )
    media_type = asset.media_type if asset is not None else "application/octet-stream"
    updates: dict = {
        "media_kind": entry.resource_kind,
        "media_type": media_type,
    }
    if proven_metadata is not None:
        updates["duration_seconds"] = duration
        for key in ("probe_status", "probe_error", "probe"):
            if key in proven_metadata:
                updates[key] = proven_metadata[key]
    else:
        terminal_failure = next(
            (
                metadata
                for metadata in metadata_sources
                if isinstance(metadata, dict) and metadata.get("probe_status") == "failed"
            ),
            None,
        )
        storage_key = item.storage_key or (asset.storage_key if asset is not None else None)
        path = asset_store.path_for(storage_key) if storage_key else None
        if terminal_failure is None and path is not None and path.is_file():
            try:
                updates.update(
                    _probe_timed_media_metadata(
                        path,
                        media_kind=entry.resource_kind,
                        media_type=media_type,
                    )
                )
            except MediaValidationError as exc:
                updates.update(
                    {
                        "probe_status": "failed",
                        "probe_error": str(exc)[:500],
                        "probe": None,
                    }
                )
        elif duration is not None:
            updates["duration_seconds"] = duration
        else:
            return False

    changed = False
    for record in (entry, version, asset):
        if record is None:
            continue
        current = load(record.metadata_json)
        current = dict(current) if isinstance(current, dict) else {}
        merged = {**current, **updates}
        if merged != current:
            record.metadata_json = dump(merged)
            changed = True
    return changed


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    recover_automation_runs()
    login_monitor.start()  # re-checks platform logins in the background
    remote_browser.start_reaper()  # trial server: clear idle accounts' logins
    asset_store = get_asset_store_port()
    # Hold throughout responses and background/FFmpeg work. An offline cache
    # purge may never unlink paths returned to readers in this process.
    cache_lock = CacheMaintenanceLock(settings.data_dir / "objects")
    sync_stop = Event()
    sync_tasks = []
    recovery_task = None
    try:
        cache_lock.acquire()
        if hasattr(asset_store, "acquire_runtime_lock"):
            asset_store.acquire_runtime_lock()
        if advertising_law_seed_enabled():
            with SessionLocal() as session:
                seed_advertising_law_rules(session)
                session.commit()
        purge_expired_provider_media()
        recover_pending_jobs()
        recovery_task = asyncio.create_task(durable_media_recovery_loop())
        from .provider_dependencies import get_weknora_client
        sync_tasks.append(asyncio.create_task(asyncio.to_thread(
            library_cleanup_loop, SessionLocal, asset_store, get_weknora_client, sync_stop,
        )))
        if settings.weknora_originals_enabled:
            from .provider_dependencies import get_weknora_client

            original_runner = OriginalArchiveRunner(
                SessionLocal, get_weknora_client(), asset_store=asset_store,
                allowed_data_levels=settings.weknora_allowed_data_levels,
                interval_seconds=settings.weknora_sync_interval_seconds,
                max_file_bytes=settings.weknora_originals_max_file_bytes,
            )
            sync_tasks.append(asyncio.create_task(asyncio.to_thread(original_runner.run_forever, sync_stop)))
        if settings.knowledge_provider == "weknora" and settings.weknora_sync_enabled:
            from .provider_dependencies import get_weknora_client
            from .weknora_sync import WeKnoraSyncRunner

            sync_runner = WeKnoraSyncRunner(
                SessionLocal, get_weknora_client(),
                allowed_data_levels=settings.weknora_allowed_data_levels,
                interval_seconds=settings.weknora_sync_interval_seconds,
                asset_store=asset_store,
            )
            sync_tasks.append(asyncio.create_task(asyncio.to_thread(sync_runner.run_forever, sync_stop)))
        yield
    finally:
        sync_stop.set()
        try:
            if sync_tasks:
                await asyncio.gather(*sync_tasks, return_exceptions=True)
            if recovery_task is not None:
                recovery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await recovery_task
            await asyncio.to_thread(dispatcher.close, wait=True)
        finally:
            if hasattr(asset_store, "close"):
                asset_store.close()
            cache_lock.close()


app = FastAPI(
    title="易发宝工作台 API",
    version="0.1.0",
    description="独立部署的选题、文案、图片、配音、分镜和视频内容服务。",
    lifespan=lifespan,
)
install_request_context(app, "content-service")
install_cors(app, "CONTENT_CORS_ORIGINS")
install_utc_datetime_responses(app)
app.middleware("http")(demo_tag_visitor)  # online trial: per-visitor video allowance
app.middleware("http")(trial_accounts.guard)  # online trial: every request needs a trial account

app.include_router(ai_model_configuration_router)
app.include_router(local_access_router)
app.include_router(knowledge_sync_router)
app.include_router(chat_router)
app.include_router(automation_router)
app.include_router(browser_session_router)
app.include_router(android_router)
app.include_router(catalog_router)
app.include_router(mcp_router)
app.include_router(trial_accounts.router)
app.include_router(remote_browser.router)

@app.exception_handler(ArchiveUnavailableError)
async def original_unavailable_handler(request, exc):
    return JSONResponse(
        status_code=503,
        content={"detail": "暂时无法读取已托管原件，请检查知识平台连接后重试", "code": exc.code},
        headers={"Cache-Control": "private, no-store"},
    )


def get_task_or_404(
    session: Session,
    task_id: str,
    identity: Identity,
    *,
    allow_admin_read: bool = False,
) -> ContentTask:
    task = session.get(ContentTask, task_id)
    if task is None:
        raise HTTPException(404, "内容任务不存在")
    if not (
        can_access(task, identity.user_id, identity.department)
        or (allow_admin_read and can_view_all_tasks(identity))
    ):
        raise HTTPException(403, "无权访问该内容任务")
    return task


def ensure_task_editable(task: ContentTask) -> None:
    """Archived tasks stay immutable; restoring creates a separate draft."""
    if is_archived_snapshot(task):
        raise HTTPException(
            409, "归档内容不能直接修改。请点击“修改归档内容”，修改后最终确认会另存为新任务。"
        )


def ensure_task_jobs_finished(session: Session, task: ContentTask) -> None:
    active_job = session.scalar(
        select(Job.id)
        .where(
            Job.task_id == task.id,
            Job.status.in_(["queued", "running", "retrying"]),
        )
        .limit(1)
    )
    if active_job is not None:
        raise HTTPException(409, "仍有生成或导出正在进行中，请完成后再归档或最终确认。")


def get_video_project_or_404(
    session: Session,
    project_id: str,
    identity: Identity,
    *,
    allow_admin_read: bool = False,
) -> tuple[VideoProject, ContentTask]:
    project = session.get(VideoProject, project_id)
    if project is None:
        raise HTTPException(404, "视频工程不存在")
    task = get_task_or_404(
        session, project.task_id, identity, allow_admin_read=allow_admin_read
    )
    return project, task


def get_project_asset_or_404(
    session: Session,
    project_id: str,
    asset_id: str,
    identity: Identity,
    *,
    allow_admin_read: bool = False,
) -> tuple[ProjectAsset, VideoProject, ContentTask]:
    project, task = get_video_project_or_404(
        session, project_id, identity, allow_admin_read=allow_admin_read
    )
    item = session.get(ProjectAsset, asset_id)
    if item is None or item.project_id != project.id:
        raise HTTPException(404, "视频工程素材不存在")
    return item, project, task


def validate_project_asset_role(media_kind: str, role: str) -> None:
    if role not in PROJECT_ASSET_ROLES:
        raise HTTPException(422, "不支持的工程素材角色")
    if role not in PROJECT_ASSET_ROLES_BY_KIND.get(media_kind, set()):
        raise HTTPException(422, f"{media_kind} 素材不能用作 {role}")


def upload_size_bytes(file: UploadFile) -> int:
    declared_size = getattr(file, "size", None)
    if declared_size is not None:
        return int(declared_size)
    stream = file.file
    position = stream.tell()
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(position)
    return int(size)


def validate_library_upload(file: UploadFile) -> tuple[int, str]:
    """Validate a library upload before it is copied to the asset store."""

    filename = safe_filename(file.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in LIBRARY_DOCUMENT_SUFFIXES and suffix not in LIBRARY_MEDIA_SUFFIXES:
        raise HTTPException(422, "不支持的资料文件类型")
    size_bytes = upload_size_bytes(file)
    if size_bytes < 1:
        raise HTTPException(422, "资料文件不能为空")
    if suffix in LIBRARY_DOCUMENT_SUFFIXES:
        if size_bytes > LIBRARY_DOCUMENT_MAX_BYTES:
            raise HTTPException(422, "资料文件超过 50 MiB 大小限制")
        return size_bytes, {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".markdown": "text/markdown",
        }[suffix]

    # Library files are ingested by the knowledge catalog, which may accept
    # provider-specific media fixtures in development.  Enforce extension/MIME
    # and type-specific byte limits here; project-media uploads perform the
    # stronger magic-byte validation before registration.
    media_type = (file.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    suffix_kind = (
        "image"
        if suffix in {".jpg", ".jpeg", ".png", ".webp"}
        else "video"
        if suffix in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
        else "audio"
        if suffix in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
        else "subtitle"
    )
    if media_type != "application/octet-stream":
        allowed_media = {
            "image": media_type.startswith("image/"),
            "video": media_type.startswith("video/"),
            "audio": media_type.startswith("audio/"),
            "subtitle": media_type
            in {"application/x-subrip", "text/srt", "text/vtt", "text/plain"},
        }
        if not allowed_media[suffix_kind]:
            raise HTTPException(422, "文件扩展名与声明的媒体类型不一致")
    limit = MediaIngestPolicy().max_bytes[suffix_kind]
    if size_bytes > limit:
        raise HTTPException(422, "资料文件超过当前类型的大小限制")
    if media_type == "application/octet-stream":
        media_type = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".mp4": "video/mp4", ".mov": "video/quicktime",
            ".m4v": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
            ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
            ".aac": "audio/aac", ".ogg": "audio/ogg", ".flac": "audio/flac",
            ".srt": "application/x-subrip", ".vtt": "text/vtt",
        }[suffix]
    return size_bytes, media_type


def library_upload_resource_kind(requested_kind: str | None, media_type: str) -> str:
    """Keep business document kinds, but never send media to the document parser."""
    if requested_kind is not None and requested_kind not in RESOURCE_KINDS:
        raise HTTPException(422, "不支持的资源类型")
    media_kind = next(
        (kind for kind in ("image", "video", "audio") if media_type.startswith(f"{kind}/")),
        None,
    )
    if requested_kind in {"image", "logo", "video", "audio"}:
        expected_kind = "image" if requested_kind == "logo" else requested_kind
        if media_kind != expected_kind:
            raise HTTPException(422, "所选资源类型与文件不一致，请重新选择资源类型或文件")
        return requested_kind
    if requested_kind == "product" and media_kind == "image":
        return "product"
    return media_kind or requested_kind or "document"


def enforce_project_asset_count(session: Session, project: VideoProject) -> None:
    count = (
        session.scalar(
            select(func.count(ProjectAsset.id)).where(
                ProjectAsset.project_id == project.id,
                ProjectAsset.active == true(),
            )
        )
        or 0
    )
    if count >= MAX_PROJECT_ASSETS_PER_TASK:
        raise HTTPException(
            422,
            f"单个视频工程最多允许 {MAX_PROJECT_ASSETS_PER_TASK} 个素材",
        )


def video_upload_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, VideoUploadNotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, (VideoUploadConflictError, VideoUploadIncompleteError)):
        return HTTPException(409, str(exc))
    if isinstance(exc, VideoUploadValidationError):
        return HTTPException(422, str(exc))
    return HTTPException(500, "分片上传处理失败")


def get_video_upload_or_404(
    session: Session,
    upload_id: str,
    identity: Identity,
) -> tuple[VideoUploadSession, VideoProject, ContentTask]:
    try:
        upload = get_upload_session(session, upload_id)
    except VideoUploadNotFoundError as exc:
        raise video_upload_http_error(exc) from exc
    project, task = get_video_project_or_404(session, upload.project_id, identity)
    if upload.owner_id != identity.user_id and not identity.has_role("admin"):
        raise HTTPException(403, "无权访问该分片上传会话")
    return upload, project, task


def enforce_project_media_quota(
    session: Session,
    project: VideoProject,
    incoming_size: int,
) -> None:
    used_size = 0
    for item in session.scalars(
        select(ProjectAsset).where(ProjectAsset.project_id == project.id)
    ).all():
        metadata = load(item.metadata_json)
        value = metadata.get("size_bytes")
        if isinstance(value, int) and value > 0:
            used_size += value
    if used_size + incoming_size > settings.project_media_quota_bytes:
        raise HTTPException(
            413,
            "视频工程素材配额不足，请归档并清理不再使用的素材后重试",
        )


def register_video_project_asset(
    session: Session,
    *,
    project: VideoProject,
    owner_id: str,
    role: str,
    filename: str,
    media_kind: str,
    media_type: str,
    storage_key: str,
    size_bytes: int,
    checksum_sha256: str,
    asset_store: AssetStorePort,
    source_metadata: dict | None = None,
) -> tuple[ProjectAsset, int]:
    """Register one already-stored upload and build best-effort edit derivatives."""

    validate_project_asset_role(media_kind, role)
    original_path = asset_store.path_for(storage_key)
    try:
        scan_result = scan_media_file(
            original_path,
            command=settings.antivirus_command,
            timeout_seconds=settings.antivirus_timeout_seconds,
            required=settings.antivirus_required,
        )
    except MalwareScanError as exc:
        original_path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc)) from exc
    probe_result = None
    probe_state = "unavailable"
    probe_error = None
    try:
        probe_result = probe_media(
            original_path,
            kind=media_kind,
            media_type=media_type,
            timeout_seconds=MediaIngestPolicy().max_probe_seconds,
        )
        probe_state = "succeeded"
    except MediaValidationError as exc:
        original_path.unlink(missing_ok=True)
        raise HTTPException(422, f"媒体探测失败：{exc}") from exc
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        probe_error = str(exc)[:500]

    probe_metadata = asdict(probe_result) if probe_result is not None else None
    duration_seconds = (
        _positive_duration_seconds(probe_result.duration_seconds)
        if probe_result is not None
        else None
    )
    metadata = {
        **(source_metadata or {}),
        "filename": filename,
        "media_kind": media_kind,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "checksum_sha256": checksum_sha256,
        "antivirus": asdict(scan_result),
        "probe_status": probe_state,
        "probe_error": probe_error,
        "probe": probe_metadata,
        "derivative_status": "pending" if probe_result is not None else "not_started",
    }
    if duration_seconds is not None:
        metadata["duration_seconds"] = duration_seconds
    entry, version = create_resource_version_record(
        session,
        owner_id=owner_id,
        resource_kind=media_kind,
        category="video-project",
        title=filename,
        text_content="",
        data_level="internal",
        version_number=1,
        created_by=owner_id,
        metadata=metadata,
        asset_key=storage_key,
        asset_media_type=media_type,
        asset_size=size_bytes,
        asset_checksum=checksum_sha256,
    )
    asset_ref = session.scalar(select(AssetRef).where(AssetRef.version_id == version.id))
    if asset_ref is None:
        raise RuntimeError("工程素材文件引用创建失败")
    project_asset = ensure_project_asset(
        session,
        project,
        source_type="asset_ref",
        source_id=asset_ref.id,
        role=role,
        label=filename,
        created_by=owner_id,
        asset_ref_id=asset_ref.id,
        metadata=metadata,
    )

    derivative_count = 0
    if probe_result is not None and media_kind != "subtitle":
        try:
            with TemporaryDirectory(prefix="content-project-derivatives-") as temp_dir:
                generated_items = generate_derivatives(
                    original_path,
                    Path(temp_dir),
                    probe=probe_result,
                    timeout_seconds=MediaIngestPolicy().max_derivative_seconds,
                )
                for generated in generated_items:
                    derivative_stem = generated.path.stem
                    variant = (
                        derivative_stem.removeprefix(f"{generated.kind}-")
                        if derivative_stem.startswith(f"{generated.kind}-")
                        else "default"
                    )
                    derivative_key = (
                        f"video-projects/{project.id}/derivatives/"
                        f"{project_asset.id}/{generated.kind}-{variant}"
                        f"{generated.path.suffix.lower()}"
                    )
                    with generated.path.open("rb") as source:
                        derivative_store = asset_store.put_stream(
                            derivative_key,
                            source,
                            generated.media_type,
                        )
                    session.add(
                        MediaDerivative(
                            project_asset_id=project_asset.id,
                            kind=generated.kind,
                            variant=variant,
                            status="ready",
                            storage_key=derivative_store.key,
                            media_type=derivative_store.media_type,
                            checksum=derivative_store.checksum_sha256,
                            metadata_json=dump(
                                {
                                    **generated.metadata,
                                    "size_bytes": derivative_store.size,
                                }
                            ),
                        )
                    )
                    derivative_count += 1
            metadata["derivative_status"] = "succeeded" if derivative_count else "not_applicable"
        except (
            MediaValidationError,
            RuntimeError,
            OSError,
            subprocess.SubprocessError,
        ) as exc:
            metadata["derivative_status"] = "partial" if derivative_count else "unavailable"
            metadata["derivative_error"] = str(exc)[:500]
    elif media_kind == "subtitle":
        metadata["derivative_status"] = "not_applicable"

    entry.metadata_json = dump(metadata)
    version.metadata_json = dump(metadata)
    asset_ref.metadata_json = dump(metadata)
    project_asset.metadata_json = dump(metadata)
    return project_asset, derivative_count


def project_asset_media_kind(session: Session, item: ProjectAsset) -> str:
    metadata = load(item.metadata_json)
    media_kind = str(metadata.get("media_kind") or "")
    if media_kind in PROJECT_ASSET_ROLES_BY_KIND:
        return media_kind
    _, media_type, _ = project_asset_storage(session, item)
    return (
        "image"
        if media_type.startswith("image/")
        else "video"
        if media_type.startswith("video/")
        else "audio"
        if media_type.startswith("audio/")
        else "subtitle"
        if media_type in {"application/x-subrip", "text/srt", "text/vtt", "text/plain"}
        else ""
    )


def get_ai_edit_proposal_or_404(
    session: Session,
    proposal_id: str,
    identity: Identity,
) -> tuple[AiEditProposal, VideoProject, ContentTask]:
    proposal = session.get(AiEditProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404, "AI 修改提案不存在")
    project, task = get_video_project_or_404(session, proposal.project_id, identity)
    return proposal, project, task


def video_project_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, VideoProjectConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, VideoProjectValidationError):
        return HTTPException(422, str(exc))
    return HTTPException(500, "视频工程处理失败")


def _generation_error_detail(exc: Exception) -> dict:
    preserved = getattr(exc, "generation_error_detail", None)
    if isinstance(preserved, dict) and isinstance(preserved.get("message"), str):
        return dict(preserved)
    if isinstance(exc, HTTPException):
        if isinstance(exc.detail, dict) and isinstance(exc.detail.get("message"), str):
            return dict(exc.detail)
        return describe_generation_error(str(exc.detail))
    return describe_generation_error(exc)


def _generation_error_message(exc: HTTPException) -> str:
    if isinstance(exc.detail, dict):
        return str(exc.detail.get("message", "生成失败，请稍后重试"))
    return str(exc.detail)


def video_project_generation_http_error(
    exc: Exception, *, clear_generation_error: bool = False
) -> HTTPException:
    if isinstance(exc, VideoProjectGenerationConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, VideoProjectGenerationSafetyError):
        return HTTPException(
            422,
            {
                "message": str(exc),
                **exc.details,
            },
        )
    if isinstance(exc, VideoProjectGenerationValidationError):
        return HTTPException(422, str(exc))
    if isinstance(exc, ProviderCallError):
        logger.warning("Video project provider call failed: %s", exc)
        if clear_generation_error:
            return HTTPException(502, _generation_error_detail(exc))
        message, details = normalize_ai_video_provider_failure(
            str(exc),
            error_code=exc.code,
            retryable=exc.retryable,
            classify=False,
        )
        return HTTPException(
            502,
            {
                "message": message,
                **details,
            },
        )
    if isinstance(exc, ProviderMediaPublicationError):
        logger.warning("Video project media publication failed: %s", exc)
        return HTTPException(502, "参考媒体暂时无法发送给视频服务，请稍后重试")
    if isinstance(exc, (TypeError, ValueError)):
        return HTTPException(422, str(exc))
    return HTTPException(500, "AI 素材生成失败")


def cleanup_failed_ai_video_submission(session: Session, exc: Exception) -> None:
    """Remove a provider-less enqueue row without committing request changes."""

    job_id = getattr(exc, "ai_video_submission_job_id", None)
    if not isinstance(job_id, str) or not job_id:
        return
    session.rollback()
    try:
        job = session.get(Job, job_id)
        if job is None or job.kind != VIDEO_PROJECT_AI_JOB_KIND:
            session.rollback()
            return
        result = load(job.result_json)
        if str(result.get("provider_task_id") or "").strip():
            session.rollback()
            return
        if job.status == "cancelled" or job.cancel_requested_at is not None:
            session.rollback()
            return
        session.delete(job)
        session.commit()
    except Exception:
        logger.exception("Failed to clean up provider-less AI video submission job %s", job_id)
        session.rollback()


def _project_operation_dicts(operations: list) -> list[dict]:
    result: list[dict] = []
    for operation in operations:
        item = operation.model_dump(mode="json")
        if operation.type in {"update", "update_canvas", "update_track"}:
            item["changes"] = operation.changes.model_dump(
                mode="json",
                exclude_unset=True,
            )
        result.append(item)
    return result


def _project_track_role(
    session: Session,
    project: VideoProject,
    operation: dict,
) -> str:
    if operation.get("track_kind"):
        return str(operation["track_kind"])
    track_id = operation.get("track_id")
    revision = session.get(VideoProjectRevision, project.current_revision_id)
    if revision is not None and track_id:
        timeline = load(revision.timeline_json)
        track = next(
            (item for item in timeline.get("tracks", []) if item.get("id") == track_id),
            None,
        )
        if track is not None:
            return str(track.get("kind") or "media")
    return VIDEO_TRACK_ID_ALIASES.get(str(track_id or ""), "video")


def _validate_project_asset_for_track(
    session: Session,
    project_asset: ProjectAsset,
    track_role: str,
) -> None:
    if track_role == "overlay" and project_asset_media_kind(session, project_asset) != "image":
        raise HTTPException(422, "一期 overlay 轨道仅支持图片素材")


def _materialize_project_operations(
    session: Session,
    project: VideoProject,
    identity: Identity,
    operations: list,
) -> list[dict]:
    prepared = _project_operation_dicts(operations)
    for operation in prepared:
        if operation.get("type") != "add":
            continue
        item = operation["item"]
        role = _project_track_role(session, project, operation)
        project_asset_id = item.get("project_asset_id")
        if project_asset_id:
            project_asset = session.get(ProjectAsset, project_asset_id)
            if (
                project_asset is None
                or project_asset.project_id != project.id
                or not project_asset.active
            ):
                raise HTTPException(422, "所选工程素材不存在或已经停用")
            project_asset_metadata = load(project_asset.metadata_json)
            if (
                project_asset.role == "logo"
                and project_asset_metadata.get("pending_brand_confirmation") is True
            ):
                raise HTTPException(409, "AI Logo 候选须人工确认后才能加入时间线")
            _validate_project_asset_for_track(session, project_asset, role)
            continue

        label = str(item.get("label") or "")
        source_fields = {
            "asset_ref_id": item.get("asset_ref_id"),
            "artifact_id": item.get("artifact_id"),
            "library_item_id": item.get("library_item_id"),
            "resource_entry_id": item.get("resource_entry_id"),
        }
        selected = [(key, value) for key, value in source_fields.items() if value]
        if not selected:
            continue
        source_field, source_id = selected[0]
        source_type = source_field.removesuffix("_id")
        asset_kwargs = {source_field: source_id}
        attachment_metadata = {"attached_via": "video_project_api"}
        if source_field == "asset_ref_id":
            asset = session.get(AssetRef, source_id)
            entry = session.get(ResourceEntry, asset.entry_id) if asset else None
            if asset is None or entry is None or not entry.active:
                raise HTTPException(422, "所选媒体素材不存在或已经停用")
            if (
                entry.owner_id != identity.user_id
                and entry.data_level != "public"
                and not has_permission(identity, "library.manage")
            ):
                raise HTTPException(403, "无权引用该媒体素材")
            label = label or entry.title
        elif source_field == "artifact_id":
            artifact = session.get(Artifact, source_id)
            if artifact is None:
                raise HTTPException(422, "所选生成素材不存在")
            get_task_or_404(session, artifact.task_id, identity)
            if artifact.status in {DISCARDED_ARTIFACT_STATUS, "legacy_mock", "stale"}:
                raise HTTPException(409, "所选生成素材已经失效")
            label = label or f"{artifact.kind} v{artifact.version}"
        elif source_field == "library_item_id":
            _, media_type = library_asset_for_identity(session, identity, source_id)
            library_item = session.get(LibraryItem, source_id)
            label = label or (library_item.name if library_item else "")
            entry = (
                session.get(ResourceEntry, library_item.resource_entry_id)
                if library_item is not None and library_item.resource_entry_id
                else None
            )
            resource_version = (
                session.scalar(
                    select(ResourceVersion).where(
                        ResourceVersion.entry_id == entry.id,
                        ResourceVersion.version == library_item.version,
                    )
                )
                if entry is not None and library_item is not None
                else None
            )
            library_metadata = (
                load(resource_version.metadata_json) if resource_version is not None else {}
            )
            attachment_metadata.update(
                {
                    "source": "enterprise-knowledge-base",
                    "source_version": library_item.version if library_item else 1,
                    "media_kind": entry.resource_kind if entry else "",
                    "media_type": media_type,
                    "resource_entry_id": entry.id if entry else None,
                }
            )
            if isinstance(library_metadata, dict):
                for key in (
                    "duration_seconds",
                    "probe_status",
                    "probe_error",
                    "probe",
                ):
                    if key in library_metadata:
                        attachment_metadata[key] = library_metadata[key]
        else:
            entry = session.get(ResourceEntry, source_id)
            if entry is None or not entry.active:
                raise HTTPException(422, "所选企业资料不存在或已经停用")
            if (
                entry.owner_id != identity.user_id
                and entry.data_level != "public"
                and not has_permission(identity, "library.manage")
            ):
                raise HTTPException(403, "无权引用该企业资料")
            label = label or entry.title
        project_asset = ensure_project_asset(
            session,
            project,
            source_type=source_type,
            source_id=str(source_id),
            role=role,
            label=label,
            created_by=identity.user_id,
            metadata=attachment_metadata,
            **asset_kwargs,
        )
        _validate_project_asset_for_track(session, project_asset, role)
        item["project_asset_id"] = project_asset.id
        for field in source_fields:
            item.pop(field, None)
    return prepared


def _require_video_ai_brief(
    task: ContentTask,
    *,
    purpose: str = "首次使用 AI 前",
) -> None:
    missing: list[str] = []
    for label, value in (
        ("任务名称", task.name),
        ("内容类型", task.content_type),
        ("表达风格", task.style),
        ("目标人群", task.audience),
    ):
        if not str(value or "").strip():
            missing.append(label)
    if not str(task.topic or "").strip() and not str(task.product or "").strip():
        missing.append("主题或产品")
    if missing:
        raise HTTPException(
            422,
            f"{purpose}请补齐：{'、'.join(missing)}",
        )


def _require_external_provider_egress(
    session: Session,
    task: ContentTask,
    provider_id: str,
    *,
    external: bool,
) -> frozenset[str]:
    if not external:
        return frozenset({"public", "internal", "restricted"})

    # Provider activation is a system-level decision.  Generation therefore no
    # longer depends on a task/provider EgressGrant, but restricted enterprise
    # material must still never be selected for an external model call.
    return frozenset({"public", "internal"})


_DATA_LEVEL_RANK = {"public": 0, "internal": 1, "restricted": 2}


def _external_media_reference_data_level(
    session: Session,
    reference: str | Artifact | LibraryItem | ResourceEntry | AssetRef | ProjectAsset,
    *,
    seen: set[tuple[str, str]] | None = None,
) -> str:
    """Resolve the most restrictive level behind an outbound media reference."""

    if seen is None:
        seen = set()
    if isinstance(reference, str):
        candidates = [
            session.get(ProjectAsset, reference),
            session.get(Artifact, reference),
            session.get(LibraryItem, reference),
            session.get(ResourceEntry, reference),
            session.get(AssetRef, reference),
        ]
        levels = [
            _external_media_reference_data_level(session, item, seen=seen)
            for item in candidates
            if item is not None
        ]
        return max(levels or ["internal"], key=_DATA_LEVEL_RANK.__getitem__)

    identity = (type(reference).__name__, reference.id)
    if identity in seen:
        return "internal"
    seen.add(identity)
    levels = ["internal"]
    if isinstance(reference, Artifact):
        content = load(reference.content_json)
        levels.append(draft_content_data_level(source_reference_metadata(content), content))
    elif isinstance(reference, ResourceEntry):
        levels.append(reference.data_level)
    elif isinstance(reference, AssetRef):
        entry = session.get(ResourceEntry, reference.entry_id)
        if entry is not None:
            levels.append(_external_media_reference_data_level(session, entry, seen=seen))
    elif isinstance(reference, LibraryItem):
        levels.append(reference.data_level)
        if reference.resource_entry_id:
            entry = session.get(ResourceEntry, reference.resource_entry_id)
            if entry is not None:
                levels.append(_external_media_reference_data_level(session, entry, seen=seen))
        if reference.storage_key:
            asset_ref = session.scalar(
                select(AssetRef).where(AssetRef.storage_key == reference.storage_key)
            )
            if asset_ref is not None:
                levels.append(_external_media_reference_data_level(session, asset_ref, seen=seen))
    else:
        metadata = load(reference.metadata_json)
        metadata_level = metadata.get("data_level") if isinstance(metadata, dict) else None
        if metadata_level in _DATA_LEVEL_RANK:
            levels.append(metadata_level)
        linked = (
            (AssetRef, reference.asset_ref_id),
            (Artifact, reference.artifact_id),
            (LibraryItem, reference.library_item_id),
            (ResourceEntry, reference.resource_entry_id),
        )
        for model, linked_id in linked:
            item = session.get(model, linked_id) if linked_id else None
            if item is not None:
                levels.append(_external_media_reference_data_level(session, item, seen=seen))
        source_models = {
            "asset_ref": AssetRef,
            "artifact": Artifact,
            "library_item": LibraryItem,
            "resource_entry": ResourceEntry,
        }
        source_model = source_models.get(reference.source_type)
        source = session.get(source_model, reference.source_id) if source_model else None
        if source is not None:
            levels.append(_external_media_reference_data_level(session, source, seen=seen))
    return max(
        (level for level in levels if level in _DATA_LEVEL_RANK),
        key=_DATA_LEVEL_RANK.__getitem__,
    )


def _require_external_media_references(
    session: Session,
    references: list[str | Artifact | LibraryItem | ResourceEntry | AssetRef | ProjectAsset],
    *,
    external: bool,
) -> None:
    if not external:
        return
    for reference in references:
        if _external_media_reference_data_level(session, reference) == "restricted":
            raise HTTPException(403, "受限等级素材不得发送给外部模型")


def _timeline_for_ai(timeline: dict) -> dict:
    """Expose only editable business fields; never provider/file storage details."""

    canvas = timeline.get("canvas") if isinstance(timeline.get("canvas"), dict) else {}
    safe_tracks: list[dict] = []
    for track in timeline.get("tracks", []):
        if not isinstance(track, dict):
            continue
        safe_items: list[dict] = []
        for item in track.get("items", []):
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            safe_items.append(
                {
                    key: value
                    for key, value in {
                        "id": item.get("id"),
                        "label": item.get("label"),
                        "start_seconds": item.get("start_seconds"),
                        "duration_seconds": item.get("duration_seconds"),
                        "trim_in_seconds": item.get("trim_in_seconds"),
                        "trim_out_seconds": item.get("trim_out_seconds"),
                        "volume": item.get("volume"),
                        "speed": item.get("speed"),
                        "enabled": item.get("enabled"),
                        "content": item.get("content"),
                        "transform": item.get("transform") or {},
                        "metadata": {
                            name: metadata[name]
                            for name in (
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
                            )
                            if name in metadata
                        },
                    }.items()
                    if value is not None
                }
            )
        safe_tracks.append(
            {
                "id": track.get("id"),
                "kind": track.get("kind"),
                "name": track.get("name"),
                "muted": bool(track.get("muted", False)),
                "locked": bool(track.get("locked", False)),
                "items": safe_items,
            }
        )
    return {
        "canvas": {
            key: canvas.get(key)
            for key in ("width", "height", "fps", "aspect_ratio")
            if canvas.get(key) is not None
        },
        "tracks": safe_tracks,
    }


def _generated_video_project_operations(content: object) -> list:
    try:
        operations = VIDEO_PROJECT_OPERATION_ADAPTER.validate_python(content)
    except PydanticValidationError as exc:
        raise HTTPException(502, "内容模型返回了无效的视频修改操作") from exc
    forbidden = {"add", "restore"}
    if any(operation.type in forbidden for operation in operations):
        raise HTTPException(502, "内容模型返回了不允许的素材或版本操作")
    if not operations:
        raise HTTPException(502, "内容模型未返回可比较的视频修改")
    return operations


def get_product_resource_for_identity(
    session: Session,
    product_resource_id: str,
    identity: Identity,
) -> ResourceEntry:
    entry = session.get(ResourceEntry, product_resource_id)
    if entry is None:
        item = session.get(LibraryItem, product_resource_id)
        if item is not None and item.resource_entry_id:
            entry = session.get(ResourceEntry, item.resource_entry_id)
    if entry is None or not entry.active:
        raise HTTPException(422, "所选产品资料不存在或已停用")
    if not is_product_resource(entry):
        raise HTTPException(422, "所选资料不是产品资料")
    if (
        entry.owner_id != identity.user_id
        and entry.data_level != "public"
        and not has_permission(identity, "library.manage")
    ):
        raise HTTPException(403, "无权引用该产品资料")
    return entry


def get_knowledge_resources_for_identity(
    session: Session,
    resource_ids: list[str],
    identity: Identity,
    *,
    allow_cross_owner: bool = False,
) -> list[str]:
    """Validate task knowledge selections and return stable, de-duplicated IDs."""
    canonical_ids: list[str] = []
    seen: set[str] = set()
    for raw_id in resource_ids:
        reference_id = str(raw_id).strip()
        if not reference_id:
            raise HTTPException(422, "知识库资料 ID 不能为空")
        entry = session.get(ResourceEntry, reference_id)
        legacy_item = None
        if entry is None:
            legacy_item = session.get(LibraryItem, reference_id)
            if legacy_item is not None and legacy_item.resource_entry_id:
                entry = session.get(ResourceEntry, legacy_item.resource_entry_id)
        resource = entry or legacy_item
        if resource is None:
            raise HTTPException(422, "所选知识库资料不存在")
        if not resource.active:
            raise HTTPException(422, "所选知识库资料已停用")
        if (
            resource.owner_id != identity.user_id
            and resource.data_level != "public"
            and not has_permission(identity, "library.manage")
            and not allow_cross_owner
        ):
            raise HTTPException(403, "无权引用所选知识库资料")
        canonical_id = entry.id if entry is not None else legacy_item.id
        if canonical_id in seen:
            continue
        seen.add(canonical_id)
        canonical_ids.append(canonical_id)
    if len(canonical_ids) > 20:
        raise HTTPException(422, "一次最多选择 20 项知识库资料")
    return canonical_ids


def resolve_generation_knowledge_resources(
    session: Session,
    task: ContentTask,
    requested_ids: list[str] | None,
    identity: Identity,
) -> tuple[list[str] | None, bool]:
    """Canonicalize per-run IDs while permitting only the task's shared defaults."""
    if requested_ids is None:
        return None, True
    stored_ids = load(task.knowledge_resource_ids_json)
    stored = set(stored_ids if isinstance(stored_ids, list) else [])
    canonical_ids = get_knowledge_resources_for_identity(
        session,
        requested_ids,
        identity,
        allow_cross_owner=True,
    )
    uses_only_task_defaults = set(canonical_ids).issubset(stored)
    if not uses_only_task_defaults and not has_permission(identity, "library.manage"):
        canonical_ids = get_knowledge_resources_for_identity(
            session,
            requested_ids,
            identity,
        )
    return canonical_ids, uses_only_task_defaults or has_permission(identity, "library.manage")


def product_has_image(session: Session, entry: ResourceEntry) -> bool:
    if not is_product_resource(entry):
        return False
    version = session.get(ResourceVersion, entry.current_version_id) if entry.current_version_id else None
    item = session.scalar(select(LibraryItem).where(
        LibraryItem.resource_entry_id == entry.id, LibraryItem.active == true(),
    ).order_by(LibraryItem.version.desc()))
    asset = _library_asset_ref(session, item, entry, version)
    return bool(asset and asset.media_type.startswith("image/"))


def visual_reference_ids_for_knowledge(
    session: Session,
    resource_ids: list[str],
) -> list[str]:
    """Return selected visuals, including image/logo media attached to products."""
    result: list[str] = []
    seen: set[str] = set()

    def append_visual(
        entry: ResourceEntry | None,
        legacy_item: LibraryItem | None = None,
    ) -> None:
        if entry is not None:
            if not entry.active or not (entry.resource_kind in {"image", "logo"} or is_product_resource(entry)):
                return
            if is_product_resource(entry) and not product_has_image(session, entry):
                return
            reference_id = entry.id
        elif legacy_item is not None:
            # Legacy rows do not carry a trustworthy resource kind.  Without a
            # canonical ResourceEntry we cannot prove this is an image/logo, so
            # fail closed instead of treating any stored file as visual input.
            return
        else:
            return
        if reference_id in seen:
            return
        has_asset = bool(legacy_item and legacy_item.storage_key)
        if entry is not None and not has_asset:
            has_asset = (
                session.scalar(select(AssetRef.id).where(AssetRef.entry_id == entry.id).limit(1))
                is not None
            )
        if has_asset:
            seen.add(reference_id)
            result.append(reference_id)

    def product_visual_entries(product: ResourceEntry) -> list[ResourceEntry]:
        metadata = load(product.metadata_json)
        candidate_ids: list[str] = []
        if isinstance(metadata, dict):
            raw_media_entry_ids = metadata.get("media_entry_ids", [])
            for raw_id in (
                raw_media_entry_ids if isinstance(raw_media_entry_ids, (list, tuple)) else []
            ):
                if isinstance(raw_id, str) and raw_id.strip():
                    candidate_ids.append(raw_id.strip())
            raw_asset_ref_ids = metadata.get("asset_ref_ids", [])
            for raw_id in raw_asset_ref_ids if isinstance(raw_asset_ref_ids, (list, tuple)) else []:
                if not isinstance(raw_id, str) or not raw_id.strip():
                    continue
                asset = session.get(AssetRef, raw_id.strip())
                if asset is not None:
                    candidate_ids.append(asset.entry_id)
            raw_library_item_ids = metadata.get("media_library_item_ids", [])
            for raw_id in (
                raw_library_item_ids if isinstance(raw_library_item_ids, (list, tuple)) else []
            ):
                if not isinstance(raw_id, str) or not raw_id.strip():
                    continue
                item = session.get(LibraryItem, raw_id.strip())
                if item is not None and item.resource_entry_id:
                    candidate_ids.append(item.resource_entry_id)
        candidate_ids.extend(
            session.scalars(
                select(ResourceEntry.id)
                .where(
                    ResourceEntry.parent_id == product.id,
                    ResourceEntry.active == true(),
                    ResourceEntry.resource_kind.in_(("image", "logo")),
                )
                .order_by(ResourceEntry.created_at, ResourceEntry.id)
            ).all()
        )
        entries: list[ResourceEntry] = []
        entry_ids: set[str] = set()
        for candidate_id in candidate_ids:
            if candidate_id in entry_ids:
                continue
            candidate = session.get(ResourceEntry, candidate_id)
            if (
                candidate is None
                or not candidate.active
                or candidate.resource_kind not in {"image", "logo"}
                or candidate.parent_id != product.id
            ):
                continue
            entry_ids.add(candidate.id)
            entries.append(candidate)
        return entries

    for reference_id in resource_ids:
        entry = session.get(ResourceEntry, reference_id)
        legacy_item = None
        if entry is None:
            legacy_item = session.get(LibraryItem, reference_id)
            if legacy_item is not None and legacy_item.resource_entry_id:
                entry = session.get(ResourceEntry, legacy_item.resource_entry_id)
        if entry is not None and is_product_resource(entry):
            append_visual(entry, legacy_item)
            for child in product_visual_entries(entry):
                append_visual(child)
            continue
        append_visual(entry, legacy_item)
    return result


def visual_assets_for_generation(
    session: Session,
    visual_ids: list[str],
    identity: Identity,
    asset_store: AssetStorePort,
    *,
    external: bool,
) -> tuple[list[str], tuple[bytes, ...], tuple[str, ...]]:
    """Load canonical visuals through their own access and egress boundaries."""
    canonical_visual_ids = list(dict.fromkeys(visual_ids))
    resolved: list[tuple[str, str]] = []
    for reference_id in canonical_visual_ids:
        entry = session.get(ResourceEntry, reference_id)
        if entry is None or not entry.active or not (entry.resource_kind in {"image", "logo"} or product_has_image(session, entry)):
            raise HTTPException(422, "产品视觉资料不存在、已停用或不是图片")
        storage_key, media_type = library_asset_for_identity(
            session,
            identity,
            reference_id,
            # A parent product relationship is not an independent access grant.
            allow_cross_owner=False,
        )
        if not media_type.startswith("image/"):
            raise HTTPException(422, "所选产品的视觉资料必须是图片类型")
        resolved.append((storage_key, media_type))
    _require_external_media_references(
        session,
        canonical_visual_ids,
        external=external,
    )
    return (
        canonical_visual_ids,
        tuple(asset_store.read_bytes(storage_key) for storage_key, _ in resolved),
        tuple(media_type for _, media_type in resolved),
    )


def product_visual_assets_for_generation(
    session: Session,
    resource_ids: list[str],
    identity: Identity,
    asset_store: AssetStorePort,
    *,
    external: bool,
) -> tuple[list[str], tuple[bytes, ...], tuple[str, ...]]:
    """Expand products, then resolve every child through its own access boundary."""
    product_ids = [
        reference_id
        for reference_id in resource_ids
        if (
            (entry := session.get(ResourceEntry, reference_id)) is not None
            and is_product_resource(entry)
        )
    ]
    return visual_assets_for_generation(
        session,
        visual_reference_ids_for_knowledge(session, product_ids),
        identity,
        asset_store,
        external=external,
    )


def bounded_knowledge_prompt_parts(
    selected_knowledge: tuple[KnowledgeChunk, ...],
    *,
    max_chars: int,
    bullet: bool = False,
) -> list[str]:
    """Represent every selected source fairly inside a shared prompt budget."""
    if not selected_knowledge or max_chars <= 0:
        return []
    per_item_budget = max(1, max_chars // len(selected_knowledge))
    parts: list[str] = []
    for item in selected_knowledge:
        normalized_text = " ".join(item.text.split())
        prefix = f"{'- ' if bullet else ''}{item.title[:120]}（{item.category[:60]}）："
        excerpt_budget = max(0, per_item_budget - len(prefix))
        parts.append(f"{prefix}{normalized_text[:excerpt_budget]}"[:per_item_budget])
    return parts


def grounded_image_prompt(
    prompt_parts: list[str],
    selected_knowledge: tuple[KnowledgeChunk, ...],
    *,
    max_chars: int = 10_000,
) -> str:
    """Add bounded enterprise facts to an image prompt without exceeding limits."""
    base_prompt = "；".join(part for part in prompt_parts if part)
    if not selected_knowledge:
        return base_prompt[:max_chars]
    knowledge_parts = bounded_knowledge_prompt_parts(
        selected_knowledge,
        max_chars=6_000,
    )
    if not knowledge_parts:
        return base_prompt[:max_chars]
    knowledge_prompt = "必须依据以下已选企业知识库资料构图，不得虚构品牌或产品事实：" + "；".join(
        knowledge_parts
    )
    separator = "；" if base_prompt else ""
    base_budget = max(0, max_chars - len(separator) - len(knowledge_prompt))
    return f"{base_prompt[:base_budget]}{separator}{knowledge_prompt}"[:max_chars]


def request_scoped_idempotency_key(
    idempotency_key: str | None,
    *,
    namespace: str,
    request_scope: dict,
) -> str | None:
    """Bind a client retry key to the provider-relevant request payload.

    Image refinement controls may reuse a stable UI action key.  The same
    request must still replay, while changing the modification prompt, source,
    references, or generation parameters must create a new provider request.
    """

    if idempotency_key is None:
        return None
    canonical_scope = json.dumps(
        request_scope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(
        f"{namespace}\0{idempotency_key}\0{canonical_scope}".encode()
    ).hexdigest()
    prefix = idempotency_key[: 128 - len(digest) - 1]
    return f"{prefix}:{digest}" if prefix else digest


PRODUCT_VISUAL_CONSISTENCY_PROMPT = (
    "必须以提供的产品参考图为主体，保持瓶型、包装结构、品牌 Logo 与标签文字一致，不得替换或虚构产品"
)


def grounded_video_prompt(
    prompt: str,
    selected_knowledge: tuple[KnowledgeChunk, ...],
    *,
    max_chars: int = 10_000,
) -> str:
    """Attach the same bounded knowledge digests used by text and image generation."""
    base_prompt = prompt.strip()
    if not selected_knowledge:
        return base_prompt[:max_chars]
    knowledge_parts = bounded_knowledge_prompt_parts(
        selected_knowledge,
        max_chars=5_000,
        bullet=True,
    )
    if not knowledge_parts:
        return base_prompt[:max_chars]
    knowledge_prompt = (
        "\n知识库重点摘要（只使用这些已核实信息，不得虚构品牌或产品事实）：\n"
        + "\n".join(knowledge_parts)
    )
    base_budget = max(0, max_chars - len(knowledge_prompt))
    return f"{base_prompt[:base_budget]}{knowledge_prompt}"[:max_chars]


def selected_stage_artifacts(
    session: Session,
    task: ContentTask,
    stage_key: str,
    kind: str,
) -> list[Artifact]:
    """Resolve only the active user selection for an applicable workflow stage."""
    stage = get_stage(session, task, stage_key)
    if stage.state == "stale":
        raise HTTPException(409, f"{stage_key} 阶段已失效，请重新选择或生成")
    artifact_ids = load(stage.selected_artifact_ids_json).get(kind, [])
    if isinstance(artifact_ids, str):
        artifact_ids = [artifact_ids]
    artifacts = [
        artifact
        for artifact_id in artifact_ids
        if (artifact := session.get(Artifact, artifact_id)) is not None
        and artifact.status not in {DISCARDED_ARTIFACT_STATUS, "stale", "legacy_mock"}
        and not load(artifact.content_json).get("legacy_mock")
    ]
    if len(artifacts) != len(artifact_ids):
        raise HTTPException(409, f"{stage_key} 阶段的已选内容不存在或已经失效")
    return artifacts


def voice_source_text(
    session: Session,
    task: ContentTask,
    explicit_text: str,
) -> tuple[str, Artifact | None]:
    """Resolve narration from the user's active workflow selection.

    A caller-provided text is an intentional override. Otherwise voice generation
    follows the selected workflow: storyboard narration first, then copy. It must
    never silently fall back to a task title or topic.
    """
    override = explicit_text.strip()
    if override:
        return override, None

    storyboards = selected_stage_artifacts(
        session,
        task,
        "storyboard",
        "storyboard",
    )
    if storyboards:
        storyboard = storyboards[0]
        shots = load(storyboard.content_json).get("shots", [])
        narration_parts: list[str] = []
        if isinstance(shots, list):
            for shot in shots:
                if not isinstance(shot, dict):
                    continue
                narration = next(
                    (
                        str(shot.get(field, "")).strip()
                        for field in ("narration", "voiceover", "voice_over", "subtitle")
                        if str(shot.get(field, "")).strip()
                    ),
                    "",
                )
                if narration:
                    narration_parts.append(narration)
        if narration_parts:
            return "\n".join(narration_parts), storyboard

    copies = selected_stage_artifacts(session, task, "copy", "copy")
    if copies:
        copy = copies[0]
        content = load(copy.content_json)
        candidate = selected_text_candidate(content)
        sources = [candidate, content] if isinstance(candidate, dict) else [content]
        for source in sources:
            for field in ("body", "text", "title"):
                value = str(source.get(field, "")).strip()
                if value:
                    return value, copy

    raise HTTPException(409, "请先选择分镜旁白或文案，或直接输入需要配音的文字")


def artifact_belongs_to_or_is_linked(
    session: Session,
    task: ContentTask,
    artifact: Artifact,
    stage_key: str | None = None,
) -> bool:
    if artifact.task_id == task.id:
        return True
    statement = select(ArtifactLink.id).where(
        ArtifactLink.target_task_id == task.id,
        ArtifactLink.artifact_id == artifact.id,
    )
    if stage_key is not None:
        statement = statement.where(ArtifactLink.stage_key == stage_key)
    return session.scalar(statement) is not None


def confirmed_full_workflow_title(
    session: Session,
    task: ContentTask,
    *,
    read_only: bool = False,
) -> tuple[Artifact, dict, str] | None:
    """Resolve the one confirmed title that owns copy titles in a full workflow.

    Older/incomplete tasks that have not confirmed the title stage retain their
    existing generation behaviour.  Once a title is confirmed, however, copy
    generation must never fall back to a selected draft or let a provider invent
    a second headline.
    """

    if task.mode != "full":
        return None
    stage = (
        session.scalar(
            select(TaskStage).where(
                TaskStage.task_id == task.id,
                TaskStage.stage_key == "topic",
            )
        )
        if read_only
        else get_stage(session, task, "topic")
    )
    if stage is None or stage.state != "confirmed":
        return None
    confirmed = load(stage.confirmed_artifact_ids_json or "{}")
    confirmed_ids = confirmed.get("topic", []) if isinstance(confirmed, dict) else []
    if isinstance(confirmed_ids, str):
        confirmed_ids = [confirmed_ids]
    confirmed_ids = list(dict.fromkeys(confirmed_ids))
    if len(confirmed_ids) != 1:
        raise HTTPException(409, "完整流程文案生成前必须且只能确认一个标题版本")
    artifact = session.get(Artifact, confirmed_ids[0])
    if (
        artifact is None
        or artifact.kind != "topic"
        or artifact.status in {DISCARDED_ARTIFACT_STATUS, "stale", "legacy_mock"}
        or not artifact_belongs_to_or_is_linked(session, task, artifact, "topic")
        or bool(load(artifact.content_json or "{}").get("legacy_mock"))
    ):
        raise HTTPException(409, "当前已确认标题不存在、已失效或不属于当前任务")
    content = load(artifact.content_json or "{}")
    candidate = selected_text_candidate(content)
    title = str(candidate.get("title") or candidate.get("text") or "").strip()
    if not title:
        raise HTTPException(422, "当前已确认标题内容为空，请先修改标题")
    return artifact, content, title[:500]


def copy_content_with_title(source_content: dict, title: str) -> dict:
    """Return provider input pinned to the workflow's canonical title."""

    content = dict(source_content)
    candidate = dict(selected_text_candidate(content))
    candidate["title"] = title
    content["candidates"] = [candidate]
    content["selected_candidate"] = candidate
    content["selected_candidate_index"] = 0
    return content


def require_current_full_copy_lineage(
    session: Session,
    task: ContentTask,
    artifact: Artifact,
    *,
    read_only: bool = False,
) -> tuple[Artifact, dict, str] | None:
    """Keep a full-workflow copy pinned to the currently confirmed title."""

    title_context = confirmed_full_workflow_title(session, task, read_only=read_only)
    if title_context is None:
        return None
    title_artifact, _title_content, title = title_context
    content = load(artifact.content_json or "{}")
    candidate_title = str(selected_text_candidate(content).get("title") or "").strip()
    source_title_id = str(content.get("source_title_artifact_id") or "")
    if candidate_title == title and source_title_id == title_artifact.id:
        return title_context
    if read_only:
        return title_context
    reminted = dict(content)
    candidates = reminted.get("candidates")
    if isinstance(candidates, list):
        reminted["candidates"] = [
            {**item, "title": title} if isinstance(item, dict) else item for item in candidates
        ]
    selected = reminted.get("selected_candidate")
    if isinstance(selected, dict):
        reminted["selected_candidate"] = {**selected, "title": title}
    reminted["source_title_artifact_id"] = title_artifact.id
    reminted["source_title_version"] = title_artifact.version
    reminted["source_title"] = title
    artifact.content_json = dump(reminted)
    return title_context


def current_copy_artifact_for_downstream(
    session: Session,
    task: ContentTask,
    *,
    requested_artifact_id: str | None = None,
    user_id: str,
) -> Artifact | None:
    """Resolve the sole server-authoritative copy used by downstream stages.

    The copy stage's current selection is authoritative, independent of the old
    manual-confirm button and the denormalized artifact status.  Only a valid
    single selection outside explicitly invalidated/unstarted states is accepted.
    A previous current selection remains usable while a replacement generation is
    running or if that replacement fails. In particular, this never falls back to
    the latest historical artifact after configuration changes have made the stage
    stale and cleared its selection.

    ``requested_artifact_id`` is retained as a compatibility guard for older
    clients.  It may identify the current selection, but can never override it.
    """

    requested_id = (requested_artifact_id or "").strip() or None

    def unavailable(message: str) -> None:
        if requested_id is not None:
            raise HTTPException(409, message)

    if "copy" not in operable_stages(task):
        unavailable("当前任务没有可引用的当前文案，请刷新视频工程后重试")
        return None
    stage = get_stage(session, task, "copy")
    if stage.state in {"stale", "not_started", "skipped"}:
        unavailable("当前文案已发生变化，请返回文案阶段重新选择后再试")
        return None
    selected = load(stage.selected_artifact_ids_json or "{}")
    selected_ids = selected.get("copy", []) if isinstance(selected, dict) else []
    if isinstance(selected_ids, str):
        selected_ids = [selected_ids]
    selected_ids = list(dict.fromkeys(item for item in selected_ids if isinstance(item, str)))
    if len(selected_ids) != 1:
        unavailable("当前文案选择已变化，请返回文案阶段选择唯一版本后再试")
        return None
    artifact = session.get(Artifact, selected_ids[0])
    if (
        artifact is None
        or artifact.kind != "copy"
        or artifact.status in {DISCARDED_ARTIFACT_STATUS, "stale", "legacy_mock"}
        or not artifact_belongs_to_or_is_linked(session, task, artifact, "copy")
        or bool(load(artifact.content_json or "{}").get("legacy_mock"))
    ):
        unavailable("当前选择的文案不存在、已失效或不属于当前任务")
        return None
    try:
        require_current_full_copy_lineage(session, task, artifact)
    except HTTPException:
        if requested_id is not None:
            raise
        return None
    if requested_id is not None and requested_id != artifact.id:
        raise HTTPException(409, "引用的文案不是当前版本，请刷新视频工程后重试")
    # Lazily migrates pre-change tasks which already had one selected copy but
    # were still waiting for the now-removed manual confirmation action. This
    # repairs denormalized readiness without changing selection authority or the
    # task version underneath an already-open video editor. The copy-stage
    # version still advances so a stale stage-selection write is rejected.
    if stage.state in {"confirmed", "awaiting_confirmation"}:
        activate_copy_selection(
            session,
            task,
            stage,
            user_id=user_id,
            bump_stage_version=True,
        )
    return artifact


def require_current_video_project_render(
    session: Session,
    task: ContentTask,
    artifacts: list[Artifact] | None,
) -> None:
    """New confirmations must represent the current committed editor timeline.

    Archived downloads and legacy tasks without a video project are untouched.
    """
    project = session.scalar(select(VideoProject).where(VideoProject.task_id == task.id))
    if project is None:
        return
    if artifacts is None:
        stage = get_stage(session, task, "video_preview")
        selected = load(stage.selected_artifact_ids_json or "{}")
        ids = selected.get("video", []) if isinstance(selected, dict) else []
        ids = [ids] if isinstance(ids, str) else ids
        artifacts = [item for artifact_id in ids if (item := session.get(Artifact, artifact_id))]
    videos = [item for item in artifacts if item.kind == "video"]
    if len(videos) != 1:
        raise HTTPException(409, "请先完成当前视频工程的合成，再进入导出；AI 视频候选不是最终成片")
    content = load(videos[0].content_json or "{}")
    if (
        not isinstance(content, dict)
        or content.get("purpose") != "final_project_render"
        or content.get("project_id") != project.id
        or not isinstance(content.get("timeline_composition_version"), int)
        or content["timeline_composition_version"] < 2
    ):
        raise HTTPException(409, "当前视频尚未包含完整时间线，请点击“完成并进入确认导出”重新合成")
    if content.get("revision_id") != project.current_revision_id:
        raise HTTPException(409, "视频工程已修改，请重新完成合成后再导出，避免遗漏最新修改")


def current_confirmed_final_video(
    session: Session,
    task: ContentTask,
) -> Artifact:
    """Resolve the sole final video currently confirmed by the workflow."""

    stage = get_stage(session, task, "video_preview")
    confirmed = load(stage.confirmed_artifact_ids_json or "{}")
    confirmed_ids = confirmed.get("video", []) if isinstance(confirmed, dict) else []
    if isinstance(confirmed_ids, str):
        confirmed_ids = [confirmed_ids]
    confirmed_ids = list(dict.fromkeys(confirmed_ids))
    if stage.state != "confirmed" or len(confirmed_ids) != 1:
        raise HTTPException(409, "请先选择并确认一个最终成片")
    artifact = session.get(Artifact, confirmed_ids[0])
    if (
        artifact is None
        or artifact.kind != "video"
        or artifact.status != "confirmed"
        or not artifact_belongs_to_or_is_linked(
            session,
            task,
            artifact,
            "video_preview",
        )
    ):
        raise HTTPException(409, "当前确认的最终成片不存在、已失效或不属于当前任务")
    content = load(artifact.content_json or "{}")
    profile = str(content.get("render_profile") or "")
    if content.get("purpose") == "exact_project_preview" or profile.startswith("preview-"):
        raise HTTPException(409, "预览视频不能作为最终成片导出")
    return artifact


def confirmed_copy_context_for_video(
    session: Session,
    task: ContentTask,
    source_copy_artifact_id: str | None,
    *,
    user_id: str | None = None,
) -> tuple[Artifact | None, str]:
    """Resolve the current copy selection for AI video.

    Only the selected candidate's title and body are forwarded.  Summaries are
    intentionally excluded because they are UI metadata, not production copy.
    """

    artifact = current_copy_artifact_for_downstream(
        session,
        task,
        requested_artifact_id=source_copy_artifact_id,
        user_id=user_id or task.owner_id,
    )
    if artifact is None:
        return None, ""
    content = load(artifact.content_json or "{}")
    candidate = selected_text_candidate(content)
    if not isinstance(candidate, dict):
        raise HTTPException(422, "当前文案内容格式无效")
    title = str(candidate.get("title") or "").strip()[:500]
    body = str(candidate.get("body") or candidate.get("text") or "").strip()[:5_000]
    if not title and not body:
        raise HTTPException(422, "当前文案缺少标题和正文")
    parts = []
    if title:
        parts.append(f"当前文案标题：{title}")
    if body:
        parts.append(f"当前文案正文：{body}")
    return artifact, "\n".join(parts)


def library_asset_for_identity(
    session: Session,
    identity: Identity,
    reference_id: str,
    *,
    allow_cross_owner: bool = False,
) -> tuple[str, str]:
    """Resolve an opaque library ID to a stored asset after access validation."""
    item = session.get(LibraryItem, reference_id)
    entry = None
    asset = None
    if item is not None:
        if not item.active:
            raise HTTPException(409, "企业素材已经停用")
        if (
            item.owner_id != identity.user_id
            and item.data_level != "public"
            and not has_permission(identity, "library.manage")
            and not allow_cross_owner
        ):
            raise HTTPException(403, "无权访问该企业素材")
        entry = (
            session.get(ResourceEntry, item.resource_entry_id) if item.resource_entry_id else None
        )
        if item.storage_key:
            asset = session.scalar(select(AssetRef).where(AssetRef.storage_key == item.storage_key))
            media_type = asset.media_type if asset is not None else "application/octet-stream"
            return item.storage_key, media_type
    if entry is None:
        entry = session.get(ResourceEntry, reference_id)
    if entry is None:
        asset = session.get(AssetRef, reference_id)
        if asset is not None:
            entry = session.get(ResourceEntry, asset.entry_id)
    if entry is None:
        raise HTTPException(404, "企业素材不存在")
    if not entry.active:
        raise HTTPException(409, "企业素材已经停用")
    if (
        entry.owner_id != identity.user_id
        and entry.data_level != "public"
        and not has_permission(identity, "library.manage")
        and not allow_cross_owner
    ):
        raise HTTPException(403, "无权访问该企业素材")
    if asset is None:
        statement = select(AssetRef).where(AssetRef.entry_id == entry.id)
        if entry.current_version_id:
            current = session.scalar(
                statement.where(AssetRef.version_id == entry.current_version_id).order_by(
                    AssetRef.created_at.desc()
                )
            )
            if current is not None:
                asset = current
        if asset is None:
            asset = session.scalar(statement.order_by(AssetRef.created_at.desc()))
    if asset is None:
        raise HTTPException(409, "该企业资料没有可用媒体文件")
    return asset.storage_key, asset.media_type


def media_asset_for_identity(
    session: Session,
    identity: Identity,
    reference_id: str,
    *,
    expected_prefix: str | None,
    allow_admin_read: bool = False,
) -> tuple[str, str]:
    artifact = session.get(Artifact, reference_id)
    if artifact is not None:
        source_task = get_task_or_404(
            session, artifact.task_id, identity, allow_admin_read=allow_admin_read
        )
        if (
            source_task is None
            or not artifact.storage_key
            or artifact.status in {DISCARDED_ARTIFACT_STATUS, "legacy_mock"}
            or load(artifact.content_json).get("legacy_mock")
        ):
            raise HTTPException(409, "引用的生成素材不可用")
        media_type = str(load(artifact.content_json).get("media_type") or "")
        if not media_type:
            media_type = {
                "image": "image/png",
                "voice": "audio/mpeg",
                "video": "video/mp4",
                "ai_video_clip": "video/mp4",
            }.get(artifact.kind, "application/octet-stream")
        result = (artifact.storage_key, media_type)
    else:
        result = library_asset_for_identity(session, identity, reference_id)
    if expected_prefix is not None and not result[1].startswith(f"{expected_prefix}/"):
        raise HTTPException(422, f"所选素材必须是 {expected_prefix} 类型")
    return result


def task_json(
    task: ContentTask,
    *,
    session: Session | None = None,
    updated_at: datetime | None = None,
    identity: Identity | None = None,
) -> dict:
    archived = is_archived_snapshot(task, session)
    status = task.status
    workflow_status = (task.archived_workflow_status or status) if archived else status
    if not workflow_status or workflow_status == "archived":
        workflow_status = "draft"
    # ``status`` is denormalized for list performance.  Older records can
    # retain ``confirmed`` after an upstream stage was changed, so never
    # advertise completion unless the export stage is currently confirmed.
    if workflow_status == "confirmed" and session is not None and not archived:
        export_stage = next(
            (item for item in ensure_workflow(session, task) if item.stage_key == "export"),
            None,
        )
        if export_stage is None or export_stage.state != "confirmed":
            workflow_status = "draft"
        status = workflow_status
    return {
        "id": task.id,
        "owner_id": task.owner_id,
        "can_edit": identity is not None and can_access(task, identity.user_id, identity.department),
        "mode": task.mode,
        "name": task.name,
        "topic": task.topic,
        "product": task.product,
        "product_resource_id": task.product_resource_id,
        "knowledge_resource_ids": load(task.knowledge_resource_ids_json),
        "keywords": load(task.keywords_json),
        "audience": task.audience,
        "content_type": task.content_type,
        "style": task.style,
        "length": task.length,
        "requirements": task.requirements,
        "current_stage": task.current_stage,
        "status": status,
        "workflow_status": workflow_status,
        "archived": archived,
        "archived_at": archive_snapshot_time(task, session),
        **edit_draft_fields(session, task.id),
        "version": task.version,
        "created_at": task.created_at,
        "updated_at": updated_at or task.updated_at,
    }


def artifact_json(item: Artifact) -> dict:
    return {
        "id": item.id,
        "task_id": item.task_id,
        "kind": item.kind,
        "stage_key": item.stage_key,
        "generation_run_id": item.generation_run_id,
        "candidate_index": item.candidate_index,
        "version": item.version,
        "status": item.status,
        "content": artifact_content(item.kind, load(item.content_json)),
        "storage_key": item.storage_key,
        "parent_id": item.parent_id,
        "created_by": item.created_by,
        "created_at": item.created_at,
    }


def job_json(job: Job) -> dict:
    result = load(job.result_json)
    result.pop(PROVIDER_MEDIA_PUBLICATIONS_RESULT_KEY, None)
    if job.kind in {"video_project_render", "video_project_export"}:
        result.pop("storage_key", None)
        if job.status == "succeeded":
            result["download_url"] = f"/api/v1/jobs/{job.id}/download"
    return {
        "id": job.id,
        "task_id": job.task_id,
        "generation_run_id": job.generation_run_id,
        "kind": job.kind,
        "status": job.status,
        "progress": job.progress,
        "result": result,
        "error": job.error,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "heartbeat_at": job.heartbeat_at,
        "cancel_requested_at": job.cancel_requested_at,
        "completed_at": job.completed_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def workflow_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkflowConflictError):
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


def run_artifacts(session: Session, run: GenerationRun) -> list[Artifact]:
    artifact_ids = load(run.result_artifact_ids_json)
    return [
        artifact
        for artifact_id in artifact_ids
        if (artifact := session.get(Artifact, artifact_id)) is not None
    ]


def legacy_candidate_run_response(
    session: Session,
    run: GenerationRun,
    artifacts: list[Artifact],
    aggregate_field: str,
) -> dict:
    if not artifacts:
        raise HTTPException(409, "生成批次尚未产生候选内容")
    response = artifact_json(artifacts[0])
    candidates = []
    for artifact in artifacts:
        content = load(artifact.content_json)
        value = content.get(aggregate_field, [])
        if isinstance(value, list):
            candidates.extend(value)
    response["content"] = {
        **load(artifacts[0].content_json),
        aggregate_field: candidates,
    }
    response["generation_run"] = generation_run_json(session, run, artifact_json)
    response["artifacts"] = [artifact_json(item) for item in artifacts]
    return response


def create_resource_version_record(
    session: Session,
    *,
    owner_id: str,
    resource_kind: str,
    category: str,
    title: str,
    text_content: str,
    data_level: str,
    version_number: int,
    created_by: str,
    entry: ResourceEntry | None = None,
    tags: list[str] | None = None,
    parent_id: str | None = None,
    source_url: str | None = None,
    crawled_at: datetime | None = None,
    freshness: str | None = None,
    facts: list | None = None,
    notes: str = "",
    metadata: dict | None = None,
    asset_key: str | None = None,
    asset_media_type: str = "application/octet-stream",
    asset_size: int | None = None,
    asset_checksum: str | None = None,
    chunks: tuple[KnowledgeChunk, ...] | None = None,
    tenant_id: str | None = None,
    department_id: str | None = None,
    allowed_roles: list[str] | None = None,
) -> tuple[ResourceEntry, ResourceVersion]:
    if resource_kind not in RESOURCE_KINDS:
        raise HTTPException(422, "不支持的资源类型")
    normalized_category = category.strip()
    if not normalized_category:
        raise HTTPException(422, "业务分类不能为空")
    metadata = dict(metadata or {})
    if entry is None:
        entry = ResourceEntry(
            owner_id=owner_id,
            resource_kind=resource_kind,
            title=title,
            category=normalized_category,
            tags_json=dump(
                list(dict.fromkeys(item.strip() for item in (tags or []) if item.strip()))
            ),
            parent_id=parent_id,
            data_level=data_level,
            tenant_id=tenant_id,
            department_id=department_id,
            allowed_roles_json=dump(list(dict.fromkeys(allowed_roles or []))),
            metadata_json=dump(metadata),
        )
        session.add(entry)
        session.flush()
    else:
        entry.resource_kind = resource_kind
        entry.title = title
        entry.category = normalized_category
        entry.tags_json = dump(
            list(dict.fromkeys(item.strip() for item in (tags or []) if item.strip()))
        )
        entry.parent_id = parent_id
        entry.data_level = data_level
        entry.tenant_id = tenant_id
        entry.department_id = department_id
        entry.allowed_roles_json = dump(list(dict.fromkeys(allowed_roles or [])))
        entry.metadata_json = dump(metadata)
        entry.active = True
    version = ResourceVersion(
        entry_id=entry.id,
        version=version_number,
        text_content=text_content,
        source_url=source_url,
        crawled_at=crawled_at,
        freshness=freshness,
        facts_json=dump(facts or []),
        notes=notes,
        metadata_json=dump(metadata),
        created_by=created_by,
    )
    session.add(version)
    session.flush()
    entry.current_version_id = version.id
    chunk_values = (
        [(chunk.text, chunk.metadata) for chunk in chunks if chunk.text.strip()]
        if chunks is not None
        else [
            (text_content[start : start + 1200], {}) for start in range(0, len(text_content), 1200)
        ]
    )
    for index, (chunk_text, chunk_metadata) in enumerate(chunk_values):
        session.add(
            ResourceChunk(
                version_id=version.id,
                sequence=index,
                text=chunk_text,
                char_count=len(chunk_text),
                metadata_json=dump(chunk_metadata),
            )
        )
    if asset_key:
        session.add(
            AssetRef(
                entry_id=entry.id,
                version_id=version.id,
                storage_key=asset_key,
                media_type=asset_media_type,
                size_bytes=asset_size,
                checksum=asset_checksum,
                metadata_json=dump(metadata),
            )
        )
    if source_url:
        session.add(
            ExternalRef(
                entry_id=entry.id,
                system="source-url",
                external_id=f"{entry.id}:{version_number}",
                source_url=source_url,
                metadata_json=dump(metadata),
            )
        )
    session.flush()
    return entry, version


def filter_declared_source_ids(value, allowed_ids: set[str]):
    if isinstance(value, dict):
        result = {key: filter_declared_source_ids(item, allowed_ids) for key, item in value.items()}
        if "source_ids" in result:
            source_ids = result["source_ids"]
            result["source_ids"] = (
                [item for item in source_ids if isinstance(item, str) and item in allowed_ids]
                if isinstance(source_ids, list)
                else []
            )
        return result
    if isinstance(value, list):
        return [filter_declared_source_ids(item, allowed_ids) for item in value]
    return value


def get_artifact_for_user(
    session: Session,
    artifact_id: str,
    identity: Identity,
    *,
    include_discarded: bool = False,
    allow_admin_read: bool = False,
) -> tuple[Artifact, ContentTask]:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(404, "内容版本不存在")
    task = get_task_or_404(
        session, artifact.task_id, identity, allow_admin_read=allow_admin_read
    )
    if artifact.status == DISCARDED_ARTIFACT_STATUS and not include_discarded:
        raise HTTPException(409, "内容版本已作废")
    return artifact, task


def prepare_content_generation(
    *,
    session: Session,
    task: ContentTask,
    identity: Identity,
    instruction: str,
    knowledge_provider: KnowledgeProvider,
    text_provider: TextProvider,
    knowledge_resource_ids: list[str] | None = None,
    source_content: dict | None = None,
) -> ContentGenerationInput:
    canonical_resource_ids, allow_cross_owner = resolve_generation_knowledge_resources(
        session,
        task,
        knowledge_resource_ids,
        identity,
    )
    try:
        return prepare_generation_input(
            session=session,
            task=task,
            requester_id=identity.user_id,
            instruction=instruction,
            knowledge_provider=knowledge_provider,
            text_provider=text_provider,
            knowledge_top_k=settings.knowledge_top_k,
            knowledge_resource_ids=canonical_resource_ids,
            allow_cross_owner_knowledge=allow_cross_owner,
            source_content=source_content,
            requester_department=identity.department,
            requester_roles=identity.roles,
        )
    except EgressDeniedError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KnowledgeProviderContractError as exc:
        raise HTTPException(502, str(exc)) from exc
    except KnowledgeSelectionError as exc:
        raise HTTPException(422, str(exc)) from exc


def generation_knowledge_resource_ids(parameters: dict) -> list[str] | None:
    if "knowledge_resource_ids" not in parameters:
        return None
    value = parameters.get("knowledge_resource_ids")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise HTTPException(422, "knowledge_resource_ids 必须是字符串数组")
    if len(value) > 20:
        raise HTTPException(422, "一次最多选择 20 项知识库资料")
    return value


def call_text_generation(
    callback: Callable[[], TextGenerationResult], *, clear_generation_error: bool = False
) -> TextGenerationResult:
    try:
        return callback()
    except ProviderCallError as exc:
        if clear_generation_error:
            raise HTTPException(502, _generation_error_detail(exc)) from exc
        raise HTTPException(502, f"内容模型调用失败：{exc}") from exc


def queue_ai_video_job(
    session: Session,
    task: ContentTask,
    run: GenerationRun,
    payload: GenerateRequest,
    identity: Identity,
    provider: AiVideoGenerationPort,
    *,
    shot_index: int = 0,
    prompt_override: str | None = None,
    image_url_override: str | None = None,
    source_asset_id: str | None = None,
    duration_seconds_override: float | None = None,
    ratio_override: str | None = None,
    resolution_override: str | None = None,
    preserve_other_shots: bool = False,
) -> Job:
    prompt = (
        prompt_override
        or payload.instruction
        or str(payload.parameters.get("prompt") or "")
        or task.topic
    )
    duration_seconds = (
        duration_seconds_override
        if duration_seconds_override is not None
        else payload.parameters.get("duration_seconds")
    )
    ratio = ratio_override or payload.parameters.get("ratio")
    resolution = resolution_override or payload.parameters.get("resolution")
    request = AiVideoGenerationRequest(
        prompt=prompt or task.name,
        image_url=image_url_override,
        duration_seconds=duration_seconds,
        ratio=ratio,
        resolution=resolution,
        callback_url=payload.parameters.get("callback_url"),
        generate_audio=bool(payload.parameters.get("generate_audio", False)),
        return_last_frame=bool(payload.parameters.get("return_last_frame", False)),
        idempotency_key=(
            f"{payload.idempotency_key}:{shot_index}" if payload.idempotency_key else None
        ),
        extra=dict(payload.parameters.get("extra", {})),
    )
    try:
        submitted = provider.submit(request)
    except ProviderCallError as exc:
        details = _generation_error_detail(exc)
        fail_generation_run(session, task, run, details["message"])
        raise HTTPException(502, details) from exc
    except ValueError as exc:
        fail_generation_run(session, task, run, str(exc))
        raise HTTPException(422, str(exc)) from exc
    job = Job(
        task_id=task.id,
        generation_run_id=run.id,
        owner_id=identity.user_id,
        kind="ai_video_generation",
        status="queued",
        progress=submitted.progress or 0,
        result_json=dump(
            {
                "provider_task_id": submitted.provider_task_id,
                "provider_id": submitted.provider_id,
                "model": submitted.model,
                "request": {
                    "instruction": payload.instruction,
                    "parameters": payload.parameters,
                    "shot_index": shot_index,
                    "prompt": prompt,
                    "source_asset_id": source_asset_id,
                    "duration_seconds": duration_seconds,
                    "ratio": ratio,
                    "resolution": resolution,
                    "preserve_other_shots": preserve_other_shots,
                },
            }
        ),
    )
    session.add(job)
    session.flush()
    return job


def run_ai_video_job(
    job_id: str,
    provider: AiVideoGenerationPort,
    asset_store: AssetStorePort,
) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None or job.status == "cancelled":
            return
        job.status = "running"
        job.progress = max(job.progress, 1)
        session.commit()
        provider_task_id = load(job.result_json).get("provider_task_id")
    try:

        def update_progress(provider_task) -> None:
            with SessionLocal() as progress_session:
                current = progress_session.get(Job, job_id)
                if current is None or current.status == "cancelled":
                    return
                current.status = "running" if provider_task.status == "running" else current.status
                current.progress = provider_task.progress or current.progress
                progress_session.commit()

        result = wait_for_ai_video_task(
            provider,
            provider_task_id,
            poll_interval_seconds=settings.ai_video_poll_interval_seconds,
            max_wait_seconds=settings.ai_video_max_wait_seconds,
            on_update=update_progress,
        )
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            if result.status == "cancelled" or job.status == "cancelled":
                job.status = "cancelled"
                job.progress = result.progress or job.progress
                run = session.get(GenerationRun, job.generation_run_id)
                if run is not None:
                    run.status = "cancelled"
                    run.completed_at = datetime.now(UTC)
                session.commit()
                return
            if result.status != "succeeded" or not result.video_url:
                raise ProviderCallError(
                    result.error_message or f"视频任务状态为 {result.status}",
                    code=result.error_code or "provider_task_failed",
                )
            task = session.get(ContentTask, job.task_id)
            run = session.get(GenerationRun, job.generation_run_id)
            if task is None or run is None:
                raise RuntimeError("AI 视频任务关联的内容任务或生成批次不存在")
            request_data = load(job.result_json).get("request", {})
            storage_key = None
            content = {
                "provider_task_id": result.provider_task_id,
                "provider": result.provider_id,
                "model": result.model,
                "video_url": result.video_url,
                "last_frame_url": result.last_frame_url,
                "metadata": result.metadata,
                "shot_index": int(request_data.get("shot_index", 0)),
                "prompt": str(request_data.get("prompt") or ""),
                "source_asset_id": request_data.get("source_asset_id"),
                "duration_seconds": request_data.get("duration_seconds"),
                "ratio": request_data.get("ratio"),
                "resolution": request_data.get("resolution"),
            }
            if result.video_url.startswith("mock://"):
                content["legacy_mock"] = True
            else:
                key = f"tasks/{task.id}/video-clips/{uuid4().hex}.mp4"
                stored = download_generated_video(
                    result,
                    asset_store=asset_store,
                    storage_key=key,
                )
                storage_key = stored.key
                content.update(
                    {
                        "size_bytes": stored.size,
                        "checksum_sha256": stored.checksum_sha256,
                    }
                )
            artifact = create_artifact(
                session,
                task,
                "ai_video_clip",
                content,
                job.owner_id,
                storage_key,
            )
            attach_artifact(
                artifact,
                run,
                "video_preview",
                int(request_data.get("shot_index", 0)),
            )
            if result.video_url.startswith("mock://"):
                artifact.status = "legacy_mock"
            job.status = "succeeded"
            job.progress = 100
            job.result_json = dump(
                {
                    **load(job.result_json),
                    "artifact_id": artifact.id,
                    "storage_key": storage_key,
                }
            )
            audit(session, job.owner_id, "ai_video.generated", "artifact", artifact.id)
            artifact_ids = load(run.result_artifact_ids_json)
            if artifact.id not in artifact_ids:
                artifact_ids.append(artifact.id)
            run.result_artifact_ids_json = dump(artifact_ids)
            session.flush()
            sibling_jobs = session.scalars(select(Job).where(Job.generation_run_id == run.id)).all()
            if sibling_jobs and all(item.status == "succeeded" for item in sibling_jobs):
                completed_artifacts = [
                    candidate
                    for artifact_id in artifact_ids
                    if (candidate := session.get(Artifact, artifact_id)) is not None
                ]
                complete_generation_run(
                    session,
                    task,
                    run,
                    completed_artifacts,
                    provider_id=result.provider_id,
                    model=result.model,
                    preserve_other_ai_video_shots=bool(
                        request_data.get("preserve_other_shots", False)
                    ),
                )
            session.commit()
    except Exception as exc:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None or job.status == "cancelled":
                return
            job.status = "failed"
            details = _generation_error_detail(exc)
            job.error = details["message"]
            job.result_json = dump({**load(job.result_json), "error_detail": details})
            run = session.get(GenerationRun, job.generation_run_id)
            task = session.get(ContentTask, job.task_id)
            if run is not None and task is not None:
                fail_generation_run(session, task, run, details["message"])
            session.commit()


class _ProjectAiVideoCancelled(RuntimeError):
    pass


class _ProjectAiVideoLeaseLost(RuntimeError):
    pass


def _transfer_project_ai_video(
    provider_task,
    *,
    asset_store: AssetStorePort,
    storage_key: str,
):
    """Archive a provider result, including a real local clip for mock mode."""

    if not str(provider_task.video_url or "").startswith("mock://"):
        return download_generated_video(
            provider_task,
            asset_store=asset_store,
            storage_key=storage_key,
        )
    ffmpeg_executable = settings.ffmpeg_path
    if not ffmpeg_executable:
        try:
            import imageio_ffmpeg

            ffmpeg_executable = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            ffmpeg_executable = "ffmpeg"
    with TemporaryDirectory(prefix="content-mock-ai-video-") as temp_dir:
        output_path = Path(temp_dir) / "generated.mp4"
        command = [
            ffmpeg_executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x176b5f:s=854x480:d=2:r=30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=min(settings.ffmpeg_timeout_seconds, 60),
        )
        if completed.returncode != 0 or not output_path.is_file():
            message = completed.stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Mock AI 视频生成失败：{message}")
        with output_path.open("rb") as source:
            return asset_store.put_stream(storage_key, source, "video/mp4")


def run_video_project_ai_generation_job(
    job_id: str,
    provider: AiVideoGenerationPort,
    asset_store: AssetStorePort,
    provider_media_publisher: ProviderMediaPublisherPort | None = None,
) -> None:
    """Poll and archive one editor AI-video job behind a fenced DB lease."""

    worker_id = f"content-project-ai-video-{uuid4().hex}"
    with SessionLocal() as session:
        candidate = session.get(Job, job_id)
        if (
            candidate is None
            or not str(load(candidate.result_json).get("provider_task_id") or "").strip()
        ):
            # A durable reservation is not a failed provider response. The
            # submitting request opens its claim gate only after persisting ID.
            return
        lease = claim_durable_job(
            session,
            worker_id=worker_id,
            job_id=job_id,
            kinds=(VIDEO_PROJECT_AI_JOB_KIND,),
            lease_seconds=120,
        )
        if lease is None:
            return
        session.expire_all()
        job = session.get(Job, job_id)
        job_result = load(job.result_json) if job is not None else {}
        provider_task_id = str(job_result.get("provider_task_id") or "")
        if not provider_task_id:
            # Also fence a concurrent legacy writer without consuming attempts.
            session.rollback()
            return
        session.commit()

    provider_terminal = False

    def update_progress(provider_task) -> None:
        nonlocal provider_terminal
        progress = max(1, min(99, int(provider_task.progress or 1)))
        with SessionLocal() as progress_session:
            heartbeat = heartbeat_durable_job(
                progress_session,
                lease,
                lease_seconds=120,
                progress=progress,
            )
            progress_session.commit()
        if heartbeat.renewed:
            return
        if heartbeat.cancel_requested or heartbeat.status == "cancelled":
            try:
                cancelled_task = provider.cancel(provider_task_id)
                provider_terminal = cancelled_task.status in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "expired",
                }
            except ProviderCallError:
                pass
            raise _ProjectAiVideoCancelled("AI 视频任务已取消")
        raise _ProjectAiVideoLeaseLost("AI 视频任务租约已失效")

    try:
        provider_result = wait_for_ai_video_task(
            provider,
            provider_task_id,
            poll_interval_seconds=settings.ai_video_poll_interval_seconds,
            max_wait_seconds=settings.ai_video_max_wait_seconds,
            on_update=update_progress,
        )
        provider_terminal = provider_result.status in {
            "succeeded",
            "failed",
            "cancelled",
            "expired",
        }
        if provider_result.status == "cancelled":
            raise _ProjectAiVideoCancelled("供应商已取消 AI 视频任务")
        if provider_result.status != "succeeded":
            retried = maybe_retry_ai_video_on_fast(
                provider,
                failed=provider_result,
                metadata=job_result if isinstance(job_result, dict) else None,
            )
            if retried is not None:
                with SessionLocal() as session:
                    job = session.get(Job, job_id)
                    if job is not None:
                        persisted = load(job.result_json)
                        persisted["provider_task_id"] = retried.provider_task_id
                        persisted["provider_status"] = retried.status
                        persisted["retry_payload"] = (retried.metadata or {}).get("retry_payload")
                        provenance = persisted.get("provenance")
                        if isinstance(provenance, dict):
                            provenance["model"] = retried.model
                            metadata = provenance.get("provider_metadata")
                            if isinstance(metadata, dict):
                                metadata.update(retried.metadata or {})
                        job.result_json = dump(persisted)
                        session.commit()
                        job_result = persisted
                provider_result = wait_for_ai_video_task(
                    provider,
                    retried.provider_task_id,
                    poll_interval_seconds=settings.ai_video_poll_interval_seconds,
                    max_wait_seconds=settings.ai_video_max_wait_seconds,
                    on_update=update_progress,
                )
                provider_terminal = provider_result.status in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "expired",
                }
        if provider_result.status == "cancelled":
            raise _ProjectAiVideoCancelled("供应商已取消 AI 视频任务")
        if provider_result.status != "succeeded":
            raise ProviderCallError(
                provider_result.error_message or f"AI 视频任务状态为 {provider_result.status}",
                code=provider_result.error_code or "provider_task_failed",
                retryable=False,
            )
        with SessionLocal() as session:
            heartbeat = heartbeat_durable_job(
                session,
                lease,
                lease_seconds=120,
                progress=99,
            )
            if not heartbeat.renewed:
                session.rollback()
                if heartbeat.cancel_requested or heartbeat.status == "cancelled":
                    raise _ProjectAiVideoCancelled("AI 视频任务已取消")
                raise _ProjectAiVideoLeaseLost("AI 视频任务租约已失效")
            job = session.get(Job, job_id)
            task = session.get(ContentTask, job.task_id) if job is not None else None
            project_id = load(job.result_json).get("project_id") if job is not None else None
            project = session.get(VideoProject, project_id) if project_id else None
            if job is None or task is None or project is None:
                raise RuntimeError("AI 视频任务关联的工程或内容任务不存在")
            archived = archive_project_ai_video_result(
                session,
                project=project,
                task=task,
                job=job,
                provider_task=provider_result,
                asset_store=asset_store,
                created_by=job.owner_id,
                video_transfer=_transfer_project_ai_video,
            )
            transition = complete_durable_job(
                session,
                lease,
                result_json=archived.job_result,
            )
            if transition is None:
                session.rollback()
                raise _ProjectAiVideoLeaseLost("AI 视频任务完成时租约已失效")
            audit(
                session,
                job.owner_id,
                "video_project.ai_video_generated",
                "project_asset",
                archived.generated.project_asset.id,
                project_id=project.id,
                provider=provider_result.provider_id,
                model=provider_result.model,
            )
            session.commit()
    except _ProjectAiVideoCancelled:
        with SessionLocal() as session:
            request_durable_job_cancel(session, job_id, lease=lease)
            session.commit()
    except _ProjectAiVideoLeaseLost:
        return
    except Exception as exc:
        logger.warning("AI video background job failed: %s", exc, exc_info=True)
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            persisted_result = load(job.result_json) if job is not None else {}
            user_error, error_detail = normalize_ai_video_provider_failure(
                exc,
                error_code=(exc.code if isinstance(exc, ProviderCallError) else None),
                request=(
                    persisted_result.get("request")
                    if isinstance(persisted_result.get("request"), dict)
                    else None
                ),
                retryable=(exc.retryable if isinstance(exc, ProviderCallError) else False),
            )
            if job is not None:
                persisted_result["error_detail"] = error_detail
                job.result_json = dump(persisted_result)
                session.flush()
            fail_durable_job(
                session,
                lease,
                error=user_error,
                retryable=bool(error_detail.get("retryable")),
            )
            session.commit()
    finally:
        if provider_terminal and provider_media_publisher is not None:
            release_provider_media_publications(
                provider_media_publisher,
                job_result,
            )


def run_video_composition_job(
    job_id: str,
    composer: VideoComposerPort,
    asset_store: AssetStorePort,
) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None or job.status == "cancelled":
            return
        job.status = "running"
        job.progress = 5
        session.commit()
        job_input = load(job.result_json)
    try:
        composition_spec = job_input.get("composition_spec", {})
        if not isinstance(composition_spec, dict):
            composition_spec = {}
        clip_specs = job_input.get("clips", [])
        clips = tuple(
            VideoClip(
                path=asset_store.path_for(item["storage_key"]),
                duration_seconds=float(item.get("duration_seconds", 3)),
                transition=str(item.get("transition", "none")),
                transition_duration_seconds=float(item.get("transition_duration_seconds", 0.4)),
                is_image=item.get("is_image"),
                fit_mode=str(
                    item.get("fit_mode")
                    or composition_spec.get("fit_mode")
                    or job_input.get("fit_mode", "contain")
                ),
            )
            for item in clip_specs
        )
        output_key = f"tasks/{job_input['task_id']}/video/{job_id}.mp4"
        output_path = asset_store.path_for(output_key)
        request = VideoCompositionRequest(
            clips=clips,
            output_path=output_path,
            width=int(composition_spec.get("width", job_input.get("width", 1280))),
            height=int(composition_spec.get("height", job_input.get("height", 720))),
            fps=int(composition_spec.get("fps", job_input.get("fps", 25))),
            fit_mode=str(composition_spec.get("fit_mode", job_input.get("fit_mode", "contain"))),
            source_audio_policy=str(
                composition_spec.get(
                    "source_audio_policy",
                    job_input.get("source_audio_policy", "mute"),
                )
            ),
            voice_path=(
                asset_store.path_for(job_input["voice_key"]) if job_input.get("voice_key") else None
            ),
            subtitle_path=(
                asset_store.path_for(job_input["subtitle_key"])
                if job_input.get("subtitle_key")
                else None
            ),
            subtitle_style=(
                composition_spec.get("subtitle_style") or job_input.get("subtitle_style") or {}
            ),
            background_music_path=(
                asset_store.path_for(job_input["background_music_key"])
                if job_input.get("background_music_key")
                else None
            ),
            background_music_volume=float(
                composition_spec.get(
                    "background_music_volume",
                    job_input.get("background_music_volume", 0.18),
                )
            ),
            logo_path=(
                asset_store.path_for(job_input["logo_key"]) if job_input.get("logo_key") else None
            ),
            logo_width=int(composition_spec.get("logo_width", job_input.get("logo_width", 160))),
            logo_position=str(
                composition_spec.get("logo_position", job_input.get("logo_position", "top_right"))
            ),
            intro_path=(
                asset_store.path_for(job_input["intro_key"]) if job_input.get("intro_key") else None
            ),
            intro_duration_seconds=composition_spec.get(
                "intro_duration_seconds", job_input.get("intro_duration_seconds")
            ),
            outro_path=(
                asset_store.path_for(job_input["outro_key"]) if job_input.get("outro_key") else None
            ),
            outro_duration_seconds=composition_spec.get(
                "outro_duration_seconds", job_input.get("outro_duration_seconds")
            ),
        )
        result = composer.compose(request)
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None or job.status == "cancelled":
                return
            task = session.get(ContentTask, job.task_id)
            run = session.get(GenerationRun, job.generation_run_id)
            if task is None or run is None:
                raise RuntimeError("视频合成任务关联数据不存在")
            artifact = create_artifact(
                session,
                task,
                "video",
                {
                    "format": "mp4",
                    "provider": composer.status.provider_id,
                    "duration_seconds": result.duration_seconds,
                    "width": result.width,
                    "height": result.height,
                    "clip_count": result.clip_count,
                    "transitions_applied": list(result.transitions_applied),
                    "subtitle_burned": result.subtitle_burned,
                    "voice_applied": result.voice_applied,
                    "background_music_applied": result.background_music_applied,
                    "logo_applied": result.logo_applied,
                    "source_artifact_ids": job_input.get("source_artifact_ids", []),
                    "source_asset_ids": job_input.get("source_asset_ids", []),
                    "composition_spec": composition_spec,
                },
                job.owner_id,
                output_key,
            )
            attach_artifact(artifact, run, "video_preview", 0)
            complete_generation_run(
                session,
                task,
                run,
                [artifact],
                provider_id=composer.status.provider_id,
                model=composer.status.model,
            )
            job.status = "succeeded"
            job.progress = 100
            job.result_json = dump(
                {
                    **job_input,
                    "artifact_id": artifact.id,
                    "storage_key": output_key,
                }
            )
            audit(session, job.owner_id, "video.composed", "artifact", artifact.id)
            session.commit()
    except Exception as exc:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None or job.status == "cancelled":
                return
            job.status = "failed"
            details = _generation_error_detail(exc)
            job.error = details["message"]
            job.result_json = dump({**load(job.result_json), "error_detail": details})
            task = session.get(ContentTask, job.task_id)
            run = session.get(GenerationRun, job.generation_run_id)
            if task is not None and run is not None:
                fail_generation_run(session, task, run, details["message"])
            session.commit()


def run_official_knowledge_import_job(job_id: str) -> None:
    """Import the configured official JSONL bundle into the unified DB catalog."""
    try:
        if settings.knowledge_dir is None:
            raise RuntimeError("未配置 CONTENT_KNOWLEDGE_DIR")
        documents = load_official_jsonl_bundle(settings.knowledge_dir)
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None or job.status == "cancelled":
                return
            job.status = "running"
            job.progress = 5
            session.commit()
        imported = 0
        unchanged = 0
        for index, document in enumerate(documents, start=1):
            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if job is None or job.status == "cancelled":
                    return
                external = session.scalar(
                    select(ExternalRef).where(
                        ExternalRef.system == "official-jsonl",
                        ExternalRef.external_id == document.document_id,
                    )
                )
                entry = session.get(ResourceEntry, external.entry_id) if external else None
                current = (
                    session.get(ResourceVersion, entry.current_version_id)
                    if entry is not None and entry.current_version_id
                    else None
                )
                if current is not None and current.text_content == document.text:
                    unchanged += 1
                else:
                    version_number = (
                        int(
                            session.scalar(
                                select(func.max(ResourceVersion.version)).where(
                                    ResourceVersion.entry_id == entry.id
                                )
                            )
                            or 0
                        )
                        + 1
                        if entry is not None
                        else 1
                    )
                    first_chunk = document.chunks[0]
                    metadata = dict(document.metadata)
                    source_url = metadata.get("source_url")
                    crawled_at = None
                    raw_crawled_at = metadata.get("crawl_time")
                    if raw_crawled_at:
                        try:
                            crawled_at = datetime.fromisoformat(
                                str(raw_crawled_at).replace("Z", "+00:00")
                            )
                        except ValueError:
                            crawled_at = None
                    facts = metadata.get("facts", [])
                    notes_value = metadata.get("notes", [])
                    if isinstance(notes_value, list):
                        notes = "\n".join(str(item) for item in notes_value)
                    else:
                        notes = str(notes_value or "")
                    entry, version = create_resource_version_record(
                        session,
                        owner_id=job.owner_id,
                        resource_kind="document",
                        category=document.category,
                        title=document.title,
                        text_content=document.text,
                        data_level=first_chunk.data_level,
                        version_number=version_number,
                        created_by=job.owner_id,
                        entry=entry,
                        tags=["官网资料"],
                        source_url=str(source_url) if source_url else None,
                        crawled_at=crawled_at,
                        freshness=str(metadata.get("freshness") or "") or None,
                        facts=facts if isinstance(facts, list) else [facts],
                        notes=notes,
                        metadata=metadata,
                        chunks=document.chunks,
                    )
                    if external is None:
                        session.add(
                            ExternalRef(
                                entry_id=entry.id,
                                system="official-jsonl",
                                external_id=document.document_id,
                                source_url=str(source_url) if source_url else None,
                                metadata_json=dump(metadata),
                            )
                        )
                    library_item = session.scalar(
                        select(LibraryItem).where(LibraryItem.resource_entry_id == entry.id)
                    )
                    if library_item is None:
                        library_item = LibraryItem(
                            owner_id=job.owner_id,
                            category=entry.category,
                            name=entry.title,
                            resource_entry_id=entry.id,
                        )
                        session.add(library_item)
                    library_item.category = entry.category
                    library_item.name = entry.title
                    library_item.text_content = version.text_content
                    library_item.data_level = entry.data_level
                    library_item.version = version.version
                    library_item.active = True
                    imported += 1
                job.progress = min(95, 5 + int(index / max(len(documents), 1) * 90))
                session.commit()
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None or job.status == "cancelled":
                return
            job.status = "succeeded"
            job.progress = 100
            job.result_json = dump(
                {
                    "document_count": len(documents),
                    "imported": imported,
                    "unchanged": unchanged,
                }
            )
            audit(
                session,
                job.owner_id,
                "knowledge.official_jsonl_imported",
                "job",
                job.id,
                imported=imported,
                unchanged=unchanged,
            )
            session.commit()
    except Exception as exc:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None or job.status == "cancelled":
                return
            job.status = "failed"
            job.error = str(exc)[:2000]
            session.commit()


def recover_pending_jobs() -> None:
    """Resume durable media work left queued/running by a prior process."""
    with SessionLocal() as session:
        recover_durable_jobs(session)
        session.flush()
        pending = list(
            session.scalars(
                select(Job).where(Job.status == "queued").order_by(Job.created_at)
            ).all()
        )
        recoverable = [
            job
            for job in pending
            if job.generation_run_id and job.kind in {"ai_video_generation", "video_composition"}
        ]
        knowledge_import_ids = [
            job.id for job in pending if job.kind == "official_knowledge_import"
        ]
        project_render_ids = [job.id for job in pending if job.kind == RENDER_JOB_KIND]
        project_export_ids = [job.id for job in pending if job.kind == EXPORT_JOB_KIND]
        project_ai_video_ids = [job.id for job in pending if job.kind == VIDEO_PROJECT_AI_JOB_KIND]
        session.commit()
        job_groups: dict[str, list[str]] = {}
        composition_ids: list[str] = []
        for job in recoverable:
            if job.kind == "ai_video_generation":
                job_groups.setdefault(str(job.generation_run_id), []).append(job.id)
            else:
                composition_ids.append(job.id)

    # Admission is bounded by the local dispatcher.  If a restart finds more
    # queued work than the development process can hold, leave the remaining
    # rows queued for the periodic recovery sweep instead of surfacing a 500
    # or marking durable work as failed.
    def submit_recoverable(operation: Callable[[], None]) -> bool:
        try:
            dispatcher.submit(operation)
        except RuntimeError:
            return False
        return True

    for job_id in knowledge_import_ids:
        submit_recoverable(lambda current_id=job_id: run_official_knowledge_import_job(current_id))
    if (
        not job_groups
        and not composition_ids
        and not project_render_ids
        and not project_export_ids
        and not project_ai_video_ids
    ):
        return
    try:
        asset_store = get_asset_store_port()
        ai_video_provider = (
            get_ai_video_generation_port() if job_groups or project_ai_video_ids else None
        )
        provider_media_publisher = get_provider_media_publisher() if project_ai_video_ids else None
        video_composer = get_video_composer_port() if composition_ids else None
        timeline_renderer = (
            get_timeline_renderer() if project_render_ids or project_export_ids else None
        )
    except Exception as exc:
        with SessionLocal() as session:
            for job_ids in [
                *job_groups.values(),
                composition_ids,
                project_render_ids,
                project_export_ids,
                project_ai_video_ids,
            ]:
                for job_id in job_ids:
                    job = session.get(Job, job_id)
                    if job is None:
                        continue
                    job.status = "failed"
                    job.error = f"服务重启后无法恢复任务：{str(exc)[:1800]}"
                    run = session.get(GenerationRun, job.generation_run_id)
                    task = session.get(ContentTask, job.task_id)
                    if run is not None and task is not None:
                        fail_generation_run(session, task, run, job.error)
            session.commit()
        return
    for job_ids in job_groups.values():
        ordered_ids = tuple(job_ids)

        def resume_clips(ids: tuple[str, ...] = ordered_ids) -> None:
            for job_id in ids:
                run_ai_video_job(job_id, ai_video_provider, asset_store)  # type: ignore[arg-type]

        submit_recoverable(resume_clips)
    for job_id in composition_ids:
        submit_recoverable(
            lambda current_id=job_id: run_video_composition_job(
                current_id,
                video_composer,  # type: ignore[arg-type]
                asset_store,
            )
        )
    for job_id in project_render_ids:
        submit_recoverable(
            lambda current_id=job_id: run_video_project_render_job(
                current_id,
                SessionLocal,
                asset_store,
                timeline_renderer,  # type: ignore[arg-type]
                settings,
            )
        )
    for job_id in project_export_ids:
        submit_recoverable(
            lambda current_id=job_id: run_video_project_export_job(
                current_id,
                SessionLocal,
                asset_store,
                timeline_renderer,  # type: ignore[arg-type]
                settings,
            )
        )
    for job_id in project_ai_video_ids:
        submit_recoverable(
            lambda current_id=job_id: run_video_project_ai_generation_job(
                current_id,
                ai_video_provider,  # type: ignore[arg-type]
                asset_store,
                provider_media_publisher,
            )
        )


DURABLE_MEDIA_RECOVERY_INTERVAL_SECONDS = 15
DURABLE_MEDIA_JOB_KINDS = (
    RENDER_JOB_KIND,
    EXPORT_JOB_KIND,
    VIDEO_PROJECT_AI_JOB_KIND,
)


def purge_expired_provider_media() -> int:
    """Best-effort reaper for expired objects under the fixed TOS temp prefix."""

    try:
        publisher = get_provider_media_publisher()
        return publisher.purge_expired() if publisher.enabled else 0
    except Exception:
        # Object-level expiry and the required bucket lifecycle rule are the
        # final backstop if TOS is temporarily unreachable during a sweep.
        return 0


def recover_expired_video_project_jobs() -> int:
    """Requeue expired media leases and dispatch work that is ready to run.

    Startup recovery intentionally leaves a job with a still-live lease alone.  A
    worker may nevertheless have disappeared during a process restart, so this
    sweep continues for the lifetime of the service and picks the job up as soon
    as that lease expires.  The workers' compare-and-set lease claim is the final
    duplicate-execution fence.
    """

    now = datetime.now(UTC)
    with SessionLocal() as session:
        recover_durable_jobs(
            session,
            kinds=DURABLE_MEDIA_JOB_KINDS,
            now=now,
        )
        session.flush()
        pending = list(
            session.scalars(
                select(Job)
                .where(
                    Job.kind.in_(DURABLE_MEDIA_JOB_KINDS),
                    Job.status == "queued",
                    Job.cancel_requested_at.is_(None),
                    Job.attempt < Job.max_attempts,
                    or_(Job.available_at.is_(None), Job.available_at <= now),
                )
                .order_by(Job.available_at, Job.created_at, Job.id)
            ).all()
        )
        render_ids = [job.id for job in pending if job.kind == RENDER_JOB_KIND]
        export_ids = [job.id for job in pending if job.kind == EXPORT_JOB_KIND]
        ai_video_ids = [job.id for job in pending if job.kind == VIDEO_PROJECT_AI_JOB_KIND]
        session.commit()

    if not render_ids and not export_ids and not ai_video_ids:
        return 0

    # A transient provider/configuration outage must not terminalize durable
    # work.  The next sweep will retry dependency resolution.
    try:
        asset_store = get_asset_store_port()
        timeline_renderer = get_timeline_renderer() if render_ids or export_ids else None
        ai_video_provider = get_ai_video_generation_port() if ai_video_ids else None
        provider_media_publisher = get_provider_media_publisher() if ai_video_ids else None
    except Exception:
        return 0

    submitted = 0

    def submit_recovered(operation: Callable[[], None]) -> bool:
        try:
            dispatcher.submit(operation)
        except RuntimeError:
            return False
        return True

    for job_id in render_ids:
        if submit_recovered(
            lambda current_id=job_id: run_video_project_render_job(
                current_id,
                SessionLocal,
                asset_store,
                timeline_renderer,  # type: ignore[arg-type]
                settings,
            )
        ):
            submitted += 1
    for job_id in export_ids:
        if submit_recovered(
            lambda current_id=job_id: run_video_project_export_job(
                current_id,
                SessionLocal,
                asset_store,
                timeline_renderer,  # type: ignore[arg-type]
                settings,
            )
        ):
            submitted += 1
    for job_id in ai_video_ids:
        if submit_recovered(
            lambda current_id=job_id: run_video_project_ai_generation_job(
                current_id,
                ai_video_provider,  # type: ignore[arg-type]
                asset_store,
                provider_media_publisher,
            )
        ):
            submitted += 1
    return submitted


async def durable_media_recovery_loop() -> None:
    """Continuously recover work whose lease expires after service startup."""

    while True:
        await asyncio.sleep(DURABLE_MEDIA_RECOVERY_INTERVAL_SECONDS)
        await asyncio.to_thread(purge_expired_provider_media)
        await asyncio.to_thread(recover_expired_video_project_jobs)


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"service": "content-service", "status": "ok", "version": app.version}


@app.api_route(
    "/api/v1/provider-media/{storage_key:path}",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
def fetch_provider_media(
    storage_key: str,
    expires: int = Query(...),
    signature: str = Query(..., min_length=64, max_length=64),
    signer: ProviderMediaUrlSigner = Depends(get_provider_media_url_signer),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> FileResponse:
    """Serve one video through an expiring, unguessable provider bearer URL."""

    if not signer.verify(storage_key, expires=expires, signature=signature):
        raise HTTPException(403, "Provider 媒体地址无效或已过期")
    try:
        path = asset_store.path_for(storage_key)
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "Provider 媒体不存在") from exc
    media_type = PROVIDER_MEDIA_TYPES.get(path.suffix.lower())
    if not path.is_file() or media_type is None:
        raise HTTPException(404, "Provider 媒体不存在")
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "Content-Disposition": "inline",
            "Content-Security-Policy": "default-src 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/v1/identity", tags=["system"])
def current_identity(response: Response, identity: Identity = Depends(get_identity)) -> dict:
    """Local verified business identity, not a replacement for /api/entry/me."""
    response.headers["Cache-Control"] = "private, no-store"
    return {
        "subject": identity.user_id,
        "display_name": identity.name,
        "department": identity.department or None,
        "roles": sorted(identity.roles),
        "clearance": identity.clearance,
    }


@app.get("/api/v1/providers", tags=["system"], response_model=list[ProviderInfo])
def list_providers(
    _: Identity = Depends(get_identity),
    text_provider: TextProvider = Depends(get_text_provider),
    knowledge_provider: KnowledgeProvider = Depends(get_knowledge_provider),
    image_provider: ImageGenerationPort = Depends(get_image_generation_port),
    speech_provider: SpeechSynthesisPort = Depends(get_speech_synthesis_port),
    music_provider: MusicGenerationPort = Depends(get_music_generation_port),
    subtitle_provider: SubtitleAlignmentPort = Depends(get_subtitle_alignment_port),
    ai_video_provider: AiVideoGenerationPort = Depends(get_ai_video_generation_port),
    video_composer: VideoComposerPort = Depends(get_video_composer_port),
    timeline_renderer=Depends(get_timeline_renderer),
) -> list[dict]:
    return [
        asdict(text_provider.status),
        asdict(knowledge_provider.status),
        asdict(image_provider.status),
        asdict(speech_provider.status),
        asdict(music_provider.status),
        asdict(subtitle_provider.status),
        asdict(ai_video_provider.status),
        asdict(video_composer.status),
        asdict(timeline_renderer.status),
    ]


@app.get(
    "/api/v1/capabilities",
    tags=["system"],
    response_model=CapabilitiesResponse,
)
def get_capabilities(
    _: Identity = Depends(get_identity),
    text_provider: TextProvider = Depends(get_text_provider),
    knowledge_provider: KnowledgeProvider = Depends(get_knowledge_provider),
    image_provider: ImageGenerationPort = Depends(get_image_generation_port),
    speech_provider: SpeechSynthesisPort = Depends(get_speech_synthesis_port),
    music_provider: MusicGenerationPort = Depends(get_music_generation_port),
    subtitle_provider: SubtitleAlignmentPort = Depends(get_subtitle_alignment_port),
    ai_video_provider: AiVideoGenerationPort = Depends(get_ai_video_generation_port),
    provider_media_publisher: ProviderMediaPublisherPort = Depends(get_provider_media_publisher),
    video_composer: VideoComposerPort = Depends(get_video_composer_port),
    timeline_renderer=Depends(get_timeline_renderer),
) -> dict:
    documents = [
        provider_capability_document(port)
        for port in (
            text_provider,
            knowledge_provider,
            image_provider,
            speech_provider,
            music_provider,
            subtitle_provider,
            ai_video_provider,
            video_composer,
            timeline_renderer,
        )
    ]
    for document in documents:
        if document["capability"] == "video-composition":
            document["parameters"] = {
                **document["parameters"],
                "defaults": {"width": 1280, "height": 720, "fps": 25},
                "default_source": "content-service",
                "custom_size": True,
            }
        elif document["capability"] == "timeline-video-composition":
            document["parameters"] = {
                **document["parameters"],
                "profiles": ["preview-480p", "final-480p", "final-720p", "final-1080p"],
                "fps": [25, 30, 60],
                "aspect_ratios": ["16:9", "9:16", "1:1", "4:3", "custom"],
                "maximum_duration_seconds": 600,
                "formal_render": "server-ffmpeg",
            }
        elif document["capability"] == "ai-video-generation":
            parameters = document["parameters"]
            reference_videos = parameters.get("reference_videos")
            if isinstance(reference_videos, dict):
                gateway_available = bool(
                    settings.provider_media_base_url and settings.provider_media_signing_secret
                )
                published_transport_available = provider_media_publisher.enabled
                local_upload_available = reference_videos.get("local_file_upload") is True
                transport_available = (
                    not bool(document.get("external"))
                    or published_transport_available
                    or gateway_available
                    or local_upload_available
                )
                if not bool(document.get("external")):
                    primary_transport = "internal-direct"
                    transports = [primary_transport]
                    transport_ttl = None
                elif published_transport_available:
                    primary_transport = provider_media_publisher.transport
                    transports = [primary_transport]
                    if gateway_available:
                        transports.append("provider-signed-https-gateway")
                    transport_ttl = provider_media_publisher.url_ttl_seconds
                elif gateway_available:
                    primary_transport = "provider-signed-https-gateway"
                    transports = [primary_transport]
                    transport_ttl = settings.provider_media_url_ttl_seconds
                elif local_upload_available:
                    primary_transport = "hiagent-runtime-upload"
                    transports = [primary_transport]
                    transport_ttl = None
                else:
                    primary_transport = "unavailable"
                    transports = []
                    transport_ttl = None
                parameters["reference_videos"] = {
                    **reference_videos,
                    "project_asset_transport_available": transport_available,
                    "project_asset_transport": primary_transport,
                    "project_asset_transports": transports,
                    "project_asset_url_ttl_seconds": transport_ttl,
                    "private_temporary_object": (
                        primary_transport
                        in {
                            "tos-private-presigned-https",
                            "private-relay-presigned-https",
                        }
                    ),
                }
    return {
        "capabilities": [
            *documents,
            {
                "provider_id": "content-workflow",
                "capability": "workflow-orchestration",
                "mode": "local",
                "enabled": True,
                "parameters": {
                    "stage_operations": {
                        "voice_subtitles": ["voice", "subtitle"],
                        "video_preview": ["clips", "compose"],
                    }
                },
            },
        ]
    }


@app.get(
    "/api/v1/service-status",
    tags=["system"],
    response_model=ServiceStatusResponse,
)
def get_service_status(
    _: Identity = Depends(get_identity),
    text_provider: TextProvider = Depends(get_text_provider),
    knowledge_provider: KnowledgeProvider = Depends(get_knowledge_provider),
    image_provider: ImageGenerationPort = Depends(get_image_generation_port),
    speech_provider: SpeechSynthesisPort = Depends(get_speech_synthesis_port),
    music_provider: MusicGenerationPort = Depends(get_music_generation_port),
    subtitle_provider: SubtitleAlignmentPort = Depends(get_subtitle_alignment_port),
    ai_video_provider: AiVideoGenerationPort = Depends(get_ai_video_generation_port),
    video_composer: VideoComposerPort = Depends(get_video_composer_port),
    timeline_renderer=Depends(get_timeline_renderer),
) -> dict:
    checked_at = datetime.now(UTC)
    provider_ports = (
        text_provider,
        knowledge_provider,
        image_provider,
        speech_provider,
        music_provider,
        subtitle_provider,
        ai_video_provider,
        video_composer,
        timeline_renderer,
    )
    providers = list_providers(
        _,
        text_provider,
        knowledge_provider,
        image_provider,
        speech_provider,
        music_provider,
        subtitle_provider,
        ai_video_provider,
        video_composer,
        timeline_renderer,
    )
    services = []
    for item, port in zip(providers, provider_ports, strict=True):
        item_checked_at = checked_at
        if not item["enabled"]:
            status = "disabled"
            message = "该能力尚未配置"
        elif item["mode"] == "mock":
            status = "degraded"
            message = "当前为本地模拟能力，不作为真实生成结果"
        elif item["external"]:
            health = current_health(port.status, getattr(port, "health_configuration", None))
            if health is None:
                status = "available"
                message = None
            else:
                status = health.status
                item_checked_at = health.checked_at
                message = health.message
        else:
            status = "available"
            message = None
        services.append(
            {
                "capability": item["capability"],
                "provider_id": item["provider_id"],
                "status": status,
                "checked_at": item_checked_at,
                "message": message,
            }
        )
    statuses = {item["status"] for item in services}
    overall = (
        "unavailable"
        if statuses <= {"unavailable", "disabled"}
        else "degraded"
        if "degraded" in statuses or "unavailable" in statuses or "disabled" in statuses
        else "available"
    )
    return {"overall": overall, "services": services}


@app.post("/api/v1/library/items", tags=["library"])
def create_library_item(
    payload: LibraryItemCreate,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    item = LibraryItem(
        owner_id=identity.user_id,
        category=payload.category,
        name=payload.name,
        text_content=payload.text_content,
        data_level=payload.data_level,
    )
    session.add(item)
    session.flush()
    entry, version = create_resource_version_record(
        session,
        owner_id=identity.user_id,
        resource_kind=payload.resource_kind,
        category=payload.category,
        title=payload.name,
        text_content=payload.text_content,
        data_level=payload.data_level,
        version_number=1,
        created_by=identity.user_id,
        tags=payload.tags,
        parent_id=payload.parent_id,
        source_url=payload.source_url,
        crawled_at=payload.crawled_at,
        freshness=payload.freshness,
        facts=payload.facts,
        notes=payload.notes,
        metadata={
            **payload.metadata,
            "legacy_library_item_id": item.id,
            **({"source_type": payload.source_type} if payload.source_type else {}),
        },
        tenant_id=payload.tenant_id,
        department_id=payload.department_id,
        allowed_roles=payload.allowed_roles,
    )
    item.resource_entry_id = entry.id
    audit(session, identity.user_id, "library.created", "library_item", item.id)
    session.commit()
    return library_item_json(item, entry, version)


@app.post(
    "/api/v1/tasks/{task_id}/image-references",
    tags=["tasks"],
    response_model=LibraryItemResponse,
)
def upload_task_image_reference(
    task_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    """Store a task reference without publishing a LibraryItem or knowledge chunks."""
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    filename = safe_filename(file.filename)
    size = upload_size_bytes(file)
    if size > 30 * 1024 * 1024:
        raise HTTPException(422, "参考图片不能超过 30 MiB")
    try:
        kind, media_type = validate_upload_header(
            filename=filename,
            declared_media_type=file.content_type,
            head=read_upload_head(file.file),
            size_bytes=size,
            policy=MediaIngestPolicy(),
        )
    except MediaValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    if kind != "image":
        raise HTTPException(422, "参考图只支持 PNG、JPEG 或 WebP 图片")
    file.file.seek(0)
    key = f"task-image-references/{task.id}/{uuid4().hex}-{filename}"
    stored = asset_store.put_stream(key, file.file, media_type)
    metadata = {"source_type": "task-image-reference", "task_id": task.id}
    entry, version = create_resource_version_record(
        session,
        owner_id=identity.user_id,
        resource_kind="image",
        category="task-image-reference",
        title=filename,
        text_content="",
        data_level="internal",
        version_number=1,
        created_by=identity.user_id,
        metadata=metadata,
        asset_key=stored.key,
        asset_media_type=stored.media_type,
        asset_size=stored.size,
        asset_checksum=stored.checksum_sha256,
        chunks=(),
    )
    audit(session, identity.user_id, "task.image_reference_uploaded", "resource_entry", entry.id)
    session.commit()
    return {
        "id": entry.id,
        "owner_id": entry.owner_id,
        "resource_entry_id": entry.id,
        "resource_version_id": version.id,
        "resource_kind": "image",
        "category": entry.category,
        "name": entry.title,
        "text_content": "",
        "data_level": entry.data_level,
        "version": version.version,
        "active": entry.active,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "metadata": metadata,
        "source_type": "task-image-reference",
        "has_asset": True,
        "download_url": f"/api/v1/tasks/{task.id}/image-references/{entry.id}/download",
    }


@app.get("/api/v1/tasks/{task_id}/image-references/{reference_id}/download", tags=["tasks"])
def download_task_image_reference(
    task_id: str,
    reference_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> FileResponse:
    get_task_or_404(session, task_id, identity, allow_admin_read=True)
    entry = session.get(ResourceEntry, reference_id)
    if entry is None or load(entry.metadata_json).get("task_id") != task_id:
        raise HTTPException(404, "当前任务的参考图片不存在")
    key, media_type = media_asset_for_identity(
        session,
        identity,
        reference_id,
        expected_prefix="image",
        allow_admin_read=True,
    )
    path = asset_store.path_for(key)
    if not path.is_file():
        raise HTTPException(404, "参考图片文件不存在")
    return FileResponse(path, filename=entry.title, media_type=media_type)


@app.post("/api/v1/library/items/upload", tags=["library"])
async def upload_library_item(
    category: str,
    name: str,
    data_level: str = "internal",
    resource_kind: str | None = None,
    tags: str = "",
    parent_id: str | None = None,
    source_url: str | None = None,
    crawled_at: datetime | None = None,
    freshness: str | None = None,
    facts: str = "",
    notes: str = "",
    file: UploadFile = File(...),
    text_content: str | None = Form(default=None, max_length=1_000_000),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
    knowledge_catalog: KnowledgeCatalogPort = Depends(get_knowledge_catalog_port),
) -> dict:
    if text_content is not None and not text_content.strip():
        raise HTTPException(422, "请填写文本内容")
    if not category.strip() or len(category) > 128:
        raise HTTPException(422, "业务分类不能为空且不能超过 128 个字符")
    if not name.strip() or len(name) > 255:
        raise HTTPException(422, "资料名称不能为空、不能全为空格且不能超过 255 个字符")
    if len(notes) > 10_000:
        raise HTTPException(422, "备注不能超过 10000 个字符")
    if data_level not in DATA_LEVELS:
        raise HTTPException(422, "不支持的数据等级")
    _, media_type = validate_library_upload(file)
    resource_kind = library_upload_resource_kind(
        resource_kind, media_type
    )
    item = LibraryItem(
        owner_id=identity.user_id,
        category=category,
        name=name,
        data_level=data_level,
    )
    session.add(item)
    session.flush()
    key = f"library/{item.id}/{Path(file.filename or 'material.bin').name}"
    stored = asset_store.put_stream(key, file.file, media_type)
    item.storage_key = stored.key
    ingested = None
    if (text_content is None and resource_kind in {"document", "product", "spec"}
            and not media_type.startswith(("image/", "video/", "audio/"))):
        try:
            ingested = knowledge_catalog.ingest(
                KnowledgeIngestRequest(
                    source_path=asset_store.path_for(stored.key),
                    owner_id=identity.user_id,
                    title=name,
                    category=category,
                    data_level=data_level,
                    metadata={"filename": file.filename, "media_type": media_type},
                )
            )
        except ValueError as exc:
            raise HTTPException(422, f"资料解析失败：{exc}") from exc
    item.text_content = text_content if text_content is not None else ingested.text if ingested else ""
    metadata = _uploaded_library_metadata(
        asset_store=asset_store,
        storage_key=stored.key,
        resource_kind=resource_kind,
        media_type=stored.media_type,
        metadata={"legacy_library_item_id": item.id, "filename": file.filename},
    )
    entry, version = create_resource_version_record(
        session,
        owner_id=identity.user_id,
        resource_kind=resource_kind,
        category=category,
        title=name,
        text_content=item.text_content,
        data_level=data_level,
        version_number=1,
        created_by=identity.user_id,
        tags=split_metadata_values(tags),
        parent_id=parent_id,
        source_url=source_url,
        crawled_at=crawled_at,
        freshness=freshness,
        facts=parse_fact_values(facts),
        notes=notes,
        metadata=metadata,
        asset_key=stored.key,
        asset_media_type=stored.media_type,
        asset_size=stored.size,
        asset_checksum=stored.checksum_sha256,
        chunks=ingested.chunks if ingested else None,
    )
    item.resource_entry_id = entry.id
    audit(
        session,
        identity.user_id,
        "library.file_uploaded",
        "library_item",
        item.id,
        storage_key=stored.key,
    )
    session.commit()
    return library_item_json(item, entry, version)


@app.get(
    "/api/v1/library/items",
    tags=["library"],
    response_model=list[LibraryItemResponse],
)
def list_library_items(
    category: str | None = None,
    keyword: str | None = Query(default=None, min_length=1, max_length=128),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> list[dict]:
    statement = select(LibraryItem).where(LibraryItem.active == true())
    if not has_permission(identity, "library.manage"):
        statement = statement.where(
            or_(
                LibraryItem.owner_id == identity.user_id,
                LibraryItem.data_level == "public",
            )
        )
    if category:
        statement = statement.where(LibraryItem.category == category)
    if keyword:
        pattern = f"%{keyword}%"
        statement = statement.where(
            or_(LibraryItem.name.ilike(pattern), LibraryItem.text_content.ilike(pattern))
        )
    rows = session.scalars(
        statement.order_by(LibraryItem.updated_at.desc(), LibraryItem.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    results = []
    metadata_changed = False
    for row in rows:
        entry = session.get(ResourceEntry, row.resource_entry_id) if row.resource_entry_id else None
        version = (
            session.get(ResourceVersion, entry.current_version_id)
            if entry is not None and entry.current_version_id
            else None
        )
        metadata_changed = (
            _ensure_library_media_metadata(
                session,
                item=row,
                entry=entry,
                version=version,
                asset_store=asset_store,
            )
            or metadata_changed
        )
        results.append(library_item_json(row, entry, version))
    if metadata_changed:
        session.commit()
    return results


@app.get("/api/v1/library/items/{item_id}/download", tags=["library"])
def download_library_item(
    item_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> FileResponse:
    """Stream an authorised library asset without exposing its storage key."""
    storage_key, media_type = library_asset_for_identity(session, identity, item_id)
    path = asset_store.path_for(storage_key)
    if not path.is_file():
        raise HTTPException(404, "企业素材文件不存在")
    return FileResponse(path, filename=path.name, media_type=media_type,
                        headers={"Cache-Control": "private, no-store"})


@app.get(
    "/api/v1/library/items/{item_id}",
    tags=["library"],
    response_model=LibraryItemDetailResponse,
)
def get_library_item_detail(
    item_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    item = session.get(LibraryItem, item_id)
    if item is None:
        entry_by_id = session.get(ResourceEntry, item_id)
        item = (
            session.scalar(
                select(LibraryItem)
                .where(
                    LibraryItem.resource_entry_id == item_id,
                    LibraryItem.active == true(),
                )
                .order_by(LibraryItem.updated_at.desc())
            )
            if entry_by_id is not None
            else None
        )
    if item is None:
        raise HTTPException(404, "企业资料或素材不存在")
    if not item.active:
        raise HTTPException(409, "企业资料或素材已经停用")
    if (
        item.owner_id != identity.user_id
        and item.data_level != "public"
        and not has_permission(identity, "library.manage")
    ):
        raise HTTPException(403, "无权查看该企业资料或素材")
    entry = session.get(ResourceEntry, item.resource_entry_id) if item.resource_entry_id else None
    version = (
        session.get(ResourceVersion, entry.current_version_id)
        if entry is not None and entry.current_version_id
        else None
    )
    metadata_changed = _ensure_library_media_metadata(
        session,
        item=item,
        entry=entry,
        version=version,
        asset_store=asset_store,
    )
    response = library_item_json(item, entry, version)
    response.update(
        {
            "parent": None,
            "children": [],
            "assets": [],
            "external_references": [],
        }
    )
    if entry is None:
        if metadata_changed:
            session.commit()
        return response

    def relation_summary(related: ResourceEntry) -> dict | None:
        if not related.active:
            return None
        if (
            related.owner_id != identity.user_id
            and related.data_level != "public"
            and not has_permission(identity, "library.manage")
        ):
            return None
        related_item = session.scalar(
            select(LibraryItem)
            .where(
                LibraryItem.resource_entry_id == related.id,
                LibraryItem.active == true(),
            )
            .order_by(LibraryItem.updated_at.desc())
        )
        related_asset = session.scalar(
            select(AssetRef)
            .where(AssetRef.entry_id == related.id)
            .order_by(AssetRef.created_at.desc())
        )
        reference_id = related_item.id if related_item is not None else related.id
        return {
            "item_id": related_item.id if related_item is not None else None,
            "resource_entry_id": related.id,
            "name": related.title,
            "resource_kind": related.resource_kind,
            "category": related.category,
            "has_asset": related_asset is not None,
            "download_url": (
                f"/api/v1/library/items/{reference_id}/download"
                if related_asset is not None
                else None
            ),
        }

    if entry.parent_id:
        parent = session.get(ResourceEntry, entry.parent_id)
        if parent is not None:
            response["parent"] = relation_summary(parent)
    children = session.scalars(
        select(ResourceEntry)
        .where(
            ResourceEntry.parent_id == entry.id,
            ResourceEntry.active == true(),
        )
        .order_by(ResourceEntry.title)
    ).all()
    response["children"] = [
        summary for child in children if (summary := relation_summary(child)) is not None
    ]
    asset_statement = select(AssetRef).where(AssetRef.entry_id == entry.id)
    if entry.current_version_id:
        asset_statement = asset_statement.where(
            or_(
                AssetRef.version_id == entry.current_version_id,
                AssetRef.version_id.is_(None),
                AssetRef.storage_key == item.storage_key,
            )
        )
    response["assets"] = [
        {
            "id": asset.id,
            "media_type": asset.media_type,
            "size_bytes": asset.size_bytes,
            "download_url": f"/api/v1/library/items/{asset.id}/download",
        }
        for asset in session.scalars(asset_statement.order_by(AssetRef.created_at.desc())).all()
    ]
    response["external_references"] = [
        {
            "system": external.system,
            "external_id": external.external_id,
            "source_url": external.source_url,
        }
        for external in session.scalars(
            select(ExternalRef)
            .where(ExternalRef.entry_id == entry.id)
            .order_by(ExternalRef.system, ExternalRef.external_id)
        ).all()
    ]
    if metadata_changed:
        session.commit()
    return response


@app.get(
    "/api/v1/library/categories",
    tags=["library"],
    response_model=list[LibraryCategoryResponse],
)
def list_library_categories(
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> list[dict]:
    statement = (
        select(
            ResourceEntry.category,
            ResourceEntry.resource_kind,
            func.count(ResourceEntry.id),
        )
        .where(ResourceEntry.active == true())
        .group_by(ResourceEntry.category, ResourceEntry.resource_kind)
        .order_by(ResourceEntry.category, ResourceEntry.resource_kind)
    )
    if not has_permission(identity, "library.manage"):
        statement = statement.where(
            or_(
                ResourceEntry.owner_id == identity.user_id,
                ResourceEntry.data_level == "public",
            )
        )
    categories: dict[str, dict] = {}
    for category, resource_kind, count in session.execute(statement).all():
        current = categories.setdefault(
            category,
            {
                "id": category,
                "name": category,
                "resource_count": 0,
                "resource_kinds": [],
            },
        )
        current["resource_count"] += count
        current["resource_kinds"].append(resource_kind)
    return list(categories.values())


@app.post("/api/v1/library/import-jsonl", tags=["library"])
def import_configured_official_jsonl(
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    if not has_permission(identity, "library.manage"):
        raise HTTPException(403, "只有管理员可以导入企业官网资料")
    if settings.knowledge_dir is None:
        raise HTTPException(409, "请先在服务端配置 CONTENT_KNOWLEDGE_DIR")
    job = Job(
        task_id="library",
        owner_id=identity.user_id,
        kind="official_knowledge_import",
        status="queued",
        progress=0,
        result_json="{}",
    )
    session.add(job)
    session.flush()
    audit(
        session,
        identity.user_id,
        "knowledge.official_jsonl_queued",
        "job",
        job.id,
    )
    session.commit()
    dispatcher.submit(lambda: run_official_knowledge_import_job(job.id))
    return job_json(job)


@app.post("/api/v1/library/items/{item_id}/versions", tags=["library"])
def create_library_version(
    item_id: str,
    payload: LibraryItemVersionCreate,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    source = session.get(LibraryItem, item_id)
    if source is None:
        raise HTTPException(404, "资料不存在")
    if source.owner_id != identity.user_id and not has_permission(identity, "library.manage"):
        raise HTTPException(403, "无权更新该资料")
    if not source.active or source.version != payload.expected_version:
        raise HTTPException(409, "资料版本已变化，请刷新后再编辑")
    item = LibraryItem(
        owner_id=source.owner_id,
        version=source.version + 1,
        category=payload.category,
        name=payload.name,
        text_content=payload.text_content,
        data_level=payload.data_level,
    )
    source.active = False
    session.add(item)
    session.flush()
    entry = (
        session.get(ResourceEntry, source.resource_entry_id) if source.resource_entry_id else None
    )
    previous_version = (
        session.get(ResourceVersion, entry.current_version_id)
        if entry and entry.current_version_id else None
    )
    previous_asset = _library_asset_ref(session, source, entry, previous_version)
    # Reuse the immutable attachment when only text changes. AssetRef.storage_key
    # is unique, so the new LibraryItem points to the existing stored asset.
    item.storage_key = previous_asset.storage_key if previous_asset else source.storage_key
    entry, resource_version = create_resource_version_record(
        session,
        owner_id=source.owner_id,
        resource_kind=payload.resource_kind,
        category=payload.category,
        title=payload.name,
        text_content=payload.text_content,
        data_level=payload.data_level,
        version_number=source.version + 1,
        created_by=identity.user_id,
        entry=entry,
        tags=payload.tags,
        parent_id=payload.parent_id,
        source_url=payload.source_url,
        crawled_at=payload.crawled_at,
        freshness=payload.freshness,
        facts=payload.facts,
        notes=payload.notes,
        metadata={**payload.metadata, "legacy_library_item_id": item.id},
        tenant_id=entry.tenant_id if entry else payload.tenant_id,
        department_id=entry.department_id if entry else payload.department_id,
        allowed_roles=load(entry.allowed_roles_json) if entry else payload.allowed_roles,
    )
    item.resource_entry_id = entry.id
    audit(
        session,
        identity.user_id,
        "library.version_created",
        "library_item",
        item.id,
        source_id=source.id,
    )
    session.commit()
    return library_item_json(item, entry, resource_version)


@app.post("/api/v1/library/items/{item_id}/versions/upload", tags=["library"])
async def upload_library_version(
    item_id: str,
    expected_version: int,
    category: str,
    name: str,
    data_level: str = "internal",
    resource_kind: str | None = None,
    tags: str = "",
    parent_id: str | None = None,
    source_url: str | None = None,
    crawled_at: datetime | None = None,
    freshness: str | None = None,
    facts: str = "",
    notes: str = "",
    file: UploadFile = File(...),
    text_content: str | None = Form(default=None, max_length=1_000_000),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
    knowledge_catalog: KnowledgeCatalogPort = Depends(get_knowledge_catalog_port),
) -> dict:
    """Create a new immutable resource version with a replacement asset."""
    source = session.get(LibraryItem, item_id)
    if source is None:
        raise HTTPException(404, "资料不存在")
    if source.owner_id != identity.user_id and not has_permission(identity, "library.manage"):
        raise HTTPException(403, "无权更新该资料")
    if not source.active or source.version != expected_version:
        raise HTTPException(409, "资料版本已变化，请刷新后再编辑")
    if text_content is not None and not text_content.strip():
        raise HTTPException(422, "请填写文本内容")
    if not category.strip() or len(category) > 128:
        raise HTTPException(422, "业务分类不能为空且不能超过 128 个字符")
    if not name.strip() or len(name) > 255:
        raise HTTPException(422, "资料名称不能为空、不能全为空格且不能超过 255 个字符")
    if len(notes) > 10_000:
        raise HTTPException(422, "备注不能超过 10000 个字符")
    if data_level not in DATA_LEVELS:
        raise HTTPException(422, "不支持的数据等级")

    entry = (
        session.get(ResourceEntry, source.resource_entry_id) if source.resource_entry_id else None
    )
    _, media_type = validate_library_upload(file)
    resource_kind = library_upload_resource_kind(
        resource_kind if resource_kind is not None else entry.resource_kind if entry else None, media_type
    )

    item = LibraryItem(
        owner_id=source.owner_id,
        version=source.version + 1,
        category=category,
        name=name,
        data_level=data_level,
    )
    session.add(item)
    session.flush()
    key = f"library/{item.id}/{Path(file.filename or 'material.bin').name}"
    stored = asset_store.put_stream(key, file.file, media_type)
    item.storage_key = stored.key

    ingested = None
    if (text_content is None and resource_kind in {"document", "product", "spec"}
            and not media_type.startswith(("image/", "video/", "audio/"))):
        try:
            ingested = knowledge_catalog.ingest(
                KnowledgeIngestRequest(
                    source_path=asset_store.path_for(stored.key),
                    owner_id=source.owner_id,
                    title=name,
                    category=category,
                    data_level=data_level,
                    metadata={"filename": file.filename, "media_type": media_type},
                )
            )
        except ValueError as exc:
            raise HTTPException(422, f"资料解析失败：{exc}") from exc
    item.text_content = text_content if text_content is not None else ingested.text if ingested else ""

    metadata = _uploaded_library_metadata(
        asset_store=asset_store,
        storage_key=stored.key,
        resource_kind=resource_kind,
        media_type=stored.media_type,
        metadata={"legacy_library_item_id": item.id, "filename": file.filename},
    )
    source.active = False
    entry, resource_version = create_resource_version_record(
        session,
        owner_id=source.owner_id,
        resource_kind=resource_kind,
        category=category,
        title=name,
        text_content=item.text_content,
        data_level=data_level,
        version_number=item.version,
        created_by=identity.user_id,
        entry=entry,
        tags=split_metadata_values(tags),
        parent_id=parent_id,
        source_url=source_url,
        crawled_at=crawled_at,
        freshness=freshness,
        facts=parse_fact_values(facts),
        notes=notes,
        metadata=metadata,
        asset_key=stored.key,
        asset_media_type=stored.media_type,
        asset_size=stored.size,
        asset_checksum=stored.checksum_sha256,
        chunks=ingested.chunks if ingested else None,
        tenant_id=entry.tenant_id if entry else None,
        department_id=entry.department_id if entry else None,
        allowed_roles=load(entry.allowed_roles_json) if entry else None,
    )
    item.resource_entry_id = entry.id
    audit(
        session,
        identity.user_id,
        "library.file_version_created",
        "library_item",
        item.id,
        source_id=source.id,
        storage_key=stored.key,
    )
    session.commit()
    return library_item_json(item, entry, resource_version)


@app.delete("/api/v1/library/items/{item_id}", tags=["library"])
def delete_library_item(
    item_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    item = session.get(LibraryItem, item_id)
    if item is None:
        raise HTTPException(404, "资料不存在")
    if item.owner_id != identity.user_id and not has_permission(identity, "library.manage"):
        raise HTTPException(403, "无权删除该资料")
    deleted_id = item.id
    try:
        cleanup_id = delete_library_records(session, item, deleted_by=identity.user_id)
    except LibraryDeletionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(session, identity.user_id, "library.deleted", "library_item", deleted_id, deletion_mode="hard")
    session.commit()
    return {"id": deleted_id, "deleted": True, "deletion_mode": "hard", "cleanup_job_id": cleanup_id}



@app.post("/api/v1/sensitive-rules", tags=["compliance"])
def create_rule(
    payload: RuleCreate,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    if not has_permission(identity, "rules.manage"):
        raise HTTPException(403, "当前账号没有敏感词维护权限，请联系项目管理员")
    phrase = payload.phrase.strip()
    if not phrase:
        raise HTTPException(422, "敏感词不能为空")
    rule_type = "forbidden" if payload.level == "forbidden" else payload.rule_type
    level = "forbidden" if rule_type == "forbidden" else "warning"
    item = session.scalar(select(SensitiveRule).where(SensitiveRule.phrase == phrase))
    created = item is None
    if item is None:
        item = SensitiveRule(
            phrase=phrase,
            rule_type=rule_type,
            level=level,
            replacement=payload.replacement,
            created_by=identity.user_id,
        )
        session.add(item)
        session.flush()
    else:
        item.rule_type = rule_type
        item.level = level
        item.replacement = payload.replacement
        item.active = True
        item.created_by = identity.user_id
    audit(
        session,
        identity.user_id,
        "sensitive_rule.created" if created else "sensitive_rule.updated",
        "sensitive_rule",
        item.id,
    )
    session.commit()
    session.refresh(item)
    return {
        "id": item.id,
        "phrase": item.phrase,
        "rule_type": item.rule_type,
        "level": item.level,
        "replacement": item.replacement,
        "created": created,
    }


@app.get("/api/v1/sensitive-rules", tags=["compliance"])
def list_rules(
    session: Session = Depends(get_session), _: Identity = Depends(get_identity)
) -> list[dict]:
    rows = session.scalars(select(SensitiveRule).where(SensitiveRule.active == true())).all()
    return [
        {
            "id": row.id,
            "phrase": row.phrase,
            "rule_type": row.rule_type,
            "level": row.level,
            "replacement": row.replacement,
        }
        for row in rows
    ]


@app.delete("/api/v1/sensitive-rules/{rule_id}", tags=["compliance"])
def delete_rule(
    rule_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    if not has_permission(identity, "rules.manage"):
        raise HTTPException(403, "当前账号没有敏感词维护权限，请联系项目管理员")
    rule = session.get(SensitiveRule, rule_id)
    if rule is None:
        raise HTTPException(404, "规则不存在")
    from .models import RuleSeedState

    seed_state = session.get(RuleSeedState, SEED_ID)
    if seed_state is not None:
        seed_state.completed = True
    audit(
        session, identity.user_id, "sensitive_rule.deleted", "sensitive_rule", rule_id,
        phrase=rule.phrase, rule_type=rule.rule_type, deletion_mode="hard",
    )
    session.delete(rule)
    session.commit()
    return {"id": rule_id, "deleted": True}


@app.post("/api/v1/tasks", tags=["tasks"], response_model=TaskResponse)
def create_task(
    payload: TaskCreate,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    values = payload.model_dump(exclude={"keywords", "knowledge_resource_ids"})
    if payload.product_resource_id:
        product = get_product_resource_for_identity(
            session,
            payload.product_resource_id,
            identity,
        )
        values["product"] = product.title
        values["product_resource_id"] = product.id
    requested_knowledge_ids = list(payload.knowledge_resource_ids)
    if payload.product_resource_id and payload.product_resource_id not in requested_knowledge_ids:
        requested_knowledge_ids.append(payload.product_resource_id)
    knowledge_resource_ids = get_knowledge_resources_for_identity(
        session,
        requested_knowledge_ids,
        identity,
    )
    task = ContentTask(
        owner_id=identity.user_id,
        keywords_json=dump(payload.keywords),
        knowledge_resource_ids_json=dump(knowledge_resource_ids),
        **values,
    )
    session.add(task)
    session.flush()
    initialize_workflow(session, task, identity.user_id)
    audit(session, identity.user_id, "task.created", "content_task", task.id, mode=task.mode)
    session.commit()
    latest = latest_task_update_times(session, [task])
    return task_json(task, session=session, updated_at=latest.get(task.id), identity=identity)


@app.get("/api/v1/tasks", tags=["tasks"], response_model=list[TaskResponse])
def list_tasks(
    status: str | None = Query(default=None, max_length=32),
    keyword: str | None = Query(default=None, min_length=1, max_length=128),
    include_archived: bool = Query(
        default=False,
        description="是否包含已归档任务；历史内容页可开启，最近任务默认关闭。",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> list[dict]:
    # Apply visibility before offset/limit.  Paginating the whole table first
    # can return an empty page when newer tasks belong to another user or
    # department, making older tasks appear to have disappeared.
    visibility = or_(
        ContentTask.owner_id == identity.user_id,
        ContentTask.allowed_users_json.like(f'%"{identity.user_id}"%'),
        ContentTask.allowed_departments_json.like(f'%"{identity.department}"%'),
    )
    admin_read = can_view_all_tasks(identity)
    statement = select(ContentTask).where(ContentTask.id.not_in(hidden_edit_draft_ids()))
    if not admin_read:
        statement = statement.where(visibility)
    if not include_archived:
        statement = statement.where(
            ContentTask.status != "archived",
            ContentTask.archived_at.is_(None),
            ContentTask.id.not_in(legacy_archived_task_ids()),
        )
    if status:
        statement = statement.where(ContentTask.status == status)
    if keyword:
        pattern = f"%{keyword}%"
        statement = statement.where(
            or_(ContentTask.name.ilike(pattern), ContentTask.topic.ilike(pattern))
        )
    rows = session.scalars(
        statement.order_by(ContentTask.updated_at.desc(), ContentTask.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    accessible_rows = [
        row for row in rows if admin_read or can_access(row, identity.user_id, identity.department)
    ]
    latest = latest_task_update_times(session, accessible_rows)
    accessible_rows.sort(
        key=lambda row: (latest.get(row.id, row.updated_at), row.id),
        reverse=True,
    )
    return [
        task_json(row, session=session, updated_at=latest.get(row.id), identity=identity)
        for row in accessible_rows
    ]


@app.get("/api/v1/tasks/{task_id}", tags=["tasks"], response_model=TaskResponse)
def get_task(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity, allow_admin_read=True)
    latest = latest_task_update_times(session, [task])
    return task_json(task, session=session, updated_at=latest.get(task.id), identity=identity)


@app.patch("/api/v1/tasks/{task_id}", tags=["tasks"], response_model=TaskResponse)
def update_task(
    task_id: str,
    payload: TaskPatch,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    if task.version != payload.expected_version:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {task.version}，请求基于版本 {payload.expected_version}",
        )
    previous_product_resource_id = task.product_resource_id
    changed = False
    values = payload.model_dump(
        exclude_unset=True,
        exclude={"expected_version", "keywords", "knowledge_resource_ids"},
    )
    if "product_resource_id" in payload.model_fields_set:
        if payload.product_resource_id:
            product = get_product_resource_for_identity(
                session,
                payload.product_resource_id,
                identity,
            )
            values["product"] = product.title
            values["product_resource_id"] = product.id
    elif "product" in payload.model_fields_set:
        # A free-text edit intentionally detaches the task from its old catalog entry.
        values["product_resource_id"] = None
    for field_name, value in values.items():
        if getattr(task, field_name) == value:
            continue
        setattr(task, field_name, value)
        changed = True
    if payload.keywords is not None:
        current_keywords = load(task.keywords_json)
        if current_keywords != payload.keywords:
            task.keywords_json = dump(payload.keywords)
            changed = True
    product_relationship_changed = "product_resource_id" in payload.model_fields_set or (
        "product" in payload.model_fields_set
        and "product_resource_id" not in payload.model_fields_set
    )
    knowledge_selection_changed = (
        "knowledge_resource_ids" in payload.model_fields_set or product_relationship_changed
    )
    if knowledge_selection_changed:
        if "knowledge_resource_ids" in payload.model_fields_set:
            requested_knowledge_ids = list(payload.knowledge_resource_ids or [])
        else:
            existing_knowledge_ids = load(task.knowledge_resource_ids_json)
            requested_knowledge_ids = (
                list(existing_knowledge_ids) if isinstance(existing_knowledge_ids, list) else []
            )

        # The compatibility product picker is one of the knowledge selections.
        # Replacing or detaching it must not leave the former product grounded in
        # subsequent generations, while an active catalog product is always kept.
        if product_relationship_changed:
            if (
                previous_product_resource_id
                and previous_product_resource_id != task.product_resource_id
            ):
                requested_knowledge_ids = [
                    item for item in requested_knowledge_ids if item != previous_product_resource_id
                ]
            if task.product_resource_id and task.product_resource_id not in requested_knowledge_ids:
                requested_knowledge_ids.append(task.product_resource_id)
        canonical_knowledge_ids = get_knowledge_resources_for_identity(
            session,
            requested_knowledge_ids,
            identity,
        )
        current_knowledge_ids = load(task.knowledge_resource_ids_json)
        if current_knowledge_ids != canonical_knowledge_ids:
            task.knowledge_resource_ids_json = dump(canonical_knowledge_ids)
            changed = True
    if not changed:
        latest = latest_task_update_times(session, [task])
        return task_json(task, session=session, updated_at=latest.get(task.id), identity=identity)
    task.version += 1
    mark_configuration_changed(session, task, identity.user_id)
    audit(
        session,
        identity.user_id,
        "task.updated",
        "content_task",
        task.id,
        version=task.version,
    )
    session.commit()
    latest = latest_task_update_times(session, [task])
    return task_json(task, session=session, updated_at=latest.get(task.id), identity=identity)


@app.delete("/api/v1/tasks/{task_id}", tags=["tasks"], response_model=TaskDeleteResponse)
def archive_task(
    task_id: str,
    expected_version: int,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    if task.owner_id != identity.user_id:
        raise HTTPException(403, "仅创建者可归档内容任务")
    if task.version != expected_version:
        raise HTTPException(409, "任务版本已变化，请刷新后重试")
    if is_archived_snapshot(task, session):
        return {"id": task.id, "archived": True}
    ensure_task_jobs_finished(session, task)
    # Archiving freezes the task as a historical snapshot.  Do not recalculate
    # the workflow state from current upstream stages here: a later edit may
    # make those stages stale, but it must not invalidate the archived version.
    workflow_status = task.status if task.status != "archived" else "draft"
    task.archived_workflow_status = workflow_status
    task.archived_at = datetime.now(UTC)
    task.status = "archived"
    task.version += 1
    audit(session, identity.user_id, "task.archived", "content_task", task.id)
    session.commit()
    return {"id": task.id, "archived": True}


@app.delete(
    "/api/v1/tasks/{task_id}/permanent",
    tags=["tasks"],
    response_model=TaskPermanentDeleteResponse,
)
def delete_task_permanently(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    if task.owner_id != identity.user_id:
        raise HTTPException(403, "仅创建者可删除内容任务")
    task_id = task.id
    try:
        delete_content_task_records(
            session,
            task,
            deleted_by=identity.user_id,
        )
    except TaskDeletionConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return {"id": task_id, "deleted": True}


@app.post("/api/v1/tasks/{task_id}/restore", tags=["tasks"], response_model=TaskResponse)
def restore_task(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    if task.owner_id != identity.user_id:
        raise HTTPException(403, "仅创建者可恢复内容任务")
    if not is_archived_snapshot(task, session):
        raise HTTPException(409, "该任务未归档")
    task = restore_archive_as_draft(session, task, identity.user_id)
    session.commit()
    latest = latest_task_update_times(session, [task])
    return task_json(task, session=session, updated_at=latest.get(task.id), identity=identity)


@app.post(
    "/api/v1/tasks/{task_id}/video-project",
    tags=["video-projects"],
    response_model=VideoProjectResponse,
)
def create_video_project_endpoint(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    if task.mode not in {"full", "video"}:
        raise HTTPException(422, "只有完整内容创作或快捷视频任务可以创建视频工程")
    project, created = create_or_get_video_project(session, task, identity.user_id)
    if created:
        audit(
            session,
            identity.user_id,
            "video_project.created",
            "video_project",
            project.id,
            task_id=task.id,
            source=video_project_json(session, project)["revision"]["source"],
        )
    session.commit()
    return video_project_json(session, project)


@app.post(
    "/api/v1/video-projects/{project_id}/assets/upload",
    tags=["video-projects"],
    response_model=ProjectAssetResponse,
)
def upload_video_project_asset(
    project_id: str,
    role: str = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    project, task = get_video_project_or_404(session, project_id, identity)
    ensure_task_editable(task)
    enforce_project_asset_count(session, project)
    filename = safe_filename(file.filename)
    file.file.seek(0)
    size_bytes = upload_size_bytes(file)
    try:
        media_kind, media_type = validate_upload_header(
            filename=filename,
            declared_media_type=file.content_type,
            head=read_upload_head(file.file),
            size_bytes=size_bytes,
            policy=MediaIngestPolicy(),
        )
    except MediaValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    validate_project_asset_role(media_kind, role)
    enforce_project_media_quota(session, project, size_bytes)
    object_key = f"video-projects/{project.id}/originals/{uuid4().hex}-{filename}"
    file.file.seek(0)
    try:
        stored = asset_store.put_stream(object_key, file.file, media_type)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    project_asset, derivative_count = register_video_project_asset(
        session,
        project=project,
        owner_id=identity.user_id,
        role=role,
        filename=filename,
        media_kind=media_kind,
        media_type=stored.media_type,
        storage_key=stored.key,
        size_bytes=stored.size,
        checksum_sha256=stored.checksum_sha256,
        asset_store=asset_store,
    )
    audit(
        session,
        identity.user_id,
        "video_project.asset_uploaded",
        "project_asset",
        project_asset.id,
        project_id=project.id,
        media_kind=media_kind,
        role=role,
        derivative_count=derivative_count,
    )
    session.commit()
    return project_asset_json(session, project_asset)


@app.post(
    "/api/v1/video-uploads",
    tags=["video-projects"],
    response_model=VideoUploadSessionResponse,
)
def create_video_upload(
    payload: VideoUploadSessionCreate,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    project, task = get_video_project_or_404(session, payload.project_id, identity)
    ensure_task_editable(task)
    enforce_project_asset_count(session, project)
    enforce_project_media_quota(session, project, payload.total_size)
    try:
        upload = create_upload_session(
            session,
            project_id=project.id,
            owner_id=identity.user_id,
            role=payload.role,
            filename=payload.filename,
            media_type=payload.media_type,
            total_size=payload.total_size,
            chunk_size=payload.chunk_size,
            checksum=payload.checksum,
        )
    except (VideoUploadConflictError, VideoUploadValidationError) as exc:
        raise video_upload_http_error(exc) from exc
    audit(
        session,
        identity.user_id,
        "video_project.upload_created",
        "video_upload",
        upload.id,
        project_id=project.id,
        total_size=upload.total_size,
        total_chunks=upload.total_chunks,
    )
    session.commit()
    return upload_session_json(
        session,
        upload,
        upload_root=settings.data_dir / "video-upload-staging",
    )


@app.get(
    "/api/v1/video-uploads/{upload_id}",
    tags=["video-projects"],
    response_model=VideoUploadSessionResponse,
)
def get_video_upload(
    upload_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    upload, _, _ = get_video_upload_or_404(session, upload_id, identity)
    return upload_session_json(
        session,
        upload,
        upload_root=settings.data_dir / "video-upload-staging",
    )


@app.put(
    "/api/v1/video-uploads/{upload_id}/chunks/{chunk_index}",
    tags=["video-projects"],
    response_model=VideoUploadChunkResponse,
)
def put_video_upload_chunk(
    upload_id: str,
    chunk_index: int,
    file: UploadFile = File(...),
    checksum_sha256: str = Header(..., alias="X-Chunk-Sha256"),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    _, _, task = get_video_upload_or_404(session, upload_id, identity)
    ensure_task_editable(task)
    file.file.seek(0)
    try:
        receipt = write_upload_chunk(
            session,
            upload_id,
            index=chunk_index,
            stream=file.file,
            checksum_sha256=checksum_sha256,
            upload_root=settings.data_dir / "video-upload-staging",
        )
    except (VideoUploadConflictError, VideoUploadValidationError) as exc:
        raise video_upload_http_error(exc) from exc
    session.commit()
    return asdict(receipt)


@app.delete(
    "/api/v1/video-uploads/{upload_id}",
    tags=["video-projects"],
    response_model=VideoUploadCancelResponse,
)
def cancel_video_upload(
    upload_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    upload, project, task = get_video_upload_or_404(session, upload_id, identity)
    ensure_task_editable(task)
    try:
        cancel_upload_session(
            session,
            upload.id,
            upload_root=settings.data_dir / "video-upload-staging",
        )
    except (VideoUploadConflictError, VideoUploadValidationError) as exc:
        raise video_upload_http_error(exc) from exc
    audit(
        session,
        identity.user_id,
        "video_project.upload_cancelled",
        "video_upload",
        upload.id,
        project_id=project.id,
    )
    session.commit()
    return {"upload_id": upload.id, "status": "cancelled"}


@app.post(
    "/api/v1/video-uploads/{upload_id}/complete",
    tags=["video-projects"],
    response_model=VideoUploadFinalizedResponse,
)
def complete_video_upload(
    upload_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    upload, project, task = get_video_upload_or_404(session, upload_id, identity)
    ensure_task_editable(task)
    if upload.project_asset_id:
        existing = session.get(ProjectAsset, upload.project_asset_id)
        if existing is None or existing.project_id != project.id:
            raise HTTPException(409, "分片上传关联的工程素材不存在")
        return {
            "upload": upload_session_json(
                session,
                upload,
                upload_root=settings.data_dir / "video-upload-staging",
            ),
            "asset": project_asset_json(session, existing),
        }
    enforce_project_asset_count(session, project)
    try:
        completed = complete_upload_session(
            session,
            upload.id,
            upload_root=settings.data_dir / "video-upload-staging",
        )
    except (
        VideoUploadConflictError,
        VideoUploadIncompleteError,
        VideoUploadValidationError,
    ) as exc:
        if upload.status == "failed":
            session.commit()
        raise video_upload_http_error(exc) from exc

    object_key = f"video-projects/{project.id}/originals/{uuid4().hex}-{upload.filename}"
    enforce_project_media_quota(session, project, completed.total_size)
    with completed.path.open("rb") as source:
        stored = asset_store.put_stream(object_key, source, completed.media_type)
    if stored.size != completed.total_size or stored.checksum_sha256 != completed.checksum_sha256:
        asset_store.path_for(stored.key).unlink(missing_ok=True)
        raise HTTPException(500, "分片上传落库后的文件校验失败")
    project_asset, derivative_count = register_video_project_asset(
        session,
        project=project,
        owner_id=identity.user_id,
        role=upload.role,
        filename=upload.filename,
        media_kind=completed.media_kind,
        media_type=stored.media_type,
        storage_key=stored.key,
        size_bytes=stored.size,
        checksum_sha256=stored.checksum_sha256,
        asset_store=asset_store,
        source_metadata={"upload_session_id": upload.id, "resumable": True},
    )
    upload.project_asset_id = project_asset.id
    audit(
        session,
        identity.user_id,
        "video_project.upload_completed",
        "project_asset",
        project_asset.id,
        project_id=project.id,
        upload_id=upload.id,
        derivative_count=derivative_count,
    )
    session.commit()
    response = {
        "upload": upload_session_json(
            session,
            upload,
            upload_root=settings.data_dir / "video-upload-staging",
        ),
        "asset": project_asset_json(session, project_asset),
    }
    completed.path.unlink(missing_ok=True)
    return response


@app.get(
    "/api/v1/video-projects/{project_id}/assets",
    tags=["video-projects"],
    response_model=list[ProjectAssetResponse],
)
def list_video_project_assets(
    project_id: str,
    active_only: bool = False,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> list[dict]:
    project, _ = get_video_project_or_404(session, project_id, identity, allow_admin_read=True)
    statement = select(ProjectAsset).where(ProjectAsset.project_id == project.id)
    if active_only:
        statement = statement.where(ProjectAsset.active == true())
    items = session.scalars(statement.order_by(ProjectAsset.created_at)).all()
    return [project_asset_json(session, item) for item in items]


@app.get(
    "/api/v1/video-projects/{project_id}/assets/{asset_id}",
    tags=["video-projects"],
    response_model=ProjectAssetResponse,
)
def get_video_project_asset(
    project_id: str,
    asset_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    item, _, _ = get_project_asset_or_404(
        session,
        project_id,
        asset_id,
        identity,
        allow_admin_read=True,
    )
    return project_asset_json(session, item)


@app.get(
    "/api/v1/video-projects/{project_id}/assets/{asset_id}/download",
    tags=["video-projects"],
)
def download_video_project_asset(
    project_id: str,
    asset_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> FileResponse:
    item, _, _ = get_project_asset_or_404(
        session,
        project_id,
        asset_id,
        identity,
        allow_admin_read=True,
    )
    try:
        storage_key, media_type, stored_filename = project_asset_storage(session, item)
    except VideoProjectValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    path = asset_store.path_for(storage_key)
    if not path.is_file():
        raise HTTPException(404, "视频工程素材原始文件不存在")
    filename = safe_filename(item.label)
    if not Path(filename).suffix:
        filename = f"{filename}{Path(stored_filename).suffix.lower()}"
    return FileResponse(path, filename=filename, media_type=media_type,
                        headers={"Cache-Control": "private, no-store"})


@app.get(
    "/api/v1/video-projects/{project_id}/assets/{asset_id}/stream",
    tags=["video-projects"],
)
def stream_video_project_asset(
    project_id: str,
    asset_id: str,
    request: Request,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
):
    item, _, _ = get_project_asset_or_404(
        session,
        project_id,
        asset_id,
        identity,
        allow_admin_read=True,
    )
    try:
        storage_key, media_type, _ = project_asset_storage(session, item)
        path = asset_store.path_for(storage_key)
    except (ValueError, VideoProjectValidationError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, "视频工程素材原始文件不存在")
    size = path.stat().st_size
    range_header = request.headers.get("range")
    start, end = 0, max(size - 1, 0)
    status_code = 200
    if range_header:
        if not range_header.startswith("bytes=") or "," in range_header:
            raise HTTPException(416, "仅支持单个 bytes Range")
        bounds = range_header[6:].split("-", 1)
        try:
            if not bounds[0]:
                suffix_length = int(bounds[1])
                if suffix_length <= 0:
                    raise ValueError
                start = max(0, size - suffix_length)
            else:
                start = int(bounds[0])
                end = int(bounds[1]) if bounds[1] else end
        except (TypeError, ValueError) as exc:
            raise HTTPException(416, "Range 格式无效") from exc
        if start < 0 or start >= size or end < start:
            raise HTTPException(
                416,
                "Range 超出文件范围",
                headers={"Content-Range": f"bytes */{size}"},
            )
        end = min(end, size - 1)
        status_code = 206
    content_length = max(0, end - start + 1)

    def iterator():
        remaining = content_length
        with path.open("rb") as source:
            source.seek(start)
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": "inline",
        "Cache-Control": "private, no-store",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        iterator(),
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


@app.patch(
    "/api/v1/video-projects/{project_id}/assets/{asset_id}",
    tags=["video-projects"],
    response_model=ProjectAssetResponse,
)
def update_video_project_asset(
    project_id: str,
    asset_id: str,
    payload: ProjectAssetPatch,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    item, project, task = get_project_asset_or_404(
        session,
        project_id,
        asset_id,
        identity,
    )
    ensure_task_editable(task)
    if payload.role is not None:
        previous_role = item.role
        media_kind = project_asset_media_kind(session, item)
        validate_project_asset_role(media_kind, payload.role)
        item.role = payload.role
        metadata = load(item.metadata_json)
        if (
            payload.role == "logo"
            and previous_role != "logo"
            and metadata.get("source") == "ai-generated"
        ):
            metadata.update(
                {
                    "pending_brand_confirmation": True,
                    "brand_asset_status": "pending_confirmation",
                    "official_brand_asset": False,
                }
            )
            item.metadata_json = dump(metadata)
    if payload.label is not None:
        label = payload.label.strip()
        if not label:
            raise HTTPException(422, "素材名称不能为空")
        item.label = label
    if payload.active is False and item.active:
        try:
            archive_project_asset(session, project, item)
        except VideoProjectConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
    elif payload.active is True:
        item.active = True
    audit(
        session,
        identity.user_id,
        "video_project.asset_updated",
        "project_asset",
        item.id,
        role=item.role,
        active=item.active,
    )
    session.commit()
    return project_asset_json(session, item)


@app.delete(
    "/api/v1/video-projects/{project_id}/assets/{asset_id}",
    tags=["video-projects"],
    response_model=ProjectAssetResponse,
)
def archive_video_project_asset_endpoint(
    project_id: str,
    asset_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    item, project, task = get_project_asset_or_404(
        session,
        project_id,
        asset_id,
        identity,
    )
    ensure_task_editable(task)
    if item.active:
        try:
            archive_project_asset(session, project, item)
        except VideoProjectConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        audit(
            session,
            identity.user_id,
            "video_project.asset_archived",
            "project_asset",
            item.id,
            project_id=project.id,
        )
        session.commit()
    return project_asset_json(session, item)


@app.get(
    "/api/v1/media-derivatives/{derivative_id}/download",
    tags=["video-projects"],
)
def download_media_derivative(
    derivative_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> FileResponse:
    derivative = session.get(MediaDerivative, derivative_id)
    if derivative is None:
        raise HTTPException(404, "媒体衍生文件不存在")
    item = session.get(ProjectAsset, derivative.project_asset_id)
    if item is None:
        raise HTTPException(404, "媒体衍生文件所属素材不存在")
    get_video_project_or_404(session, item.project_id, identity, allow_admin_read=True)
    if derivative.status != "ready" or not derivative.storage_key:
        raise HTTPException(409, "媒体衍生文件尚未生成完成")
    path = asset_store.path_for(derivative.storage_key)
    if not path.is_file():
        raise HTTPException(404, "媒体衍生文件不存在")
    filename = safe_filename(f"{derivative.kind}-{derivative.variant}{path.suffix.lower()}")
    return FileResponse(
        path,
        filename=filename,
        media_type=derivative.media_type or "application/octet-stream",
    )


@app.post(
    "/api/v1/video-projects/{project_id}/assets/{asset_id}/confirm-brand",
    tags=["video-projects"],
    response_model=ProjectAssetResponse,
)
def confirm_video_project_brand_asset(
    project_id: str,
    asset_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    item, project, task = get_project_asset_or_404(
        session,
        project_id,
        asset_id,
        identity,
    )
    ensure_task_editable(task)
    metadata = load(item.metadata_json)
    if item.role != "logo" or metadata.get("source") != "ai-generated":
        raise HTTPException(422, "仅 AI 生成的 Logo 候选需要品牌确认")
    if metadata.get("pending_brand_confirmation") is not False:
        confirmed_at = datetime.now(UTC).isoformat()
        metadata.update(
            {
                "pending_brand_confirmation": False,
                "brand_asset_status": "confirmed",
                "official_brand_asset": True,
                "brand_confirmed_by": identity.user_id,
                "brand_confirmed_at": confirmed_at,
            }
        )
        item.metadata_json = dump(metadata)
        if item.artifact_id:
            artifact = session.get(Artifact, item.artifact_id)
            if artifact is not None:
                artifact_content = load(artifact.content_json)
                artifact_content.update(
                    {
                        "pending_brand_confirmation": False,
                        "brand_asset_status": "confirmed",
                        "official_brand_asset": True,
                        "brand_confirmed_by": identity.user_id,
                        "brand_confirmed_at": confirmed_at,
                    }
                )
                artifact.content_json = dump(artifact_content)
        audit(
            session,
            identity.user_id,
            "video_project.ai_logo_confirmed",
            "project_asset",
            item.id,
            project_id=project.id,
        )
        session.commit()
    return project_asset_json(session, item)


@app.post(
    "/api/v1/video-projects/{project_id}/generate-voice",
    tags=["video-projects"],
    response_model=ProjectAssetResponse,
)
def generate_video_project_voice(
    project_id: str,
    payload: VideoProjectVoiceGenerationRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    speech_provider: SpeechSynthesisPort = Depends(get_speech_synthesis_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    project, task = get_video_project_or_404(session, project_id, identity)
    ensure_task_editable(task)
    if project.current_revision != payload.expected_revision:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {project.current_revision}，"
            f"请求基于版本 {payload.expected_revision}",
        )
    _require_video_ai_brief(task)
    _require_external_provider_egress(
        session,
        task,
        speech_provider.status.provider_id,
        external=speech_provider.status.external,
    )
    try:
        generated = generate_project_voice_asset(
            session,
            project=project,
            task=task,
            created_by=identity.user_id,
            provider=speech_provider,
            asset_store=asset_store,
            spec=ProjectVoiceGenerationSpec(
                text=payload.text,
                label=payload.label,
                voice=payload.voice,
                speed=payload.speed,
                volume=payload.volume,
                audio_format=payload.audio_format,
                sample_rate=payload.sample_rate,
                with_timestamps=payload.with_timestamps,
                instruction=payload.instruction,
                parent_artifact_id=payload.parent_artifact_id,
            ),
        )
    except Exception as exc:
        raise video_project_generation_http_error(exc) from exc
    audit(
        session,
        identity.user_id,
        "video_project.voice_generated",
        "project_asset",
        generated.project_asset.id,
        project_id=project.id,
        provider=speech_provider.status.provider_id,
    )
    session.commit()
    return project_asset_json(session, generated.project_asset)


@app.post(
    "/api/v1/video-projects/{project_id}/generate-subtitles",
    tags=["video-projects"],
    response_model=VideoProjectSubtitleGenerationResponse,
)
def generate_video_project_subtitles(
    project_id: str,
    payload: VideoProjectSubtitleGenerationRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    subtitle_provider: SubtitleAlignmentPort = Depends(get_subtitle_alignment_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    project, task = get_video_project_or_404(session, project_id, identity)
    ensure_task_editable(task)
    if project.current_revision != payload.expected_revision:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {project.current_revision}，"
            f"请求基于版本 {payload.expected_revision}",
        )
    try:
        generated = generate_project_subtitle_asset(
            session,
            project=project,
            task=task,
            created_by=identity.user_id,
            provider=subtitle_provider,
            asset_store=asset_store,
            spec=ProjectSubtitleGenerationSpec(
                text=payload.text,
                label=payload.label,
                source_project_asset_id=payload.source_project_asset_id,
                duration_seconds=payload.duration_seconds,
                max_chars_per_line=payload.max_chars_per_line,
                max_lines=payload.max_lines,
                parent_artifact_id=payload.parent_artifact_id,
            ),
        )
    except Exception as exc:
        raise video_project_generation_http_error(exc) from exc
    segment_payload = [
        {
            "sequence": segment.sequence,
            "text": segment.text,
            "start_seconds": segment.start_seconds,
            "end_seconds": segment.end_seconds,
            "timing_source": segment.timing_source,
        }
        for segment in generated.segments
    ]
    suggested_edit = {
        "expected_revision": project.current_revision,
        "operations": [
            {
                "op_id": f"add-generated-subtitle-{uuid4().hex}",
                "type": "add",
                "track_kind": "subtitle",
                "item": {
                    "id": f"subtitle-{uuid4().hex}",
                    "project_asset_id": generated.project_asset.id,
                    "label": generated.project_asset.label,
                    "start_seconds": 0,
                    "duration_seconds": generated.duration_seconds,
                    "trim_in_seconds": 0,
                    "trim_out_seconds": 0,
                    "volume": 1,
                    "speed": 1,
                    "enabled": True,
                    "metadata": {
                        "source": "ai-generated",
                        "segment_count": len(generated.segments),
                        "source_project_asset_id": generated.source_project_asset_id,
                    },
                },
            }
        ],
    }
    audit(
        session,
        identity.user_id,
        "video_project.subtitles_generated",
        "project_asset",
        generated.project_asset.id,
        project_id=project.id,
        source_project_asset_id=generated.source_project_asset_id,
        provider=subtitle_provider.status.provider_id,
    )
    session.commit()
    return {
        "asset": project_asset_json(session, generated.project_asset),
        "artifact_id": generated.artifact.id,
        "source_project_asset_id": generated.source_project_asset_id,
        "text": generated.text,
        "duration_seconds": generated.duration_seconds,
        "segments": segment_payload,
        "suggested_edit": suggested_edit,
    }


@app.post(
    "/api/v1/video-projects/{project_id}/generate-images",
    tags=["video-projects"],
    response_model=list[ProjectAssetResponse],
)
def generate_video_project_images(
    project_id: str,
    payload: VideoProjectImageGenerationRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    image_provider: ImageGenerationPort = Depends(get_image_generation_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> list[dict]:
    project, task = get_video_project_or_404(session, project_id, identity)
    ensure_task_editable(task)
    if project.current_revision != payload.expected_revision:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {project.current_revision}，"
            f"请求基于版本 {payload.expected_revision}",
        )
    _require_video_ai_brief(task)
    image_egress_levels = _require_external_provider_egress(
        session,
        task,
        image_provider.status.provider_id,
        external=image_provider.status.external,
    )
    _require_external_media_references(
        session,
        list(payload.reference_asset_ids),
        external=image_provider.status.external,
    )
    stored_knowledge_ids = load(task.knowledge_resource_ids_json)
    knowledge_resource_ids = get_knowledge_resources_for_identity(
        session,
        stored_knowledge_ids if isinstance(stored_knowledge_ids, list) else [],
        identity,
        allow_cross_owner=True,
    )
    try:
        selected_knowledge = selected_knowledge_chunks(
            session,
            knowledge_resource_ids,
            requester_id=identity.user_id,
            allowed_levels=image_egress_levels,
            query_text=" ".join(
                value
                for value in (
                    task.name,
                    task.topic,
                    task.product,
                    task.style,
                    payload.prompt,
                )
                if value
            ),
            allow_cross_owner=True,
        )
    except EgressDeniedError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KnowledgeSelectionError as exc:
        raise HTTPException(422, str(exc)) from exc
    reference_images: list[bytes] = []
    reference_media_types: list[str] = []
    product_knowledge_ids = [
        reference_id
        for reference_id in knowledge_resource_ids
        if (
            (entry := session.get(ResourceEntry, reference_id)) is not None
            and is_product_resource(entry)
        )
    ]
    product_knowledge_id_set = set(product_knowledge_ids)
    other_knowledge_ids = [
        reference_id
        for reference_id in knowledge_resource_ids
        if reference_id not in product_knowledge_id_set
    ]
    product_visual_ids = visual_reference_ids_for_knowledge(
        session,
        product_knowledge_ids,
    )
    other_visual_ids = visual_reference_ids_for_knowledge(session, other_knowledge_ids)
    knowledge_visual_ids = list(dict.fromkeys([*product_visual_ids, *other_visual_ids]))
    prompt_parts = (
        [PRODUCT_VISUAL_CONSISTENCY_PROMPT, payload.prompt]
        if product_visual_ids
        else [payload.prompt]
    )
    prompt = grounded_image_prompt(prompt_parts, selected_knowledge)
    _require_external_media_references(
        session,
        knowledge_visual_ids,
        external=image_provider.status.external,
    )
    for reference_id in product_visual_ids:
        storage_key, media_type = library_asset_for_identity(
            session,
            identity,
            reference_id,
            # Selecting a parent product does not bypass child media ownership.
            allow_cross_owner=False,
        )
        if not media_type.startswith("image/"):
            raise HTTPException(422, "所选产品的视觉资料必须是图片类型")
        reference_images.append(asset_store.read_bytes(storage_key))
        reference_media_types.append(media_type)
    for reference_id in other_visual_ids:
        storage_key, media_type = library_asset_for_identity(
            session,
            identity,
            reference_id,
            allow_cross_owner=True,
        )
        if not media_type.startswith("image/"):
            raise HTTPException(422, "所选知识库视觉资料必须是图片类型")
        reference_images.append(asset_store.read_bytes(storage_key))
        reference_media_types.append(media_type)
    try:
        generated = generate_project_image_candidates(
            session,
            project=project,
            task=task,
            created_by=identity.user_id,
            provider=image_provider,
            asset_store=asset_store,
            spec=ProjectImageGenerationSpec(
                prompt=prompt,
                role=payload.role,
                count=payload.count,
                label=payload.label,
                size=payload.size,
                ratio=payload.ratio,
                watermark=payload.watermark,
                seed=payload.seed,
                reference_images=tuple(reference_images),
                reference_media_types=tuple(reference_media_types),
                reference_asset_ids=tuple(payload.reference_asset_ids),
                knowledge_resource_ids=tuple(knowledge_resource_ids),
                visual_reference_resource_ids=tuple(knowledge_visual_ids),
                parent_artifact_id=payload.parent_artifact_id,
            ),
        )
    except Exception as exc:
        raise video_project_generation_http_error(exc, clear_generation_error=True) from exc
    asset_ids = [item.project_asset.id for item in generated.candidates]
    audit(
        session,
        identity.user_id,
        "video_project.images_generated",
        "video_project",
        project.id,
        project_asset_ids=asset_ids,
        role=payload.role,
        provider=image_provider.status.provider_id,
    )
    session.commit()
    return [project_asset_json(session, item.project_asset) for item in generated.candidates]


@app.post(
    "/api/v1/video-projects/{project_id}/generate-video",
    tags=["video-projects"],
    response_model=MediaJobResponse,
)
def generate_video_project_ai_video(
    project_id: str,
    payload: VideoProjectAiVideoGenerationRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    ai_video_provider: AiVideoGenerationPort = Depends(get_ai_video_generation_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
    provider_media_publisher: ProviderMediaPublisherPort = Depends(get_provider_media_publisher),
) -> dict:
    project, task = get_video_project_or_404(session, project_id, identity)
    ensure_task_editable(task)
    if project.current_revision != payload.expected_revision:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {project.current_revision}，"
            f"请求基于版本 {payload.expected_revision}",
        )
    _require_video_ai_brief(task)
    source_copy, copy_context = confirmed_copy_context_for_video(
        session,
        task,
        payload.source_copy_artifact_id,
        user_id=identity.user_id,
    )
    video_egress_levels = _require_external_provider_egress(
        session,
        task,
        ai_video_provider.status.provider_id,
        external=ai_video_provider.status.external,
    )
    _require_external_media_references(
        session,
        [
            reference_id
            for reference_id in (
                payload.source_project_asset_id,
                *payload.reference_image_asset_ids,
                *payload.reference_video_asset_ids,
            )
            if reference_id
        ],
        external=ai_video_provider.status.external,
    )
    stored_knowledge_ids = load(task.knowledge_resource_ids_json)
    knowledge_resource_ids = get_knowledge_resources_for_identity(
        session,
        stored_knowledge_ids if isinstance(stored_knowledge_ids, list) else [],
        identity,
        allow_cross_owner=True,
    )
    try:
        selected_knowledge = selected_knowledge_chunks(
            session,
            knowledge_resource_ids,
            requester_id=identity.user_id,
            allowed_levels=video_egress_levels,
            query_text=" ".join(
                value
                for value in (
                    task.name,
                    task.topic,
                    task.product,
                    task.style,
                    payload.prompt,
                    copy_context,
                )
                if value
            ),
            allow_cross_owner=True,
        )
    except EgressDeniedError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KnowledgeSelectionError as exc:
        raise HTTPException(422, str(exc)) from exc
    (
        product_visual_ids,
        product_reference_images,
        product_reference_media_types,
    ) = product_visual_assets_for_generation(
        session,
        knowledge_resource_ids,
        identity,
        asset_store,
        external=ai_video_provider.status.external,
    )
    video_prompt_parts = []
    if product_visual_ids:
        video_prompt_parts.append(PRODUCT_VISUAL_CONSISTENCY_PROMPT)
    # Reference-video edits must not mix the current instruction into old copy
    # and knowledge. Keep the existing text/image-to-video path unchanged.
    if not payload.reference_video_asset_ids:
        video_prompt_parts.append(payload.prompt)
    if copy_context:
        video_prompt_parts.append(copy_context)
    video_prompt = "\n".join(video_prompt_parts)
    grounded_prompt = grounded_video_prompt(
        video_prompt,
        selected_knowledge,
        max_chars=8_000,
    )
    # Checked before queueing; counted only when a new job is created (a repeated request is free).
    demo_quota_check(1)
    try:
        queued = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by=identity.user_id,
            provider=ai_video_provider,
            asset_store=asset_store,
            provider_media_publisher=provider_media_publisher,
            spec=ProjectAiVideoGenerationSpec(
                prompt=payload.prompt if payload.reference_video_asset_ids else grounded_prompt,
                supporting_context=(grounded_prompt if payload.reference_video_asset_ids else None),
                label=payload.label,
                source_copy_artifact_id=source_copy.id if source_copy is not None else None,
                reference_images=product_reference_images,
                reference_media_types=product_reference_media_types,
                source_project_asset_id=payload.source_project_asset_id,
                reference_image_asset_ids=tuple(payload.reference_image_asset_ids),
                reference_video_asset_ids=tuple(payload.reference_video_asset_ids),
                knowledge_resource_ids=tuple(knowledge_resource_ids),
                visual_reference_resource_ids=tuple(product_visual_ids),
                duration_seconds=payload.duration_seconds,
                ratio=payload.ratio,
                resolution=payload.resolution,
                generate_audio=payload.generate_audio,
                return_last_frame=payload.return_last_frame,
                idempotency_key=payload.idempotency_key,
                parent_artifact_id=payload.parent_artifact_id,
            ),
        )
    except Exception as exc:
        cleanup_failed_ai_video_submission(session, exc)
        raise video_project_generation_http_error(exc, clear_generation_error=True) from exc
    if queued.created:
        demo_quota_record(1)
        audit(
            session,
            identity.user_id,
            "video_project.ai_video_queued",
            "job",
            queued.job.id,
            project_id=project.id,
            provider=ai_video_provider.status.provider_id,
        )
    job_id = queued.job.id
    should_dispatch = queued.created and queued.job.status == "queued"
    session.commit()
    if should_dispatch:
        dispatcher.submit(
            lambda current_id=job_id: run_video_project_ai_generation_job(
                current_id,
                ai_video_provider,
                asset_store,
                provider_media_publisher,
            )
        )
    return job_json(queued.job)


@app.post(
    "/api/v1/video-projects/{project_id}/generate-music",
    tags=["video-projects"],
    response_model=ProjectAssetResponse,
)
def generate_video_project_music(
    project_id: str,
    payload: MusicGenerationCreateRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    music_provider: MusicGenerationPort = Depends(get_music_generation_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    project, task = get_video_project_or_404(session, project_id, identity)
    ensure_task_editable(task)
    if project.current_revision != payload.expected_revision:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {project.current_revision}，"
            f"请求基于版本 {payload.expected_revision}",
        )
    _require_video_ai_brief(task)
    _require_external_provider_egress(
        session,
        task,
        music_provider.status.provider_id,
        external=music_provider.status.external,
    )
    try:
        generated = music_provider.generate(
            MusicGenerationRequest(
                prompt=payload.prompt,
                duration_seconds=payload.duration_seconds,
                audio_format=payload.audio_format,
                response_format=payload.response_format,
            )
        )
    except ProviderCallError as exc:
        raise HTTPException(502, f"背景音乐生成失败：{exc}") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc

    storage_key = (
        f"tasks/{task.id}/video-projects/{project.id}/generated-music/"
        f"{uuid4().hex}.{generated.extension}"
    )
    stored = asset_store.put_bytes(
        storage_key,
        generated.audio,
        generated.media_type,
    )
    artifact = create_artifact(
        session,
        task,
        "music",
        {
            "prompt": payload.prompt,
            "instrumental": True,
            "duration_seconds": generated.duration_seconds,
            "requested_duration_seconds": generated.requested_duration_seconds,
            "provider": generated.provider_id,
            "model": generated.model,
            "media_type": stored.media_type,
            "size_bytes": stored.size,
            "checksum_sha256": stored.checksum_sha256,
            "metadata": generated.metadata,
            "source": "ai-generated",
        },
        identity.user_id,
        stored.key,
    )
    project_asset = ensure_project_asset(
        session,
        project,
        source_type="artifact",
        source_id=artifact.id,
        artifact_id=artifact.id,
        role="music",
        label=payload.label,
        created_by=identity.user_id,
        metadata={
            "source": "ai-generated",
            "prompt": payload.prompt,
            "provider": generated.provider_id,
            "model": generated.model,
            "duration_seconds": generated.duration_seconds,
            "requested_duration_seconds": generated.requested_duration_seconds,
            "checksum_sha256": stored.checksum_sha256,
            "loop_recommended": bool(
                generated.duration_seconds is not None
                and generated.duration_seconds < payload.duration_seconds
            ),
        },
    )
    audit(
        session,
        identity.user_id,
        "video_project.music_generated",
        "project_asset",
        project_asset.id,
        project_id=project.id,
        provider=generated.provider_id,
        model=generated.model,
        requested_duration_seconds=payload.duration_seconds,
    )
    session.commit()
    return project_asset_json(session, project_asset)


@app.post(
    "/api/v1/video-projects/{project_id}/renders",
    tags=["video-projects"],
    response_model=MediaJobResponse,
)
def create_video_project_render(
    project_id: str,
    payload: VideoProjectRenderRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
    timeline_renderer=Depends(get_timeline_renderer),
) -> dict:
    project, task = get_video_project_or_404(session, project_id, identity)
    ensure_task_editable(task)
    if project.current_revision != payload.expected_revision:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {project.current_revision}，"
            f"请求基于版本 {payload.expected_revision}",
        )
    if not payload.profile.startswith("preview-"):
        _require_video_ai_brief(task, purpose="最终渲染前")
    job_payload = {
        "project_id": project.id,
        "revision_id": project.current_revision_id,
        "profile": payload.profile,
        "fps": payload.fps,
        "subtitle_mode": payload.subtitle_mode,
        "include_audio": payload.include_audio,
    }
    queued = enqueue_job(
        session,
        task_id=task.id,
        owner_id=identity.user_id,
        kind=RENDER_JOB_KIND,
        result_json=job_payload,
        idempotency_key=payload.idempotency_key,
        max_attempts=3,
    )
    if not queued.created:
        existing_payload = load(queued.job.result_json or "{}")
        if not isinstance(existing_payload, dict) or any(
            existing_payload.get(key) != value for key, value in job_payload.items()
        ):
            raise HTTPException(
                409,
                "该幂等键已经用于不同的视频渲染请求，请刷新后重试",
            )
    if queued.created:
        audit(
            session,
            identity.user_id,
            "video_project.render_queued",
            "job",
            queued.job.id,
            project_id=project.id,
            revision_id=project.current_revision_id,
            profile=payload.profile,
        )
    job_id = queued.job.id
    session.commit()
    if queued.created:
        dispatcher.submit(
            lambda current_id=job_id: run_video_project_render_job(
                current_id,
                SessionLocal,
                asset_store,
                timeline_renderer,
                settings,
            )
        )
    return job_json(queued.job)


@app.post(
    "/api/v1/video-projects/{project_id}/exports",
    tags=["video-projects"],
    response_model=MediaJobResponse,
)
def create_video_project_export(
    project_id: str,
    payload: VideoProjectExportRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
    timeline_renderer=Depends(get_timeline_renderer),
) -> dict:
    project, task = get_video_project_or_404(session, project_id, identity)
    ensure_task_editable(task)
    if project.current_revision != payload.expected_revision:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {project.current_revision}，"
            f"请求基于版本 {payload.expected_revision}",
        )
    _require_video_ai_brief(task, purpose="最终导出前")
    job_payload = {
        "project_id": project.id,
        "revision_id": project.current_revision_id,
        "deliverables": payload.deliverables,
        "profile": f"final-{payload.video_profile}",
        "fps": payload.fps,
        "subtitle_mode": payload.subtitle_mode,
        "subtitle_format": payload.subtitle_format,
        "audio_format": payload.audio_format,
        "clip_item_ids": payload.clip_item_ids,
        "project_track_kinds": payload.project_track_kinds or None,
        "project_asset_ids": payload.project_asset_ids,
        "include_original_assets": payload.include_original_assets,
    }
    queued = enqueue_job(
        session,
        task_id=task.id,
        owner_id=identity.user_id,
        kind=EXPORT_JOB_KIND,
        result_json=job_payload,
        idempotency_key=payload.idempotency_key,
        max_attempts=3,
    )
    if queued.created:
        audit(
            session,
            identity.user_id,
            "video_project.export_queued",
            "job",
            queued.job.id,
            project_id=project.id,
            revision_id=project.current_revision_id,
            deliverables=payload.deliverables,
        )
    job_id = queued.job.id
    session.commit()
    if queued.created:
        dispatcher.submit(
            lambda current_id=job_id: run_video_project_export_job(
                current_id,
                SessionLocal,
                asset_store,
                timeline_renderer,
                settings,
            )
        )
    return job_json(queued.job)


@app.get(
    "/api/v1/video-projects/{project_id}",
    tags=["video-projects"],
    response_model=VideoProjectResponse,
)
def get_video_project_endpoint(
    project_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    project, _ = get_video_project_or_404(session, project_id, identity, allow_admin_read=True)
    return video_project_json(session, project)


@app.get(
    "/api/v1/video-projects/{project_id}/revisions",
    tags=["video-projects"],
    response_model=list[VideoProjectRevisionResponse],
)
def list_video_project_revisions(
    project_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> list[dict]:
    project, _ = get_video_project_or_404(session, project_id, identity, allow_admin_read=True)
    revisions = session.scalars(
        select(VideoProjectRevision)
        .where(
            VideoProjectRevision.project_id == project.id,
            VideoProjectRevision.revision.is_not(None),
        )
        .order_by(VideoProjectRevision.revision.desc())
    ).all()
    return [video_project_revision_json(item) for item in revisions]


@app.get(
    "/api/v1/video-projects/{project_id}/jobs",
    tags=["video-projects"],
    response_model=list[MediaJobResponse],
)
def list_video_project_jobs(
    project_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> list[dict]:
    project, _ = get_video_project_or_404(session, project_id, identity, allow_admin_read=True)
    jobs = session.scalars(
        select(Job)
        .where(
            Job.task_id == project.task_id,
            Job.kind.in_(
                (
                    RENDER_JOB_KIND,
                    EXPORT_JOB_KIND,
                    VIDEO_PROJECT_AI_JOB_KIND,
                )
            ),
        )
        .order_by(Job.created_at.desc())
    ).all()
    # A task normally owns one editor project, but restored/migrated tasks may
    # contain historical jobs from another project.  Keep this endpoint truly
    # project-scoped so the client never resumes or displays the wrong job.
    scoped_jobs = []
    for item in jobs:
        payload = load(item.result_json)
        if isinstance(payload, dict) and str(payload.get("project_id") or "") == project.id:
            scoped_jobs.append(job_json(item))
    return scoped_jobs


@app.post(
    "/api/v1/video-projects/{project_id}/edits",
    tags=["video-projects"],
    response_model=VideoProjectResponse,
)
def edit_video_project_endpoint(
    project_id: str,
    payload: VideoProjectEditRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    project, task = get_video_project_or_404(session, project_id, identity)
    ensure_task_editable(task)
    if project.current_revision != payload.expected_revision:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {project.current_revision}，"
            f"请求基于版本 {payload.expected_revision}",
        )
    operations = _materialize_project_operations(
        session,
        project,
        identity,
        payload.operations,
    )
    try:
        revision = edit_video_project(
            session,
            project,
            expected_revision=payload.expected_revision,
            operations=operations,
            created_by=identity.user_id,
        )
    except (VideoProjectConflictError, VideoProjectValidationError) as exc:
        raise video_project_http_error(exc) from exc
    audit(
        session,
        identity.user_id,
        "video_project.edited",
        "video_project",
        project.id,
        revision=revision.revision,
        operation_ids=[item.get("op_id") for item in load(revision.operations_json)],
    )
    session.commit()
    return video_project_json(session, project)


@app.post(
    "/api/v1/video-projects/{project_id}/ai-edit-proposals",
    tags=["video-projects"],
    response_model=AiEditProposalResponse,
)
def create_ai_edit_proposal_endpoint(
    project_id: str,
    payload: AiEditProposalCreate,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    text_provider: TextProvider = Depends(get_text_provider),
    knowledge_provider: KnowledgeProvider = Depends(get_knowledge_provider),
) -> dict:
    project, task = get_video_project_or_404(session, project_id, identity)
    ensure_task_editable(task)
    if project.current_revision != payload.expected_revision:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {project.current_revision}，"
            f"请求基于版本 {payload.expected_revision}",
        )
    provider_result: TextGenerationResult | None = None
    requested_operations = payload.operations
    if not requested_operations:
        _require_video_ai_brief(task)
        revision = session.get(VideoProjectRevision, project.current_revision_id)
        if revision is None:
            raise HTTPException(409, "视频工程当前版本不存在，请重新载入")
        timeline = _timeline_for_ai(load(revision.timeline_json))
        item_ids = {
            item.get("id")
            for track in timeline["tracks"]
            for item in track["items"]
            if isinstance(item.get("id"), str)
        }
        if any(item_id not in item_ids for item_id in payload.target_item_ids):
            raise HTTPException(422, "AI 修改范围包含不存在的时间线片段")
        generation_input = prepare_content_generation(
            session=session,
            task=task,
            identity=identity,
            instruction=payload.prompt,
            knowledge_provider=knowledge_provider,
            text_provider=text_provider,
            source_content={"content_data_level": "internal"},
        )
        generation_input = replace(
            generation_input,
            source_content={
                "timeline": timeline,
                "target_item_ids": payload.target_item_ids,
                "content_data_level": "internal",
            },
        )
        provider_result = call_text_generation(
            lambda: text_provider.timeline_edits(generation_input)
        )
        requested_operations = _generated_video_project_operations(provider_result.content)
    operations = _materialize_project_operations(
        session,
        project,
        identity,
        requested_operations,
    )
    try:
        proposal = create_ai_edit_proposal(
            session,
            project,
            expected_revision=payload.expected_revision,
            prompt=payload.prompt,
            operations=operations,
            created_by=identity.user_id,
        )
    except (VideoProjectConflictError, VideoProjectValidationError) as exc:
        raise video_project_http_error(exc) from exc
    audit(
        session,
        identity.user_id,
        "video_project.ai_edit_proposed",
        "ai_edit_proposal",
        proposal.id,
        project_id=project.id,
        base_revision=payload.expected_revision,
        target_item_ids=payload.target_item_ids,
        provider=(provider_metadata(provider_result) if provider_result else None),
    )
    session.commit()
    return ai_edit_proposal_json(proposal)


@app.get(
    "/api/v1/ai-edit-proposals/{proposal_id}/compare",
    tags=["video-projects"],
    response_model=AiEditProposalCompareResponse,
)
def compare_ai_edit_proposal_endpoint(
    proposal_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    proposal, _, _ = get_ai_edit_proposal_or_404(session, proposal_id, identity)
    return ai_edit_compare_json(session, proposal)


@app.post(
    "/api/v1/ai-edit-proposals/{proposal_id}/adopt",
    tags=["video-projects"],
    response_model=AiEditProposalDecisionResponse,
)
def adopt_ai_edit_proposal_endpoint(
    proposal_id: str,
    payload: AiEditProposalAdoptRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    proposal, project, task = get_ai_edit_proposal_or_404(
        session,
        proposal_id,
        identity,
    )
    ensure_task_editable(task)
    try:
        revision = adopt_ai_edit_proposal(
            session,
            project,
            proposal,
            expected_revision=payload.expected_revision,
            selected_operation_ids=payload.selected_operation_ids,
            resolved_by=identity.user_id,
        )
    except (VideoProjectConflictError, VideoProjectValidationError) as exc:
        raise video_project_http_error(exc) from exc
    audit(
        session,
        identity.user_id,
        "video_project.ai_edit_adopted",
        "ai_edit_proposal",
        proposal.id,
        revision=revision.revision,
        selected_operation_ids=payload.selected_operation_ids,
    )
    session.commit()
    return {
        "proposal": ai_edit_proposal_json(proposal),
        "project": video_project_json(session, project),
    }


@app.post(
    "/api/v1/ai-edit-proposals/{proposal_id}/reject",
    tags=["video-projects"],
    response_model=AiEditProposalDecisionResponse,
)
def reject_ai_edit_proposal_endpoint(
    proposal_id: str,
    payload: AiEditProposalRejectRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    proposal, project, task = get_ai_edit_proposal_or_404(
        session,
        proposal_id,
        identity,
    )
    ensure_task_editable(task)
    try:
        reject_ai_edit_proposal(
            project,
            proposal,
            expected_revision=payload.expected_revision,
            resolved_by=identity.user_id,
        )
    except (VideoProjectConflictError, VideoProjectValidationError) as exc:
        raise video_project_http_error(exc) from exc
    audit(
        session,
        identity.user_id,
        "video_project.ai_edit_rejected",
        "ai_edit_proposal",
        proposal.id,
        revision=project.current_revision,
    )
    session.commit()
    return {
        "proposal": ai_edit_proposal_json(proposal),
        "project": video_project_json(session, project),
    }


@app.get(
    "/api/v1/tasks/{task_id}/workflow",
    tags=["workflow"],
    response_model=WorkflowResponse,
)
def get_task_workflow(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity, allow_admin_read=True)
    result = workflow_json(session, task)
    session.commit()
    return result


@app.post(
    "/api/v1/tasks/{target_task_id}/artifact-links",
    tags=["workflow"],
    response_model=ArtifactLinkResponse,
)
def link_artifacts_to_task(
    target_task_id: str,
    payload: ArtifactLinkCreate,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    target = get_task_or_404(session, target_task_id, identity)
    ensure_task_editable(target)
    if target.mode not in {"full", "video"}:
        raise HTTPException(422, "独立生成结果只能加入完整内容创作或快捷视频任务")
    if target.version != payload.expected_version:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {target.version}，请求基于版本 {payload.expected_version}",
        )
    source = get_task_or_404(session, payload.source_task_id, identity)
    if source.id == target.id:
        raise HTTPException(422, "来源任务和目标任务不能相同")
    artifacts = []
    for artifact_id in dict.fromkeys(payload.artifact_ids):
        artifact = session.get(Artifact, artifact_id)
        if artifact is None or artifact.task_id != source.id:
            raise HTTPException(422, "存在不属于来源任务的内容版本")
        if artifact.status != "confirmed" or load(artifact.content_json).get("legacy_mock"):
            raise HTTPException(409, "只有已确认的真实生成结果可以加入目标任务")
        stage_key = ARTIFACT_STAGE.get(artifact.kind)
        if stage_key is None or (
            stage_key not in operable_stages(target)
            and not (target.mode == "video" and artifact.kind == "copy")
        ):
            raise HTTPException(422, "存在不能加入目标任务的内容类型")
        artifacts.append(artifact)
    selectable_artifacts = [
        artifact
        for artifact in artifacts
        if ARTIFACT_STAGE.get(artifact.kind) in operable_stages(target)
    ]
    try:
        if selectable_artifacts:
            select_linked_artifacts(
                session,
                target,
                selectable_artifacts,
                user_id=identity.user_id,
            )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    links = []
    for artifact in artifacts:
        link = session.scalar(
            select(ArtifactLink).where(
                ArtifactLink.target_task_id == target.id,
                ArtifactLink.artifact_id == artifact.id,
            )
        )
        if link is None:
            link = ArtifactLink(
                target_task_id=target.id,
                source_task_id=source.id,
                artifact_id=artifact.id,
                stage_key=ARTIFACT_STAGE[artifact.kind],
                created_by=identity.user_id,
            )
            session.add(link)
            session.flush()
        links.append(link)
    if artifacts and not selectable_artifacts:
        target.version += 1
    audit(
        session,
        identity.user_id,
        "artifacts.linked",
        "content_task",
        target.id,
        source_task_id=source.id,
        artifact_ids=[item.id for item in artifacts],
    )
    session.commit()
    return {
        "links": [
            {
                "id": link.id,
                "target_task_id": link.target_task_id,
                "source_task_id": link.source_task_id,
                "artifact_id": link.artifact_id,
                "stage_key": link.stage_key,
                "created_by": link.created_by,
                "created_at": link.created_at,
            }
            for link in links
        ],
        "workflow": workflow_json(session, target),
    }


@app.patch("/api/v1/tasks/{task_id}/stages/{stage_key}/requirements", tags=["workflow"])
def set_stage_requirements(
    task_id: str,
    stage_key: str,
    payload: StageRequirementsUpdate,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    try:
        stage = update_requirements(
            session,
            task,
            stage_key,
            payload.requirements,
            payload.expected_version,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    audit(
        session,
        identity.user_id,
        "stage.requirements_updated",
        "task_stage",
        stage.id,
        stage_key=stage_key,
        version=stage.version,
    )
    session.commit()
    return stage_json(task, stage)


@app.put("/api/v1/tasks/{task_id}/stages/{stage_key}/selection", tags=["workflow"])
def select_stage_candidates(
    task_id: str,
    stage_key: str,
    payload: StageSelectRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    artifacts = []
    for artifact_id in dict.fromkeys(payload.artifact_ids):
        artifact, _ = get_artifact_for_user(session, artifact_id, identity)
        if not artifact_belongs_to_or_is_linked(
            session,
            task,
            artifact,
            stage_key,
        ):
            raise HTTPException(422, "候选内容不属于当前任务")
        if stage_key == "copy" and artifact.kind == "copy":
            require_current_full_copy_lineage(session, task, artifact)
        artifacts.append(artifact)
    try:
        stage = select_artifacts(
            session,
            task,
            stage_key,
            artifacts,
            payload.expected_version,
            identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    audit(
        session,
        identity.user_id,
        "stage.candidates_selected",
        "task_stage",
        stage.id,
        stage_key=stage_key,
        artifact_ids=payload.artifact_ids,
    )
    session.commit()
    return stage_json(task, stage)


@app.post("/api/v1/tasks/{task_id}/stages/{stage_key}/confirm", tags=["workflow"])
def confirm_task_stage(
    task_id: str,
    stage_key: str,
    payload: StageConfirmRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    if stage_key == "export" and (task.archived_at is not None or task.status == "archived"):
        lifecycle = edit_draft_fields(session, task.id)
        if lifecycle["source_task_id"] and not lifecycle["is_edit_draft"]:
            stage = get_stage(session, task, "export")
            if stage.state == "confirmed":
                # A retry after a successful promotion returns the same result;
                # it never creates another task or rewrites the frozen archive.
                return stage_json(task, stage)
    ensure_task_editable(task)
    if stage_key == "export":
        ensure_task_jobs_finished(session, task)
    if stage_key == "export" and task.mode in {"full", "video"}:
        current_confirmed_final_video(session, task)
    artifacts = None
    if payload.artifact_ids:
        artifacts = []
        for artifact_id in dict.fromkeys(payload.artifact_ids):
            artifact, _ = get_artifact_for_user(session, artifact_id, identity)
            if not artifact_belongs_to_or_is_linked(
                session,
                task,
                artifact,
                stage_key,
            ):
                raise HTTPException(422, "候选内容不属于当前任务")
            if stage_key == "copy" and artifact.kind == "copy":
                require_current_full_copy_lineage(session, task, artifact)
            artifacts.append(artifact)
    if stage_key == "video_preview" and task.mode in {"full", "video"}:
        require_current_video_project_render(session, task, artifacts)
    try:
        stage = confirm_stage(
            session,
            task,
            stage_key,
            payload.expected_version,
            identity.user_id,
            artifacts,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    if stage_key == "export" and stage.state == "confirmed":
        # Confirming the final stage creates the immutable archived snapshot.
        # Keep the legacy task status value for API compatibility; archive state
        # is represented by archived_at/archived_workflow_status.
        task.archived_workflow_status = task.status if task.status != "archived" else "confirmed"
        task.archived_at = datetime.now(UTC)
        if task.mode == "full":
            final_video = current_confirmed_final_video(session, task)
            final_content = load(final_video.content_json)
            # Freeze the complete deliverable set independently of generation
            # readiness. Later configuration edits must not erase this package.
            if final_video.task_id == task.id:
                final_content["deliverable_artifact_ids"] = [
                    item.id for item in resolve_task_export_artifacts(session, task)
                ]
                final_content["deliverable_task_id"] = task.id
                final_content["deliverable_selection_archived_at"] = task.archived_at.isoformat()
                final_content["deliverable_export_stage_version"] = stage.version
                final_video.content_json = dump(final_content)
        promote_edit_draft(session, task, identity.user_id)
    audit(
        session,
        identity.user_id,
        "stage.confirmed",
        "task_stage",
        stage.id,
        stage_key=stage_key,
    )
    session.commit()
    return stage_json(task, stage)


@app.post("/api/v1/tasks/{task_id}/stages/{stage_key}/skip", tags=["workflow"])
def skip_task_stage(
    task_id: str,
    stage_key: str,
    payload: StageSkipRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    try:
        stage = skip_stage(
            session,
            task,
            stage_key,
            payload.expected_version,
            payload.reason,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    audit(
        session,
        identity.user_id,
        "stage.skipped",
        "task_stage",
        stage.id,
        stage_key=stage_key,
        reason=stage.skipped_reason,
    )
    session.commit()
    return stage_json(task, stage)


@app.get(
    "/api/v1/tasks/{task_id}/generation-runs",
    tags=["workflow"],
    response_model=list[GenerationRunResponse],
)
def list_generation_runs(
    task_id: str,
    stage_key: str | None = None,
    status: str | None = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> list[dict]:
    get_task_or_404(session, task_id, identity, allow_admin_read=True)
    statement = select(GenerationRun).where(GenerationRun.task_id == task_id)
    if stage_key:
        statement = statement.where(GenerationRun.stage_key == stage_key)
    if status:
        statement = statement.where(GenerationRun.status == status)
    runs = session.scalars(
        statement.order_by(GenerationRun.created_at.desc(), GenerationRun.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return [generation_run_json(session, run, artifact_json) for run in runs]


@app.get(
    "/api/v1/generation-runs/{run_id}",
    tags=["workflow"],
    response_model=GenerationRunResponse,
)
def get_generation_run(
    run_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    run = session.get(GenerationRun, run_id)
    if run is None:
        raise HTTPException(404, "生成批次不存在")
    get_task_or_404(session, run.task_id, identity, allow_admin_read=True)
    return generation_run_json(session, run, artifact_json)


@app.put("/api/v1/tasks/{task_id}/access", tags=["tasks"])
def update_task_access(
    task_id: str,
    payload: dict = Body(...),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    if task.owner_id != identity.user_id:
        raise HTTPException(403, "仅创建者可调整协作权限")
    task.allowed_users_json = dump(payload.get("user_ids", []))
    task.allowed_departments_json = dump(payload.get("departments", []))
    session.commit()
    return {
        "user_ids": load(task.allowed_users_json),
        "departments": load(task.allowed_departments_json),
    }


@app.post("/api/v1/tasks/{task_id}/egress-grants", tags=["security"])
def approve_egress(
    task_id: str,
    payload: EgressGrantCreate,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    text_provider: TextProvider = Depends(get_text_provider),
    image_provider: ImageGenerationPort = Depends(get_image_generation_port),
    music_provider: MusicGenerationPort = Depends(get_music_generation_port),
    ai_video_provider: AiVideoGenerationPort = Depends(get_ai_video_generation_port),
    speech_provider: SpeechSynthesisPort = Depends(get_speech_synthesis_port),
) -> dict:
    get_task_or_404(session, task_id, identity)
    if not identity.has_role("admin"):
        raise HTTPException(403, "仅管理员可批准资料外发")
    configured_provider_ids = {
        text_provider.status.provider_id,
        image_provider.status.provider_id,
        music_provider.status.provider_id,
        ai_video_provider.status.provider_id,
        speech_provider.status.provider_id,
    }
    if payload.provider not in configured_provider_ids:
        raise HTTPException(
            422,
            "外发授权的 Provider 与当前内容、图片、配音、配乐或 AI 视频服务不一致",
        )
    grant = EgressGrant(
        task_id=task_id,
        provider=payload.provider,
        allowed_data_levels_json=dump(
            {
                "data_levels": payload.allowed_data_levels,
                "source_data_levels": payload.allowed_source_data_levels,
                "allow_task_context": payload.allow_task_context,
                "allow_compliance_terms": payload.allow_compliance_terms,
            }
        ),
        approved_by=identity.user_id,
    )
    session.add(grant)
    session.flush()
    audit(
        session,
        identity.user_id,
        "egress.approved",
        "content_task",
        task_id,
        provider=payload.provider,
        allowed_data_levels=payload.allowed_data_levels,
        allowed_source_data_levels=payload.allowed_source_data_levels,
        allow_task_context=payload.allow_task_context,
        allow_compliance_terms=payload.allow_compliance_terms,
    )
    session.commit()
    return {
        "id": grant.id,
        "provider": grant.provider,
        "allowed_data_levels": payload.allowed_data_levels,
        "allowed_source_data_levels": payload.allowed_source_data_levels,
        "allow_task_context": payload.allow_task_context,
        "allow_compliance_terms": payload.allow_compliance_terms,
    }


@app.get(
    "/api/v1/tasks/{task_id}/egress-status",
    tags=["security"],
    response_model=EgressStatusResponse,
)
def get_egress_status(
    task_id: str,
    provider: str | None = None,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    text_provider: TextProvider = Depends(get_text_provider),
    image_provider: ImageGenerationPort = Depends(get_image_generation_port),
    music_provider: MusicGenerationPort = Depends(get_music_generation_port),
    ai_video_provider: AiVideoGenerationPort = Depends(get_ai_video_generation_port),
    speech_provider: SpeechSynthesisPort = Depends(get_speech_synthesis_port),
) -> dict:
    get_task_or_404(session, task_id, identity, allow_admin_read=True)
    ports = {
        text_provider.status.provider_id: text_provider,
        image_provider.status.provider_id: image_provider,
        music_provider.status.provider_id: music_provider,
        ai_video_provider.status.provider_id: ai_video_provider,
        speech_provider.status.provider_id: speech_provider,
    }
    selected_provider_id = provider or text_provider.status.provider_id
    selected_port = ports.get(selected_provider_id)
    if selected_port is None:
        raise HTTPException(422, "所查 Provider 不是当前内容、图片、配音、配乐或 AI 视频服务")
    status = selected_port.status
    if not status.external:
        return {
            "provider_id": status.provider_id,
            "external": False,
            "required": False,
            "granted": True,
            "allowed_data_levels": ["public", "internal", "restricted"],
            "allowed_source_data_levels": ["public", "internal", "restricted"],
            "allow_task_context": True,
            "allow_compliance_terms": True,
            "can_approve": False,
        }

    return {
        "provider_id": status.provider_id,
        "external": True,
        "required": False,
        "granted": True,
        "allowed_data_levels": ["public", "internal"],
        "allowed_source_data_levels": ["public", "internal"],
        "allow_task_context": True,
        "allow_compliance_terms": True,
        "can_approve": False,
    }


@app.post("/api/v1/tasks/{task_id}/generate-topics", tags=["generation"])
def generate_topics(
    task_id: str,
    payload: GenerateRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    text_provider: TextProvider = Depends(get_text_provider),
    knowledge_provider: KnowledgeProvider = Depends(get_knowledge_provider),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    try:
        run, replayed = begin_generation_run(
            session,
            task,
            "topic",
            count=payload.count,
            instruction=payload.instruction,
            parameters=payload.parameters,
            idempotency_key=payload.idempotency_key,
            user_id=identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    if replayed:
        if run.status != "succeeded":
            raise HTTPException(409, f"相同幂等键的生成批次状态为 {run.status}")
        artifacts = run_artifacts(session, run)
        return legacy_candidate_run_response(session, run, artifacts, "candidates")
    generation_input = prepare_content_generation(
        session=session,
        task=task,
        identity=identity,
        instruction=payload.instruction,
        knowledge_provider=knowledge_provider,
        text_provider=text_provider,
        knowledge_resource_ids=generation_knowledge_resource_ids(payload.parameters),
    )
    # SQLite has one writer: commit the started run before the slow model call so
    # other users' saves are not blocked while this request waits for the model.
    session.commit()
    try:
        result = call_text_generation(
            lambda: text_provider.topics(generation_input, payload.count),
            clear_generation_error=True,
        )
    except HTTPException as exc:
        fail_generation_run(session, task, run, _generation_error_message(exc))
        session.commit()
        raise
    except Exception:
        session.rollback()
        fail_generation_run(session, task, run, "生成未完成，请重试")
        session.commit()
        raise
    reference_ids = used_reference_ids(
        result.content,
        generation_input.knowledge,
        fallback_to_retrieved=True,
    )
    references = reference_metadata(generation_input.knowledge, reference_ids)
    artifacts = []
    for index, candidate in enumerate(result.content):
        artifact = create_artifact(
            session,
            task,
            "topic",
            {
                "candidates": [candidate],
                "instruction": payload.instruction,
                "provider": result.provider_id,
                "generation": provider_metadata(result),
                "reference_item_ids": reference_ids,
                "reference_mode": "retrieved_context",
                "references": references,
                "content_data_level": draft_content_data_level(references),
            },
            identity.user_id,
        )
        attach_artifact(artifact, run, "topic", index)
        artifacts.append(artifact)
    complete_generation_run(
        session,
        task,
        run,
        artifacts,
        provider_id=result.provider_id,
        model=result.model,
    )
    audit(
        session,
        identity.user_id,
        "topics.generated",
        "artifact",
        run.id,
        provider_id=result.provider_id,
        model=result.model,
        reference_item_ids=reference_ids,
    )
    session.commit()
    return legacy_candidate_run_response(session, run, artifacts, "candidates")


@app.post("/api/v1/tasks/{task_id}/generate-copy", tags=["generation"])
def generate_copy(
    task_id: str,
    payload: GenerateRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    text_provider: TextProvider = Depends(get_text_provider),
    knowledge_provider: KnowledgeProvider = Depends(get_knowledge_provider),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    source_content = None
    title_context = confirmed_full_workflow_title(session, task)
    if title_context is not None:
        source_title_artifact, source_content, source_title = title_context
    else:
        source_title_artifact = None
        source_title = ""
    if source_title_artifact is None and task.mode != "full" and "topic" in applicable_stages(task):
        topic_stage = get_stage(session, task, "topic")
        selected_topics = load(topic_stage.confirmed_artifact_ids_json).get("topic", [])
        if not selected_topics:
            selected_topics = load(topic_stage.selected_artifact_ids_json).get("topic", [])
        if selected_topics:
            selected_topic = session.get(Artifact, selected_topics[0])
            if selected_topic is not None:
                source_content = load(selected_topic.content_json)
    run_parameters = dict(payload.parameters)
    if source_title_artifact is not None:
        run_parameters.update(
            {
                "source_title_artifact_id": source_title_artifact.id,
                "source_title_version": source_title_artifact.version,
            }
        )
    try:
        run, replayed = begin_generation_run(
            session,
            task,
            "copy",
            count=payload.count,
            instruction=payload.instruction,
            parameters=run_parameters,
            idempotency_key=payload.idempotency_key,
            user_id=identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    if replayed:
        if (
            run.requested_count != payload.count
            or run.instruction != payload.instruction
            or load(run.parameters_json or "{}") != run_parameters
        ):
            raise HTTPException(409, "相同幂等键已经用于不同的文案生成请求")
        if run.status != "succeeded":
            raise HTTPException(409, f"相同幂等键的生成批次状态为 {run.status}")
        artifacts = run_artifacts(session, run)
        return legacy_candidate_run_response(session, run, artifacts, "candidates")
    generation_input = prepare_content_generation(
        session=session,
        task=task,
        identity=identity,
        instruction=payload.instruction,
        knowledge_provider=knowledge_provider,
        text_provider=text_provider,
        knowledge_resource_ids=generation_knowledge_resource_ids(payload.parameters),
        source_content=source_content,
    )
    # SQLite has one writer: commit the started run before the slow model call so
    # other users' saves are not blocked while this request waits for the model.
    session.commit()
    try:
        result = call_text_generation(
            lambda: text_provider.copies(generation_input, payload.count),
            clear_generation_error=True,
        )
    except HTTPException as exc:
        fail_generation_run(session, task, run, _generation_error_message(exc))
        session.commit()
        raise
    except Exception:
        session.rollback()
        fail_generation_run(session, task, run, "生成未完成，请重试")
        session.commit()
        raise
    if source_title_artifact is not None:
        canonical_candidates = []
        for candidate in result.content:
            if not isinstance(candidate, dict):
                fail_generation_run(session, task, run, "内容模型返回的文案格式无效")
                session.commit()
                raise HTTPException(502, "内容模型返回的文案格式无效")
            canonical_candidate = dict(candidate)
            canonical_candidate["title"] = source_title
            canonical_candidates.append(canonical_candidate)
        result = replace(result, content=canonical_candidates)
    reference_ids = used_reference_ids(result.content, generation_input.knowledge)
    references = reference_metadata(generation_input.knowledge, reference_ids)
    compliance = compliance_result(session, result.content)
    artifacts = []
    for index, candidate in enumerate(result.content):
        artifact = create_artifact(
            session,
            task,
            "copy",
            {
                "candidates": [candidate],
                "instruction": payload.instruction,
                "provider": result.provider_id,
                "generation": provider_metadata(result),
                "reference_item_ids": reference_ids,
                "reference_mode": (
                    "provider_declared" if text_provider.status.external else "retrieved_context"
                ),
                "references": references,
                "content_data_level": draft_content_data_level(references),
                "compliance": compliance,
                **(
                    {
                        "source_title_artifact_id": source_title_artifact.id,
                        "source_title_version": source_title_artifact.version,
                        "source_title": source_title,
                    }
                    if source_title_artifact is not None
                    else {}
                ),
            },
            identity.user_id,
        )
        if source_title_artifact is not None:
            artifact.parent_id = source_title_artifact.id
        attach_artifact(artifact, run, "copy", index)
        artifacts.append(artifact)
    complete_generation_run(
        session,
        task,
        run,
        artifacts,
        provider_id=result.provider_id,
        model=result.model,
    )
    audit(
        session,
        identity.user_id,
        "copy.generated",
        "artifact",
        run.id,
        provider_id=result.provider_id,
        model=result.model,
        reference_item_ids=reference_ids,
        compliance_passed=compliance["passed"],
        source_title_artifact_id=(
            source_title_artifact.id if source_title_artifact is not None else None
        ),
    )
    session.commit()
    return legacy_candidate_run_response(session, run, artifacts, "candidates")


@app.post(
    "/api/v1/tasks/{task_id}/stages/copy/import",
    tags=["workflow"],
    response_model=ArtifactResponse,
)
def import_existing_copy(
    task_id: str,
    payload: CopyImportRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    """Save user-provided copy without forcing an AI generation call."""
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    if "copy" not in applicable_stages(task):
        raise HTTPException(422, "当前任务入口不包含文案阶段")
    stage = get_stage(session, task, "copy")
    if stage.version != payload.expected_version:
        raise HTTPException(
            409,
            f"版本冲突：当前阶段版本为 {stage.version}，请求基于版本 {payload.expected_version}",
        )
    text = payload.text.strip()
    if not text:
        raise HTTPException(422, "已有文案不能为空")
    title_context = confirmed_full_workflow_title(session, task)
    if title_context is not None:
        source_title_artifact, _source_title_content, copy_title = title_context
    else:
        source_title_artifact = None
        copy_title = task.name
    run_parameters = {"source": "human_copy"}
    if source_title_artifact is not None:
        run_parameters.update(
            {
                "source_title_artifact_id": source_title_artifact.id,
                "source_title_version": source_title_artifact.version,
            }
        )
    try:
        run, _ = begin_generation_run(
            session,
            task,
            "copy",
            count=1,
            instruction="人工输入已有文案",
            parameters=run_parameters,
            idempotency_key=None,
            user_id=identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    candidate = {
        "title": copy_title,
        "body": text,
        "source_ids": [],
    }
    content = {
        "candidates": [candidate],
        "selected_candidate": candidate,
        "source": "human_copy",
        "content_data_level": "internal",
        **(
            {
                "source_title_artifact_id": source_title_artifact.id,
                "source_title_version": source_title_artifact.version,
                "source_title": copy_title,
            }
            if source_title_artifact is not None
            else {}
        ),
    }
    content["compliance"] = compliance_result(session, content)
    artifact = create_artifact(
        session,
        task,
        "copy",
        content,
        identity.user_id,
    )
    if source_title_artifact is not None:
        artifact.parent_id = source_title_artifact.id
    attach_artifact(artifact, run, "copy", 0)
    complete_generation_run(
        session,
        task,
        run,
        [artifact],
        provider_id="human-input",
    )
    audit(
        session,
        identity.user_id,
        "copy.imported",
        "artifact",
        artifact.id,
    )
    session.commit()
    return artifact_json(artifact)


def transform_preview_source_content(source_content: dict, draft_content: dict) -> dict:
    """Use unsaved text without trusting client-supplied provenance or data levels."""
    draft_candidate = selected_text_candidate(draft_content)
    if not any(isinstance(draft_candidate.get(key), str) for key in ("title", "body")):
        raise HTTPException(422, "预览草稿必须包含有效的标题或正文，请检查内容后重试。")
    candidate = artifact_content("copy", dict(selected_text_candidate(source_content)))
    for key in ("title", "body"):
        if key in draft_candidate:
            if not isinstance(draft_candidate[key], str):
                raise HTTPException(422, "预览草稿的标题和正文必须是文字。")
            candidate[key] = draft_candidate[key]
    allowed_ids = {
        value for value in source_content.get("reference_item_ids", []) if isinstance(value, str)
    }
    candidate["source_ids"] = (
        [
            value
            for value in draft_candidate.get("source_ids", [])
            if isinstance(value, str) and value in allowed_ids
        ]
        if isinstance(draft_candidate.get("source_ids"), list)
        else []
    )
    return {
        **source_content,
        "candidates": [candidate],
        "selected_candidate": candidate,
        "selected_candidate_index": 0,
    }


@app.post("/api/v1/artifacts/{artifact_id}/transform-copy", tags=["generation"])
def transform_copy(
    artifact_id: str,
    payload: TransformCopyRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    text_provider: TextProvider = Depends(get_text_provider),
    knowledge_provider: KnowledgeProvider = Depends(get_knowledge_provider),
) -> dict:
    source, source_task = get_artifact_for_user(session, artifact_id, identity)
    if source.kind not in {"topic", "copy"}:
        raise HTTPException(422, "仅标题和文案内容支持 AI 调整")
    stage_key = source.kind
    task = source_task
    if payload.target_task_id and payload.target_task_id != source_task.id:
        target = get_task_or_404(session, payload.target_task_id, identity)
        if not artifact_belongs_to_or_is_linked(
            session,
            target,
            source,
            stage_key,
        ):
            raise HTTPException(422, "目标任务没有引用该文案版本")
        if stage_key not in operable_stages(target):
            raise HTTPException(422, "目标任务不包含可调整的内容阶段")
        task = target
    ensure_task_editable(task)
    if payload.expected_version is not None and source.version != payload.expected_version:
        raise HTTPException(409, "原内容版本已变化，请重新打开内容后再进行 AI 预览。")
    source_content = load(source.content_json)
    if payload.draft_content is not None:
        source_content = transform_preview_source_content(source_content, payload.draft_content)
    title_context = (
        require_current_full_copy_lineage(session, task, source, read_only=payload.preview)
        if source.kind == "copy"
        else None
    )
    if title_context is not None:
        source_title_artifact, _source_title_content, source_title = title_context
        source_content = copy_content_with_title(source_content, source_title)
    else:
        source_title_artifact = None
        source_title = ""
    run_parameters = {"operation": payload.operation, "source_artifact_id": source.id}
    if source_title_artifact is not None:
        run_parameters.update(
            {
                "source_title_artifact_id": source_title_artifact.id,
                "source_title_version": source_title_artifact.version,
            }
        )
    run = None
    if not payload.preview:
        try:
            run, replayed = begin_generation_run(
                session,
                task,
                stage_key,
                count=1,
                instruction=payload.instruction,
                parameters=run_parameters,
                idempotency_key=payload.idempotency_key,
                user_id=identity.user_id,
            )
        except (WorkflowConflictError, WorkflowValidationError) as exc:
            raise workflow_http_error(exc) from exc
        if replayed:
            if (
                run.requested_count != 1
                or run.instruction != payload.instruction
                or load(run.parameters_json or "{}") != run_parameters
            ):
                raise HTTPException(409, "相同幂等键已经用于不同的文案修改请求")
            artifacts = run_artifacts(session, run)
            if run.status == "succeeded" and artifacts:
                return artifact_json(artifacts[0])
            raise HTTPException(409, f"相同幂等键的生成批次状态为 {run.status}")
    generation_input = prepare_content_generation(
        session=session,
        task=task,
        identity=identity,
        instruction=payload.instruction,
        knowledge_provider=knowledge_provider,
        text_provider=text_provider,
        source_content=source_content,
    )

    def generate_changed_copy() -> TextGenerationResult:
        generated = text_provider.transform_copy(generation_input, payload.operation)
        generated_content = generated.content
        validation_instruction = payload.instruction
        if source_title_artifact is not None:
            if not isinstance(generated_content, dict):
                raise ProviderCallError(
                    "模型返回的文案格式无效",
                    code="provider_transform_invalid",
                    retryable=True,
                )
            generated_content = dict(generated_content)
            generated_content["title"] = source_title
            generated = replace(generated, content=generated_content)
            # A full-workflow title is owned by step 2.  AI modification of
            # step 3 must materially change the body without renaming it.
            validation_instruction = "标题保持不变"
        unchanged_fields = unchanged_transform_fields(
            generation_input.source_content,
            generated_content if isinstance(generated_content, dict) else None,
            instruction=validation_instruction,
        )
        if unchanged_fields:
            field_labels = {"title": "标题", "body": "正文"}
            labels = "、".join(
                field_labels.get(field_name, field_name) for field_name in unchanged_fields
            )
            raise ProviderCallError(
                f"模型返回的{labels}与原内容相同，请调整指令后重试",
                code="provider_transform_no_effect",
                retryable=True,
            )
        return generated

    # SQLite has one writer: commit the started run before the slow model call so
    # other users' saves are not blocked while this request waits for the model.
    session.commit()
    try:
        result = call_text_generation(generate_changed_copy, clear_generation_error=True)
    except HTTPException as exc:
        if run is not None:
            fail_generation_run(session, task, run, _generation_error_message(exc))
            session.commit()
        raise
    except Exception:
        session.rollback()
        if run is not None:
            fail_generation_run(session, task, run, "生成未完成，请重试")
            session.commit()
        raise
    reference_ids = used_reference_ids(
        result.content,
        generation_input.knowledge,
        source_content=source_content,
    )
    references = reference_metadata(
        generation_input.knowledge,
        reference_ids,
        source_content,
    )
    compliance = compliance_result(session, result.content)
    content = {
        "candidates": [result.content],
        "transformation": payload.operation,
        "instruction": payload.instruction,
        "provider": result.provider_id,
        "generation": provider_metadata(result),
        "reference_item_ids": reference_ids,
        "reference_mode": (
            "provider_declared" if text_provider.status.external else "retrieved_context"
        ),
        "references": references,
        "content_data_level": draft_content_data_level(references, source_content),
        "compliance": compliance,
        **(
            {
                "source_title_artifact_id": source_title_artifact.id,
                "source_title_version": source_title_artifact.version,
                "source_title": source_title,
            }
            if source_title_artifact is not None
            else {}
        ),
    }
    if payload.preview:
        audit(
            session,
            identity.user_id,
            "copy.transform_previewed",
            "artifact",
            source.id,
            provider_id=result.provider_id,
            model=result.model,
            operation=payload.operation,
        )
        session.commit()
        return {"content": content}
    assert run is not None
    item = create_artifact(session, task, source.kind, content, identity.user_id)
    item.parent_id = source.id
    attach_artifact(item, run, stage_key, 0)
    complete_generation_run(
        session,
        task,
        run,
        [item],
        provider_id=result.provider_id,
        model=result.model,
    )
    audit(
        session,
        identity.user_id,
        "copy.transformed",
        "artifact",
        item.id,
        source_id=source.id,
        provider_id=result.provider_id,
        model=result.model,
        reference_item_ids=reference_ids,
        compliance_passed=compliance["passed"],
        source_title_artifact_id=(
            source_title_artifact.id if source_title_artifact is not None else None
        ),
    )
    session.commit()
    return artifact_json(item)


@app.post("/api/v1/artifacts/{artifact_id}/compliance-check", tags=["compliance"])
def compliance_check(
    artifact_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    artifact, task = get_artifact_for_user(session, artifact_id, identity)
    if artifact.kind == "copy":
        require_current_full_copy_lineage(session, task, artifact)
    result = compliance_result(session, load(artifact.content_json))
    return {"artifact_id": artifact.id, **result}


@app.post("/api/v1/tasks/{task_id}/generate-images", tags=["generation"])
def generate_images(
    task_id: str,
    payload: GenerateRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    image_provider: ImageGenerationPort = Depends(get_image_generation_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> list[dict]:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)

    def task_image_reference(reference_id: str) -> Artifact:
        artifact, _ = get_artifact_for_user(session, reference_id, identity)
        if artifact.kind != "image":
            raise HTTPException(422, "仅图片版本支持作为生成参考图")
        if not artifact_belongs_to_or_is_linked(session, task, artifact, "image"):
            raise HTTPException(422, "图片参考版本必须属于当前任务或已正式回引到当前任务")
        return artifact

    source_artifact: Artifact | None = None
    if payload.source_artifact_id:
        source_artifact = task_image_reference(payload.source_artifact_id)

    raw_reference_artifact_ids = payload.parameters.get("reference_artifact_ids", [])
    if not isinstance(raw_reference_artifact_ids, (list, tuple)):
        raise HTTPException(422, "reference_artifact_ids 必须是图片版本 ID 列表")
    reference_artifacts = [
        task_image_reference(str(reference_id)) for reference_id in raw_reference_artifact_ids
    ]
    reference_artifact_ids = [artifact.id for artifact in reference_artifacts]

    run_parameters = dict(payload.parameters)
    if source_artifact is not None:
        run_parameters["source_artifact_id"] = source_artifact.id
    effective_idempotency_key = request_scoped_idempotency_key(
        payload.idempotency_key,
        namespace="task-image-generation",
        request_scope={
            "count": payload.count,
            "instruction": payload.instruction.strip(),
            "parameters": run_parameters,
            "product_resource_id": task.product_resource_id,
            "knowledge_resource_ids": load(task.knowledge_resource_ids_json),
        },
    )
    image_egress_levels = _require_external_provider_egress(
        session,
        task,
        image_provider.status.provider_id,
        external=image_provider.status.external,
    )
    try:
        run, replayed = begin_generation_run(
            session,
            task,
            "image",
            count=payload.count,
            instruction=payload.instruction,
            parameters=run_parameters,
            idempotency_key=effective_idempotency_key,
            user_id=identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    if replayed:
        if run.status != "succeeded":
            raise HTTPException(409, f"相同幂等键的生成批次状态为 {run.status}")
        return [artifact_json(item) for item in run_artifacts(session, run)]
    requested_knowledge_ids = generation_knowledge_resource_ids(payload.parameters)
    resolved_knowledge_ids, allow_cross_owner_knowledge = resolve_generation_knowledge_resources(
        session,
        task,
        requested_knowledge_ids,
        identity,
    )
    if resolved_knowledge_ids is None:
        stored_knowledge_ids = load(task.knowledge_resource_ids_json)
        requested_knowledge_ids = (
            stored_knowledge_ids if isinstance(stored_knowledge_ids, list) else []
        )
        knowledge_resource_ids = get_knowledge_resources_for_identity(
            session,
            requested_knowledge_ids,
            identity,
            allow_cross_owner=True,
        )
    else:
        knowledge_resource_ids = resolved_knowledge_ids
    if source_artifact is not None or reference_artifacts:
        stored_task_knowledge = load(task.knowledge_resource_ids_json)
        stored_task_knowledge_values = (
            stored_task_knowledge if isinstance(stored_task_knowledge, list) else []
        )
        stored_task_knowledge_ids = {
            str(item) for item in stored_task_knowledge_values if isinstance(item, str)
        }
        inherited_knowledge_ids: list[str] = []
        for artifact in [
            *([source_artifact] if source_artifact is not None else []),
            *reference_artifacts,
        ]:
            artifact_content = load(artifact.content_json)
            artifact_knowledge_ids = artifact_content.get("knowledge_resource_ids", [])
            if not isinstance(artifact_knowledge_ids, list):
                continue
            inherited_knowledge_ids.extend(
                str(item)
                for item in artifact_knowledge_ids
                if isinstance(item, str) and item in stored_task_knowledge_ids
            )
        if task.product_resource_id:
            # Older tasks/revisions may not have retained knowledge IDs.  Recheck
            # the current product against the caller before using it as the
            # authoritative brand identity for a refinement.
            current_product = get_product_resource_for_identity(
                session,
                task.product_resource_id,
                identity,
            )
            inherited_knowledge_ids.append(current_product.id)
        if inherited_knowledge_ids:
            inherited_knowledge_ids = get_knowledge_resources_for_identity(
                session,
                list(dict.fromkeys(inherited_knowledge_ids)),
                identity,
                allow_cross_owner=True,
            )
            knowledge_resource_ids = list(
                dict.fromkeys([*knowledge_resource_ids, *inherited_knowledge_ids])
            )
            allow_cross_owner_knowledge = True
    knowledge_query_text = " ".join(
        value
        for value in (
            task.name,
            task.topic,
            task.product,
            task.style,
            str(payload.parameters.get("prompt") or ""),
            payload.instruction,
        )
        if value
    )
    try:
        selected_knowledge = selected_knowledge_chunks(
            session,
            knowledge_resource_ids,
            requester_id=identity.user_id,
            allowed_levels=image_egress_levels,
            query_text=knowledge_query_text,
            allow_cross_owner=allow_cross_owner_knowledge,
        )
    except EgressDeniedError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KnowledgeSelectionError as exc:
        raise HTTPException(422, str(exc)) from exc
    reference_images = []
    reference_media_types = []
    reference_ids: list[str] = []
    if source_artifact is not None:
        reference_ids.append(source_artifact.id)
    reference_ids.extend(reference_artifact_ids)
    reference_ids.extend(payload.parameters.get("reference_asset_ids", []))
    _require_external_media_references(
        session,
        [str(item) for item in reference_ids],
        external=image_provider.status.external,
    )
    seen_reference_storage_keys: dict[str, int] = {}
    seen_reference_checksums: dict[str, int] = {}
    product_reference_indices: list[int] = []
    source_reference_index: int | None = None

    def append_reference_image(storage_key: str, media_type: str) -> int:
        content = asset_store.read_bytes(storage_key)
        checksum = hashlib.sha256(content).hexdigest()
        if storage_key in seen_reference_storage_keys:
            return seen_reference_storage_keys[storage_key]
        if checksum in seen_reference_checksums:
            return seen_reference_checksums[checksum]
        reference_images.append(content)
        reference_media_types.append(media_type)
        if len(reference_images) > 9:
            raise HTTPException(422, "参考图片与所选知识库视觉资料合计最多 9 张")
        reference_index = len(reference_images)
        seen_reference_storage_keys[storage_key] = reference_index
        seen_reference_checksums[checksum] = reference_index
        return reference_index

    product_knowledge_ids = [
        reference_id
        for reference_id in knowledge_resource_ids
        if (
            (entry := session.get(ResourceEntry, reference_id)) is not None
            and is_product_resource(entry)
        )
    ]
    product_knowledge_id_set = set(product_knowledge_ids)
    other_knowledge_ids = [
        reference_id
        for reference_id in knowledge_resource_ids
        if reference_id not in product_knowledge_id_set
    ]
    product_visual_ids = visual_reference_ids_for_knowledge(
        session,
        product_knowledge_ids,
    )
    other_visual_ids = visual_reference_ids_for_knowledge(session, other_knowledge_ids)
    knowledge_visual_ids = list(dict.fromkeys([*product_visual_ids, *other_visual_ids]))
    _require_external_media_references(
        session,
        knowledge_visual_ids,
        external=image_provider.status.external,
    )
    # Product media is deliberately first so a previous AI draft cannot become
    # the primary reference and displace the original bottle/brand artwork.
    for reference_id in product_visual_ids:
        storage_key, media_type = library_asset_for_identity(
            session,
            identity,
            reference_id,
            # A relationship to an allowed product does not independently grant
            # access to a child media resource.
            allow_cross_owner=False,
        )
        if not media_type.startswith("image/"):
            raise HTTPException(422, "所选产品的视觉资料必须是图片类型")
        product_reference_indices.append(append_reference_image(storage_key, media_type))
    for reference_id in dict.fromkeys(str(item) for item in reference_ids):
        reference_entry = session.get(ResourceEntry, reference_id)
        if reference_entry is None:
            reference_asset = session.get(AssetRef, reference_id)
            if reference_asset is not None:
                reference_entry = session.get(ResourceEntry, reference_asset.entry_id)
        if reference_entry is not None:
            task_reference_metadata = load(reference_entry.metadata_json)
            if (
                task_reference_metadata.get("source_type") == "task-image-reference"
                and task_reference_metadata.get("task_id") != task.id
            ):
                raise HTTPException(422, "本地参考图片不属于当前任务，请重新上传")
        storage_key, media_type = media_asset_for_identity(
            session,
            identity,
            reference_id,
            expected_prefix="image",
        )
        reference_index = append_reference_image(storage_key, media_type)
        if source_artifact is not None and reference_id == source_artifact.id:
            source_reference_index = reference_index
    for reference_id in other_visual_ids:
        storage_key, media_type = library_asset_for_identity(
            session,
            identity,
            reference_id,
            allow_cross_owner=allow_cross_owner_knowledge,
        )
        if not media_type.startswith("image/"):
            raise HTTPException(422, "所选知识库视觉资料必须是图片类型")
        append_reference_image(storage_key, media_type)
    prompt_subject = str(payload.parameters.get("prompt") or "").strip()
    if not prompt_subject:
        prompt_subject = (task.topic or task.product or task.name).strip()
    prompt_parts = (
        [PRODUCT_VISUAL_CONSISTENCY_PROMPT, prompt_subject]
        if product_visual_ids
        else [prompt_subject]
    )
    scene = str(payload.parameters.get("scene") or "").strip()
    style = str(payload.parameters.get("style") or "").strip()
    ratio = str(payload.parameters.get("ratio") or "").strip()
    instruction = payload.instruction.strip()
    if scene:
        prompt_parts.append(f"场景：{scene}")
    if style:
        prompt_parts.append(f"风格：{style}")
    if ratio and ratio.lower() not in {"adaptive", "auto"}:
        # Ark image generation accepts size but not a provider-neutral ratio field.
        # Preserve the requested composition intent in the prompt for every adapter.
        prompt_parts.append(f"画面比例：{ratio}")
    if instruction:
        prompt_parts.append(f"补充要求：{instruction}")
    if task.mode in {"full", "image"}:
        # This policy belongs only to the two task-image entry points. Keep the
        # shared video-project prompt and provider adapter behavior unchanged.
        prompt_parts = task_image_prompt_parts(
            instruction=instruction,
            subject=prompt_subject,
            scene=scene,
            style=style,
            ratio=ratio,
            reference_count=len(reference_images),
            product_reference_indices=product_reference_indices,
            source_reference_index=source_reference_index,
        )
        prompt = "；".join(prompt_parts)[:10_000]
        knowledge_prefix = "；已选企业资料（仅核对事实，不覆盖本次画面修改要求）："
        knowledge_budget = max(0, 10_000 - len(prompt) - len(knowledge_prefix))
        knowledge_parts = bounded_knowledge_prompt_parts(
            selected_knowledge,
            max_chars=min(6_000, knowledge_budget),
        )
        if knowledge_parts and knowledge_budget:
            prompt += knowledge_prefix + "；".join(knowledge_parts)[:knowledge_budget]
    else:
        prompt = grounded_image_prompt(prompt_parts, selected_knowledge)
    selected_reference_ids = [item.id for item in selected_knowledge]
    selected_references = reference_metadata(
        selected_knowledge,
        selected_reference_ids,
    )
    # SQLite has one writer: commit the started run before the slow model call so
    # other users' saves are not blocked while this request waits for the model.
    session.commit()
    try:
        generated = image_provider.generate(
            ImageGenerationRequest(
                prompt=prompt,
                count=payload.count,
                size=(
                    payload.parameters.get("size")
                    if isinstance(payload.parameters.get("size"), str)
                    else None
                ),
                ratio=(
                    payload.parameters.get("ratio")
                    if isinstance(payload.parameters.get("ratio"), str)
                    else None
                ),
                reference_images=tuple(reference_images),
                reference_media_types=tuple(reference_media_types),
                watermark=bool(payload.parameters.get("watermark", False)),
                seed=payload.parameters.get("seed"),
                extra=dict(payload.parameters.get("extra", {})),
            )
        )
    except ProviderCallError as exc:
        details = _generation_error_detail(exc)
        fail_generation_run(session, task, run, details["message"])
        session.commit()
        raise HTTPException(502, details) from exc
    except ValueError as exc:
        fail_generation_run(session, task, run, str(exc))
        session.commit()
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        session.rollback()
        fail_generation_run(session, task, run, "生成未完成，请重试")
        session.commit()
        raise
    artifacts = []
    for index, generated_asset in enumerate(generated):
        key = f"tasks/{task.id}/images/{uuid4().hex}.{generated_asset.extension.lstrip('.')}"
        stored = asset_store.put_bytes(key, generated_asset.content, generated_asset.media_type)
        metadata = {
            **generated_asset.metadata,
            **payload.parameters,
            "instruction": payload.instruction,
            "prompt": prompt,
            "knowledge_resource_ids": knowledge_resource_ids,
            "visual_reference_resource_ids": knowledge_visual_ids,
            "source_reference_ids": selected_reference_ids,
            "references": selected_references,
            "provider": generated_asset.provider_id,
            "model": generated_asset.model,
            "media_type": stored.media_type,
            "size_bytes": stored.size,
            "checksum_sha256": stored.checksum_sha256,
        }
        if source_artifact is not None:
            metadata["source_artifact_id"] = source_artifact.id
        artifact = create_artifact(
            session,
            task,
            "image",
            metadata,
            identity.user_id,
            stored.key,
        )
        if source_artifact is not None:
            artifact.parent_id = source_artifact.id
        attach_artifact(artifact, run, "image", index)
        artifacts.append(artifact)
    complete_generation_run(
        session,
        task,
        run,
        artifacts,
        provider_id=image_provider.status.provider_id,
        model=image_provider.status.model,
    )
    audit(
        session,
        identity.user_id,
        "images.generated",
        "content_task",
        task.id,
        count=len(artifacts),
        source_artifact_id=(source_artifact.id if source_artifact is not None else None),
    )
    session.commit()
    return [artifact_json(item) for item in artifacts]


@app.post("/api/v1/tasks/{task_id}/generate-storyboard", tags=["generation"])
def generate_storyboard(
    task_id: str,
    payload: GenerateRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    text_provider: TextProvider = Depends(get_text_provider),
    knowledge_provider: KnowledgeProvider = Depends(get_knowledge_provider),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    try:
        run, replayed = begin_generation_run(
            session,
            task,
            "storyboard",
            count=payload.count,
            instruction=payload.instruction,
            parameters=payload.parameters,
            idempotency_key=payload.idempotency_key,
            user_id=identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    if replayed:
        artifacts = run_artifacts(session, run)
        if run.status == "succeeded" and artifacts:
            return artifact_json(artifacts[0])
        raise HTTPException(409, f"相同幂等键的生成批次状态为 {run.status}")
    source_artifact = None
    if "copy" in applicable_stages(task):
        selected_copy = selected_stage_artifacts(session, task, "copy", "copy")
        if not selected_copy:
            raise HTTPException(409, "请先选择文案版本后再生成分镜")
        if payload.source_artifact_id and payload.source_artifact_id not in {
            item.id for item in selected_copy
        }:
            raise HTTPException(409, "分镜来源必须是当前已选择的文案版本")
        source_artifact = next(
            (item for item in selected_copy if item.id == payload.source_artifact_id),
            selected_copy[0],
        )
    elif payload.source_artifact_id:
        source_artifact, _ = get_artifact_for_user(
            session,
            payload.source_artifact_id,
            identity,
        )
        if source_artifact.kind != "copy":
            raise HTTPException(422, "分镜来源必须是文案版本")
        if (
            source_artifact.task_id != task.id
            and session.scalar(
                select(ArtifactLink.id).where(
                    ArtifactLink.target_task_id == task.id,
                    ArtifactLink.artifact_id == source_artifact.id,
                    ArtifactLink.stage_key == "copy",
                )
            )
            is None
        ):
            raise HTTPException(422, "分镜只能引用当前任务或已回引的文案版本")
    source_content = load(source_artifact.content_json) if source_artifact else None
    generation_input = prepare_content_generation(
        session=session,
        task=task,
        identity=identity,
        instruction=payload.instruction,
        knowledge_provider=knowledge_provider,
        text_provider=text_provider,
        knowledge_resource_ids=generation_knowledge_resource_ids(payload.parameters),
        source_content=source_content,
    )
    # SQLite has one writer: commit the started run before the slow model call so
    # other users' saves are not blocked while this request waits for the model.
    session.commit()
    try:
        result = call_text_generation(
            lambda: text_provider.storyboard(generation_input, payload.count)
        )
    except HTTPException as exc:
        fail_generation_run(session, task, run, str(exc.detail))
        session.commit()
        raise
    except Exception:
        session.rollback()
        fail_generation_run(session, task, run, "生成未完成，请重试")
        session.commit()
        raise
    reference_ids = used_reference_ids(
        result.content,
        generation_input.knowledge,
        source_content=source_content,
    )
    references = reference_metadata(
        generation_input.knowledge,
        reference_ids,
        source_content,
    )
    artifact = create_artifact(
        session,
        task,
        "storyboard",
        {
            "shots": result.content,
            "instruction": payload.instruction,
            "parameters": payload.parameters,
            "provider": result.provider_id,
            "generation": provider_metadata(result),
            "reference_item_ids": reference_ids,
            "reference_mode": (
                "provider_declared" if text_provider.status.external else "retrieved_context"
            ),
            "references": references,
            "content_data_level": draft_content_data_level(references, source_content),
            "source_artifact_id": source_artifact.id if source_artifact else None,
        },
        identity.user_id,
    )
    attach_artifact(artifact, run, "storyboard", 0)
    complete_generation_run(
        session,
        task,
        run,
        [artifact],
        provider_id=result.provider_id,
        model=result.model,
    )
    audit(
        session,
        identity.user_id,
        "storyboard.generated",
        "artifact",
        artifact.id,
        provider_id=result.provider_id,
        model=result.model,
        reference_item_ids=reference_ids,
    )
    session.commit()
    return artifact_json(artifact)


@app.post(
    "/api/v1/tasks/{task_id}/stages/storyboard/import",
    tags=["workflow"],
    response_model=ArtifactResponse,
)
def import_storyboard_script(
    task_id: str,
    payload: StoryboardImportRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    """Save a user's existing script as a selectable storyboard artifact."""
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    if "storyboard" not in operable_stages(task):
        raise HTTPException(422, "当前任务入口不包含视频脚本阶段")
    stage = get_stage(session, task, "storyboard")
    if stage.version != payload.expected_version:
        raise HTTPException(
            409,
            f"版本冲突：当前阶段版本为 {stage.version}，请求基于版本 {payload.expected_version}",
        )
    script_text = payload.text.strip()
    lines = [line.strip() for line in script_text.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(422, "已有脚本不能为空")
    try:
        run, _ = begin_generation_run(
            session,
            task,
            "storyboard",
            count=1,
            instruction="人工导入已有脚本",
            parameters={"source": "human_script"},
            idempotency_key=None,
            user_id=identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    shots = [
        {
            "sequence": index,
            "visual": f"围绕{task.topic or task.product or task.name}匹配或上传画面素材",
            "narration": line,
            "subtitle": line,
            "duration_seconds": max(1.0, round(len(line) / 6, 1)),
            "transition": "none",
            "source_ids": [],
        }
        for index, line in enumerate(lines, start=1)
    ]
    artifact = create_artifact(
        session,
        task,
        "storyboard",
        {
            "shots": shots,
            "script_text": script_text,
            "source": "human_script",
            "content_data_level": "internal",
        },
        identity.user_id,
    )
    attach_artifact(artifact, run, "storyboard", 0)
    complete_generation_run(
        session,
        task,
        run,
        [artifact],
        provider_id="human-input",
    )
    audit(
        session,
        identity.user_id,
        "storyboard.imported",
        "artifact",
        artifact.id,
        line_count=len(lines),
    )
    session.commit()
    return artifact_json(artifact)


@app.post("/api/v1/tasks/{task_id}/generate-voice", tags=["generation"])
def generate_voice(
    task_id: str,
    payload: GenerateRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    speech_provider: SpeechSynthesisPort = Depends(get_speech_synthesis_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    try:
        run, replayed = begin_generation_run(
            session,
            task,
            "voice_subtitles",
            count=1,
            instruction=payload.instruction,
            parameters={**payload.parameters, "operation": "voice"},
            idempotency_key=payload.idempotency_key,
            user_id=identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    if replayed:
        artifacts = run_artifacts(session, run)
        if run.status == "succeeded" and artifacts:
            return artifact_json(artifacts[0])
        raise HTTPException(409, f"相同幂等键的生成批次状态为 {run.status}")
    text, source_artifact = voice_source_text(
        session,
        task,
        payload.instruction,
    )
    # SQLite has one writer: commit the started run before the slow model call so
    # other users' saves are not blocked while this request waits for the model.
    session.commit()
    try:
        generated = speech_provider.synthesize(
            SpeechSynthesisRequest(
                text=text,
                voice=payload.parameters.get("voice"),
                speed=float(payload.parameters.get("speed", 1.0)),
                volume=float(payload.parameters.get("volume", 1.0)),
                audio_format=payload.parameters.get("audio_format", "mp3"),
                sample_rate=int(payload.parameters.get("sample_rate", 24000)),
                with_timestamps=True,
                user_id=identity.user_id,
                extra=dict(payload.parameters.get("extra", {})),
            )
        )
    except ProviderCallError as exc:
        fail_generation_run(session, task, run, str(exc))
        session.commit()
        raise HTTPException(502, f"配音模型调用失败：{exc}") from exc
    except (TypeError, ValueError) as exc:
        fail_generation_run(session, task, run, str(exc))
        session.commit()
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        session.rollback()
        fail_generation_run(session, task, run, "生成未完成，请重试")
        session.commit()
        raise
    key = f"tasks/{task.id}/audio/{uuid4().hex}.{generated.extension.lstrip('.')}"
    stored = asset_store.put_bytes(key, generated.audio, generated.media_type)
    duration = generated.duration_seconds or min(max(len(text) / 8.0, 0.5), 300.0)
    artifact = create_artifact(
        session,
        task,
        "voice",
        {
            "text": text,
            "duration": duration,
            "voice": generated.voice,
            "speed": payload.parameters.get("speed", 1.0),
            "volume": payload.parameters.get("volume", 1.0),
            "provider": generated.provider_id,
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
            "metadata": generated.metadata,
            "source_artifact_id": source_artifact.id if source_artifact else None,
            "source_kind": source_artifact.kind if source_artifact else "explicit_text",
        },
        identity.user_id,
        stored.key,
    )
    attach_artifact(artifact, run, "voice_subtitles", 0)
    complete_generation_run(
        session,
        task,
        run,
        [artifact],
        provider_id=speech_provider.status.provider_id,
        model=speech_provider.status.model,
    )
    audit(session, identity.user_id, "voice.generated", "artifact", artifact.id)
    session.commit()
    return artifact_json(artifact)


@app.post("/api/v1/tasks/{task_id}/generate-subtitles", tags=["generation"])
def generate_subtitles(
    task_id: str,
    payload: GenerateRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    subtitle_provider: SubtitleAlignmentPort = Depends(get_subtitle_alignment_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    try:
        run, replayed = begin_generation_run(
            session,
            task,
            "voice_subtitles",
            count=1,
            instruction=payload.instruction,
            parameters={**payload.parameters, "operation": "subtitle"},
            idempotency_key=payload.idempotency_key,
            user_id=identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    if replayed:
        artifacts = run_artifacts(session, run)
        if run.status == "succeeded" and artifacts:
            return artifact_json(artifacts[0])
        raise HTTPException(409, f"相同幂等键的生成批次状态为 {run.status}")
    selected_voice = selected_stage_artifacts(
        session,
        task,
        "voice_subtitles",
        "voice",
    )
    voice = selected_voice[0] if selected_voice else None
    if voice is not None:
        voice_content = load(voice.content_json)
        text = payload.instruction.strip() or str(voice_content.get("text") or "").strip()
        duration_seconds = float(voice_content.get("duration", 3))
        word_timings = tuple(
            WordTiming(
                text=str(item.get("text", "")),
                start_seconds=float(item.get("start_seconds", 0)),
                end_seconds=float(item.get("end_seconds", 0)),
            )
            for item in voice_content.get("word_timings", [])
            if isinstance(item, dict)
        )
        source_artifact = voice
    else:
        text, source_artifact = voice_source_text(session, task, payload.instruction)
        duration_seconds = min(max(len(text) / 8.0, 0.5), 300.0)
        word_timings = ()
    try:
        segments = tuple(
            subtitle_provider.align(
                SubtitleAlignmentRequest(
                    text=text,
                    duration_seconds=duration_seconds,
                    word_timings=word_timings,
                    max_chars_per_line=payload.parameters.get("max_chars_per_line"),
                    max_lines=payload.parameters.get("max_lines"),
                )
            )
        )
    except (TypeError, ValueError) as exc:
        fail_generation_run(session, task, run, str(exc))
        session.commit()
        raise HTTPException(422, str(exc)) from exc
    key = f"tasks/{task.id}/subtitles/{uuid4().hex}.srt"
    stored = asset_store.put_bytes(
        key,
        render_srt(segments).encode("utf-8"),
        "application/x-subrip",
    )
    artifact = create_artifact(
        session,
        task,
        "subtitle",
        {
            "text": text,
            "format": "srt",
            "timed": True,
            "parameters": payload.parameters,
            "source_artifact_id": source_artifact.id if source_artifact else None,
            "provider": subtitle_provider.status.provider_id,
            "segments": [
                {
                    "sequence": segment.sequence,
                    "text": segment.text,
                    "start_seconds": segment.start_seconds,
                    "end_seconds": segment.end_seconds,
                    "timing_source": segment.timing_source,
                }
                for segment in segments
            ],
            "checksum_sha256": stored.checksum_sha256,
        },
        identity.user_id,
        stored.key,
    )
    attach_artifact(artifact, run, "voice_subtitles", 0)
    complete_generation_run(
        session,
        task,
        run,
        [artifact],
        provider_id=subtitle_provider.status.provider_id,
        model=subtitle_provider.status.model,
    )
    audit(session, identity.user_id, "subtitles.generated", "artifact", artifact.id)
    session.commit()
    return artifact_json(artifact)


@app.post("/api/v1/tasks/{task_id}/generate-ai-video", tags=["video"])
def generate_ai_video(
    task_id: str,
    payload: GenerateRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    ai_video_provider: AiVideoGenerationPort = Depends(get_ai_video_generation_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    video_egress_levels = _require_external_provider_egress(
        session,
        task,
        ai_video_provider.status.provider_id,
        external=ai_video_provider.status.external,
    )
    storyboards = selected_stage_artifacts(
        session,
        task,
        "storyboard",
        "storyboard",
    )
    if not storyboards:
        raise HTTPException(409, "请先选择分镜脚本")
    if payload.source_artifact_id and payload.source_artifact_id not in {
        item.id for item in storyboards
    }:
        raise HTTPException(409, "AI 视频来源必须是当前已选择的分镜脚本")
    storyboard = next(
        (item for item in storyboards if item.id == payload.source_artifact_id),
        storyboards[0],
    )
    _require_external_media_references(
        session,
        [storyboard],
        external=ai_video_provider.status.external,
    )
    storyboard_shots = load(storyboard.content_json).get("shots", [])
    prompt_shots = [
        (
            index,
            str(shot.get("visual") or shot.get("narration") or "").strip(),
            shot,
        )
        for index, shot in enumerate(storyboard_shots)
        if isinstance(shot, dict) and str(shot.get("visual") or shot.get("narration") or "").strip()
    ]
    if not prompt_shots:
        raise HTTPException(422, "已选择的分镜脚本没有可生成的视频镜头")
    requested_shot_index = payload.parameters.get("shot_index")
    if requested_shot_index is not None:
        if isinstance(requested_shot_index, bool) or not isinstance(requested_shot_index, int):
            raise HTTPException(422, "shot_index 必须是从 0 开始的整数")
        if requested_shot_index < 0 or requested_shot_index >= len(storyboard_shots):
            raise HTTPException(422, "shot_index 超出分镜范围")
        prompt_shots = [item for item in prompt_shots if item[0] == requested_shot_index]
        if not prompt_shots:
            raise HTTPException(422, "指定分镜没有可用于生成视频的画面或旁白说明")
    if payload.instruction.strip():
        prompt_shots = [
            (
                shot_index,
                f"{prompt}\n补充要求：{payload.instruction.strip()}",
                shot,
            )
            for shot_index, prompt, shot in prompt_shots
        ]
    demo_use_videos(len(prompt_shots))  # one model-generated clip per shot
    requested_knowledge_ids = generation_knowledge_resource_ids(payload.parameters)
    resolved_knowledge_ids, allow_cross_owner_knowledge = resolve_generation_knowledge_resources(
        session,
        task,
        requested_knowledge_ids,
        identity,
    )
    if resolved_knowledge_ids is None:
        stored_knowledge_ids = load(task.knowledge_resource_ids_json)
        knowledge_resource_ids = get_knowledge_resources_for_identity(
            session,
            stored_knowledge_ids if isinstance(stored_knowledge_ids, list) else [],
            identity,
            allow_cross_owner=True,
        )
    else:
        knowledge_resource_ids = resolved_knowledge_ids
    try:
        selected_knowledge = selected_knowledge_chunks(
            session,
            knowledge_resource_ids,
            requester_id=identity.user_id,
            allowed_levels=video_egress_levels,
            query_text=" ".join(
                value
                for value in (
                    task.name,
                    task.topic,
                    task.product,
                    task.style,
                    payload.instruction,
                    *(item[1] for item in prompt_shots),
                )
                if value
            ),
            allow_cross_owner=allow_cross_owner_knowledge,
        )
    except EgressDeniedError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KnowledgeSelectionError as exc:
        raise HTTPException(422, str(exc)) from exc
    prompt_shots = [
        (shot_index, grounded_video_prompt(prompt, selected_knowledge), shot)
        for shot_index, prompt, shot in prompt_shots
    ]
    prompts = [item[1] for item in prompt_shots]
    try:
        run, replayed = begin_generation_run(
            session,
            task,
            "video_preview",
            count=len(prompts),
            instruction=payload.instruction,
            parameters={
                **payload.parameters,
                "operation": "clips",
                "source_artifact_id": storyboard.id,
                "shot_prompts": prompts,
            },
            idempotency_key=payload.idempotency_key,
            user_id=identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    if replayed:
        jobs = session.scalars(
            select(Job).where(Job.generation_run_id == run.id).order_by(Job.created_at)
        ).all()
        return {
            "generation_run": generation_run_json(session, run, artifact_json),
            "jobs": [job_json(item) for item in jobs],
        }
    jobs = []
    try:
        for shot_index, prompt, shot in prompt_shots:
            source_asset_id = str(shot.get("asset_id") or shot.get("image_asset_id") or "").strip()
            raw_shot_duration = shot.get("duration_seconds") or shot.get("duration")
            try:
                shot_duration = float(raw_shot_duration) if raw_shot_duration is not None else None
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    422,
                    f"第 {shot_index + 1} 镜建议时长格式无效",
                ) from exc
            image_url = None
            if source_asset_id:
                _require_external_media_references(
                    session,
                    [source_asset_id],
                    external=ai_video_provider.status.external,
                )
                storage_key, media_type = media_asset_for_identity(
                    session,
                    identity,
                    source_asset_id,
                    expected_prefix="image",
                )
                image_url = (
                    f"data:{media_type};base64,"
                    f"{base64.b64encode(asset_store.read_bytes(storage_key)).decode('ascii')}"
                )
            jobs.append(
                queue_ai_video_job(
                    session,
                    task,
                    run,
                    payload,
                    identity,
                    ai_video_provider,
                    shot_index=shot_index,
                    prompt_override=prompt,
                    image_url_override=image_url,
                    source_asset_id=source_asset_id or None,
                    duration_seconds_override=shot_duration,
                    ratio_override=str(shot.get("ratio") or "").strip() or None,
                    resolution_override=(str(shot.get("resolution") or "").strip() or None),
                    preserve_other_shots=requested_shot_index is not None,
                )
            )
    except HTTPException:
        for queued in jobs:
            provider_task_id = load(queued.result_json).get("provider_task_id")
            if provider_task_id:
                with suppress(Exception):
                    ai_video_provider.cancel(provider_task_id)
        session.commit()
        raise
    audit(
        session,
        identity.user_id,
        "ai_video.queued",
        "generation_run",
        run.id,
        job_ids=[item.id for item in jobs],
        shot_count=len(prompts),
        provider_id=ai_video_provider.status.provider_id,
    )
    session.commit()
    # Complete one generation run in shot order.  Apart from preserving storyboard
    # order, this prevents concurrent workers from overwriting the same persisted
    # result_artifact_ids_json value when several clips finish at once.
    job_ids = [job.id for job in jobs]

    def generate_clips_in_order() -> None:
        for job_id in job_ids:
            run_ai_video_job(job_id, ai_video_provider, asset_store)

    dispatcher.submit(generate_clips_in_order)
    return {
        "generation_run": generation_run_json(session, run, artifact_json),
        "jobs": [job_json(item) for item in jobs],
    }


@app.post("/api/v1/tasks/{task_id}/compose-video", tags=["video"])
def compose_video(
    task_id: str,
    payload: GenerateRequest | None = Body(default=None),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    video_composer: VideoComposerPort = Depends(get_video_composer_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    request_payload = payload or GenerateRequest(count=1)
    parameters = dict(request_payload.parameters)
    composition_fields = set(VideoCompositionSpec.model_fields)
    composition_values = {
        key: value for key, value in parameters.items() if key in composition_fields
    }
    if request_payload.composition_spec is not None:
        composition_values.update(request_payload.composition_spec.model_dump(exclude_unset=True))
    try:
        composition = VideoCompositionSpec.model_validate(composition_values)
    except ValueError as exc:
        raise HTTPException(422, f"视频合成参数无效：{exc}") from exc
    try:
        run, replayed = begin_generation_run(
            session,
            task,
            "video_preview",
            count=1,
            instruction=request_payload.instruction,
            parameters={
                **parameters,
                "composition_spec": composition.model_dump(mode="json"),
                "operation": "compose",
            },
            idempotency_key=request_payload.idempotency_key,
            user_id=identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    if replayed:
        existing_job = session.scalar(
            select(Job).where(Job.generation_run_id == run.id).order_by(Job.created_at.desc())
        )
        if existing_job is None:
            raise HTTPException(409, "相同幂等键的生成批次没有关联任务")
        return job_json(existing_job)

    forbidden_storage_keys = {
        "background_music_key",
        "logo_key",
        "intro_key",
        "outro_key",
    }
    if forbidden_storage_keys.intersection(parameters):
        raise HTTPException(422, "媒体素材必须提交资源 ID，不能提交存储路径")

    storyboards = selected_stage_artifacts(
        session,
        task,
        "storyboard",
        "storyboard",
    )
    if not storyboards:
        raise HTTPException(409, "请先选择分镜脚本")
    storyboard = storyboards[0]
    raw_shots = load(storyboard.content_json).get("shots", [])
    shots = [item if isinstance(item, dict) else {} for item in raw_shots]

    raw_source_asset_ids = parameters.get("source_asset_ids", [])
    if not isinstance(raw_source_asset_ids, list):
        raise HTTPException(422, "source_asset_ids 必须是保持镜头顺序的数组")
    # Repeating the same source is intentional in a storyboard (for example an
    # establishing shot reused as an outro), so preserve both order and duplicates.
    source_asset_ids = [str(item) for item in raw_source_asset_ids if str(item).strip()]

    def clip_source_from_reference(reference_id: str) -> dict:
        storage_key, media_type = media_asset_for_identity(
            session,
            identity,
            reference_id,
            expected_prefix=None,
        )
        if not (media_type.startswith(("image/", "video/"))):
            raise HTTPException(422, "快捷成片素材只能选择图片或视频")
        artifact = session.get(Artifact, reference_id)
        return {
            "source_id": reference_id,
            "asset_id": None if artifact is not None else reference_id,
            "artifact_id": artifact.id if artifact is not None else None,
            "storage_key": storage_key,
            "kind": "image" if media_type.startswith("image/") else "video",
            "content": load(artifact.content_json) if artifact is not None else {},
        }

    fallback_sources = [
        clip_source_from_reference(source_asset_id) for source_asset_id in source_asset_ids
    ]
    if not fallback_sources:
        selected_clips = selected_stage_artifacts(
            session,
            task,
            "video_preview",
            "ai_video_clip",
        )
        selected_clips = [artifact for artifact in selected_clips if artifact.storage_key]
        if not selected_clips:
            selected_clips = selected_stage_artifacts(
                session,
                task,
                "video_preview",
                "video",
            )
            selected_clips = [artifact for artifact in selected_clips if artifact.storage_key]
        if not selected_clips:
            selected_images = selected_stage_artifacts(session, task, "image", "image")
            selected_clips = [artifact for artifact in selected_images if artifact.storage_key]
        fallback_sources = [
            {
                "source_id": artifact.id,
                "asset_id": None,
                "artifact_id": artifact.id,
                "storage_key": artifact.storage_key,
                "kind": artifact.kind,
                "content": load(artifact.content_json),
            }
            for artifact in selected_clips
        ]

    # A confirmed storyboard is the authoritative timeline.  Bind each shot to its
    # own selected asset first, including repeated IDs; explicit/source-stage assets
    # are only fallbacks for shots without a binding.
    shot_asset_ids = [
        str(
            shot.get("asset_id")
            or shot.get("image_asset_id")
            or shot.get("video_asset_id")
            or shot.get("source_asset_id")
            or ""
        ).strip()
        for shot in shots
    ]
    clip_sources: list[dict]
    if any(shot_asset_ids):
        clip_sources = []
        for index, shot_asset_id in enumerate(shot_asset_ids):
            if shot_asset_id:
                clip_sources.append(clip_source_from_reference(shot_asset_id))
            elif index < len(fallback_sources):
                clip_sources.append(fallback_sources[index])
            else:
                raise HTTPException(409, f"第 {index + 1} 镜尚未选择图片或视频素材")
    else:
        clip_sources = fallback_sources
    if not clip_sources:
        raise HTTPException(409, "请先选择可用于合成的视频片段或图片")

    # Persist the actual ordered library source list.  Do not deduplicate: repeated
    # assets represent repeated shots and must survive recovery/export.
    source_asset_ids = [str(item["asset_id"]) for item in clip_sources if item.get("asset_id")]

    for flag_name in ("include_voice", "include_subtitles"):
        if flag_name in parameters and not isinstance(parameters[flag_name], bool):
            raise HTTPException(422, f"{flag_name} 必须是布尔值")
    include_voice = parameters.get("include_voice", True)
    include_subtitles = parameters.get("include_subtitles", True)

    voice = None
    voice_asset_id = None
    voice_key = None
    if include_voice:
        requested_voice_asset_id = str(parameters.get("voice_asset_id") or "").strip()
        if requested_voice_asset_id:
            voice_asset_id = requested_voice_asset_id
            voice_key = media_asset_for_identity(
                session,
                identity,
                requested_voice_asset_id,
                expected_prefix="audio",
            )[0]
        else:
            voices = selected_stage_artifacts(session, task, "voice_subtitles", "voice")
            voice = next((item for item in voices if item.storage_key), None)
            voice_key = voice.storage_key if voice is not None else None

    subtitle = None
    subtitle_key = None
    if include_subtitles:
        requested_subtitle_id = str(parameters.get("subtitle_artifact_id") or "").strip()
        if requested_subtitle_id:
            candidate, _ = get_artifact_for_user(
                session,
                requested_subtitle_id,
                identity,
            )
            if (
                candidate.kind != "subtitle"
                or not candidate.storage_key
                or candidate.status in {DISCARDED_ARTIFACT_STATUS, "legacy_mock"}
                or not artifact_belongs_to_or_is_linked(
                    session,
                    task,
                    candidate,
                    "voice_subtitles",
                )
            ):
                raise HTTPException(422, "字幕必须来自当前任务或已回引的有效字幕版本")
            subtitle = candidate
        else:
            subtitles = selected_stage_artifacts(
                session,
                task,
                "voice_subtitles",
                "subtitle",
            )
            subtitle = next((item for item in subtitles if item.storage_key), None)
        subtitle_key = subtitle.storage_key if subtitle is not None else None

    if composition.source_audio_policy == "replace_by_voice" and not voice_key:
        raise HTTPException(422, "选择主配音替换源音轨时必须提供配音版本或 voice_asset_id")
    uploaded_voice_text = str(parameters.get("uploaded_voice_text") or "").strip()
    if len(uploaded_voice_text) > 20000:
        raise HTTPException(422, "上传配音对应文本不能超过 20000 字")

    default_duration = float(parameters.get("clip_duration_seconds", 3))
    if not 0 < default_duration <= 600:
        raise HTTPException(422, "默认镜头时长必须大于 0 且不超过 600 秒")
    clip_specs = []
    for index, clip in enumerate(clip_sources):
        shot = shots[index] if index < len(shots) and isinstance(shots[index], dict) else {}
        transition = str(shot.get("transition") or composition.transition).strip().lower()
        if transition not in {"none", "cut", "fade", "fadeblack", "fadewhite"}:
            raise HTTPException(422, f"第 {index + 1} 镜使用了不支持的转场")
        transition_duration = float(
            shot.get(
                "transition_duration_seconds",
                composition.transition_duration_seconds,
            )
        )
        if not 0 <= transition_duration <= 5:
            raise HTTPException(422, f"第 {index + 1} 镜转场时长必须在 0 到 5 秒之间")
        fit_mode = str(shot.get("fit_mode") or composition.fit_mode).strip().lower()
        if fit_mode not in {"contain", "cover"}:
            raise HTTPException(422, f"第 {index + 1} 镜画面适配方式无效")
        duration_seconds = float(
            shot.get("duration_seconds")
            or shot.get("duration")
            or clip["content"].get("duration_seconds")
            or default_duration
        )
        if not 0 < duration_seconds <= 600:
            raise HTTPException(422, f"第 {index + 1} 镜时长必须大于 0 且不超过 600 秒")
        clip_specs.append(
            {
                "artifact_id": clip["artifact_id"],
                "asset_id": clip["asset_id"],
                "storage_key": clip["storage_key"],
                "duration_seconds": duration_seconds,
                "transition": transition,
                "transition_duration_seconds": transition_duration,
                "is_image": clip["kind"] == "image",
                "fit_mode": fit_mode,
            }
        )

    def optional_asset(
        parameter_names: tuple[str, ...],
        expected_prefix: str,
    ) -> tuple[str | None, str | None]:
        asset_id = next(
            (str(parameters[name]) for name in parameter_names if parameters.get(name)),
            None,
        )
        if asset_id is None:
            return None, None
        storage_key = media_asset_for_identity(
            session,
            identity,
            asset_id,
            expected_prefix=expected_prefix,
        )[0]
        return asset_id, storage_key

    background_music_asset_id, background_music_key = optional_asset(
        ("bgm_asset_id", "background_music_asset_id"),
        "audio",
    )
    logo_asset_id, logo_key = optional_asset(("logo_asset_id",), "image")
    intro_asset_id, intro_key = optional_asset(("intro_asset_id",), "video")
    outro_asset_id, outro_key = optional_asset(("outro_asset_id",), "video")
    if callable(start_video_job):
        legacy_job = start_video_job(
            task.id,
            identity.user_id,
            clip_sources[0]["storage_key"],
            voice_key,
            subtitle_key,
            parameters,
        )
        return {
            "id": legacy_job.id,
            "kind": legacy_job.kind,
            "status": legacy_job.status,
        }
    composition_spec = {
        "schema_version": 1,
        **composition.model_dump(mode="json"),
        "clips": [
            {
                "order": index + 1,
                "source_artifact_id": item["artifact_id"],
                "source_asset_id": item["asset_id"],
                "duration_seconds": item["duration_seconds"],
                "transition": item["transition"],
                "transition_duration_seconds": item["transition_duration_seconds"],
                "fit_mode": item["fit_mode"],
            }
            for index, item in enumerate(clip_specs)
        ],
        "storyboard_artifact_id": storyboard.id,
        "voice_artifact_id": voice.id if voice is not None else None,
        "voice_asset_id": voice_asset_id,
        "uploaded_voice_text": uploaded_voice_text or None,
        "subtitle_artifact_id": subtitle.id if subtitle is not None else None,
        "subtitle_alignment_status": (
            "provided_artifact"
            if subtitle is not None
            else (
                "uploaded_text_not_aligned"
                if voice_asset_id and uploaded_voice_text
                else ("uploaded_audio_not_transcribed" if voice_asset_id else "not_included")
            )
        ),
        "background_music_asset_id": background_music_asset_id,
        "logo_asset_id": logo_asset_id,
        "intro_asset_id": intro_asset_id,
        "outro_asset_id": outro_asset_id,
    }
    job_input = {
        "task_id": task.id,
        "clips": clip_specs,
        "composition_spec": composition_spec,
        "source_artifact_ids": [
            *[str(item["artifact_id"]) for item in clip_sources if item["artifact_id"]],
            *([voice.id] if voice is not None else []),
            *([subtitle.id] if subtitle is not None else []),
            *([storyboard.id] if storyboard else []),
        ],
        "source_asset_ids": source_asset_ids,
        "voice_key": voice_key,
        "subtitle_key": subtitle_key,
        "width": composition.width,
        "height": composition.height,
        "fps": composition.fps,
        "fit_mode": composition.fit_mode,
        "source_audio_policy": composition.source_audio_policy,
        "background_music_key": background_music_key,
        "background_music_volume": composition.background_music_volume,
        "logo_key": logo_key,
        "logo_width": composition.logo_width,
        "logo_position": composition.logo_position,
        "subtitle_style": composition.subtitle_style.model_dump(mode="json"),
        "intro_key": intro_key,
        "intro_duration_seconds": composition.intro_duration_seconds,
        "outro_key": outro_key,
        "outro_duration_seconds": composition.outro_duration_seconds,
    }
    job = Job(
        task_id=task.id,
        generation_run_id=run.id,
        owner_id=identity.user_id,
        kind="video_composition",
        status="queued",
        progress=0,
        result_json=dump(job_input),
    )
    session.add(job)
    session.flush()
    audit(
        session,
        identity.user_id,
        "video.composition_queued",
        "job",
        job.id,
        generation_run_id=run.id,
    )
    session.commit()
    dispatcher.submit(lambda: run_video_composition_job(job.id, video_composer, asset_store))
    return job_json(job)


@app.post(
    "/api/v1/tasks/{task_id}/stages/{stage_key}/generate",
    tags=["workflow"],
    response_model=GenerationRunResponse,
)
def generate_stage(
    task_id: str,
    stage_key: str,
    payload: GenerateRequest,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    text_provider: TextProvider = Depends(get_text_provider),
    knowledge_provider: KnowledgeProvider = Depends(get_knowledge_provider),
    image_provider: ImageGenerationPort = Depends(get_image_generation_port),
    speech_provider: SpeechSynthesisPort = Depends(get_speech_synthesis_port),
    subtitle_provider: SubtitleAlignmentPort = Depends(get_subtitle_alignment_port),
    ai_video_provider: AiVideoGenerationPort = Depends(get_ai_video_generation_port),
    video_composer: VideoComposerPort = Depends(get_video_composer_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    task = get_task_or_404(session, task_id, identity)
    ensure_task_editable(task)
    if stage_key == "topic":
        response = generate_topics(
            task_id, payload, session, identity, text_provider, knowledge_provider
        )
        run_id = response["generation_run"]["id"]
    elif stage_key == "copy":
        response = generate_copy(
            task_id, payload, session, identity, text_provider, knowledge_provider
        )
        run_id = response["generation_run"]["id"]
    elif stage_key == "compliance":
        source = current_copy_artifact_for_downstream(
            session,
            task,
            requested_artifact_id=payload.source_artifact_id,
            user_id=identity.user_id,
        )
        if source is None:
            raise HTTPException(409, "请先生成或选择一个当前文案后再进行合规检测")
        try:
            run, replayed = begin_generation_run(
                session,
                task,
                "compliance",
                count=1,
                instruction=payload.instruction,
                parameters={
                    **payload.parameters,
                    "source_artifact_id": source.id,
                },
                idempotency_key=payload.idempotency_key,
                user_id=identity.user_id,
            )
        except (WorkflowConflictError, WorkflowValidationError) as exc:
            raise workflow_http_error(exc) from exc
        if not replayed:
            result = compliance_result(session, load(source.content_json))
            artifact = create_artifact(
                session,
                task,
                "compliance",
                {**result, "source_artifact_id": source.id},
                identity.user_id,
            )
            attach_artifact(artifact, run, "compliance", 0)
            complete_generation_run(
                session,
                task,
                run,
                [artifact],
                provider_id="local-rule-engine",
            )
            audit(
                session,
                identity.user_id,
                "compliance.generated",
                "artifact",
                artifact.id,
                source_artifact_id=source.id,
            )
            session.commit()
        run_id = run.id
    elif stage_key == "image":
        generate_images(
            task_id,
            payload,
            session,
            identity,
            image_provider,
            asset_store,
        )
        run_id = session.scalar(
            select(GenerationRun.id)
            .where(
                GenerationRun.task_id == task.id,
                GenerationRun.stage_key == "image",
            )
            .order_by(GenerationRun.created_at.desc())
        )
    elif stage_key == "storyboard":
        response = generate_storyboard(
            task_id, payload, session, identity, text_provider, knowledge_provider
        )
        run_id = response["generation_run_id"]
    elif stage_key == "voice_subtitles":
        operation = payload.parameters.get("operation", "voice")
        if operation not in {"voice", "subtitle"}:
            raise HTTPException(422, "配音字幕阶段 operation 仅支持 voice 或 subtitle")
        if operation == "subtitle":
            response = generate_subtitles(
                task_id,
                payload,
                session,
                identity,
                subtitle_provider,
                asset_store,
            )
        else:
            response = generate_voice(
                task_id,
                payload,
                session,
                identity,
                speech_provider,
                asset_store,
            )
        run_id = response["generation_run_id"]
    elif stage_key == "video_preview":
        operation = payload.parameters.get("operation", "clips")
        if operation not in {"clips", "compose"}:
            raise HTTPException(422, "视频预览阶段 operation 仅支持 clips 或 compose")
        if operation == "compose":
            response = compose_video(
                task_id,
                payload,
                session,
                identity,
                video_composer,
                asset_store,
            )
            run_id = response["generation_run_id"]
        else:
            response = generate_ai_video(
                task_id,
                payload,
                session,
                identity,
                ai_video_provider,
                asset_store,
            )
            run_id = response["generation_run"]["id"]
    else:
        raise HTTPException(422, "该阶段不支持生成操作")
    run = session.get(GenerationRun, run_id)
    if run is None:
        raise HTTPException(500, "生成批次未正确保存")
    return generation_run_json(session, run, artifact_json)


@app.get(
    "/api/v1/jobs/{job_id}",
    tags=["tasks"],
    response_model=MediaJobResponse,
)
def get_job(
    job_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.owner_id != identity.user_id and not can_view_all_tasks(identity):
        raise HTTPException(403, "无权查看该任务")
    return job_json(job)


@app.get("/api/v1/jobs/{job_id}/download", tags=["tasks"])
def download_job_result(
    job_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> FileResponse:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.owner_id != identity.user_id and not can_view_all_tasks(identity):
        raise HTTPException(403, "无权下载该任务结果")
    if job.status != "succeeded":
        raise HTTPException(409, "任务尚未成功完成")
    result = load(job.result_json)
    storage_key = str(result.get("storage_key") or "")
    if not storage_key:
        raise HTTPException(409, "该任务没有可下载文件")
    try:
        path = asset_store.path_for(storage_key)
    except ValueError as exc:
        raise HTTPException(409, "任务结果存储引用无效") from exc
    if not path.is_file():
        raise HTTPException(404, "任务结果文件不存在")
    media_types = {
        ".mp4": "video/mp4",
        ".zip": "application/zip",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
    }
    return FileResponse(
        path,
        filename=str(result.get("download_filename") or path.name),
        media_type=str(
            result.get("media_type")
            or media_types.get(path.suffix.lower(), "application/octet-stream")
        ),
    )


@app.post(
    "/api/v1/jobs/{job_id}/cancel",
    tags=["tasks"],
    response_model=MediaJobResponse,
)
def cancel_job(
    job_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    ai_video_provider: AiVideoGenerationPort = Depends(get_ai_video_generation_port),
    provider_media_publisher: ProviderMediaPublisherPort = Depends(get_provider_media_publisher),
) -> dict:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.owner_id != identity.user_id and not can_view_all_tasks(identity):
        raise HTTPException(403, "无权取消该任务")
    get_task_or_404(session, job.task_id, identity)
    if job.status == "cancelled":
        return job_json(job)
    if job.status in {"succeeded", "failed"}:
        raise HTTPException(409, f"状态为 {job.status} 的任务不能取消")
    siblings = [job]
    if job.generation_run_id:
        siblings = list(
            session.scalars(select(Job).where(Job.generation_run_id == job.generation_run_id)).all()
        )
    for sibling in siblings:
        if sibling.status in {"succeeded", "failed", "cancelled"}:
            continue
        if sibling.kind == "ai_video_generation":
            provider_task_id = load(sibling.result_json).get("provider_task_id")
            if provider_task_id:
                try:
                    ai_video_provider.cancel(provider_task_id)
                except ProviderCallError as exc:
                    raise HTTPException(502, f"取消 AI 视频任务失败：{exc}") from exc
        elif sibling.kind == "video_composition" and sibling.status == "running":
            raise HTTPException(409, "本地视频合成已经开始，暂不支持中断")
        elif sibling.kind == VIDEO_PROJECT_AI_JOB_KIND:
            sibling_result = load(sibling.result_json)
            provider_task_id = sibling_result.get("provider_task_id")
            provider_cancelled = not provider_task_id
            if provider_task_id:
                try:
                    provider_task = ai_video_provider.cancel(str(provider_task_id))
                    provider_cancelled = provider_task.status in {
                        "succeeded",
                        "failed",
                        "cancelled",
                        "expired",
                    }
                except ProviderCallError as exc:
                    raise HTTPException(502, f"取消 AI 视频任务失败：{exc}") from exc
            request_durable_job_cancel(session, sibling.id)
            if provider_cancelled:
                release_provider_media_publications(
                    provider_media_publisher,
                    sibling_result,
                )
            continue
        elif sibling.kind in {RENDER_JOB_KIND, EXPORT_JOB_KIND}:
            request_durable_job_cancel(session, sibling.id)
            continue
        sibling.status = "cancelled"
    run = session.get(GenerationRun, job.generation_run_id) if job.generation_run_id else None
    if run is not None:
        run.status = "cancelled"
        run.completed_at = datetime.now(UTC)
    audit(session, identity.user_id, "job.cancelled", "job", job.id)
    session.commit()
    session.refresh(job)
    return job_json(job)


@app.post(
    "/api/v1/jobs/{job_id}/retry",
    tags=["tasks"],
    response_model=MediaJobResponse,
)
def retry_job(
    job_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    ai_video_provider: AiVideoGenerationPort = Depends(get_ai_video_generation_port),
    video_composer: VideoComposerPort = Depends(get_video_composer_port),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
    provider_media_publisher: ProviderMediaPublisherPort = Depends(get_provider_media_publisher),
    timeline_renderer=Depends(get_timeline_renderer),
) -> dict:
    source = session.get(Job, job_id)
    if source is None:
        raise HTTPException(404, "任务不存在")
    if source.owner_id != identity.user_id and not can_view_all_tasks(identity):
        raise HTTPException(403, "无权重试该任务")
    get_task_or_404(session, source.task_id, identity)
    if source.status not in {"failed", "cancelled"}:
        raise HTTPException(409, "只有失败或已取消的任务可以重试")
    source_task = session.get(ContentTask, source.task_id)
    if source_task is not None:
        ensure_task_editable(source_task)
    if source.kind == VIDEO_PROJECT_AI_JOB_KIND:
        source_data = load(source.result_json)
        project_id = source_data.get("project_id")
        project, task = get_video_project_or_404(session, str(project_id), identity)
        request_data = source_data.get("request", {})
        if not isinstance(request_data, dict):
            raise HTTPException(409, "AI 视频任务的原始请求已损坏")
        _require_video_ai_brief(task)
        _require_external_provider_egress(
            session,
            task,
            ai_video_provider.status.provider_id,
            external=ai_video_provider.status.external,
        )
        if restore_misclassified_project_ai_video_job(session, source.id):
            # This provider has already accepted the job. Retrying submission
            # would create a second billable generation instead of recovering it.
            recovered_id = source.id
            audit(
                session,
                identity.user_id,
                "video_project.ai_video_submission_recovered",
                "job",
                recovered_id,
            )
            session.commit()
            dispatcher.submit(
                lambda current_id=recovered_id: run_video_project_ai_generation_job(
                    current_id, ai_video_provider, asset_store, provider_media_publisher
                )
            )
            return job_json(source)
        _require_external_media_references(
            session,
            [
                str(reference_id)
                for reference_id in (
                    request_data.get("source_project_asset_id"),
                    *(request_data.get("reference_image_asset_ids") or ()),
                    *(request_data.get("reference_video_asset_ids") or ()),
                )
                if reference_id
            ],
            external=ai_video_provider.status.external,
        )
        stored_knowledge_ids = load(task.knowledge_resource_ids_json)
        recorded_knowledge_ids = request_data.get("knowledge_resource_ids")
        knowledge_resource_ids = get_knowledge_resources_for_identity(
            session,
            (
                recorded_knowledge_ids
                if isinstance(recorded_knowledge_ids, list)
                else stored_knowledge_ids
                if isinstance(stored_knowledge_ids, list)
                else []
            ),
            identity,
            allow_cross_owner=True,
        )
        _require_external_media_references(
            session,
            knowledge_resource_ids,
            external=ai_video_provider.status.external,
        )
        (
            product_visual_ids,
            product_reference_images,
            product_reference_media_types,
        ) = (
            visual_assets_for_generation(
                session,
                [
                    item.strip()
                    for item in request_data["visual_reference_resource_ids"]
                    if isinstance(item, str) and item.strip()
                ],
                identity,
                asset_store,
                external=ai_video_provider.status.external,
            )
            if isinstance(request_data.get("visual_reference_resource_ids"), list)
            else product_visual_assets_for_generation(
                session,
                knowledge_resource_ids,
                identity,
                asset_store,
                external=ai_video_provider.status.external,
            )
        )
        retry_prompt = str(request_data.get("prompt") or "")
        supporting_context = request_data.get("supporting_context")
        if (
            not isinstance(supporting_context, str)
            and product_visual_ids
            and PRODUCT_VISUAL_CONSISTENCY_PROMPT not in retry_prompt
        ):
            retry_prompt = f"{PRODUCT_VISUAL_CONSISTENCY_PROMPT}；{retry_prompt}"[:10_000]
        try:
            retried = enqueue_project_ai_video(
                session,
                project=project,
                task=task,
                created_by=identity.user_id,
                provider=ai_video_provider,
                asset_store=asset_store,
                provider_media_publisher=provider_media_publisher,
                spec=ProjectAiVideoGenerationSpec(
                    prompt=retry_prompt,
                    supporting_context=(
                        supporting_context if isinstance(supporting_context, str) else None
                    ),
                    label=str(request_data.get("label") or "AI 生成视频"),
                    source_copy_artifact_id=request_data.get("source_copy_artifact_id"),
                    reference_images=product_reference_images,
                    reference_media_types=product_reference_media_types,
                    source_project_asset_id=request_data.get("source_project_asset_id"),
                    reference_image_asset_ids=tuple(
                        request_data.get("reference_image_asset_ids") or ()
                    ),
                    reference_video_asset_ids=tuple(
                        request_data.get("reference_video_asset_ids") or ()
                    ),
                    knowledge_resource_ids=tuple(knowledge_resource_ids),
                    visual_reference_resource_ids=tuple(product_visual_ids),
                    duration_seconds=request_data.get("duration_seconds"),
                    ratio=request_data.get("ratio"),
                    resolution=request_data.get("resolution"),
                    generate_audio=bool(request_data.get("generate_audio", False)),
                    return_last_frame=bool(request_data.get("return_last_frame", False)),
                    parent_artifact_id=request_data.get("parent_artifact_id"),
                ),
            )
        except Exception as exc:
            cleanup_failed_ai_video_submission(session, exc)
            raise video_project_generation_http_error(exc, clear_generation_error=True) from exc
        retried_id = retried.job.id
        audit(
            session,
            identity.user_id,
            "job.retried",
            "job",
            retried_id,
            source_job_id=source.id,
        )
        session.commit()
        dispatcher.submit(
            lambda current_id=retried_id: run_video_project_ai_generation_job(
                current_id,
                ai_video_provider,
                asset_store,
                provider_media_publisher,
            )
        )
        return job_json(retried.job)
    if source.kind in {RENDER_JOB_KIND, EXPORT_JOB_KIND}:
        retry_durable_job(session, source.id)
        audit(
            session,
            identity.user_id,
            "job.retried",
            "job",
            source.id,
            source_job_id=source.id,
        )
        session.commit()
        session.refresh(source)
        if source.kind == RENDER_JOB_KIND:
            dispatcher.submit(
                lambda: run_video_project_render_job(
                    source.id,
                    SessionLocal,
                    asset_store,
                    timeline_renderer,
                    settings,
                )
            )
        else:
            dispatcher.submit(
                lambda: run_video_project_export_job(
                    source.id,
                    SessionLocal,
                    asset_store,
                    timeline_renderer,
                    settings,
                )
            )
        return job_json(source)
    if source.kind == "official_knowledge_import":
        retried = Job(
            task_id="library",
            owner_id=identity.user_id,
            kind="official_knowledge_import",
            status="queued",
            progress=0,
            result_json="{}",
        )
        session.add(retried)
        session.flush()
        audit(
            session,
            identity.user_id,
            "job.retried",
            "job",
            retried.id,
            source_job_id=source.id,
        )
        session.commit()
        dispatcher.submit(lambda: run_official_knowledge_import_job(retried.id))
        return job_json(retried)
    task = get_task_or_404(session, source.task_id, identity)
    source_data = load(source.result_json)
    try:
        run, _ = begin_generation_run(
            session,
            task,
            "video_preview",
            count=1,
            instruction="重试视频任务",
            parameters={"retry_of_job_id": source.id},
            idempotency_key=None,
            user_id=identity.user_id,
        )
    except (WorkflowConflictError, WorkflowValidationError) as exc:
        raise workflow_http_error(exc) from exc
    if source.kind == "ai_video_generation":
        request_data = source_data.get("request", {})
        source_asset_id = request_data.get("source_asset_id")
        image_url = None
        if source_asset_id:
            storage_key, media_type = media_asset_for_identity(
                session,
                identity,
                str(source_asset_id),
                expected_prefix="image",
            )
            image_url = (
                f"data:{media_type};base64,"
                f"{base64.b64encode(asset_store.read_bytes(storage_key)).decode('ascii')}"
            )
        payload = GenerateRequest(
            count=1,
            instruction=request_data.get("instruction", ""),
            parameters=request_data.get("parameters", {}),
        )
        retried = queue_ai_video_job(
            session,
            task,
            run,
            payload,
            identity,
            ai_video_provider,
            shot_index=int(request_data.get("shot_index", 0)),
            prompt_override=request_data.get("prompt"),
            image_url_override=image_url,
            source_asset_id=str(source_asset_id) if source_asset_id else None,
            duration_seconds_override=request_data.get("duration_seconds"),
            ratio_override=request_data.get("ratio"),
            resolution_override=request_data.get("resolution"),
            preserve_other_shots=True,
        )
        retry_job_id = retried.id

        def worker() -> None:
            run_ai_video_job(retry_job_id, ai_video_provider, asset_store)
    elif source.kind == "video_composition":
        retried = Job(
            task_id=task.id,
            generation_run_id=run.id,
            owner_id=identity.user_id,
            kind="video_composition",
            status="queued",
            progress=0,
            result_json=dump(
                {
                    key: value
                    for key, value in source_data.items()
                    if key not in {"artifact_id", "storage_key"}
                }
            ),
        )
        session.add(retried)
        session.flush()
        retry_job_id = retried.id

        def worker() -> None:
            run_video_composition_job(retry_job_id, video_composer, asset_store)
    else:
        raise HTTPException(422, "该任务类型不支持重试")
    audit(
        session,
        identity.user_id,
        "job.retried",
        "job",
        retried.id,
        source_job_id=source.id,
    )
    session.commit()
    dispatcher.submit(worker)
    return job_json(retried)


@app.get(
    "/api/v1/tasks/{task_id}/artifacts",
    tags=["artifacts"],
    response_model=list[ArtifactResponse],
)
def list_artifacts(
    task_id: str,
    kind: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> list[dict]:
    get_task_or_404(session, task_id, identity, allow_admin_read=True)
    statement = select(Artifact).where(
        Artifact.task_id == task_id,
        Artifact.status != DISCARDED_ARTIFACT_STATUS,
    )
    if kind:
        statement = statement.where(Artifact.kind == kind)
    rows = list(session.scalars(statement.order_by(Artifact.created_at)).all())
    result = [
        {
            **artifact_json(row),
            "linked": False,
            "link_id": None,
            "source_task_id": None,
        }
        for row in rows
    ]
    seen = {row.id for row in rows}
    links = session.scalars(
        select(ArtifactLink)
        .where(ArtifactLink.target_task_id == task_id)
        .order_by(ArtifactLink.created_at)
    ).all()
    for link in links:
        if link.artifact_id in seen:
            continue
        source_task = session.get(ContentTask, link.source_task_id)
        if source_task is None or not (
            can_view_all_tasks(identity)
            or can_access(source_task, identity.user_id, identity.department)
        ):
            continue
        artifact = session.get(Artifact, link.artifact_id)
        if (
            artifact is None
            or artifact.status == DISCARDED_ARTIFACT_STATUS
            or (kind is not None and artifact.kind != kind)
        ):
            continue
        result.append(
            {
                **artifact_json(artifact),
                "linked": True,
                "link_id": link.id,
                "source_task_id": link.source_task_id,
            }
        )
        seen.add(artifact.id)
    start = (page - 1) * page_size
    return result[start : start + page_size]


@app.delete(
    "/api/v1/artifacts/{artifact_id}",
    tags=["artifacts"],
    response_model=ArtifactDiscardResponse,
)
def discard_artifact(
    artifact_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    artifact, task = get_artifact_for_user(
        session,
        artifact_id,
        identity,
        include_discarded=True,
    )
    ensure_task_editable(task)
    if artifact.status == DISCARDED_ARTIFACT_STATUS:
        return {
            "id": artifact.id,
            "status": DISCARDED_ARTIFACT_STATUS,
            "discarded": True,
            "already_discarded": True,
        }
    if artifact_is_currently_confirmed(session, artifact):
        raise HTTPException(409, "当前已确认的内容版本不能删除")
    if artifact.status == "selected":
        if artifact_is_currently_selected(session, artifact):
            raise HTTPException(409, "当前使用中的内容版本不能删除，请先选择其他版本")
        artifact.status = "draft"
    elif artifact.status == "confirmed":
        # Repair historical UI/backend drift: task-stage mappings are the
        # source of truth.  An orphaned denormalized status must not prevent a
        # visibly inactive historical version from being deleted.
        artifact.status = "draft"
    if artifact.status != "draft":
        status_labels = {
            "selected": "当前使用中",
            "stale": "需更新",
            "legacy_mock": "历史模拟结果",
            "failed": "生成失败",
        }
        status_label = status_labels.get(artifact.status, "非草稿状态")
        detail = f"{status_label}的内容版本不能删除；只有待确认草稿可以删除"
        raise HTTPException(409, detail)

    previous_status = artifact.status
    artifact.status = DISCARDED_ARTIFACT_STATUS
    audit(
        session,
        identity.user_id,
        "artifact.discarded",
        "artifact",
        artifact.id,
        task_id=task.id,
        kind=artifact.kind,
        version=artifact.version,
        previous_status=previous_status,
    )
    session.commit()
    return {
        "id": artifact.id,
        "status": DISCARDED_ARTIFACT_STATUS,
        "discarded": True,
        "already_discarded": False,
    }


@app.patch("/api/v1/artifacts/{artifact_id}", tags=["artifacts"])
def revise_artifact(
    artifact_id: str,
    payload: ArtifactPatch,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> dict:
    source, source_task = get_artifact_for_user(session, artifact_id, identity)
    task = source_task
    if payload.target_task_id and payload.target_task_id != source_task.id:
        target = get_task_or_404(session, payload.target_task_id, identity)
        stage_key = ARTIFACT_STAGE.get(source.kind)
        if stage_key is None or not artifact_belongs_to_or_is_linked(
            session,
            target,
            source,
            stage_key,
        ):
            raise HTTPException(422, "目标任务没有引用该内容版本")
        if stage_key not in operable_stages(target):
            raise HTTPException(422, "目标任务不包含该内容阶段")
        task = target
    ensure_task_editable(task)
    if source.version != payload.expected_version:
        raise HTTPException(
            409,
            f"版本冲突：当前版本为 {source.version}，请求基于版本 {payload.expected_version}",
        )
    source_content = load(source.content_json)
    allowed_reference_ids = {
        item for item in source_content.get("reference_item_ids", []) if isinstance(item, str)
    }
    revision_content = filter_declared_source_ids(
        payload.content,
        allowed_reference_ids,
    )
    for field_name in ("reference_item_ids", "references", "reference_mode"):
        if field_name in source_content:
            revision_content[field_name] = source_content[field_name]
        else:
            revision_content.pop(field_name, None)
    title_context = (
        require_current_full_copy_lineage(session, task, source) if source.kind == "copy" else None
    )
    if title_context is not None:
        source_title_artifact, _source_title_content, source_title = title_context
        revision_content["source_title_artifact_id"] = source_title_artifact.id
        revision_content["source_title_version"] = source_title_artifact.version
        revision_content["source_title"] = source_title
    for field_name in (
        "provider",
        "generation",
        "source_generation",
        "revision",
        "compliance",
        "content_data_level",
    ):
        revision_content.pop(field_name, None)
    if source.kind in {"topic", "copy"}:
        candidates = revision_content.get("candidates")
        selected_index = revision_content.get("selected_candidate_index")
        if (
            isinstance(candidates, list)
            and isinstance(selected_index, int)
            and not isinstance(selected_index, bool)
            and 0 <= selected_index < len(candidates)
            and isinstance(candidates[selected_index], dict)
        ):
            revision_content["selected_candidate"] = candidates[selected_index]
        elif isinstance(candidates, list) and isinstance(
            source_content.get("selected_candidate"), dict
        ):
            source_candidates = source_content.get("candidates")
            if isinstance(source_candidates, list):
                selected_snapshot = source_content["selected_candidate"]
                snapshot_index = next(
                    (
                        index
                        for index, candidate in enumerate(source_candidates)
                        if isinstance(candidate, dict) and candidate == selected_snapshot
                    ),
                    None,
                )
                if (
                    snapshot_index is not None
                    and snapshot_index < len(candidates)
                    and isinstance(candidates[snapshot_index], dict)
                ):
                    revision_content["selected_candidate"] = candidates[snapshot_index]
    if title_context is not None:
        candidates = revision_content.get("candidates")
        requested_titles = (
            [str(candidate.get("title") or "").strip() for candidate in candidates]
            if isinstance(candidates, list)
            and all(isinstance(candidate, dict) for candidate in candidates)
            else []
        )
        selected_title = str(selected_text_candidate(revision_content).get("title") or "").strip()
        if selected_title != source_title or any(
            requested_title != source_title for requested_title in requested_titles
        ):
            raise HTTPException(422, "完整流程的文案标题由已确认标题决定，请在标题阶段修改")
    editable_source_content = dict(source_content)
    for field_name in (
        "provider",
        "generation",
        "source_generation",
        "revision",
        "compliance",
        "content_data_level",
    ):
        editable_source_content.pop(field_name, None)
    if revision_content == editable_source_content:
        return artifact_json(source)
    if isinstance(source_content.get("generation"), dict):
        revision_content["source_generation"] = source_content["generation"]
    revision_content["content_data_level"] = draft_content_data_level(
        source_content.get("references", []),
        source_content,
    )
    revision_content["revision"] = {
        "method": "human",
        "source_artifact_id": source.id,
    }
    if source.kind == "copy":
        revision_content["compliance"] = compliance_result(session, revision_content)
    revision_storage_key = source.storage_key
    if source.kind == "storyboard":
        raw_shots = revision_content.get("shots")
        if not isinstance(raw_shots, list) or not raw_shots:
            raise HTTPException(422, "分镜修改必须至少包含一个镜头")
        normalized_shots = []
        for index, shot in enumerate(raw_shots, start=1):
            if not isinstance(shot, dict):
                raise HTTPException(422, "分镜镜头格式无效")
            try:
                duration_seconds = float(shot.get("duration_seconds", shot.get("duration", 3)))
                transition_duration_seconds = float(shot.get("transition_duration_seconds", 0.4))
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, "分镜时长格式无效") from exc
            if duration_seconds <= 0 or transition_duration_seconds < 0:
                raise HTTPException(422, "分镜时长必须大于 0，转场时长不能小于 0")
            normalized = {
                **shot,
                "sequence": index,
                "order": index,
                "visual": str(shot.get("visual", "")),
                "narration": str(shot.get("narration", "")),
                "subtitle": str(shot.get("subtitle", "")),
                "duration_seconds": duration_seconds,
                "transition": str(shot.get("transition", "none")),
                "transition_duration_seconds": transition_duration_seconds,
            }
            normalized.pop("duration", None)
            normalized_shots.append(normalized)
        revision_content["shots"] = normalized_shots
    if source.kind == "subtitle":
        raw_segments = revision_content.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise HTTPException(422, "字幕修改必须包含非空的 segments")
        segments = []
        previous_end = 0.0
        for index, item in enumerate(raw_segments, start=1):
            if not isinstance(item, dict):
                raise HTTPException(422, "字幕分段格式无效")
            try:
                start_seconds = float(item["start_seconds"])
                end_seconds = float(item["end_seconds"])
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(422, "字幕时间格式无效") from exc
            if start_seconds < previous_end or end_seconds <= start_seconds:
                raise HTTPException(422, "字幕时间段必须递增、互不重叠且结束晚于开始")
            segments.append(
                AlignedSubtitleSegment(
                    sequence=index,
                    text=str(item.get("text", "")),
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    timing_source=str(item.get("timing_source", "estimated")),
                )
            )
            previous_end = end_seconds
        revision_content["segments"] = [
            {
                "sequence": segment.sequence,
                "text": segment.text,
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "timing_source": segment.timing_source,
            }
            for segment in segments
        ]
        key = f"tasks/{task.id}/subtitles/{uuid4().hex}.srt"
        stored = asset_store.put_bytes(
            key,
            render_srt(tuple(segments)).encode("utf-8"),
            "application/x-subrip",
        )
        revision_storage_key = stored.key
        revision_content["checksum_sha256"] = stored.checksum_sha256
    revision = create_artifact(
        session,
        task,
        source.kind,
        revision_content,
        identity.user_id,
        revision_storage_key,
    )
    revision.parent_id = source.parent_id or source.id
    revision.stage_key = source.stage_key or ARTIFACT_STAGE.get(source.kind)
    adopt_revision(session, task, revision)
    audit(
        session,
        identity.user_id,
        "artifact.revised",
        "artifact",
        revision.id,
        source_id=source.id,
    )
    session.commit()
    return artifact_json(revision)


@app.post("/api/v1/artifacts/{artifact_id}/confirm", tags=["artifacts"])
def confirm_artifact(
    artifact_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> dict:
    artifact, task = get_artifact_for_user(session, artifact_id, identity)
    ensure_task_editable(task)
    if artifact.status == "legacy_mock" or load(artifact.content_json).get("legacy_mock"):
        raise HTTPException(409, "模拟产物不能确认为正式内容")
    if artifact.kind == "copy":
        require_current_full_copy_lineage(session, task, artifact)
    stage_key = artifact.stage_key or ARTIFACT_STAGE.get(artifact.kind)
    if stage_key and stage_key in ARTIFACT_STAGE.values():
        try:
            stage = get_stage(session, task, stage_key)
            if stage_key == "voice_subtitles":
                select_artifacts(
                    session,
                    task,
                    stage_key,
                    [artifact],
                    stage.version,
                    identity.user_id,
                )
                artifact.status = "confirmed"
            else:
                confirm_stage(
                    session,
                    task,
                    stage_key,
                    stage.version,
                    identity.user_id,
                    [artifact],
                )
        except (WorkflowConflictError, WorkflowValidationError) as exc:
            raise workflow_http_error(exc) from exc
    else:
        artifact.status = "confirmed"
    audit(session, identity.user_id, "artifact.confirmed", "artifact", artifact.id)
    session.commit()
    return artifact_json(artifact)


@app.get("/api/v1/artifacts/{artifact_id}/download", tags=["artifacts"])
def download_artifact(
    artifact_id: str,
    export_format: str | None = Query(None, alias="format"),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> FileResponse:
    artifact, task = get_artifact_for_user(session, artifact_id, identity, allow_admin_read=True)
    try:
        if export_format and artifact.kind == "topic":
            topic_stage = get_stage(session, task, "topic")
            selected = load(topic_stage.selected_artifact_ids_json or "{}")
            confirmed = load(topic_stage.confirmed_artifact_ids_json or "{}")
            selected_ids = selected.get("topic", []) if isinstance(selected, dict) else []
            confirmed_ids = confirmed.get("topic", []) if isinstance(confirmed, dict) else []
            if isinstance(selected_ids, str):
                selected_ids = [selected_ids]
            if isinstance(confirmed_ids, str):
                confirmed_ids = [confirmed_ids]
            current_title = (
                topic_stage.state == "confirmed"
                and artifact.status == "confirmed"
                and artifact.id in selected_ids
                and artifact.id in confirmed_ids
            )
            completed_deliverable = (
                task.mode == "full"
                and (task.archived_at is not None or task.status in {"confirmed", "archived"})
                and any(
                    item.id == artifact.id for item in resolve_task_export_artifacts(session, task)
                )
            )
            if not current_title and not completed_deliverable:
                raise HTTPException(409, "只能导出当前已选择并确认的标题")
            path = export_topic_artifact(task, artifact, export_format)
        elif export_format and artifact.kind == "copy":
            path = export_copy_artifact(task, artifact, export_format)
        elif export_format and artifact.kind == "image":
            path = export_image_artifact(task, artifact, export_format)
        elif export_format:
            raise HTTPException(422, "当前内容类型不支持选择导出格式")
        elif artifact.kind in {"topic", "copy"}:
            # Rebuild readable text from the stored content, even when a legacy
            # artifact still points at an old raw TXT/JSON diagnostic file.
            path = export_text_artifact(task, artifact)
        elif artifact.storage_key:
            path = storage.path_for(artifact.storage_key)
        elif artifact.kind == "storyboard":
            path = export_text_artifact(task, artifact)
        else:
            raise HTTPException(409, "该内容版本没有文件")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".wav": "audio/wav",
        ".srt": "application/x-subrip",
        ".mp4": "video/mp4",
        ".txt": "text/plain; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".json": "application/json",
    }
    return FileResponse(
        path,
        filename=path.name,
        media_type=media_types.get(path.suffix.lower(), "application/octet-stream"),
    )


@app.get("/api/v1/tasks/{task_id}/export", tags=["export"])
def export_task(
    task_id: str,
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
    asset_store: AssetStorePort = Depends(get_asset_store_port),
) -> FileResponse:
    task = get_task_or_404(session, task_id, identity, allow_admin_read=True)
    required_final_video: Artifact | None = None
    artifacts = resolve_task_export_artifacts(session, task)
    artifacts = [
        item
        for item in artifacts
        if item.status != "legacy_mock" and not load(item.content_json).get("legacy_mock")
    ]
    if not artifacts:
        raise HTTPException(409, "当前任务没有可导出内容")
    if task.mode in {"full", "video"}:
        export_stage = get_stage(session, task, "export")
        if export_stage.state != "confirmed":
            raise HTTPException(409, "请先确认导出阶段")
        required_final_video = current_confirmed_final_video(session, task)

    # The user download contains finished content, not an editable project backup.
    # Resolve companion audio/subtitles from the final video's immutable version;
    # original shots, logos and other visual inputs are already in the final video
    # and must not make its download depend on those source files still existing.
    referenced_roles: dict[str, list[str]] = {}
    referenced_project_assets: dict[str, dict] = {}
    video_project_snapshot: dict | None = None

    def register_asset(asset_id: object, role: str) -> None:
        normalized = str(asset_id or "").strip()
        if not normalized:
            return
        roles = referenced_roles.setdefault(normalized, [])
        if role not in roles:
            roles.append(role)

    composed_video = required_final_video or next(
        (item for item in reversed(artifacts) if item.kind == "video"), None
    )
    if composed_video is not None:
        video_content = load(composed_video.content_json)
        composition_spec = video_content.get("composition_spec")
        if isinstance(composition_spec, dict):
            for field, role in (
                ("voice_asset_id", "主配音"),
                ("background_music_asset_id", "背景音乐"),
            ):
                register_asset(composition_spec.get(field), role)

        # New video-editor renders pin an immutable project revision instead of
        # embedding the old flat composition_spec.  Read companion files from that
        # exact revision, never the mutable head or a later draft.
        project_id = str(video_content.get("project_id") or "").strip()
        revision_id = str(video_content.get("revision_id") or "").strip()
        if project_id or revision_id:
            if not project_id or not revision_id:
                raise HTTPException(409, "最终成片缺少完整的视频工程版本信息，无法整包导出")
            project = session.get(VideoProject, project_id)
            revision = session.get(VideoProjectRevision, revision_id)
            if (
                project is None
                or project.task_id != task.id
                or revision is None
                or revision.project_id != project.id
            ):
                raise HTTPException(409, "最终成片关联的不可变视频工程版本不存在")
            timeline = load(revision.timeline_json or "{}")
            operations = load(revision.operations_json or "[]")
            if not isinstance(timeline, dict) or not isinstance(operations, list):
                raise HTTPException(409, "最终成片关联的视频工程版本数据损坏")
            video_project_snapshot = {
                "project_id": project.id,
                "task_id": task.id,
                "revision_id": revision.id,
                "revision": revision.revision,
                "parent_revision_id": revision.parent_revision_id,
                "source": revision.source,
                "timeline": timeline,
                "operations": operations,
                "created_by": revision.created_by,
                "created_at": revision.created_at.isoformat(),
                "render_artifact_id": composed_video.id,
                "render_profile": video_content.get("render_profile"),
            }
            tracks = timeline.get("tracks", [])
            if not isinstance(tracks, list):
                raise HTTPException(409, "最终成片关联的视频工程时间线格式无效")
            for track_index, track in enumerate(tracks, start=1):
                if not isinstance(track, dict):
                    continue
                if bool(track.get("muted", False)):
                    continue
                track_kind = str(track.get("kind") or f"轨道-{track_index}").strip()
                if track_kind not in {"voice", "music", "sfx", "subtitle"}:
                    continue
                track_name = str(track.get("name") or track_kind).strip()
                items = track.get("items", [])
                if not isinstance(items, list):
                    continue
                for item_index, timeline_item in enumerate(items, start=1):
                    if not isinstance(timeline_item, dict):
                        continue
                    if not bool(timeline_item.get("enabled", True)):
                        continue
                    project_asset_id = str(timeline_item.get("project_asset_id") or "").strip()
                    if not project_asset_id:
                        continue
                    project_asset = session.get(ProjectAsset, project_asset_id)
                    if project_asset is None or project_asset.project_id != project.id:
                        raise HTTPException(
                            409,
                            "最终成片的不可变工程版本引用了不存在的工程素材",
                        )
                    try:
                        storage_key, media_type, _ = project_asset_storage(
                            session,
                            project_asset,
                        )
                    except VideoProjectValidationError as exc:
                        raise HTTPException(409, str(exc)) from exc
                    item_label = str(timeline_item.get("label") or f"片段-{item_index}").strip()
                    dependency_role = f"{track_name}/{item_label}"
                    dependency = referenced_project_assets.setdefault(
                        project_asset.id,
                        {
                            "id": project_asset.id,
                            "project_asset_id": project_asset.id,
                            "source_type": project_asset.source_type,
                            "source_id": project_asset.source_id,
                            "role": project_asset.role,
                            "label": project_asset.label,
                            "roles": [],
                            "path": asset_store.path_for(storage_key),
                            "media_type": media_type,
                        },
                    )
                    if dependency_role not in dependency["roles"]:
                        dependency["roles"].append(dependency_role)

    referenced_assets = []
    for asset_id, roles in referenced_roles.items():
        storage_key, media_type = media_asset_for_identity(
            session,
            identity,
            asset_id,
            expected_prefix=None,
            allow_admin_read=True,
        )
        referenced_assets.append(
            {
                "id": asset_id,
                "roles": roles,
                "path": asset_store.path_for(storage_key),
                "media_type": media_type,
            }
        )
    referenced_assets.extend(referenced_project_assets.values())
    workflow_confirmed_selection = {
        item["key"]: item["confirmed_artifact_ids"]
        for item in workflow_json(session, task)["stages"]
        if item["applicable"] and item["state"] == "confirmed"
    }
    try:
        path = export_task_bundle(
            task,
            artifacts,
            referenced_assets,
            video_project_snapshot=video_project_snapshot,
            workflow_confirmed_selection=workflow_confirmed_selection,
            resolved_artifact_ids={item.id for item in artifacts},
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(session, identity.user_id, "task.exported", "content_task", task.id)
    session.commit()
    return FileResponse(path, filename=path.name, media_type="application/zip")


@app.get("/api/v1/audit-logs", tags=["audit"])
def list_audit_logs(
    action: str | None = Query(default=None, max_length=100),
    resource_type: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_session),
    identity: Identity = Depends(get_identity),
) -> list[dict]:
    if not has_permission(identity, "audit.read"):
        raise HTTPException(403, "当前账号没有审计查看权限，请联系项目管理员")
    statement = select(AuditLog)
    if action:
        statement = statement.where(AuditLog.action == action)
    if resource_type:
        statement = statement.where(AuditLog.resource_type == resource_type)
    rows = session.scalars(
        statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return [
        {
            "id": row.id,
            "owner_id": row.owner_id,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "details": load(row.details_json),
            "created_at": row.created_at,
        }
        for row in rows
    ]
