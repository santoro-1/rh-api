"""Add frozen manual H3 prompt override.

Revision ID: 0047_h3_prompt_override
Revises: 0046_h3_remote_asr_jobs
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0047_h3_prompt_override"
down_revision = "0046_h3_remote_asr_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "h3_batch_configs",
        sa.Column("prompt_override", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("h3_batch_configs", "prompt_override")
