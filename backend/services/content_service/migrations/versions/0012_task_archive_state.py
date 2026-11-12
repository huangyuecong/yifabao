"""Keep task workflow state separate from its archive state."""

import sqlalchemy as sa
from alembic import op

revision = "content_0012"
down_revision = "content_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("content_tasks") as batch:
        batch.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("archived_workflow_status", sa.String(32), nullable=True))
        batch.create_index("ix_content_tasks_archived_at", ["archived_at"])

    # Older rows only stored ``status=archived``. Preserve their historical
    # meaning using the export stage before the API starts exposing the two
    # dimensions independently.
    op.execute(
        sa.text(
            "UPDATE content_tasks SET archived_at = updated_at "
            "WHERE status = 'archived' AND archived_at IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE content_tasks SET archived_workflow_status = CASE "
            "WHEN EXISTS (SELECT 1 FROM task_stages "
            "WHERE task_stages.task_id = content_tasks.id "
            "AND task_stages.stage_key = 'export' "
            "AND task_stages.state = 'confirmed') THEN 'confirmed' ELSE 'draft' END "
            "WHERE status = 'archived' AND archived_workflow_status IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("content_tasks") as batch:
        batch.drop_index("ix_content_tasks_archived_at")
        batch.drop_column("archived_workflow_status")
        batch.drop_column("archived_at")
