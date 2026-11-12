"""Add explicit content workflow, generation runs and optimistic versions."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "content_0002"
down_revision = "content_0001"
branch_labels = None
depends_on = None


STAGES = (
    "configuration",
    "topic",
    "copy",
    "compliance",
    "image",
    "storyboard",
    "voice_subtitles",
    "video_preview",
    "export",
)
MODE_STAGES = {
    "full": frozenset(STAGES),
    "copy": frozenset({"configuration", "copy", "compliance", "export"}),
    "image": frozenset({"configuration", "image", "export"}),
    "video": frozenset(
        {
            "configuration",
            "image",
            "storyboard",
            "voice_subtitles",
            "video_preview",
            "export",
        }
    ),
}
KIND_STAGE = {
    "topic": "topic",
    "copy": "copy",
    "compliance": "compliance",
    "image": "image",
    "storyboard": "storyboard",
    "voice": "voice_subtitles",
    "subtitle": "voice_subtitles",
    "ai_video_clip": "video_preview",
    "video": "video_preview",
}


def _legacy_mock(content: str, storage_key: str | None) -> bool:
    lowered = content.lower()
    return (
        "deterministic-" in lowered
        or '"mode": "mock"' in lowered
        or bool(storage_key and "-mock-" in storage_key.lower())
    )


def upgrade() -> None:
    op.create_table(
        "resource_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("resource_kind", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("category", sa.String(128), nullable=False),
        sa.Column("tags_json", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.String(36), nullable=True),
        sa.Column("data_level", sa.String(32), nullable=False),
        sa.Column("current_version_id", sa.String(36), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_resource_entries_owner_id", "resource_entries", ["owner_id"])
    op.create_index("ix_resource_entries_resource_kind", "resource_entries", ["resource_kind"])
    op.create_index("ix_resource_entries_category", "resource_entries", ["category"])
    op.create_index("ix_resource_entries_parent_id", "resource_entries", ["parent_id"])
    op.create_table(
        "resource_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entry_id", sa.String(36), sa.ForeignKey("resource_entries.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("source_url", sa.String(2048), nullable=True),
        sa.Column("crawled_at", sa.DateTime(), nullable=True),
        sa.Column("freshness", sa.String(64), nullable=True),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("entry_id", "version", name="uq_resource_entry_version"),
    )
    op.create_index("ix_resource_versions_entry_id", "resource_versions", ["entry_id"])
    op.create_table(
        "resource_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(36),
            sa.ForeignKey("resource_versions.id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("version_id", "sequence", name="uq_resource_chunk_sequence"),
    )
    op.create_index("ix_resource_chunks_version_id", "resource_chunks", ["version_id"])
    op.create_table(
        "asset_refs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entry_id", sa.String(36), sa.ForeignKey("resource_entries.id"), nullable=False),
        sa.Column(
            "version_id", sa.String(36), sa.ForeignKey("resource_versions.id"), nullable=True
        ),
        sa.Column("storage_key", sa.String(512), nullable=False, unique=True),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(128), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_asset_refs_entry_id", "asset_refs", ["entry_id"])
    op.create_index("ix_asset_refs_version_id", "asset_refs", ["version_id"])
    op.create_table(
        "external_refs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entry_id", sa.String(36), sa.ForeignKey("resource_entries.id"), nullable=False),
        sa.Column("system", sa.String(128), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("source_url", sa.String(2048), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("system", "external_id", name="uq_external_ref_system_id"),
    )
    op.create_index("ix_external_refs_entry_id", "external_refs", ["entry_id"])
    op.create_index("ix_external_refs_system", "external_refs", ["system"])
    with op.batch_alter_table("library_items") as batch:
        batch.add_column(sa.Column("resource_entry_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_library_items_resource_entry_id",
            "resource_entries",
            ["resource_entry_id"],
            ["id"],
        )
        batch.create_index("ix_library_items_resource_entry_id", ["resource_entry_id"])

    op.add_column(
        "content_tasks",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "content_tasks",
        sa.Column("workflow_enforced", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "generation_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("content_tasks.id"), nullable=False),
        sa.Column("stage_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("parameters_json", sa.Text(), nullable=False),
        sa.Column("result_artifact_ids_json", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.String(128), nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "task_id",
            "stage_key",
            "idempotency_key",
            name="uq_generation_run_idempotency",
        ),
    )
    op.create_index("ix_generation_runs_task_id", "generation_runs", ["task_id"])
    op.create_index("ix_generation_runs_stage_key", "generation_runs", ["stage_key"])
    op.create_index("ix_generation_runs_status", "generation_runs", ["status"])

    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("generation_run_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_jobs_generation_run_id",
            "generation_runs",
            ["generation_run_id"],
            ["id"],
        )
        batch.create_index("ix_jobs_generation_run_id", ["generation_run_id"])

    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("stage_key", sa.String(64), nullable=True))
        batch.add_column(sa.Column("generation_run_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("candidate_index", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_artifacts_generation_run_id",
            "generation_runs",
            ["generation_run_id"],
            ["id"],
        )
        batch.create_index("ix_artifacts_stage_key", ["stage_key"])
        batch.create_index("ix_artifacts_generation_run_id", ["generation_run_id"])

    op.create_table(
        "artifact_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "target_task_id", sa.String(36), sa.ForeignKey("content_tasks.id"), nullable=False
        ),
        sa.Column(
            "source_task_id", sa.String(36), sa.ForeignKey("content_tasks.id"), nullable=False
        ),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("stage_key", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("target_task_id", "artifact_id", name="uq_target_artifact_link"),
    )
    op.create_index("ix_artifact_links_target_task_id", "artifact_links", ["target_task_id"])
    op.create_index("ix_artifact_links_source_task_id", "artifact_links", ["source_task_id"])
    op.create_index("ix_artifact_links_artifact_id", "artifact_links", ["artifact_id"])
    op.create_index("ix_artifact_links_stage_key", "artifact_links", ["stage_key"])

    op.create_table(
        "task_stages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("content_tasks.id"), nullable=False),
        sa.Column("stage_key", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("requirements", sa.Text(), nullable=False),
        sa.Column("selected_artifact_ids_json", sa.Text(), nullable=False),
        sa.Column("confirmed_artifact_ids_json", sa.Text(), nullable=False),
        sa.Column("skipped_reason", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("confirmed_by", sa.String(128), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("task_id", "stage_key", name="uq_task_stage"),
    )
    op.create_index("ix_task_stages_task_id", "task_stages", ["task_id"])
    op.create_index("ix_task_stages_stage_key", "task_stages", ["stage_key"])
    op.create_index("ix_task_stages_state", "task_stages", ["state"])

    bind = op.get_bind()
    library_rows = list(
        bind.execute(
            sa.text(
                "SELECT id, owner_id, category, name, text_content, storage_key, data_level, "
                "version, active, created_at, updated_at FROM library_items"
            )
        ).mappings()
    )
    for library in library_rows:
        entry_id = str(uuid4())
        version_id = str(uuid4())
        storage_key = library["storage_key"]
        suffix = (storage_key or "").lower().rsplit(".", 1)[-1]
        if library["category"] == "product":
            resource_kind = "product"
        elif library["category"] == "rule":
            resource_kind = "spec"
        elif suffix in {"png", "jpg", "jpeg", "gif", "webp", "svg"}:
            resource_kind = "image"
        elif suffix in {"mp4", "mov", "webm", "mkv"}:
            resource_kind = "video"
        elif suffix in {"wav", "mp3", "m4a", "aac"}:
            resource_kind = "audio"
        else:
            resource_kind = "document"
        metadata = json.dumps(
            {"legacy_library_item_id": library["id"], "legacy_category": library["category"]},
            ensure_ascii=False,
        )
        bind.execute(
            sa.text(
                "INSERT INTO resource_entries "
                "(id, owner_id, resource_kind, title, category, tags_json, parent_id, "
                "data_level, current_version_id, metadata_json, active, created_at, updated_at) "
                "VALUES (:id, :owner_id, :resource_kind, :title, :category, '[]', NULL, "
                ":data_level, :current_version_id, :metadata_json, :active, "
                ":created_at, :updated_at)"
            ),
            {
                "id": entry_id,
                "owner_id": library["owner_id"],
                "resource_kind": resource_kind,
                "title": library["name"],
                "category": library["category"],
                "data_level": library["data_level"],
                "current_version_id": version_id,
                "metadata_json": metadata,
                "active": library["active"],
                "created_at": library["created_at"],
                "updated_at": library["updated_at"],
            },
        )
        bind.execute(
            sa.text(
                "UPDATE library_items SET resource_entry_id = :entry_id WHERE id = :library_id"
            ),
            {"entry_id": entry_id, "library_id": library["id"]},
        )
        bind.execute(
            sa.text(
                "INSERT INTO resource_versions "
                "(id, entry_id, version, text_content, source_url, crawled_at, freshness, "
                "facts_json, notes, metadata_json, created_by, created_at) "
                "VALUES (:id, :entry_id, :version, :text_content, NULL, NULL, NULL, "
                "'[]', '', :metadata_json, :created_by, :created_at)"
            ),
            {
                "id": version_id,
                "entry_id": entry_id,
                "version": library["version"],
                "text_content": library["text_content"],
                "metadata_json": metadata,
                "created_by": library["owner_id"],
                "created_at": library["created_at"],
            },
        )
        if storage_key:
            bind.execute(
                sa.text(
                    "INSERT INTO asset_refs "
                    "(id, entry_id, version_id, storage_key, media_type, size_bytes, checksum, "
                    "metadata_json, created_at) VALUES (:id, :entry_id, :version_id, "
                    ":storage_key, 'application/octet-stream', NULL, NULL, '{}', :created_at)"
                ),
                {
                    "id": str(uuid4()),
                    "entry_id": entry_id,
                    "version_id": version_id,
                    "storage_key": storage_key,
                    "created_at": library["created_at"],
                },
            )

    task_rows = bind.execute(sa.text("SELECT id, owner_id, mode FROM content_tasks")).mappings()
    artifact_rows = list(
        bind.execute(
            sa.text(
                "SELECT id, task_id, kind, status, content_json, storage_key, version "
                "FROM artifacts ORDER BY version"
            )
        ).mappings()
    )
    artifact_by_task_stage: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for artifact in artifact_rows:
        stage_key = KIND_STAGE.get(artifact["kind"])
        if stage_key is None:
            continue
        bind.execute(
            sa.text("UPDATE artifacts SET stage_key = :stage_key WHERE id = :id"),
            {"stage_key": stage_key, "id": artifact["id"]},
        )
        if _legacy_mock(artifact["content_json"], artifact["storage_key"]):
            bind.execute(
                sa.text("UPDATE artifacts SET status = 'legacy_mock' WHERE id = :id"),
                {"id": artifact["id"]},
            )
            continue
        if artifact["status"] == "discarded":
            continue
        key = (artifact["task_id"], stage_key)
        artifact_by_task_stage.setdefault(key, {}).setdefault(artifact["kind"], []).append(
            dict(artifact)
        )

    now = datetime.now(UTC)
    stage_table = sa.table(
        "task_stages",
        sa.column("id", sa.String),
        sa.column("task_id", sa.String),
        sa.column("stage_key", sa.String),
        sa.column("state", sa.String),
        sa.column("requirements", sa.Text),
        sa.column("selected_artifact_ids_json", sa.Text),
        sa.column("confirmed_artifact_ids_json", sa.Text),
        sa.column("skipped_reason", sa.Text),
        sa.column("version", sa.Integer),
        sa.column("confirmed_by", sa.String),
        sa.column("confirmed_at", sa.DateTime),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    inserts = []
    for task in task_rows:
        applicable = MODE_STAGES.get(task["mode"], MODE_STAGES["full"])
        for stage_key in STAGES:
            grouped = artifact_by_task_stage.get((task["id"], stage_key), {})
            selected = {kind: [values[-1]["id"]] for kind, values in grouped.items() if values}
            confirmed = {
                kind: [values[-1]["id"]]
                for kind, values in grouped.items()
                if values and values[-1]["status"] == "confirmed"
            }
            if stage_key == "configuration":
                state = "confirmed"
            elif stage_key not in applicable:
                state = "skipped"
            elif confirmed and confirmed.keys() == selected.keys():
                state = "confirmed"
            elif selected:
                state = "awaiting_confirmation"
            else:
                state = "not_started"
            inserts.append(
                {
                    "id": str(uuid4()),
                    "task_id": task["id"],
                    "stage_key": stage_key,
                    "state": state,
                    "requirements": "",
                    "selected_artifact_ids_json": json.dumps(selected),
                    "confirmed_artifact_ids_json": json.dumps(confirmed),
                    "skipped_reason": "当前入口不适用" if stage_key not in applicable else None,
                    "version": 1,
                    "confirmed_by": task["owner_id"] if stage_key == "configuration" else None,
                    "confirmed_at": now if stage_key == "configuration" else None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
    if inserts:
        op.bulk_insert(stage_table, inserts)

    bind.execute(
        sa.text(
            "UPDATE content_tasks SET current_stage = CASE current_stage "
            "WHEN 'configured' THEN 'configuration' "
            "WHEN 'topics' THEN 'topic' "
            "WHEN 'images' THEN 'image' "
            "WHEN 'confirmed' THEN 'export' "
            "ELSE current_stage END"
        )
    )


def downgrade() -> None:
    op.drop_table("task_stages")
    op.drop_table("artifact_links")
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_index("ix_artifacts_generation_run_id")
        batch.drop_index("ix_artifacts_stage_key")
        batch.drop_constraint("fk_artifacts_generation_run_id", type_="foreignkey")
        batch.drop_column("candidate_index")
        batch.drop_column("generation_run_id")
        batch.drop_column("stage_key")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_generation_run_id")
        batch.drop_constraint("fk_jobs_generation_run_id", type_="foreignkey")
        batch.drop_column("generation_run_id")
    op.drop_table("generation_runs")
    op.drop_column("content_tasks", "workflow_enforced")
    op.drop_column("content_tasks", "version")
    with op.batch_alter_table("library_items") as batch:
        batch.drop_index("ix_library_items_resource_entry_id")
        batch.drop_constraint("fk_library_items_resource_entry_id", type_="foreignkey")
        batch.drop_column("resource_entry_id")
    op.drop_table("external_refs")
    op.drop_table("asset_refs")
    op.drop_table("resource_chunks")
    op.drop_table("resource_versions")
    op.drop_table("resource_entries")
