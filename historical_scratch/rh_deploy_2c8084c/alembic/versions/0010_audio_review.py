"""Add optional full-audio review and retained MiniMax attempts."""

from alembic import op
import sqlalchemy as sa


revision = "0010_audio_review"
down_revision = "0009_pronunciation_dict"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite supports ADD COLUMN directly. Rebuilding this parent table would
    # briefly drop it and trigger ON DELETE CASCADE on existing batch items.
    op.add_column(
        "generation_batches",
        sa.Column(
            "review_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )
    )
    op.add_column(
        "audio_generation_tasks",
        sa.Column(
            "generation_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        )
    )
    op.add_column(
        "audio_generation_tasks",
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        )
    )
    op.create_table(
        "audio_generation_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("audio_task_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("provider_task_id", sa.String(length=100), nullable=True),
        sa.Column("provider_file_id", sa.String(length=100), nullable=True),
        sa.Column("output_path", sa.String(length=500), nullable=False),
        sa.Column("subtitle_path", sa.String(length=500), nullable=True),
        sa.Column(
            "status",
            sa.String(length=30),
            nullable=False,
            server_default="READY",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["audio_task_id"],
            ["audio_generation_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "audio_task_id",
            "version",
            name="uq_audio_attempts_task_version",
        ),
    )
    op.create_index(
        "ix_audio_generation_attempts_audio_task_id",
        "audio_generation_attempts",
        ["audio_task_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audio_generation_attempts_audio_task_id",
        table_name="audio_generation_attempts",
    )
    op.drop_table("audio_generation_attempts")
    op.drop_column("audio_generation_tasks", "reviewed_at")
    op.drop_column("audio_generation_tasks", "generation_version")
    op.drop_column("generation_batches", "review_required")
