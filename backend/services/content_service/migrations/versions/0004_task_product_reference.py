"""Keep a stable content-library product reference on content tasks."""

import sqlalchemy as sa
from alembic import op

revision = "content_0004"
down_revision = "content_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("content_tasks") as batch:
        batch.add_column(sa.Column("product_resource_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_content_tasks_product_resource_id",
            "resource_entries",
            ["product_resource_id"],
            ["id"],
        )
        batch.create_index(
            "ix_content_tasks_product_resource_id",
            ["product_resource_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("content_tasks") as batch:
        batch.drop_index("ix_content_tasks_product_resource_id")
        batch.drop_constraint(
            "fk_content_tasks_product_resource_id",
            type_="foreignkey",
        )
        batch.drop_column("product_resource_id")
