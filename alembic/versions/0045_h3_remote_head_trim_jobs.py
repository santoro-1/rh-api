"""queue H3 head-trim ASR on remote media nodes

Revision ID: 0045_h3_remote_head_trim_jobs
Revises: 0044_system_workflow_configs
Create Date: 2026-08-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0045_h3_remote_head_trim_jobs"
down_revision = "0044_system_workflow_configs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "h3_head_trim_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("generation_task_id", sa.String(length=36), nullable=False),
        sa.Column("source_video_path", sa.String(length=500), nullable=False),
        sa.Column("source_video_name", sa.String(length=255), nullable=False),
        sa.Column("script_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("decision_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("remote_lease_id", sa.String(length=36), nullable=True),
        sa.Column("remote_worker_id", sa.String(length=100), nullable=True),
        sa.Column("remote_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "remote_last_heartbeat_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("remote_metrics_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["generation_task_id"],
            ["generation_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_task_id"),
    )
    op.create_index(
        "ix_h3_head_trim_jobs_generation_task_id",
        "h3_head_trim_jobs",
        ["generation_task_id"],
        unique=True,
    )
    op.create_index(
        "ix_h3_head_trim_jobs_status",
        "h3_head_trim_jobs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_h3_head_trim_jobs_remote_lease_id",
        "h3_head_trim_jobs",
        ["remote_lease_id"],
        unique=False,
    )
    op.create_index(
        "ix_h3_head_trim_jobs_remote_lease_expires_at",
        "h3_head_trim_jobs",
        ["remote_lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_h3_head_trim_jobs_remote_lease_expires_at",
        table_name="h3_head_trim_jobs",
    )
    op.drop_index(
        "ix_h3_head_trim_jobs_remote_lease_id",
        table_name="h3_head_trim_jobs",
    )
    op.drop_index("ix_h3_head_trim_jobs_status", table_name="h3_head_trim_jobs")
    op.drop_index(
        "ix_h3_head_trim_jobs_generation_task_id",
        table_name="h3_head_trim_jobs",
    )
    op.drop_table("h3_head_trim_jobs")
