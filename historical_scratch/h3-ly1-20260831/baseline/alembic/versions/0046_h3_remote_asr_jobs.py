"""generalize H3 remote ASR jobs

Revision ID: 0046_h3_remote_asr_jobs
Revises: 0045_h3_remote_head_trim_jobs
Create Date: 2026-08-26
"""

from __future__ import annotations

import hashlib

from alembic import op
import sqlalchemy as sa


revision = "0046_h3_remote_asr_jobs"
down_revision = "0045_h3_remote_head_trim_jobs"
branch_labels = None
depends_on = None


def _create_remote_asr_table() -> None:
    op.create_table(
        "h3_remote_asr_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("generation_task_id", sa.String(length=36), nullable=True),
        sa.Column("staged_asset_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("idempotency_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.String(length=500), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("script_text", sa.Text(), nullable=False),
        sa.Column("script_sha256", sa.String(length=64), nullable=False),
        sa.Column("audio_batch_id", sa.String(length=100), nullable=True),
        sa.Column("audio_item_id", sa.String(length=100), nullable=True),
        sa.Column("audio_generation_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("remote_lease_id", sa.String(length=36), nullable=True),
        sa.Column("remote_worker_id", sa.String(length=100), nullable=True),
        sa.Column("remote_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "remote_last_heartbeat_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("remote_metrics_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["generation_task_id"],
            ["generation_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["staged_asset_id"], ["staged_assets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_task_id"),
        sa.UniqueConstraint("idempotency_sha256"),
    )
    for name, columns, unique in (
        ("ix_h3_remote_asr_jobs_user_id", ["user_id"], False),
        ("ix_h3_remote_asr_jobs_generation_task_id", ["generation_task_id"], True),
        ("ix_h3_remote_asr_jobs_staged_asset_id", ["staged_asset_id"], False),
        ("ix_h3_remote_asr_jobs_action", ["action"], False),
        ("ix_h3_remote_asr_jobs_idempotency_sha256", ["idempotency_sha256"], True),
        ("ix_h3_remote_asr_jobs_status", ["status"], False),
        ("ix_h3_remote_asr_jobs_remote_lease_id", ["remote_lease_id"], False),
        (
            "ix_h3_remote_asr_jobs_remote_lease_expires_at",
            ["remote_lease_expires_at"],
            False,
        ),
    ):
        op.create_index(name, "h3_remote_asr_jobs", columns, unique=unique)


def upgrade() -> None:
    connection = op.get_bind()
    old = sa.table(
        "h3_head_trim_jobs",
        sa.column("id"),
        sa.column("generation_task_id"),
        sa.column("source_video_path"),
        sa.column("source_video_name"),
        sa.column("script_text"),
        sa.column("status"),
        sa.column("decision_json"),
        sa.column("error_code"),
        sa.column("error_message"),
        sa.column("remote_lease_id"),
        sa.column("remote_worker_id"),
        sa.column("remote_lease_expires_at"),
        sa.column("remote_last_heartbeat_at"),
        sa.column("remote_metrics_json"),
        sa.column("created_at"),
        sa.column("updated_at"),
        sa.column("completed_at"),
    )
    tasks = sa.table(
        "generation_tasks", sa.column("id"), sa.column("user_id")
    )
    rows = connection.execute(sa.select(old)).mappings().all()
    user_ids = {
        row["generation_task_id"]: connection.execute(
            sa.select(tasks.c.user_id).where(
                tasks.c.id == row["generation_task_id"]
            )
        ).scalar_one()
        for row in rows
    }

    _create_remote_asr_table()
    new = sa.table(
        "h3_remote_asr_jobs",
        *[
            sa.column(name)
            for name in (
                "id",
                "user_id",
                "generation_task_id",
                "staged_asset_id",
                "action",
                "idempotency_sha256",
                "source_path",
                "source_name",
                "source_sha256",
                "script_text",
                "script_sha256",
                "audio_batch_id",
                "audio_item_id",
                "audio_generation_version",
                "status",
                "result_json",
                "error_code",
                "error_message",
                "remote_lease_id",
                "remote_worker_id",
                "remote_lease_expires_at",
                "remote_last_heartbeat_at",
                "remote_metrics_json",
                "created_at",
                "updated_at",
                "completed_at",
            )
        ],
    )
    for row in rows:
        script_text = str(row["script_text"] or "")
        task_id = str(row["generation_task_id"])
        connection.execute(
            new.insert().values(
                id=row["id"],
                user_id=user_ids[row["generation_task_id"]],
                generation_task_id=row["generation_task_id"],
                staged_asset_id=None,
                action="h3_head_trim",
                idempotency_sha256=hashlib.sha256(
                    f"h3_head_trim\0{task_id}\0legacy-0045".encode("utf-8")
                ).hexdigest(),
                source_path=row["source_video_path"],
                source_name=row["source_video_name"],
                source_sha256="0" * 64,
                script_text=script_text,
                script_sha256=hashlib.sha256(
                    script_text.encode("utf-8")
                ).hexdigest(),
                audio_batch_id=None,
                audio_item_id=None,
                audio_generation_version=None,
                status=row["status"],
                result_json=row["decision_json"],
                error_code=row["error_code"],
                error_message=row["error_message"],
                remote_lease_id=row["remote_lease_id"],
                remote_worker_id=row["remote_worker_id"],
                remote_lease_expires_at=row["remote_lease_expires_at"],
                remote_last_heartbeat_at=row["remote_last_heartbeat_at"],
                remote_metrics_json=row["remote_metrics_json"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                completed_at=row["completed_at"],
            )
        )
    op.drop_table("h3_head_trim_jobs")


def downgrade() -> None:
    connection = op.get_bind()
    new = sa.table(
        "h3_remote_asr_jobs",
        *[
            sa.column(name)
            for name in (
                "id",
                "generation_task_id",
                "action",
                "source_path",
                "source_name",
                "script_text",
                "status",
                "result_json",
                "error_code",
                "error_message",
                "remote_lease_id",
                "remote_worker_id",
                "remote_lease_expires_at",
                "remote_last_heartbeat_at",
                "remote_metrics_json",
                "created_at",
                "updated_at",
                "completed_at",
            )
        ],
    )
    rows = connection.execute(
        sa.select(new).where(new.c.action == "h3_head_trim")
    ).mappings().all()
    for name in (
        "ix_h3_remote_asr_jobs_remote_lease_expires_at",
        "ix_h3_remote_asr_jobs_remote_lease_id",
        "ix_h3_remote_asr_jobs_status",
        "ix_h3_remote_asr_jobs_idempotency_sha256",
        "ix_h3_remote_asr_jobs_action",
        "ix_h3_remote_asr_jobs_staged_asset_id",
        "ix_h3_remote_asr_jobs_generation_task_id",
        "ix_h3_remote_asr_jobs_user_id",
    ):
        op.drop_index(name, table_name="h3_remote_asr_jobs")
    op.drop_table("h3_remote_asr_jobs")

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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["generation_task_id"],
            ["generation_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_task_id"),
    )
    old = sa.table(
        "h3_head_trim_jobs",
        *[
            sa.column(name)
            for name in (
                "id",
                "generation_task_id",
                "source_video_path",
                "source_video_name",
                "script_text",
                "status",
                "decision_json",
                "error_code",
                "error_message",
                "remote_lease_id",
                "remote_worker_id",
                "remote_lease_expires_at",
                "remote_last_heartbeat_at",
                "remote_metrics_json",
                "created_at",
                "updated_at",
                "completed_at",
            )
        ],
    )
    for row in rows:
        connection.execute(
            old.insert().values(
                id=row["id"],
                generation_task_id=row["generation_task_id"],
                source_video_path=row["source_path"],
                source_video_name=row["source_name"],
                script_text=row["script_text"],
                status=row["status"],
                decision_json=row["result_json"],
                error_code=row["error_code"],
                error_message=row["error_message"],
                remote_lease_id=row["remote_lease_id"],
                remote_worker_id=row["remote_worker_id"],
                remote_lease_expires_at=row["remote_lease_expires_at"],
                remote_last_heartbeat_at=row["remote_last_heartbeat_at"],
                remote_metrics_json=row["remote_metrics_json"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                completed_at=row["completed_at"],
            )
        )
    for name, columns, unique in (
        ("ix_h3_head_trim_jobs_generation_task_id", ["generation_task_id"], True),
        ("ix_h3_head_trim_jobs_status", ["status"], False),
        ("ix_h3_head_trim_jobs_remote_lease_id", ["remote_lease_id"], False),
        (
            "ix_h3_head_trim_jobs_remote_lease_expires_at",
            ["remote_lease_expires_at"],
            False,
        ),
    ):
        op.create_index(name, "h3_head_trim_jobs", columns, unique=unique)
