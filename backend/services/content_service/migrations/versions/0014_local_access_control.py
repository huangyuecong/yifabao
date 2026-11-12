"""Register signed users and store agent-owned business permissions."""

import sqlalchemy as sa
from alembic import op

revision = "content_0014"
down_revision = "content_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("local_access_lock", sa.Column("id", sa.Integer(), primary_key=True))
    op.execute(sa.text("INSERT INTO local_access_lock (id) VALUES (1)"))
    op.create_table(
        "local_users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("department", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "is_project_admin",
            sa.Boolean(create_constraint=True),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("permissions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_local_user_revision"),
    )
    op.create_index("ix_local_users_subject", "local_users", ["subject"], unique=True)
    op.create_index("ix_local_users_last_seen_at", "local_users", ["last_seen_at"])


def downgrade() -> None:
    op.drop_table("local_users")
    op.drop_table("local_access_lock")
