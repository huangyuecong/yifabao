"""Add durable content media job leases and retry fencing."""

import sqlalchemy as sa
from alembic import op

revision = "content_0007"
down_revision = "content_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(128), nullable=True))
        batch.add_column(
            sa.Column(
                "attempt",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "max_attempts",
                sa.Integer(),
                nullable=False,
                server_default="3",
            )
        )
        batch.add_column(sa.Column("lease_owner", sa.String(128), nullable=True))
        batch.add_column(sa.Column("lease_token", sa.String(64), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("cancel_requested_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("available_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("completed_at", sa.DateTime(), nullable=True))

    op.execute(sa.text("UPDATE jobs SET available_at = created_at WHERE available_at IS NULL"))

    with op.batch_alter_table("jobs") as batch:
        batch.alter_column("available_at", existing_type=sa.DateTime(), nullable=False)
        batch.create_unique_constraint(
            "uq_content_job_idempotency",
            ["task_id", "kind", "idempotency_key"],
        )
        batch.create_check_constraint("ck_content_job_attempt", "attempt >= 0")
        batch.create_check_constraint(
            "ck_content_job_max_attempts",
            "max_attempts > 0",
        )
        batch.create_index("ix_jobs_status", ["status"])
        batch.create_index("ix_jobs_available_at", ["available_at"])
        batch.create_index("ix_jobs_lease_expires_at", ["lease_expires_at"])


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_lease_expires_at")
        batch.drop_index("ix_jobs_available_at")
        batch.drop_index("ix_jobs_status")
        batch.drop_constraint("ck_content_job_max_attempts", type_="check")
        batch.drop_constraint("ck_content_job_attempt", type_="check")
        batch.drop_constraint("uq_content_job_idempotency", type_="unique")
        batch.drop_column("completed_at")
        batch.drop_column("available_at")
        batch.drop_column("cancel_requested_at")
        batch.drop_column("heartbeat_at")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_token")
        batch.drop_column("lease_owner")
        batch.drop_column("max_attempts")
        batch.drop_column("attempt")
        batch.drop_column("idempotency_key")
