"""add immutable task-level SeedVR2 switch

Revision ID: 0033_generation_task_seedvr2_switch
Revises: 0032_runninghub_pool_runtime_control
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa


revision = "0033_generation_task_seedvr2_switch"
down_revision = "0032_runninghub_pool_runtime_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_tasks",
        sa.Column(
            "seedvr2_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("generation_tasks", "seedvr2_enabled")
