"""Keep deleted default rules deleted across application restarts."""

import sqlalchemy as sa
from alembic import op

revision = "content_0016"
down_revision = "content_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "rule_seed_state",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("completed", sa.Boolean(), nullable=False),
    )
    connection = op.get_bind()
    rules = sa.table("sensitive_rules", sa.column("id"))
    logs = sa.table("audit_logs", sa.column("action"))
    # An established rule catalog must never be replenished during an upgrade.
    established = (
        connection.scalar(sa.select(sa.func.count()).select_from(rules)) > 0
        or connection.scalar(
            sa.select(sa.func.count()).select_from(logs)
            .where(logs.c.action.in_([
                "sensitive_rule.created", "sensitive_rule.updated", "sensitive_rule.deleted",
            ]))
        ) > 0
    )
    op.bulk_insert(table, [{"id": "advertising-law-v1", "completed": established}])


def downgrade() -> None:
    op.drop_table("rule_seed_state")
