"""add workflow-specific H3 account capabilities

Revision ID: 0038_runninghub_h3_capability
Revises: 0037_multi_camera_web
Create Date: 2026-08-22
"""

from alembic import op
import sqlalchemy as sa


revision = "0038_runninghub_h3_capability"
down_revision = "0037_multi_camera_web"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runninghub_h3_capabilities",
        sa.Column("execution_account_id", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("workflow_id", sa.String(length=100), nullable=False),
        sa.Column("instance_type", sa.String(length=20), nullable=False),
        sa.Column("max_concurrent_tasks", sa.Integer(), nullable=False),
        sa.Column("safe_note", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "instance_type IN ('default', 'plus')",
            name="ck_runninghub_h3_capability_instance_type",
        ),
        sa.CheckConstraint(
            "max_concurrent_tasks >= 1 AND max_concurrent_tasks <= 5",
            name="ck_runninghub_h3_capability_concurrency",
        ),
        sa.ForeignKeyConstraint(
            ["execution_account_id"],
            ["runninghub_execution_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("execution_account_id"),
    )


def downgrade() -> None:
    op.drop_table("runninghub_h3_capabilities")

