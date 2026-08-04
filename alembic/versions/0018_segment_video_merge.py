"""add automatic segment video merge and optional final review

Revision ID: 0018_segment_video_merge
Revises: 0017_batch_long_audio
"""

from alembic import op
import sqlalchemy as sa


revision = "0018_segment_video_merge"
down_revision = "0017_batch_long_audio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These parent tables both have dependent rows. Native ADD COLUMN keeps the
    # existing SQLite tables in place and avoids cascade loss during migration.
    op.add_column(
        "generation_batches",
        sa.Column(
            "video_review_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "generation_batch_items",
        sa.Column(
            "merged_video_status",
            sa.String(length=30),
            nullable=False,
            server_default="NOT_APPLICABLE",
        ),
    )
    op.add_column(
        "generation_batch_items",
        sa.Column("merged_video_path", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "generation_batch_items",
        sa.Column("merged_video_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "generation_batch_items",
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "generation_batch_items",
        sa.Column("merged_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE generation_batch_items
        SET merged_video_status = 'MERGE_PENDING'
        WHERE EXISTS (
            SELECT 1
            FROM generation_segments
            WHERE generation_segments.batch_item_id = generation_batch_items.id
        )
        """
    )


def downgrade() -> None:
    op.drop_column("generation_batch_items", "merged_reviewed_at")
    op.drop_column("generation_batch_items", "merged_at")
    op.drop_column("generation_batch_items", "merged_video_error")
    op.drop_column("generation_batch_items", "merged_video_path")
    op.drop_column("generation_batch_items", "merged_video_status")
    op.drop_column("generation_batches", "video_review_required")
