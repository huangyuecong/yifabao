"""Track original-file custody independently of disposable knowledge indexes."""

import sqlalchemy as sa
from alembic import op

revision = "content_0017"
down_revision = "content_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "original_archives",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("entry_id", sa.String(36), sa.ForeignKey("resource_entries.id"), nullable=False),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("knowledge_base_id", sa.String(128), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_marker", sa.String(255), nullable=False),
        sa.Column("remote_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("upload_attempted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("connection_id", "storage_key", name="uq_original_archive_key"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_original_archive_size"),
    )
    for field in ("storage_key", "entry_id", "connection_id", "status", "available_at"):
        op.create_index(f"ix_original_archives_{field}", "original_archives", [field])


def downgrade() -> None:
    # Dropping custody records could strand the sole remote original after a
    # verified cache purge. An automatic downgrade must never do that.
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT COUNT(*) FROM original_archives")).scalar()
    if count:
        raise RuntimeError("原件托管记录非空，需先恢复并迁出原件才能降级")
    op.drop_table("original_archives")
