"""Add bounded automatic retries for confirmed RunningHub failures."""

from alembic import op
import sqlalchemy as sa


revision = "0015_runninghub_auto_retry"
down_revision = "0014_runninghub_failure_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("runninghub_attempt_history", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "runninghub_auto_retry_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "runninghub_auto_retry_after",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_generation_tasks_runninghub_auto_retry_after",
            ["runninghub_auto_retry_after"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.drop_index(
            "ix_generation_tasks_runninghub_auto_retry_after"
        )
        batch_op.drop_column("runninghub_auto_retry_after")
        batch_op.drop_column("runninghub_auto_retry_count")
        batch_op.drop_column("runninghub_attempt_history")
