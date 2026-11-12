"""Persist WeKnora ingestion, parsing and remote cleanup work."""

import sqlalchemy as sa
from alembic import op

revision = "content_0015"
down_revision = "content_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weknora_sync_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entry_id", sa.String(36), sa.ForeignKey("resource_entries.id"), nullable=False),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("resource_versions.id"), nullable=False),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("knowledge_base_id", sa.String(128), nullable=False),
        sa.Column("remote_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("operation", sa.String(16), nullable=False, server_default="index"),
        sa.Column("upload_attempted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("delete_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("connection_id", "version_id", name="uq_weknora_sync_version"),
    )
    for name in ("entry_id", "version_id", "connection_id", "status", "available_at"):
        op.create_index(f"ix_weknora_sync_jobs_{name}", "weknora_sync_jobs", [name])


def downgrade() -> None:
    op.drop_table("weknora_sync_jobs")
