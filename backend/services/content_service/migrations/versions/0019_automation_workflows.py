"""Persist owner-scoped content publishing workflows and execution receipts."""

import sqlalchemy as sa
from alembic import op

revision = "content_0019"
down_revision = "content_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_workflows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_automation_workflows_owner_id", "automation_workflows", ["owner_id"])
    op.create_table(
        "automation_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workflow_id", sa.String(36), sa.ForeignKey("automation_workflows.id"), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("platform_results_json", sa.Text(), nullable=False),
        sa.Column("error", sa.String(500), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("publish_requested", sa.Boolean(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_automation_run_request"),
    )
    for column in ("workflow_id", "owner_id", "status"):
        op.create_index("ix_automation_runs_" + column, "automation_runs", [column])
    op.create_table(
        "automation_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("automation_runs.id"), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("platform", sa.String(24), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_automation_events_run_id", "automation_events", ["run_id"])


def downgrade() -> None:
    op.drop_table("automation_events")
    op.drop_table("automation_runs")
    op.drop_table("automation_workflows")
