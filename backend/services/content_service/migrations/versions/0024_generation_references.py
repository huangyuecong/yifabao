"""Knowledge references retrieved for batch copy generation (RAG)."""

import sqlalchemy as sa
from alembic import op

revision = "content_0024"
down_revision = "content_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_items") as batch:
        batch.add_column(sa.Column("references_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("generation_items") as batch:
        batch.drop_column("references_json")
