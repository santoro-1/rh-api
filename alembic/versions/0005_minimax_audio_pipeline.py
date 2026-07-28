"""Add MiniMax account, voice assets, and persistent audio tasks."""

from alembic import op
import sqlalchemy as sa


revision = "0005_minimax_audio_pipeline"
down_revision = "0004_batch_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "minimax_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("requests_per_minute", sa.Integer(), nullable=False),
        sa.Column("last_t2a_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_table(
        "minimax_voice_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("voice_id", sa.String(length=256), nullable=False),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("source_relative_path", sa.String(length=500), nullable=True),
        sa.Column("source_original_name", sa.String(length=255), nullable=True),
        sa.Column("remote_file_id", sa.String(length=100), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["config_id"], ["minimax_configs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "config_id", "voice_id", name="uq_minimax_voice_config_id"
        ),
    )
    op.create_index(
        "ix_minimax_voice_assets_credential_fingerprint",
        "minimax_voice_assets",
        ["credential_fingerprint"],
        unique=False,
    )
    op.create_index(
        "ix_minimax_voice_assets_config_id",
        "minimax_voice_assets",
        ["config_id"],
        unique=False,
    )
    op.create_index(
        "ix_minimax_voice_assets_user_id",
        "minimax_voice_assets",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "audio_generation_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=False),
        sa.Column("batch_item_id", sa.String(length=36), nullable=False),
        sa.Column("voice_a_id", sa.String(length=36), nullable=False),
        sa.Column("voice_b_id", sa.String(length=36), nullable=False),
        sa.Column("planned_generation_task_id", sa.String(length=36), nullable=False),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("primary_kind", sa.String(length=20), nullable=False),
        sa.Column("primary_path", sa.String(length=500), nullable=False),
        sa.Column("primary_original_name", sa.String(length=255), nullable=False),
        sa.Column("speech_script", sa.Text(), nullable=False),
        sa.Column("video_parameters_json", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("weight_a", sa.Integer(), nullable=False),
        sa.Column("weight_b", sa.Integer(), nullable=False),
        sa.Column("speed", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("pitch", sa.Integer(), nullable=False),
        sa.Column("language_boost", sa.String(length=50), nullable=False),
        sa.Column("output_format", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("output_path", sa.String(length=500), nullable=True),
        sa.Column("cost_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_item_id"],
            ["generation_batch_items.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["config_id"], ["minimax_configs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["voice_a_id"], ["minimax_voice_assets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["voice_b_id"], ["minimax_voice_assets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_item_id"),
        sa.UniqueConstraint("planned_generation_task_id"),
    )
    op.create_index(
        "ix_audio_generation_tasks_batch_item_id",
        "audio_generation_tasks",
        ["batch_item_id"],
        unique=True,
    )
    op.create_index(
        "ix_audio_generation_tasks_config_id",
        "audio_generation_tasks",
        ["config_id"],
        unique=False,
    )
    op.create_index(
        "ix_audio_generation_tasks_status",
        "audio_generation_tasks",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_audio_generation_tasks_user_id",
        "audio_generation_tasks",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audio_generation_tasks_user_id",
        table_name="audio_generation_tasks",
    )
    op.drop_index(
        "ix_audio_generation_tasks_status",
        table_name="audio_generation_tasks",
    )
    op.drop_index(
        "ix_audio_generation_tasks_config_id",
        table_name="audio_generation_tasks",
    )
    op.drop_index(
        "ix_audio_generation_tasks_batch_item_id",
        table_name="audio_generation_tasks",
    )
    op.drop_table("audio_generation_tasks")
    op.drop_index(
        "ix_minimax_voice_assets_user_id",
        table_name="minimax_voice_assets",
    )
    op.drop_index(
        "ix_minimax_voice_assets_credential_fingerprint",
        table_name="minimax_voice_assets",
    )
    op.drop_index(
        "ix_minimax_voice_assets_config_id",
        table_name="minimax_voice_assets",
    )
    op.drop_table("minimax_voice_assets")
    op.drop_table("minimax_configs")
