"""connect automatic long-audio processing to batch rows

Revision ID: 0017_batch_long_audio
Revises: 0016_long_audio_workflows
"""

from alembic import op
import sqlalchemy as sa


revision = "0017_batch_long_audio"
down_revision = "0016_long_audio_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("long_audio_projects") as batch_op:
        batch_op.add_column(
            sa.Column("batch_item_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_long_audio_projects_batch_item_id",
            "generation_batch_items",
            ["batch_item_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_long_audio_projects_batch_item_id",
            ["batch_item_id"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("long_audio_projects") as batch_op:
        batch_op.drop_index("ix_long_audio_projects_batch_item_id")
        batch_op.drop_constraint(
            "fk_long_audio_projects_batch_item_id",
            type_="foreignkey",
        )
        batch_op.drop_column("batch_item_id")
