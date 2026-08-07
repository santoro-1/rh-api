"""add independent semantic visual analysis cache

Revision ID: 0026_visual_analysis_cache
Revises: 0025_runninghub_execution_pool
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0026_visual_analysis_cache"
down_revision = "0025_runninghub_execution_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "visual_analysis_caches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("script_sha256", sa.String(length=64), nullable=False),
        sa.Column("script_length", sa.Integer(), nullable=False),
        sa.Column("catalog_version", sa.String(length=128), nullable=False),
        sa.Column("candidate_set_sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("provider_request_id", sa.String(length=200), nullable=True),
        sa.Column("provider_attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "script_sha256",
            "catalog_version",
            "candidate_set_sha256",
            "schema_version",
            "prompt_version",
            "model",
            name="uq_visual_analysis_cache_key",
        ),
    )
    op.create_index(
        "ix_visual_analysis_caches_user_id",
        "visual_analysis_caches",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_visual_analysis_caches_user_id",
        table_name="visual_analysis_caches",
    )
    op.drop_table("visual_analysis_caches")
