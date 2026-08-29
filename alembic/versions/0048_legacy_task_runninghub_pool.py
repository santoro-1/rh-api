"""Freeze RunningHub execution-account candidates on standalone tasks.

Revision ID: 0048_legacy_task_runninghub_pool
Revises: 0047_h3_prompt_override
Create Date: 2026-08-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0048_legacy_task_runninghub_pool"
down_revision = "0047_h3_prompt_override"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_tasks",
        sa.Column("runninghub_execution_account_ids_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("generation_tasks", "runninghub_execution_account_ids_json")
