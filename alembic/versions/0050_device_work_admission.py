"""Persist device admission for queued work without changing task parent tables.

Revision ID: 0050_device_work_admission
Revises: 0049_workbench_devices
"""

from alembic import op
import sqlalchemy as sa

revision = "0050_device_work_admission"
down_revision = "0049_workbench_devices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "voice_creation_tasks",
        sa.Column(
            "source_channel",
            sa.String(30),
            nullable=False,
            server_default="legacy_web",
        ),
    )
    op.create_index(
        "ix_voice_creation_tasks_source_channel",
        "voice_creation_tasks",
        ["source_channel"],
    )
    op.create_table(
        "workbench_device_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_id", sa.String(36), nullable=True),
        sa.Column("grant_id", sa.String(36), nullable=True),
        sa.Column("thumbprint", sa.String(43), nullable=True),
        sa.Column("grant_revision", sa.Integer(), nullable=True),
        sa.Column("policy_revision", sa.Integer(), nullable=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("operation_kind", sa.String(40), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("admission_mode", sa.String(10), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_workbench_device_operations_user_id",
        "workbench_device_operations",
        ["user_id"],
    )
    op.create_table(
        "workbench_device_work_bindings",
        sa.Column("resource_kind", sa.String(32), primary_key=True),
        sa.Column("resource_id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "operation_id",
            sa.String(36),
            sa.ForeignKey("workbench_device_operations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("blocked_code", sa.String(64), nullable=True),
        sa.Column("blocked_at", sa.Integer(), nullable=True),
        sa.Column("last_phase", sa.String(16), nullable=True),
        sa.CheckConstraint(
            "resource_kind IN ("
            "'generation_segment','generation_task',"
            "'audio_generation_task','voice_creation_task'"
            ")",
            name="ck_device_work_resource_kind",
        ),
    )
    op.create_index(
        "ix_workbench_device_work_bindings_user_id",
        "workbench_device_work_bindings",
        ["user_id"],
    )


def downgrade() -> None:
    # Explicit schema downgrade only. Ordinary application rollback keeps records.
    op.drop_table("workbench_device_work_bindings")
    op.drop_table("workbench_device_operations")
    op.drop_index(
        "ix_voice_creation_tasks_source_channel",
        table_name="voice_creation_tasks",
    )
    op.drop_column("voice_creation_tasks", "source_channel")
