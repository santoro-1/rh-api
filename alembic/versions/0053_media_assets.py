"""Add isolated OSS media upload records.

Revision ID: 0053_media_assets
Revises: 0052_h3_upload_metadata
"""

from alembic import op
import sqlalchemy as sa


revision = "0053_media_assets"
down_revision = "0052_h3_upload_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("source_channel", sa.String(30), nullable=False),
        sa.Column("asset_role", sa.String(40), nullable=False),
        sa.Column("upload_key", sa.String(128), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("expected_sha256", sa.String(64), nullable=False),
        sa.Column("verified_sha256", sa.String(64), nullable=True),
        sa.Column("object_key", sa.String(500), nullable=False),
        sa.Column("multipart_upload_id", sa.String(255), nullable=False),
        sa.Column("part_size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('UPLOADING','UPLOADED','VERIFYING','READY','REJECTED')", name="ck_media_asset_status"),
    )
    op.create_index("ix_media_assets_user_id", "media_assets", ["user_id"])
    op.create_index("uq_media_assets_upload_key", "media_assets", ["user_id", "source_channel", "upload_key"], unique=True)
    op.create_index("uq_media_assets_object_key", "media_assets", ["object_key"], unique=True)


def downgrade() -> None:
    op.drop_table("media_assets")
