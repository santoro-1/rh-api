"""Track the time a task was submitted to RunningHub."""

from alembic import op
import sqlalchemy as sa


revision = "0002_runninghub_submitted_at"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("runninghub_submitted_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("generation_tasks") as batch_op:
        batch_op.drop_column("runninghub_submitted_at")
