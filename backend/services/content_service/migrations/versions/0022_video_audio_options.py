"""Narration and background-music options for generated product videos."""

import sqlalchemy as sa
from alembic import op

revision = "content_0022"
down_revision = "content_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_batches") as batch:
        batch.add_column(sa.Column("video_voice", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("video_music", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("generation_items") as batch:
        batch.add_column(sa.Column("video_note", sa.String(300), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("generation_items") as batch:
        batch.drop_column("video_note")
    with op.batch_alter_table("generation_batches") as batch:
        batch.drop_column("video_music")
        batch.drop_column("video_voice")
