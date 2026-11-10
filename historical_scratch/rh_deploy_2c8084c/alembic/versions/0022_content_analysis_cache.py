"""add validated whole-script content analysis cache

Revision ID: 0022_content_analysis_cache
Revises: 0021_ark_configs
"""

from alembic import op
import sqlalchemy as sa


revision = "0022_content_analysis_cache"
down_revision = "0021_ark_configs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_analysis_caches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("script_sha256", sa.String(length=64), nullable=False),
        sa.Column("script_length", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("overall_status", sa.String(length=20), nullable=False),
        sa.Column("music_analysis_status", sa.String(length=20), nullable=False),
        sa.Column("subtitle_analysis_status", sa.String(length=20), nullable=False),
        sa.Column("music_intent_json", sa.Text(), nullable=True),
        sa.Column("subtitle_units_json", sa.Text(), nullable=True),
        sa.Column("music_error_code", sa.String(length=100), nullable=True),
        sa.Column("music_error_summary", sa.String(length=500), nullable=True),
        sa.Column("subtitle_error_code", sa.String(length=100), nullable=True),
        sa.Column("subtitle_error_summary", sa.String(length=500), nullable=True),
        sa.Column("provider_request_id", sa.String(length=200), nullable=True),
        sa.Column("provider_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cacheable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "script_sha256",
            "schema_version",
            "prompt_version",
            "model",
            name="uq_content_analysis_cache_key",
        ),
    )
    op.create_index(
        "ix_content_analysis_caches_user_id",
        "content_analysis_caches",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_content_analysis_caches_user_id",
        table_name="content_analysis_caches",
    )
    op.drop_table("content_analysis_caches")
