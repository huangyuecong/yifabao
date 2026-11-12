"""AI 个性化内容智能体初始数据库基线。"""

import sqlalchemy as sa
from alembic import op

revision = "content_0001"
down_revision = None
branch_labels = ("content_service",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "library_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=True),
        sa.Column("data_level", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_library_items_owner_id", "library_items", ["owner_id"])
    op.create_index("ix_library_items_category", "library_items", ["category"])

    op.create_table(
        "sensitive_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("phrase", sa.String(255), nullable=False),
        sa.Column("level", sa.String(32), nullable=False),
        sa.Column("replacement", sa.String(255), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.UniqueConstraint("phrase", name="uq_sensitive_rules_phrase"),
    )
    op.create_index("ix_sensitive_rules_phrase", "sensitive_rules", ["phrase"])

    op.create_table(
        "content_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("topic", sa.String(255), nullable=False),
        sa.Column("product", sa.String(255), nullable=False),
        sa.Column("keywords_json", sa.Text(), nullable=False),
        sa.Column("audience", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("style", sa.String(64), nullable=False),
        sa.Column("length", sa.String(32), nullable=False),
        sa.Column("requirements", sa.Text(), nullable=False),
        sa.Column("current_stage", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("allowed_users_json", sa.Text(), nullable=False),
        sa.Column("allowed_departments_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_content_tasks_owner_id", "content_tasks", ["owner_id"])

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("content_tasks.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=True),
        sa.Column("parent_id", sa.String(36), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_artifacts_task_id", "artifacts", ["task_id"])
    op.create_index("ix_artifacts_kind", "artifacts", ["kind"])

    op.create_table(
        "egress_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("content_tasks.id"), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("allowed_data_levels_json", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_egress_grants_task_id", "egress_grants", ["task_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_jobs_task_id", "jobs", ["task_id"])
    op.create_index("ix_jobs_owner_id", "jobs", ["owner_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_logs_owner_id", "audit_logs", ["owner_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_resource_id", "audit_logs", ["resource_id"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("jobs")
    op.drop_table("egress_grants")
    op.drop_table("artifacts")
    op.drop_table("content_tasks")
    op.drop_table("sensitive_rules")
    op.drop_table("library_items")
