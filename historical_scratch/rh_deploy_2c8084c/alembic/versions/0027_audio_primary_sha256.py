"""bind workbench composition to the selected image content

Revision ID: 0027_audio_primary_sha256
Revises: 0026_visual_analysis_cache
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0027_audio_primary_sha256"
down_revision = "0026_visual_analysis_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audio_generation_tasks",
        sa.Column("primary_sha256", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audio_generation_tasks", "primary_sha256")
