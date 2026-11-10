"""separate legacy web batches from new workbench batches

Revision ID: 0020_batch_source_channel
Revises: 0019_deferred_audio_primary
"""

from alembic import op
import sqlalchemy as sa


revision = "0020_batch_source_channel"
down_revision = "0019_deferred_audio_primary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every row that predates this migration came from the legacy website.
    # Native ADD COLUMN preserves SQLite child rows and gives existing data an
    # explicit, deterministic source instead of guessing from request keys.
    op.add_column(
        "generation_batches",
        sa.Column(
            "source_channel",
            sa.String(length=30),
            nullable=False,
            server_default="legacy_web",
        ),
    )
    op.create_index(
        "ix_generation_batches_source_channel",
        "generation_batches",
        ["source_channel"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_generation_batches_source_channel",
        table_name="generation_batches",
    )
    op.drop_column("generation_batches", "source_channel")
