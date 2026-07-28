"""Persist batch-level MiniMax pronunciation rules."""

from alembic import op
import sqlalchemy as sa


revision = "0009_pronunciation_dict"
down_revision = "0008_minimax_async_timestamps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("audio_generation_tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "pronunciation_dict_json",
                sa.Text(),
                nullable=False,
                server_default="[]",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("audio_generation_tasks") as batch_op:
        batch_op.drop_column("pronunciation_dict_json")
