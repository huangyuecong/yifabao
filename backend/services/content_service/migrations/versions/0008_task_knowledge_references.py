"""Add task-level multi-resource knowledge references and normalized length values."""

import json

import sqlalchemy as sa
from alembic import op

revision = "content_0008"
down_revision = "content_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("content_tasks") as batch:
        batch.add_column(
            sa.Column(
                "knowledge_resource_ids_json",
                sa.Text(),
                nullable=False,
                server_default="[]",
            )
        )
    connection = op.get_bind()
    legacy_products = connection.execute(
        sa.text(
            "SELECT id, product_resource_id FROM content_tasks "
            "WHERE product_resource_id IS NOT NULL"
        )
    ).mappings()
    for row in legacy_products:
        connection.execute(
            sa.text(
                "UPDATE content_tasks SET knowledge_resource_ids_json = :resource_ids "
                "WHERE id = :task_id"
            ),
            {
                "resource_ids": json.dumps([row["product_resource_id"]]),
                "task_id": row["id"],
            },
        )
    op.execute(
        sa.text("UPDATE content_tasks SET length = '中' WHERE length IN ('中等', '中篇', 'medium')")
    )
    op.execute(sa.text("UPDATE content_tasks SET length = '短' WHERE length = '短篇'"))
    op.execute(sa.text("UPDATE content_tasks SET length = '长' WHERE length = '长篇'"))
    op.execute(
        sa.text("UPDATE content_tasks SET length = '中' WHERE length NOT IN ('短', '中', '长')")
    )


def downgrade() -> None:
    op.execute(sa.text("UPDATE content_tasks SET length = '中等' WHERE length = '中'"))
    with op.batch_alter_table("content_tasks") as batch:
        batch.drop_column("knowledge_resource_ids_json")
