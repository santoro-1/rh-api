"""Queue durable OSS media verification without touching legacy assets.

Revision ID: 0054_media_verification_jobs
Revises: 0053_media_assets
"""

from alembic import op
import sqlalchemy as sa


revision = "0054_media_verification_jobs"
down_revision = "0053_media_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_verification_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("lease_id", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("receipt_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('PENDING','RUNNING','SUCCEEDED','REJECTED')", name="ck_media_verify_status"),
    )
    op.create_index("uq_media_verification_jobs_asset_id", "media_verification_jobs", ["asset_id"], unique=True)
    op.create_index("ix_media_verification_jobs_status", "media_verification_jobs", ["status"])


def downgrade() -> None:
    op.drop_table("media_verification_jobs")
