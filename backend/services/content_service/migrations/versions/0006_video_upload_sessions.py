"""Add resumable upload sessions and immutable chunk receipts."""

import sqlalchemy as sa
from alembic import op

revision = "content_0006"
down_revision = "content_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_upload_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("video_projects.id"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("media_kind", sa.String(32), nullable=False),
        sa.Column("total_size", sa.BigInteger(), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False),
        sa.Column("total_chunks", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="uploading"),
        sa.Column("object_key", sa.String(512), nullable=True),
        sa.Column(
            "project_asset_id",
            sa.String(36),
            sa.ForeignKey("project_assets.id"),
            nullable=True,
            unique=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("total_size > 0", name="ck_video_upload_total_size"),
        sa.CheckConstraint("chunk_size > 0", name="ck_video_upload_chunk_size"),
        sa.CheckConstraint("total_chunks > 0", name="ck_video_upload_total_chunks"),
        sa.CheckConstraint(
            "status IN ('uploading', 'completed', 'cancelled', 'failed')",
            name="ck_video_upload_status",
        ),
    )
    op.create_index(
        "ix_video_upload_sessions_project_id",
        "video_upload_sessions",
        ["project_id"],
    )
    op.create_index(
        "ix_video_upload_sessions_owner_id",
        "video_upload_sessions",
        ["owner_id"],
    )
    op.create_index(
        "ix_video_upload_sessions_status",
        "video_upload_sessions",
        ["status"],
    )

    op.create_table(
        "video_upload_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "upload_session_id",
            sa.String(36),
            sa.ForeignKey("video_upload_sessions.id"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "upload_session_id",
            "chunk_index",
            name="uq_video_upload_chunk_index",
        ),
        sa.CheckConstraint("chunk_index >= 0", name="ck_video_upload_chunk_index"),
        sa.CheckConstraint(
            "size_bytes > 0",
            name="ck_video_upload_chunk_receipt_size",
        ),
    )
    op.create_index(
        "ix_video_upload_chunks_upload_session_id",
        "video_upload_chunks",
        ["upload_session_id"],
    )


def downgrade() -> None:
    op.drop_table("video_upload_chunks")
    op.drop_table("video_upload_sessions")
