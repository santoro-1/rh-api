"""Add temporary long-audio preprocessing projects."""

from alembic import op
import sqlalchemy as sa


revision = "0012_long_audio_projects"
down_revision = "0011_system_voice_categories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "long_audio_projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("script_text", sa.Text(), nullable=False),
        sa.Column("audio_path", sa.String(length=500), nullable=False),
        sa.Column("audio_original_name", sa.String(length=255), nullable=False),
        sa.Column("video_path", sa.String(length=500), nullable=False),
        sa.Column("video_original_name", sa.String(length=255), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("parameters_json", sa.Text(), nullable=False),
        sa.Column("plan_json", sa.Text(), nullable=True),
        sa.Column(
            "alignment_provider",
            sa.String(length=50),
            nullable=False,
            server_default="funasr_http",
        ),
        sa.Column(
            "status",
            sa.String(length=30),
            nullable=False,
            server_default="PENDING_ANALYSIS",
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["generation_batches.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id"),
    )
    op.create_index(
        "ix_long_audio_projects_user_id",
        "long_audio_projects",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_long_audio_projects_status",
        "long_audio_projects",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_long_audio_projects_expires_at",
        "long_audio_projects",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_long_audio_projects_expires_at",
        table_name="long_audio_projects",
    )
    op.drop_index(
        "ix_long_audio_projects_status",
        table_name="long_audio_projects",
    )
    op.drop_index(
        "ix_long_audio_projects_user_id",
        table_name="long_audio_projects",
    )
    op.drop_table("long_audio_projects")
