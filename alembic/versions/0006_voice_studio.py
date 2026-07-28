"""Separate reusable voice creation from batch speech generation."""

from alembic import op
import sqlalchemy as sa


revision = "0006_voice_studio"
down_revision = "0005_minimax_audio_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("minimax_voice_assets") as batch_op:
        batch_op.add_column(
            sa.Column(
                "method",
                sa.String(length=20),
                nullable=False,
                server_default="legacy",
            )
        )
        batch_op.add_column(
            sa.Column(
                "is_saved",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "preview_relative_path",
                sa.String(length=500),
                nullable=True,
            )
        )

    with op.batch_alter_table("audio_generation_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("voice_asset_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_audio_generation_tasks_voice_asset",
            "minimax_voice_assets",
            ["voice_asset_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_audio_generation_tasks_voice_asset_id",
        "audio_generation_tasks",
        ["voice_asset_id"],
        unique=False,
    )

    op.create_table(
        "voice_creation_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=False),
        sa.Column("voice_asset_id", sa.String(length=36), nullable=True),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("preview_text", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("weight_a", sa.Integer(), nullable=True),
        sa.Column("weight_b", sa.Integer(), nullable=True),
        sa.Column("noise_reduction", sa.Boolean(), nullable=False),
        sa.Column("volume_normalization", sa.Boolean(), nullable=False),
        sa.Column("source_a_relative_path", sa.String(length=500), nullable=False),
        sa.Column("source_a_original_name", sa.String(length=255), nullable=False),
        sa.Column("source_b_relative_path", sa.String(length=500), nullable=True),
        sa.Column("source_b_original_name", sa.String(length=255), nullable=True),
        sa.Column("source_a_file_id", sa.String(length=100), nullable=True),
        sa.Column("source_b_file_id", sa.String(length=100), nullable=True),
        sa.Column("final_file_id", sa.String(length=100), nullable=True),
        sa.Column("temporary_voice_a_id", sa.String(length=256), nullable=True),
        sa.Column("temporary_voice_b_id", sa.String(length=256), nullable=True),
        sa.Column("final_voice_id", sa.String(length=256), nullable=True),
        sa.Column("preview_relative_path", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("cost_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("save_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["config_id"], ["minimax_configs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["voice_asset_id"],
            ["minimax_voice_assets.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("voice_asset_id"),
    )
    op.create_index(
        "ix_voice_creation_tasks_config_id",
        "voice_creation_tasks",
        ["config_id"],
        unique=False,
    )
    op.create_index(
        "ix_voice_creation_tasks_status",
        "voice_creation_tasks",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_voice_creation_tasks_user_id",
        "voice_creation_tasks",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_voice_creation_tasks_user_id", table_name="voice_creation_tasks"
    )
    op.drop_index(
        "ix_voice_creation_tasks_status", table_name="voice_creation_tasks"
    )
    op.drop_index(
        "ix_voice_creation_tasks_config_id", table_name="voice_creation_tasks"
    )
    op.drop_table("voice_creation_tasks")
    op.drop_index(
        "ix_audio_generation_tasks_voice_asset_id",
        table_name="audio_generation_tasks",
    )
    with op.batch_alter_table("audio_generation_tasks") as batch_op:
        batch_op.drop_constraint(
            "fk_audio_generation_tasks_voice_asset",
            type_="foreignkey",
        )
        batch_op.drop_column("voice_asset_id")
    with op.batch_alter_table("minimax_voice_assets") as batch_op:
        batch_op.drop_column("preview_relative_path")
        batch_op.drop_column("is_saved")
        batch_op.drop_column("method")
