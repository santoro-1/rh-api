"""add durable SeedVR2 video enhancement stage

Revision ID: 0029_seedvr2_video_enhancement
Revises: 0028_unified_content_visual_plan
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa


revision = "0029_seedvr2_video_enhancement"
down_revision = "0028_unified_content_visual_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generation_task_enhancements",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("generation_task_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("workflow_kind", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("source_result_path", sa.String(length=500), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=True),
        sa.Column("source_size", sa.Integer(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column("source_output_metadata_json", sa.Text(), nullable=True),
        sa.Column("remote_task_id", sa.String(length=100), nullable=True),
        sa.Column("execution_account_id", sa.Integer(), nullable=True),
        sa.Column("result_path", sa.String(length=500), nullable=True),
        sa.Column("result_filename", sa.String(length=255), nullable=True),
        sa.Column("result_size", sa.Integer(), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("output_metadata_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("failed_reason_json", sa.Text(), nullable=True),
        sa.Column("usage_json", sa.Text(), nullable=True),
        sa.Column("auto_retry_count", sa.Integer(), nullable=False),
        sa.Column("auto_retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["generation_task_id"], ["generation_tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["execution_account_id"],
            ["runninghub_execution_accounts.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_task_id"),
        sa.UniqueConstraint("remote_task_id"),
    )
    op.create_index(
        "ix_generation_task_enhancements_generation_task_id",
        "generation_task_enhancements",
        ["generation_task_id"],
        unique=True,
    )
    op.create_index(
        "ix_generation_task_enhancements_status",
        "generation_task_enhancements",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_generation_task_enhancements_remote_task_id",
        "generation_task_enhancements",
        ["remote_task_id"],
        unique=True,
    )
    op.create_index(
        "ix_generation_task_enhancements_execution_account_id",
        "generation_task_enhancements",
        ["execution_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_task_enhancements_auto_retry_after",
        "generation_task_enhancements",
        ["auto_retry_after"],
        unique=False,
    )

    op.create_table(
        "generation_task_enhancement_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("enhancement_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("execution_account_id", sa.Integer(), nullable=True),
        sa.Column("remote_task_id", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("payload_summary_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("failed_reason_json", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["enhancement_id"],
            ["generation_task_enhancements.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_account_id"],
            ["runninghub_execution_accounts.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "enhancement_id",
            "attempt_number",
            name="uq_generation_task_enhancement_attempt_number",
        ),
        sa.UniqueConstraint("remote_task_id"),
    )
    op.create_index(
        "ix_generation_task_enhancement_attempts_enhancement_id",
        "generation_task_enhancement_attempts",
        ["enhancement_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_task_enhancement_attempts_execution_account_id",
        "generation_task_enhancement_attempts",
        ["execution_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_task_enhancement_attempts_remote_task_id",
        "generation_task_enhancement_attempts",
        ["remote_task_id"],
        unique=True,
    )
    op.create_index(
        "ix_generation_task_enhancement_attempts_status",
        "generation_task_enhancement_attempts",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_generation_task_enhancement_attempts_status",
        table_name="generation_task_enhancement_attempts",
    )
    op.drop_index(
        "ix_generation_task_enhancement_attempts_remote_task_id",
        table_name="generation_task_enhancement_attempts",
    )
    op.drop_index(
        "ix_generation_task_enhancement_attempts_execution_account_id",
        table_name="generation_task_enhancement_attempts",
    )
    op.drop_index(
        "ix_generation_task_enhancement_attempts_enhancement_id",
        table_name="generation_task_enhancement_attempts",
    )
    op.drop_table("generation_task_enhancement_attempts")
    op.drop_index(
        "ix_generation_task_enhancements_auto_retry_after",
        table_name="generation_task_enhancements",
    )
    op.drop_index(
        "ix_generation_task_enhancements_execution_account_id",
        table_name="generation_task_enhancements",
    )
    op.drop_index(
        "ix_generation_task_enhancements_remote_task_id",
        table_name="generation_task_enhancements",
    )
    op.drop_index(
        "ix_generation_task_enhancements_status",
        table_name="generation_task_enhancements",
    )
    op.drop_index(
        "ix_generation_task_enhancements_generation_task_id",
        table_name="generation_task_enhancements",
    )
    op.drop_table("generation_task_enhancements")
