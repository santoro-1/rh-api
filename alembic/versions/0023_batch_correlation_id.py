"""add durable batch correlation id

Revision ID: 0023_batch_correlation_id
Revises: 0022_content_analysis_cache
"""

from alembic import op
import sqlalchemy as sa


revision = "0023_batch_correlation_id"
down_revision = "0022_content_analysis_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_batches",
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
    )
    # 历史批次没有独立关联号，用稳定的批次 ID 回填，确保上线后立即可检索。
    op.execute(
        "UPDATE generation_batches SET correlation_id = id "
        "WHERE correlation_id IS NULL OR correlation_id = ''"
    )
    op.create_index(
        "ix_generation_batches_correlation_id",
        "generation_batches",
        ["correlation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_generation_batches_correlation_id",
        table_name="generation_batches",
    )
    op.drop_column("generation_batches", "correlation_id")
