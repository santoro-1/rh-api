"""Create initial application tables."""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=False)
    op.create_table(
        "runninghub_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("ai_app_id", sa.String(length=100), nullable=False),
        sa.Column("instance_type", sa.String(length=20), nullable=False),
        sa.Column("default_prompt", sa.Text(), nullable=False),
        sa.Column("max_concurrent_tasks", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_table(
        "generation_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("runninghub_task_id", sa.String(length=100), nullable=True),
        sa.Column("image_path", sa.String(length=500), nullable=False),
        sa.Column("audio_path", sa.String(length=500), nullable=False),
        sa.Column("image_original_name", sa.String(length=255), nullable=False),
        sa.Column("audio_original_name", sa.String(length=255), nullable=False),
        sa.Column("audio_duration_seconds", sa.Float(), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("runninghub_usage", sa.Text(), nullable=True),
        sa.Column("result_path", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("runninghub_task_id"),
    )
    op.create_index("ix_generation_tasks_user_id", "generation_tasks", ["user_id"], unique=False)
    op.create_index("ix_generation_tasks_runninghub_task_id", "generation_tasks", ["runninghub_task_id"], unique=False)
    op.create_index("ix_generation_tasks_status", "generation_tasks", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_generation_tasks_status", table_name="generation_tasks")
    op.drop_index("ix_generation_tasks_runninghub_task_id", table_name="generation_tasks")
    op.drop_index("ix_generation_tasks_user_id", table_name="generation_tasks")
    op.drop_table("generation_tasks")
    op.drop_table("runninghub_configs")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
