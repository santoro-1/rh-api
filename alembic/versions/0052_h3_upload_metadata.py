"""Add H3 upload provenance and idempotency metadata.

Revision ID: 0052_h3_upload_metadata
Revises: 0051_ark_analysis_operations
"""

from alembic import op
import sqlalchemy as sa


revision = "0052_h3_upload_metadata"
down_revision = "0051_ark_analysis_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, length in (
        ("source_channel", 30),
        ("asset_role", 40),
        ("upload_key", 128),
        ("source_sha256", 64),
        ("transport_sha256", 64),
        ("transport_profile", 40),
    ):
        op.add_column("staged_assets", sa.Column(name, sa.String(length), nullable=True))
    op.create_index(
        "uq_staged_assets_h3_upload_key",
        "staged_assets",
        ["user_id", "source_channel", "upload_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_staged_assets_h3_upload_key", table_name="staged_assets")
    for name in (
        "transport_profile",
        "transport_sha256",
        "source_sha256",
        "upload_key",
        "asset_role",
        "source_channel",
    ):
        op.drop_column("staged_assets", name)
