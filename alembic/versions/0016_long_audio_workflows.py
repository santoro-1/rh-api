"""support both long-audio workflows and optional review

Revision ID: 0016_long_audio_workflows
Revises: 0015_runninghub_auto_retry
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_long_audio_workflows"
down_revision = "0015_runninghub_auto_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("long_audio_projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "workflow_type",
                sa.String(length=100),
                nullable=False,
                server_default="ltx_lip_sync",
            )
        )
        batch_op.add_column(
            sa.Column(
                "review_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index(
            "ix_long_audio_projects_workflow_type",
            ["workflow_type"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("long_audio_projects") as batch_op:
        batch_op.drop_index("ix_long_audio_projects_workflow_type")
        batch_op.drop_column("review_required")
        batch_op.drop_column("workflow_type")
