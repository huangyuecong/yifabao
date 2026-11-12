from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class LocalAccessLock(Base):
    """Singleton row serializing first-admin and grant changes across workers."""

    __tablename__ = "local_access_lock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class LocalUser(Base):
    __tablename__ = "local_users"
    __table_args__ = (CheckConstraint("revision >= 1", name="ck_local_user_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    subject: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    department: Mapped[str] = mapped_column(String(255), default="")
    is_project_admin: Mapped[bool] = mapped_column(Boolean(create_constraint=True), default=False)
    permissions_json: Mapped[str] = mapped_column(Text, default="[]")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class LibraryItem(Base):
    __tablename__ = "library_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255))
    text_content: Mapped[str] = mapped_column(Text, default="")
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resource_entry_id: Mapped[str | None] = mapped_column(
        ForeignKey("resource_entries.id"), nullable=True, index=True
    )
    data_level: Mapped[str] = mapped_column(String(32), default="internal")
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, index=True)


class ResourceEntry(Base):
    __tablename__ = "resource_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    resource_kind: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(128), default="", index=True)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    data_level: Mapped[str] = mapped_column(String(32), default="internal")
    # Optional tenant/department scoping.  ``department_id`` remains nullable so
    # the local single-tenant knowledge base keeps its shared-library semantics.
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    department_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    allowed_roles_json: Mapped[str] = mapped_column(Text, default="[]")
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, index=True)


class ResourceVersion(Base):
    __tablename__ = "resource_versions"
    __table_args__ = (UniqueConstraint("entry_id", "version", name="uq_resource_entry_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entry_id: Mapped[str] = mapped_column(ForeignKey("resource_entries.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    text_content: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    crawled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    freshness: Mapped[str | None] = mapped_column(String(64), nullable=True)
    facts_json: Mapped[str] = mapped_column(Text, default="[]")
    notes: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ResourceChunk(Base):
    __tablename__ = "resource_chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "sequence", name="uq_resource_chunk_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(ForeignKey("resource_versions.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AssetRef(Base):
    __tablename__ = "asset_refs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entry_id: Mapped[str] = mapped_column(ForeignKey("resource_entries.id"), index=True)
    version_id: Mapped[str | None] = mapped_column(
        ForeignKey("resource_versions.id"), nullable=True, index=True
    )
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    media_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ExternalRef(Base):
    __tablename__ = "external_refs"
    __table_args__ = (UniqueConstraint("system", "external_id", name="uq_external_ref_system_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entry_id: Mapped[str] = mapped_column(ForeignKey("resource_entries.id"), index=True)
    system: Mapped[str] = mapped_column(String(128), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class WeKnoraSyncJob(Base):
    __tablename__ = "weknora_sync_jobs"
    __table_args__ = (
        UniqueConstraint("connection_id", "version_id", name="uq_weknora_sync_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    entry_id: Mapped[str] = mapped_column(ForeignKey("resource_entries.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("resource_versions.id"), index=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(128))
    remote_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    operation: Mapped[str] = mapped_column(String(16), default="index")
    upload_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    delete_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class RuleSeedState(Base):
    """Remember initialization independently of deletable rules and audit retention."""

    __tablename__ = "rule_seed_state"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)


class SensitiveRule(Base):
    __tablename__ = "sensitive_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    phrase: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    rule_type: Mapped[str] = mapped_column(String(32), default="sensitive")
    level: Mapped[str] = mapped_column(String(32), default="warning")
    replacement: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(128))


class ContentTask(Base):
    __tablename__ = "content_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    mode: Mapped[str] = mapped_column(String(32), default="full")
    name: Mapped[str] = mapped_column(String(255))
    topic: Mapped[str] = mapped_column(String(255), default="")
    product: Mapped[str] = mapped_column(String(255), default="")
    product_resource_id: Mapped[str | None] = mapped_column(
        ForeignKey("resource_entries.id"), nullable=True, index=True
    )
    knowledge_resource_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    keywords_json: Mapped[str] = mapped_column(Text, default="[]")
    audience: Mapped[str] = mapped_column(String(255), default="")
    content_type: Mapped[str] = mapped_column(String(64), default="短视频")
    style: Mapped[str] = mapped_column(String(64), default="品牌宣传型")
    length: Mapped[str] = mapped_column(String(32), default="中")
    requirements: Mapped[str] = mapped_column(Text, default="")
    current_stage: Mapped[str] = mapped_column(String(64), default="configured")
    status: Mapped[str] = mapped_column(String(32), default="draft")
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    archived_workflow_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    workflow_enforced: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_users_json: Mapped[str] = mapped_column(Text, default="[]")
    allowed_departments_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, index=True)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("content_tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    stage_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    generation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_runs.id"), nullable=True, index=True
    )
    candidate_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    content_json: Mapped[str] = mapped_column(Text, default="{}")
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ArtifactLink(Base):
    __tablename__ = "artifact_links"
    __table_args__ = (
        UniqueConstraint("target_task_id", "artifact_id", name="uq_target_artifact_link"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    target_task_id: Mapped[str] = mapped_column(ForeignKey("content_tasks.id"), index=True)
    source_task_id: Mapped[str] = mapped_column(ForeignKey("content_tasks.id"), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    stage_key: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class GenerationRun(Base):
    __tablename__ = "generation_runs"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "stage_key",
            "idempotency_key",
            name="uq_generation_run_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("content_tasks.id"), index=True)
    stage_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    requested_count: Mapped[int] = mapped_column(Integer, default=1)
    instruction: Mapped[str] = mapped_column(Text, default="")
    parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    result_artifact_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    provider_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class TaskStage(Base):
    __tablename__ = "task_stages"
    __table_args__ = (UniqueConstraint("task_id", "stage_key", name="uq_task_stage"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("content_tasks.id"), index=True)
    stage_key: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32), default="not_started", index=True)
    requirements: Mapped[str] = mapped_column(Text, default="")
    selected_artifact_ids_json: Mapped[str] = mapped_column(Text, default="{}")
    confirmed_artifact_ids_json: Mapped[str] = mapped_column(Text, default="{}")
    skipped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class EgressGrant(Base):
    __tablename__ = "egress_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("content_tasks.id"), index=True)
    provider: Mapped[str] = mapped_column(String(100))
    allowed_data_levels_json: Mapped[str] = mapped_column(Text, default="[]")
    approved_by: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "kind",
            "idempotency_key",
            name="uq_content_job_idempotency",
        ),
        CheckConstraint("attempt >= 0", name="ck_content_job_attempt"),
        CheckConstraint("max_attempts > 0", name="ck_content_job_max_attempts"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    generation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_runs.id"), nullable=True, index=True
    )
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    available_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class VideoProject(Base):
    __tablename__ = "video_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("content_tasks.id"), unique=True, index=True)
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    # Kept as an explicit pointer rather than a database foreign key to avoid a
    # circular create dependency with the immutable revision table.  Revision
    # creation updates this pointer and the revision row in one transaction.
    current_revision_id: Mapped[str] = mapped_column(String(36), index=True)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class VideoProjectRevision(Base):
    __tablename__ = "video_project_revisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "revision",
            name="uq_video_project_committed_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("video_projects.id"), index=True)
    # AI candidates deliberately have NULL here.  Only revisions reachable from
    # VideoProject.current_revision_id receive a monotonic committed number.
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("video_project_revisions.id"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(32), default="human")
    timeline_json: Mapped[str] = mapped_column(Text, default="{}")
    operations_json: Mapped[str] = mapped_column(Text, default="[]")
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ProjectAsset(Base):
    __tablename__ = "project_assets"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "source_type",
            "source_id",
            name="uq_video_project_asset_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("video_projects.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    asset_ref_id: Mapped[str | None] = mapped_column(
        ForeignKey("asset_refs.id"), nullable=True, index=True
    )
    artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id"), nullable=True, index=True
    )
    library_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("library_items.id"), nullable=True, index=True
    )
    resource_entry_id: Mapped[str | None] = mapped_column(
        ForeignKey("resource_entries.id"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(32), default="media")
    label: Mapped[str] = mapped_column(String(255), default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class MediaDerivative(Base):
    __tablename__ = "media_derivatives"
    __table_args__ = (
        UniqueConstraint(
            "project_asset_id",
            "kind",
            "variant",
            name="uq_media_derivative_variant",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_asset_id: Mapped[str] = mapped_column(ForeignKey("project_assets.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    variant: Mapped[str] = mapped_column(String(64), default="default")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AiEditProposal(Base):
    __tablename__ = "ai_edit_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("video_projects.id"), index=True)
    base_revision_id: Mapped[str] = mapped_column(
        ForeignKey("video_project_revisions.id"), index=True
    )
    candidate_revision_id: Mapped[str] = mapped_column(
        ForeignKey("video_project_revisions.id"), index=True
    )
    prompt: Mapped[str] = mapped_column(Text, default="")
    operations_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    selected_operation_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_by: Mapped[str] = mapped_column(String(128))
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)


class VideoUploadSession(Base):
    __tablename__ = "video_upload_sessions"
    __table_args__ = (
        CheckConstraint("total_size > 0", name="ck_video_upload_total_size"),
        CheckConstraint("chunk_size > 0", name="ck_video_upload_chunk_size"),
        CheckConstraint("total_chunks > 0", name="ck_video_upload_total_chunks"),
        CheckConstraint(
            "status IN ('uploading', 'completed', 'cancelled', 'failed')",
            name="ck_video_upload_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("video_projects.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(128))
    media_kind: Mapped[str] = mapped_column(String(32))
    total_size: Mapped[int] = mapped_column(BigInteger)
    chunk_size: Mapped[int] = mapped_column(Integer)
    total_chunks: Mapped[int] = mapped_column(Integer)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="uploading", index=True)
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    project_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("project_assets.id"), nullable=True, unique=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class VideoUploadChunk(Base):
    __tablename__ = "video_upload_chunks"
    __table_args__ = (
        UniqueConstraint(
            "upload_session_id",
            "chunk_index",
            name="uq_video_upload_chunk_index",
        ),
        CheckConstraint("chunk_index >= 0", name="ck_video_upload_chunk_index"),
        CheckConstraint("size_bytes > 0", name="ck_video_upload_chunk_receipt_size"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    upload_session_id: Mapped[str] = mapped_column(
        ForeignKey("video_upload_sessions.id"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(36), index=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class OriginalArchive(Base):
    """Immutable original-file custody, independent of replaceable search indexes."""

    __tablename__ = "original_archives"
    __table_args__ = (
        UniqueConstraint("connection_id", "storage_key", name="uq_original_archive_key"),
        CheckConstraint("size_bytes >= 0", name="ck_original_archive_size"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    storage_key: Mapped[str] = mapped_column(String(512), index=True)
    entry_id: Mapped[str] = mapped_column(ForeignKey("resource_entries.id"), index=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(128))
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    source_marker: Mapped[str] = mapped_column(String(255))
    remote_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Shared originals keep each local upload's own source_marker while the
    # remote object retains the verified first upload's immutable marker.
    remote_source_marker: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    upload_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
