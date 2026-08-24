"""add isolated H3 workbench snapshots

Revision ID: 0039_h3_workbench_snapshots
Revises: 0038_runninghub_h3_capability
Create Date: 2026-08-22
"""

from alembic import op
import sqlalchemy as sa


revision = "0039_h3_workbench_snapshots"
down_revision = "0038_runninghub_h3_capability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "h3_batch_configs",
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("contract_schema", sa.String(length=100), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("prompt_profile_id", sa.String(length=100), nullable=False),
        sa.Column("prompt_template_version", sa.String(length=100), nullable=False),
        sa.Column("continuity_mode", sa.String(length=30), nullable=False),
        sa.Column("aspect_ratio", sa.String(length=100), nullable=False),
        sa.Column("megapixels", sa.Float(), nullable=False),
        sa.Column("multiple", sa.Integer(), nullable=False),
        sa.Column("generation_tail_seconds", sa.Float(), nullable=False),
        sa.Column("adapter_version", sa.String(length=100), nullable=False),
        sa.Column("reference_images_json", sa.Text(), nullable=False),
        sa.Column("fee_snapshot_json", sa.Text(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "continuity_mode IN ('fast', 'soft_chain')",
            name="ck_h3_batch_continuity_mode",
        ),
        sa.CheckConstraint("multiple = 32", name="ck_h3_batch_multiple"),
        sa.CheckConstraint(
            "generation_tail_seconds >= 0 AND generation_tail_seconds <= 1",
            name="ck_h3_batch_generation_tail",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["generation_batches.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("batch_id"),
    )
    op.create_index(
        op.f("ix_h3_batch_configs_input_sha256"),
        "h3_batch_configs",
        ["input_sha256"],
        unique=False,
    )

    op.create_table(
        "h3_item_configs",
        sa.Column("batch_item_id", sa.String(length=36), nullable=False),
        sa.Column("script_sha256", sa.String(length=64), nullable=False),
        sa.Column("reference_video_asset_id", sa.String(length=36), nullable=False),
        sa.Column("reference_video_path", sa.String(length=500), nullable=False),
        sa.Column("reference_video_original_name", sa.String(length=255), nullable=False),
        sa.Column("reference_video_sha256", sa.String(length=64), nullable=False),
        sa.Column("audio_batch_id", sa.String(length=36), nullable=False),
        sa.Column("audio_item_id", sa.String(length=36), nullable=False),
        sa.Column("audio_generation_version", sa.Integer(), nullable=False),
        sa.Column("full_audio_path", sa.String(length=500), nullable=False),
        sa.Column("full_audio_original_name", sa.String(length=255), nullable=False),
        sa.Column("full_audio_sha256", sa.String(length=64), nullable=False),
        sa.Column("raw_cues_path", sa.String(length=500), nullable=False),
        sa.Column("raw_cues_sha256", sa.String(length=64), nullable=False),
        sa.Column("raw_cues_version", sa.String(length=100), nullable=False),
        sa.Column("audio_duration_seconds", sa.Float(), nullable=False),
        sa.Column("user_direction", sa.Text(), nullable=False),
        sa.Column("continuity_mode", sa.String(length=30), nullable=False),
        sa.Column("aspect_ratio", sa.String(length=100), nullable=False),
        sa.Column("megapixels", sa.Float(), nullable=False),
        sa.Column("multiple", sa.Integer(), nullable=False),
        sa.Column("segment_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "continuity_mode IN ('fast', 'soft_chain')",
            name="ck_h3_item_continuity_mode",
        ),
        sa.CheckConstraint("multiple = 32", name="ck_h3_item_multiple"),
        sa.CheckConstraint("segment_count >= 1", name="ck_h3_item_segment_count"),
        sa.ForeignKeyConstraint(
            ["batch_item_id"], ["generation_batch_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("batch_item_id"),
    )

    op.create_table(
        "h3_segment_configs",
        sa.Column("segment_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("segment_audio_sha256", sa.String(length=64), nullable=False),
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("prompt_template_version", sa.String(length=100), nullable=False),
        sa.Column("requested_generation_duration_seconds", sa.Float(), nullable=False),
        sa.Column("quantized_frame_count", sa.Integer(), nullable=False),
        sa.Column("effective_generation_duration_seconds", sa.Float(), nullable=False),
        sa.Column("continuity_mode", sa.String(length=30), nullable=False),
        sa.Column("previous_segment_id", sa.String(length=36), nullable=True),
        sa.Column("continuity_anchor_path", sa.String(length=500), nullable=True),
        sa.Column("continuity_anchor_sha256", sa.String(length=64), nullable=True),
        sa.Column("dynamic_workflow_sha256", sa.String(length=64), nullable=True),
        sa.Column("normalized_video_path", sa.String(length=500), nullable=True),
        sa.Column("normalized_video_sha256", sa.String(length=64), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "continuity_mode IN ('fast', 'soft_chain')",
            name="ck_h3_segment_continuity_mode",
        ),
        sa.CheckConstraint(
            "quantized_frame_count >= 5",
            name="ck_h3_segment_quantized_frames",
        ),
        sa.ForeignKeyConstraint(
            ["previous_segment_id"], ["generation_segments.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["generation_segments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("segment_id"),
        sa.UniqueConstraint("idempotency_sha256"),
    )
    op.create_index(
        op.f("ix_h3_segment_configs_idempotency_sha256"),
        "h3_segment_configs",
        ["idempotency_sha256"],
        unique=True,
    )
    op.create_index(
        op.f("ix_h3_segment_configs_input_sha256"),
        "h3_segment_configs",
        ["input_sha256"],
        unique=False,
    )
    op.create_index(
        op.f("ix_h3_segment_configs_previous_segment_id"),
        "h3_segment_configs",
        ["previous_segment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_h3_segment_configs_previous_segment_id"),
        table_name="h3_segment_configs",
    )
    op.drop_index(
        op.f("ix_h3_segment_configs_input_sha256"),
        table_name="h3_segment_configs",
    )
    op.drop_index(
        op.f("ix_h3_segment_configs_idempotency_sha256"),
        table_name="h3_segment_configs",
    )
    op.drop_table("h3_segment_configs")
    op.drop_table("h3_item_configs")
    op.drop_index(
        op.f("ix_h3_batch_configs_input_sha256"),
        table_name="h3_batch_configs",
    )
    op.drop_table("h3_batch_configs")
