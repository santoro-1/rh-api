"""Persist MiniMax async TTS jobs and sentence-level timestamps."""

from alembic import op
import sqlalchemy as sa


revision = "0008_minimax_async_timestamps"
down_revision = "0007_full_script_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("audio_generation_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("subtitle_path", sa.String(length=500), nullable=True)
        )
        batch_op.add_column(
            sa.Column("provider_task_id", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("provider_file_id", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "provider_submitted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
    op.create_index(
        "ix_audio_generation_tasks_provider_task_id",
        "audio_generation_tasks",
        ["provider_task_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audio_generation_tasks_provider_task_id",
        table_name="audio_generation_tasks",
    )
    with op.batch_alter_table("audio_generation_tasks") as batch_op:
        batch_op.drop_column("provider_submitted_at")
        batch_op.drop_column("provider_file_id")
        batch_op.drop_column("provider_task_id")
        batch_op.drop_column("subtitle_path")
