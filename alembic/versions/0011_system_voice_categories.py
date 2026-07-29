"""Add a display category for provider system voices."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0011_system_voice_categories"
down_revision = "0010_audio_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite supports ADD COLUMN directly. Do not rebuild this referenced table.
    # SQLite DDL is non-transactional: if startup is interrupted immediately
    # after ADD COLUMN but before Alembic records the revision, the column
    # remains while alembic_version still points to 0010. Make this migration
    # safe to resume instead of failing every later launcher attempt.
    columns = {
        column["name"]
        for column in inspect(op.get_bind()).get_columns(
            "minimax_voice_assets"
        )
    }
    if "category" not in columns:
        op.add_column(
            "minimax_voice_assets",
            sa.Column("category", sa.String(length=50), nullable=True),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in inspect(op.get_bind()).get_columns(
            "minimax_voice_assets"
        )
    }
    if "category" in columns:
        op.drop_column("minimax_voice_assets", "category")
