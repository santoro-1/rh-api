"""Persist structured RunningHub workflow failure details."""

from alembic import op
import sqlalchemy as sa


revision = "0014_runninghub_failure_details"
down_revision = "0013_remote_media_worker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("runninghub_failed_reason", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.drop_column("runninghub_failed_reason")
