"""Add the durable Ark analysis idempotency ledger.

Revision ID: 0051_ark_analysis_operations
Revises: 0050_device_work_admission
"""

from alembic import op
import sqlalchemy as sa


revision = "0051_ark_analysis_operations"
down_revision = "0050_device_work_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ark_analysis_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("business_key_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("cache_kind", sa.String(20), nullable=True),
        sa.Column("cache_id", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_summary", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('content','visual')", name="ck_ark_analysis_operation_kind"
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','EXPIRED')",
            name="ck_ark_analysis_operation_status",
        ),
    )
    op.create_index(
        "ix_ark_analysis_operations_user_id",
        "ark_analysis_operations",
        ["user_id"],
    )
    op.create_index(
        "ix_ark_analysis_operations_user_business",
        "ark_analysis_operations",
        ["user_id", "business_key_sha256"],
    )
    op.create_index(
        "ix_ark_analysis_operations_status_updated",
        "ark_analysis_operations",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    # Explicit schema downgrade only. Ordinary application rollback keeps records.
    op.drop_table("ark_analysis_operations")
