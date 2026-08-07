"""add administrator RunningHub execution account pools

Revision ID: 0025_runninghub_execution_pool
Revises: 0024_shared_minimax_voices
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0025_runninghub_execution_pool"
down_revision = "0024_shared_minimax_voices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These native ADD COLUMN operations deliberately avoid rebuilding the
    # parent tables. SQLite foreign-key cascades are enabled in production,
    # so a batch table rebuild could otherwise delete existing child rows.
    op.add_column(
        "runninghub_configs",
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_runninghub_configs_credential_fingerprint",
        "runninghub_configs",
        ["credential_fingerprint"],
        unique=False,
    )

    op.create_table(
        "runninghub_execution_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("digital_human_ai_app_id", sa.String(length=100), nullable=False),
        sa.Column("max_concurrent_tasks", sa.Integer(), server_default="5", nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("health_status", sa.String(length=30), server_default="UNKNOWN", nullable=False),
        sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_error_code", sa.String(length=100), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_concurrent_tasks >= 1 AND max_concurrent_tasks <= 5",
            name="ck_runninghub_execution_account_concurrency",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_fingerprint"),
    )
    op.create_index(
        "ix_runninghub_execution_accounts_credential_fingerprint",
        "runninghub_execution_accounts",
        ["credential_fingerprint"],
        unique=True,
    )
    op.create_index(
        "ix_runninghub_execution_accounts_cooldown_until",
        "runninghub_execution_accounts",
        ["cooldown_until"],
        unique=False,
    )

    op.create_table(
        "runninghub_pool_memberships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admin_user_id", sa.Integer(), nullable=False),
        sa.Column("execution_account_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["admin_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["execution_account_id"],
            ["runninghub_execution_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "admin_user_id",
            "execution_account_id",
            name="uq_runninghub_pool_admin_account",
        ),
    )
    op.create_index(
        "ix_runninghub_pool_memberships_admin_user_id",
        "runninghub_pool_memberships",
        ["admin_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_runninghub_pool_memberships_execution_account_id",
        "runninghub_pool_memberships",
        ["execution_account_id"],
        unique=False,
    )

    op.add_column(
        "generation_batches",
        sa.Column("runninghub_execution_account_ids_json", sa.Text(), nullable=True),
    )
    # SQLite cannot add a foreign-key constraint in a separate ALTER TABLE
    # operation. Adding the nullable column with its REFERENCES clause in the
    # same native statement preserves the existing parent table and all rows.
    op.execute(
        "ALTER TABLE generation_tasks ADD COLUMN execution_account_id "
        "INTEGER REFERENCES runninghub_execution_accounts(id) ON DELETE SET NULL"
    )
    op.create_index(
        "ix_generation_tasks_execution_account_id",
        "generation_tasks",
        ["execution_account_id"],
        unique=False,
    )

    op.create_table(
        "generation_task_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("generation_task_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("execution_account_id", sa.Integer(), nullable=True),
        sa.Column("remote_task_id", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="RESERVED", nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("failed_reason_json", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_account_id"],
            ["runninghub_execution_accounts.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["generation_task_id"], ["generation_tasks.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("remote_task_id"),
        sa.UniqueConstraint(
            "generation_task_id",
            "attempt_number",
            name="uq_generation_task_attempt_number",
        ),
    )
    op.create_index(
        "ix_generation_task_attempts_generation_task_id",
        "generation_task_attempts",
        ["generation_task_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_task_attempts_execution_account_id",
        "generation_task_attempts",
        ["execution_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_task_attempts_remote_task_id",
        "generation_task_attempts",
        ["remote_task_id"],
        unique=True,
    )
    op.create_index(
        "ix_generation_task_attempts_status",
        "generation_task_attempts",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_generation_task_attempts_status", table_name="generation_task_attempts")
    op.drop_index("ix_generation_task_attempts_remote_task_id", table_name="generation_task_attempts")
    op.drop_index("ix_generation_task_attempts_execution_account_id", table_name="generation_task_attempts")
    op.drop_index("ix_generation_task_attempts_generation_task_id", table_name="generation_task_attempts")
    op.drop_table("generation_task_attempts")

    op.drop_index("ix_generation_tasks_execution_account_id", table_name="generation_tasks")
    op.drop_column("generation_tasks", "execution_account_id")
    op.drop_column("generation_batches", "runninghub_execution_account_ids_json")

    op.drop_index(
        "ix_runninghub_pool_memberships_execution_account_id",
        table_name="runninghub_pool_memberships",
    )
    op.drop_index(
        "ix_runninghub_pool_memberships_admin_user_id",
        table_name="runninghub_pool_memberships",
    )
    op.drop_table("runninghub_pool_memberships")
    op.drop_index(
        "ix_runninghub_execution_accounts_cooldown_until",
        table_name="runninghub_execution_accounts",
    )
    op.drop_index(
        "ix_runninghub_execution_accounts_credential_fingerprint",
        table_name="runninghub_execution_accounts",
    )
    op.drop_table("runninghub_execution_accounts")

    op.drop_index(
        "ix_runninghub_configs_credential_fingerprint",
        table_name="runninghub_configs",
    )
    op.drop_column("runninghub_configs", "credential_fingerprint")
