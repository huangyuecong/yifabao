"""Retain verified remote ownership when local uploads share original bytes."""

import sqlalchemy as sa
from alembic import op

revision = "content_0018"
down_revision = "content_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL preserves the original source_marker behavior for existing rows.
    op.add_column("original_archives", sa.Column(
        "remote_source_marker", sa.String(255), nullable=True,
    ))


def downgrade() -> None:
    # Removing a shared marker would make its sole remote original unreadable.
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM original_archives "
        "WHERE remote_source_marker IS NOT NULL AND remote_source_marker <> source_marker"
    )).scalar()
    if count:
        raise RuntimeError("已有共享原件托管记录，需先恢复并迁出原件才能降级")
    op.drop_column("original_archives", "remote_source_marker")
