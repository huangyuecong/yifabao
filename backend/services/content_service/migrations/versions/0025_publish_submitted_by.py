"""Online trial: which trial account submitted a publish record."""

import sqlalchemy as sa
from alembic import op

revision = "content_0025"
down_revision = "content_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("publish_records") as batch:
        batch.add_column(sa.Column("submitted_by", sa.String(64), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("publish_records") as batch:
        batch.drop_column("submitted_by")
