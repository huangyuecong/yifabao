"""Add optional tenant/department/role scope to knowledge resources."""

import sqlalchemy as sa
from alembic import op

revision = "content_0010"
down_revision = "content_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resource_entries", sa.Column("tenant_id", sa.String(128), nullable=True))
    op.add_column("resource_entries", sa.Column("department_id", sa.String(128), nullable=True))
    op.add_column(
        "resource_entries",
        sa.Column("allowed_roles_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_index("ix_resource_entries_tenant_id", "resource_entries", ["tenant_id"])
    op.create_index("ix_resource_entries_department_id", "resource_entries", ["department_id"])


def downgrade() -> None:
    op.drop_index("ix_resource_entries_department_id", table_name="resource_entries")
    op.drop_index("ix_resource_entries_tenant_id", table_name="resource_entries")
    op.drop_column("resource_entries", "allowed_roles_json")
    op.drop_column("resource_entries", "department_id")
    op.drop_column("resource_entries", "tenant_id")
