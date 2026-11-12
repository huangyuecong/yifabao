"""Remember which image model a generation batch prefers."""

import sqlalchemy as sa
from alembic import op

revision = "content_0021"
down_revision = "content_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_batches") as batch:
        batch.add_column(sa.Column("image_model", sa.String(128), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("generation_batches") as batch:
        batch.drop_column("image_model")
