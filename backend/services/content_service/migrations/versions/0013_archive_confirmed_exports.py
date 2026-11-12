"""Archive tasks whose final export stage was already confirmed."""

import sqlalchemy as sa
from alembic import op

revision = "content_0013"
down_revision = "content_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Before final-stage auto-archiving was introduced, completed tasks were
    # left as ``status=confirmed``. Promote those rows to the same immutable
    # archive representation without changing their legacy status value.
    op.execute(
        sa.text(
            "UPDATE content_tasks SET archived_at = updated_at, "
            "archived_workflow_status = 'confirmed' "
            "WHERE status = 'confirmed' AND archived_at IS NULL "
            "AND EXISTS (SELECT 1 FROM task_stages "
            "WHERE task_stages.task_id = content_tasks.id "
            "AND task_stages.stage_key = 'export' "
            "AND task_stages.state = 'confirmed')"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE content_tasks SET archived_at = NULL, "
            "archived_workflow_status = NULL "
            "WHERE status = 'confirmed' AND archived_workflow_status = 'confirmed'"
        )
    )
