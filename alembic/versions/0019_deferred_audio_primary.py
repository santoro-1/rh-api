"""allow workbench audio tasks to bind their picture at composition time

Revision ID: 0019_deferred_audio_primary
Revises: 0018_segment_video_merge
"""

from alembic import op
import sqlalchemy as sa


revision = "0019_deferred_audio_primary"
down_revision = "0018_segment_video_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("audio_generation_tasks") as batch_op:
        batch_op.alter_column(
            "primary_kind",
            existing_type=sa.String(length=20),
            nullable=True,
        )
        batch_op.alter_column(
            "primary_path",
            existing_type=sa.String(length=500),
            nullable=True,
        )
        batch_op.alter_column(
            "primary_original_name",
            existing_type=sa.String(length=255),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("audio_generation_tasks") as batch_op:
        batch_op.alter_column(
            "primary_original_name",
            existing_type=sa.String(length=255),
            nullable=False,
        )
        batch_op.alter_column(
            "primary_path",
            existing_type=sa.String(length=500),
            nullable=False,
        )
        batch_op.alter_column(
            "primary_kind",
            existing_type=sa.String(length=20),
            nullable=False,
        )
