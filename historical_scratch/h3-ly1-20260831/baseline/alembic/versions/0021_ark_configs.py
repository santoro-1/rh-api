"""add encrypted user-level Volcengine Ark configurations

Revision ID: 0021_ark_configs
Revises: 0020_batch_source_channel
"""

from alembic import op
import sqlalchemy as sa


revision = "0021_ark_configs"
down_revision = "0020_batch_source_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ark_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column(
            "base_url",
            sa.String(length=500),
            server_default="https://ark.cn-beijing.volces.com/api/v3",
            nullable=False,
        ),
        sa.Column("model", sa.String(length=200), server_default="", nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default="30", nullable=False),
        sa.Column("max_retries", sa.Integer(), server_default="2", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("ark_configs")
