"""Add immutable video-editor projects, assets, derivatives, and AI proposals."""

import sqlalchemy as sa
from alembic import op

revision = "content_0005"
down_revision = "content_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("content_tasks.id"),
            nullable=False,
        ),
        sa.Column("current_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("current_revision_id", sa.String(36), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("task_id", name="uq_video_projects_task_id"),
    )
    op.create_index("ix_video_projects_task_id", "video_projects", ["task_id"])
    op.create_index(
        "ix_video_projects_current_revision_id",
        "video_projects",
        ["current_revision_id"],
    )

    op.create_table(
        "video_project_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("video_projects.id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column(
            "parent_revision_id",
            sa.String(36),
            sa.ForeignKey("video_project_revisions.id"),
            nullable=True,
        ),
        sa.Column("source", sa.String(32), nullable=False, server_default="human"),
        sa.Column("timeline_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("operations_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "revision",
            name="uq_video_project_committed_revision",
        ),
    )
    op.create_index(
        "ix_video_project_revisions_project_id",
        "video_project_revisions",
        ["project_id"],
    )
    op.create_index(
        "ix_video_project_revisions_parent_revision_id",
        "video_project_revisions",
        ["parent_revision_id"],
    )

    op.create_table(
        "project_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("video_projects.id"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column(
            "asset_ref_id",
            sa.String(36),
            sa.ForeignKey("asset_refs.id"),
            nullable=True,
        ),
        sa.Column(
            "artifact_id",
            sa.String(36),
            sa.ForeignKey("artifacts.id"),
            nullable=True,
        ),
        sa.Column(
            "library_item_id",
            sa.String(36),
            sa.ForeignKey("library_items.id"),
            nullable=True,
        ),
        sa.Column(
            "resource_entry_id",
            sa.String(36),
            sa.ForeignKey("resource_entries.id"),
            nullable=True,
        ),
        sa.Column("role", sa.String(32), nullable=False, server_default="media"),
        sa.Column("label", sa.String(255), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "source_type",
            "source_id",
            name="uq_video_project_asset_source",
        ),
    )
    for column in (
        "project_id",
        "source_type",
        "source_id",
        "asset_ref_id",
        "artifact_id",
        "library_item_id",
        "resource_entry_id",
    ):
        op.create_index(f"ix_project_assets_{column}", "project_assets", [column])

    op.create_table(
        "media_derivatives",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_asset_id",
            sa.String(36),
            sa.ForeignKey("project_assets.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("variant", sa.String(64), nullable=False, server_default="default"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("storage_key", sa.String(512), nullable=True),
        sa.Column("media_type", sa.String(128), nullable=True),
        sa.Column("checksum", sa.String(128), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "project_asset_id",
            "kind",
            "variant",
            name="uq_media_derivative_variant",
        ),
    )
    op.create_index(
        "ix_media_derivatives_project_asset_id",
        "media_derivatives",
        ["project_asset_id"],
    )
    op.create_index("ix_media_derivatives_kind", "media_derivatives", ["kind"])
    op.create_index("ix_media_derivatives_status", "media_derivatives", ["status"])

    op.create_table(
        "ai_edit_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("video_projects.id"),
            nullable=False,
        ),
        sa.Column(
            "base_revision_id",
            sa.String(36),
            sa.ForeignKey("video_project_revisions.id"),
            nullable=False,
        ),
        sa.Column(
            "candidate_revision_id",
            sa.String(36),
            sa.ForeignKey("video_project_revisions.id"),
            nullable=False,
        ),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("operations_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column(
            "selected_operation_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("resolved_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    for column in (
        "project_id",
        "base_revision_id",
        "candidate_revision_id",
        "status",
    ):
        op.create_index(
            f"ix_ai_edit_proposals_{column}",
            "ai_edit_proposals",
            [column],
        )


def downgrade() -> None:
    op.drop_table("ai_edit_proposals")
    op.drop_table("media_derivatives")
    op.drop_table("project_assets")
    op.drop_table("video_project_revisions")
    op.drop_table("video_projects")
