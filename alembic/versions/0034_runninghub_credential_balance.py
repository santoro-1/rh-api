"""add shared RunningHub credential balance cache

Revision ID: 0034_runninghub_credential_balance
Revises: 0033_generation_task_seedvr2_switch
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa


revision = "0034_runninghub_credential_balance"
down_revision = "0033_generation_task_seedvr2_switch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runninghub_credential_balances",
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "balance_status",
            sa.String(length=30),
            nullable=False,
            server_default="UNKNOWN",
        ),
        sa.Column("remain_coins", sa.String(length=100), nullable=True),
        sa.Column("remain_money", sa.String(length=100), nullable=True),
        sa.Column("currency", sa.String(length=20), nullable=True),
        sa.Column("api_type", sa.String(length=50), nullable=True),
        sa.Column("remote_current_task_count", sa.Integer(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("credential_fingerprint"),
    )
    op.create_index(
        "ix_runninghub_credential_balances_checked_at",
        "runninghub_credential_balances",
        ["checked_at"],
        unique=False,
    )
    op.create_index(
        "ix_runninghub_credential_balances_retry_after",
        "runninghub_credential_balances",
        ["retry_after"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runninghub_credential_balances_retry_after",
        table_name="runninghub_credential_balances",
    )
    op.drop_index(
        "ix_runninghub_credential_balances_checked_at",
        table_name="runninghub_credential_balances",
    )
    op.drop_table("runninghub_credential_balances")
