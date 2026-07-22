"""Add workflow definitions to task and configuration persistence."""

from alembic import op
import sqlalchemy as sa


revision = "0003_workflow_adapters"
down_revision = "0002_runninghub_submitted_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("workflow_key", sa.String(length=100), nullable=False),
        sa.Column("ai_app_id", sa.String(length=100), nullable=False),
        sa.Column("instance_type", sa.String(length=20), nullable=False),
        sa.Column("default_prompt", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "workflow_key", name="uq_workflow_configs_user_workflow"),
    )
    op.create_index("ix_workflow_configs_user_id", "workflow_configs", ["user_id"], unique=False)

    # Existing account-level digital-human settings become the first workflow
    # settings row.  Credentials stay in runninghub_configs and are not copied.
    op.execute(
        """
        INSERT INTO workflow_configs
            (user_id, workflow_key, ai_app_id, instance_type, default_prompt,
             is_enabled, settings_json, created_at, updated_at)
        SELECT user_id, 'digital_human', ai_app_id, instance_type, default_prompt,
               1, NULL, created_at, updated_at
        FROM runninghub_configs
        """
    )

    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "workflow_type",
                sa.String(length=100),
                nullable=False,
                server_default="digital_human",
            )
        )
        batch_op.add_column(sa.Column("input_payload", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("output_metadata", sa.Text(), nullable=True))
    op.create_index(
        "ix_generation_tasks_workflow_type",
        "generation_tasks",
        ["workflow_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_generation_tasks_workflow_type", table_name="generation_tasks")
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.drop_column("output_metadata")
        batch_op.drop_column("input_payload")
        batch_op.drop_column("workflow_type")
    op.drop_index("ix_workflow_configs_user_id", table_name="workflow_configs")
    op.drop_table("workflow_configs")
