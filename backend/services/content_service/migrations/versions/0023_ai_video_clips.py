"""AI motion clips (Seedance image-to-video) as a video option for generation batches."""

import sqlalchemy as sa
from alembic import op

revision = "content_0023"
down_revision = "content_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_batches") as batch:
        batch.add_column(sa.Column("video_mode", sa.String(16), nullable=False, server_default="slideshow"))
        batch.add_column(sa.Column("ai_clip_count", sa.Integer(), nullable=False, server_default="3"))
    with op.batch_alter_table("generation_items") as batch:
        batch.add_column(sa.Column("video_model", sa.String(128), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("generation_items") as batch:
        batch.drop_column("video_model")
    with op.batch_alter_table("generation_batches") as batch:
        batch.drop_column("ai_clip_count")
        batch.drop_column("video_mode")
