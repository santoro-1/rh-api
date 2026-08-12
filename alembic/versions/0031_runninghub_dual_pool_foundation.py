"""add feature-gated RunningHub dual-pool foundation

Revision ID: 0031_runninghub_dual_pool_foundation
Revises: 0030_content_analysis_title
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa


revision = "0031_runninghub_dual_pool_foundation"
down_revision = "0030_content_analysis_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runninghub_dual_pool_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("allow_non_admin", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index(
        "ix_runninghub_dual_pool_grants_user_id",
        "runninghub_dual_pool_grants",
        ["user_id"],
        unique=True,
    )

    op.create_table(
        "seedvr2_execution_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("seedvr2_ai_app_id", sa.String(length=100), nullable=False),
        sa.Column("max_concurrent_tasks", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("health_status", sa.String(length=30), nullable=False),
        sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_error_code", sa.String(length=100), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_concurrent_tasks >= 1 AND max_concurrent_tasks <= 5",
            name="ck_seedvr2_execution_account_concurrency",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_seedvr2_execution_accounts_credential_fingerprint",
        "seedvr2_execution_accounts",
        ["credential_fingerprint"],
        unique=True,
    )
    op.create_index(
        "ix_seedvr2_execution_accounts_cooldown_until",
        "seedvr2_execution_accounts",
        ["cooldown_until"],
        unique=False,
    )

    op.create_table(
        "seedvr2_pool_memberships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("execution_account_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["execution_account_id"],
            ["seedvr2_execution_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "execution_account_id",
            name="uq_seedvr2_pool_user_account",
        ),
    )
    op.create_index(
        "ix_seedvr2_pool_memberships_user_id",
        "seedvr2_pool_memberships",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_seedvr2_pool_memberships_execution_account_id",
        "seedvr2_pool_memberships",
        ["execution_account_id"],
        unique=False,
    )

    op.add_column(
        "generation_batches",
        sa.Column("seedvr2_execution_account_ids_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "generation_batches",
        sa.Column("execution_mode", sa.String(length=30), nullable=True),
    )
    op.create_index(
        "ix_generation_batches_execution_mode",
        "generation_batches",
        ["execution_mode"],
        unique=False,
    )

    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE generation_task_enhancements "
            "ADD COLUMN seedvr2_execution_account_id INTEGER "
            "REFERENCES seedvr2_execution_accounts(id) ON DELETE SET NULL"
        )
    else:
        op.add_column(
            "generation_task_enhancements",
            sa.Column(
                "seedvr2_execution_account_id",
                sa.Integer(),
                sa.ForeignKey("seedvr2_execution_accounts.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    op.create_index(
        "ix_generation_task_enhancements_seedvr2_execution_account_id",
        "generation_task_enhancements",
        ["seedvr2_execution_account_id"],
        unique=False,
    )
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            "ALTER TABLE generation_task_enhancement_attempts "
            "ADD COLUMN seedvr2_execution_account_id INTEGER "
            "REFERENCES seedvr2_execution_accounts(id) ON DELETE SET NULL"
        )
    else:
        op.add_column(
            "generation_task_enhancement_attempts",
            sa.Column(
                "seedvr2_execution_account_id",
                sa.Integer(),
                sa.ForeignKey("seedvr2_execution_accounts.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    op.create_index(
        "ix_generation_task_enhancement_attempts_seedvr2_execution_account_id",
        "generation_task_enhancement_attempts",
        ["seedvr2_execution_account_id"],
        unique=False,
    )

    # This one-time lookup records the explicitly approved test exception by
    # immutable user ID.  The global feature switch still defaults to OFF.
    op.execute(
        sa.text(
            "INSERT INTO runninghub_dual_pool_grants "
            "(user_id, is_enabled, allow_non_admin, note, created_at, updated_at) "
            "SELECT id, 1, 1, "
            "'Initial controlled dual-pool test grant', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "FROM users WHERE username = 'Cx_ceshi'"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_generation_task_enhancement_attempts_seedvr2_execution_account_id",
        table_name="generation_task_enhancement_attempts",
    )
    op.drop_column(
        "generation_task_enhancement_attempts", "seedvr2_execution_account_id"
    )
    op.drop_index(
        "ix_generation_task_enhancements_seedvr2_execution_account_id",
        table_name="generation_task_enhancements",
    )
    op.drop_column("generation_task_enhancements", "seedvr2_execution_account_id")
    op.drop_index(
        "ix_generation_batches_execution_mode", table_name="generation_batches"
    )
    op.drop_column("generation_batches", "execution_mode")
    op.drop_column("generation_batches", "seedvr2_execution_account_ids_json")
    op.drop_index(
        "ix_seedvr2_pool_memberships_execution_account_id",
        table_name="seedvr2_pool_memberships",
    )
    op.drop_index(
        "ix_seedvr2_pool_memberships_user_id",
        table_name="seedvr2_pool_memberships",
    )
    op.drop_table("seedvr2_pool_memberships")
    op.drop_index(
        "ix_seedvr2_execution_accounts_cooldown_until",
        table_name="seedvr2_execution_accounts",
    )
    op.drop_index(
        "ix_seedvr2_execution_accounts_credential_fingerprint",
        table_name="seedvr2_execution_accounts",
    )
    op.drop_table("seedvr2_execution_accounts")
    op.drop_index(
        "ix_runninghub_dual_pool_grants_user_id",
        table_name="runninghub_dual_pool_grants",
    )
    op.drop_table("runninghub_dual_pool_grants")
