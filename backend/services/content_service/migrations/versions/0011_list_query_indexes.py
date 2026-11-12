"""Add indexes used by paginated list queries."""

from alembic import op

revision = "content_0011"
down_revision = "content_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_library_items_updated_at", "library_items", ["updated_at"])
    op.create_index("ix_content_tasks_updated_at", "content_tasks", ["updated_at"])
    op.create_index("ix_generation_runs_created_at", "generation_runs", ["created_at"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_generation_runs_created_at", table_name="generation_runs")
    op.drop_index("ix_content_tasks_updated_at", table_name="content_tasks")
    op.drop_index("ix_library_items_updated_at", table_name="library_items")
