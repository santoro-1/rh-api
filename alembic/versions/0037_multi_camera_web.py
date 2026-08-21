"""add controlled multi-camera web orchestration

Revision ID: 0037_multi_camera_web
Revises: 0036_ltx_workbench_preparation
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa


revision = "0037_multi_camera_web"
down_revision = "0036_ltx_workbench_preparation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "multi_camera_user_access",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    # Username is used once to bootstrap the environment-specific user IDs.
    # Every runtime authorization decision reads this ID grant table instead.
    op.execute(
        sa.text(
            """
            INSERT INTO multi_camera_user_access
                (user_id, is_enabled, created_at, updated_at)
            SELECT id, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM users
            WHERE username IN ('admin', 'Cx_ceshi') AND is_active = 1
            """
        )
    )
    op.create_table(
        "multi_camera_batch_configs",
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("resolution", sa.String(length=20), nullable=False),
        sa.Column("instance_type", sa.String(length=20), nullable=False),
        sa.Column("seedvr2_enabled", sa.Boolean(), nullable=False),
        sa.Column("segmentation_policy", sa.String(length=50), nullable=False),
        sa.Column("ordering_policy", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["generation_batches.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("batch_id"),
    )
    op.create_table(
        "multi_camera_image_groups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("client_key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["generation_batches.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id", "position", name="uq_multi_camera_group_position"
        ),
        sa.UniqueConstraint(
            "batch_id", "client_key", name="uq_multi_camera_group_client_key"
        ),
    )
    op.create_index(
        op.f("ix_multi_camera_image_groups_batch_id"),
        "multi_camera_image_groups",
        ["batch_id"],
        unique=False,
    )
    op.create_table(
        "multi_camera_image_group_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("image_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"], ["multi_camera_image_groups.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id", "position", name="uq_multi_camera_group_asset_position"
        ),
    )
    op.create_index(
        op.f("ix_multi_camera_image_group_assets_group_id"),
        "multi_camera_image_group_assets",
        ["group_id"],
        unique=False,
    )
    op.create_table(
        "multi_camera_item_bindings",
        sa.Column("batch_item_id", sa.String(length=36), nullable=False),
        sa.Column("image_group_id", sa.String(length=36), nullable=False),
        sa.Column("audio_original_name", sa.String(length=255), nullable=False),
        sa.Column("audio_sha256", sa.String(length=64), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_item_id"], ["generation_batch_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["image_group_id"],
            ["multi_camera_image_groups.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("batch_item_id"),
    )
    op.create_index(
        op.f("ix_multi_camera_item_bindings_image_group_id"),
        "multi_camera_item_bindings",
        ["image_group_id"],
        unique=False,
    )
    op.create_table(
        "multi_camera_segment_bindings",
        sa.Column("segment_id", sa.String(length=36), nullable=False),
        sa.Column("image_asset_id", sa.String(length=36), nullable=False),
        sa.Column("camera_position", sa.Integer(), nullable=False),
        sa.Column("image_sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["image_asset_id"],
            ["multi_camera_image_group_assets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["generation_segments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("segment_id"),
    )
    op.create_index(
        op.f("ix_multi_camera_segment_bindings_image_asset_id"),
        "multi_camera_segment_bindings",
        ["image_asset_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_multi_camera_segment_bindings_image_asset_id"),
        table_name="multi_camera_segment_bindings",
    )
    op.drop_table("multi_camera_segment_bindings")
    op.drop_index(
        op.f("ix_multi_camera_item_bindings_image_group_id"),
        table_name="multi_camera_item_bindings",
    )
    op.drop_table("multi_camera_item_bindings")
    op.drop_index(
        op.f("ix_multi_camera_image_group_assets_group_id"),
        table_name="multi_camera_image_group_assets",
    )
    op.drop_table("multi_camera_image_group_assets")
    op.drop_index(
        op.f("ix_multi_camera_image_groups_batch_id"),
        table_name="multi_camera_image_groups",
    )
    op.drop_table("multi_camera_image_groups")
    op.drop_table("multi_camera_batch_configs")
    op.drop_table("multi_camera_user_access")
