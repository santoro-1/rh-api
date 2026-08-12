"""add web-managed RunningHub pool runtime control

Revision ID: 0032_runninghub_pool_runtime_control
Revises: 0031_runninghub_dual_pool_foundation
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa


revision = "0032_runninghub_pool_runtime_control"
down_revision = "0031_runninghub_dual_pool_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runninghub_pool_runtime_controls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dual_pool_enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_runninghub_pool_runtime_singleton"),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("runninghub_pool_runtime_controls")
