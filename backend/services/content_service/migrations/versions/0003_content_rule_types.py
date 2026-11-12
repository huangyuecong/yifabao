"""Distinguish keyword, sensitive-word and forbidden-word rules."""

import sqlalchemy as sa
from alembic import op

revision = "content_0003"
down_revision = "content_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sensitive_rules") as batch:
        batch.add_column(
            sa.Column(
                "rule_type",
                sa.String(32),
                nullable=False,
                server_default="sensitive",
            )
        )
    op.execute(
        sa.text("UPDATE sensitive_rules SET rule_type = 'forbidden' WHERE level = 'forbidden'")
    )


def downgrade() -> None:
    with op.batch_alter_table("sensitive_rules") as batch:
        batch.drop_column("rule_type")
