"""add unified LTX workbench preparation jobs

Revision ID: 0036_ltx_workbench_preparation
Revises: 0035_workbench_item_execution_pool
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa


revision = "0036_ltx_workbench_preparation"
down_revision = "0035_workbench_item_execution_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ltx_preparation_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("batch_item_id", sa.String(length=36), nullable=False),
        sa.Column("long_audio_project_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("source_video_path", sa.String(length=500), nullable=False),
        sa.Column("source_video_original_name", sa.String(length=255), nullable=False),
        sa.Column("source_video_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_audio_path", sa.String(length=500), nullable=False),
        sa.Column("source_audio_original_name", sa.String(length=255), nullable=False),
        sa.Column("source_audio_sha256", sa.String(length=64), nullable=False),
        sa.Column("script_text", sa.Text(), nullable=False),
        sa.Column("script_sha256", sa.String(length=64), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("video_duration_seconds", sa.Float(), nullable=False),
        sa.Column("alignment_provider", sa.String(length=50), nullable=True),
        sa.Column("alignment_score", sa.Float(), nullable=True),
        sa.Column("alignment_timeline_json", sa.Text(), nullable=True),
        sa.Column("segment_plan_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["batch_item_id"], ["generation_batch_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["long_audio_project_id"], ["long_audio_projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_item_id"),
        sa.UniqueConstraint("long_audio_project_id"),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_ltx_preparation_user_idempotency",
        ),
    )
    op.create_index(
        op.f("ix_ltx_preparation_jobs_user_id"),
        "ltx_preparation_jobs",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ltx_preparation_jobs_batch_item_id"),
        "ltx_preparation_jobs",
        ["batch_item_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_ltx_preparation_jobs_long_audio_project_id"),
        "ltx_preparation_jobs",
        ["long_audio_project_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_ltx_preparation_jobs_status"),
        "ltx_preparation_jobs",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_ltx_preparation_jobs_status"),
        table_name="ltx_preparation_jobs",
    )
    op.drop_index(
        op.f("ix_ltx_preparation_jobs_long_audio_project_id"),
        table_name="ltx_preparation_jobs",
    )
    op.drop_index(
        op.f("ix_ltx_preparation_jobs_batch_item_id"),
        table_name="ltx_preparation_jobs",
    )
    op.drop_index(
        op.f("ix_ltx_preparation_jobs_user_id"),
        table_name="ltx_preparation_jobs",
    )
    op.drop_table("ltx_preparation_jobs")
