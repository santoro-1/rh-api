"""move H3 entitlement to user management

Revision ID: 0041_user_h3_access
Revises: 0040_h3_access_password
Create Date: 2026-08-23
"""

from alembic import op
import sqlalchemy as sa


revision = "0041_user_h3_access"
down_revision = "0040_h3_access_password"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "h3_access_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Preserve users who already had an enabled H3 account membership.
    op.execute(
        """
        UPDATE users
        SET h3_access_enabled = 1
        WHERE id IN (
            SELECT DISTINCT membership.admin_user_id
            FROM runninghub_pool_memberships AS membership
            JOIN runninghub_h3_capabilities AS capability
              ON capability.execution_account_id = membership.execution_account_id
            WHERE capability.is_enabled = 1
        )
        """
    )


def downgrade() -> None:
    op.drop_column("users", "h3_access_enabled")
