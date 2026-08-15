"""add per-item workbench execution account snapshots

Revision ID: 0035_workbench_item_execution_pool
Revises: 0034_runninghub_credential_balance
Create Date: 2026-08-15
"""

from alembic import op
import sqlalchemy as sa


revision = "0035_workbench_item_execution_pool"
down_revision = "0034_runninghub_credential_balance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_batch_items",
        sa.Column("runninghub_execution_account_ids_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "generation_batch_items",
        sa.Column("seedvr2_execution_account_ids_json", sa.Text(), nullable=True),
    )
    op.execute(
        """
        UPDATE generation_batch_items
        SET runninghub_execution_account_ids_json = (
                SELECT generation_batches.runninghub_execution_account_ids_json
                FROM generation_batches
                WHERE generation_batches.id = generation_batch_items.batch_id
            ),
            seedvr2_execution_account_ids_json = (
                SELECT generation_batches.seedvr2_execution_account_ids_json
                FROM generation_batches
                WHERE generation_batches.id = generation_batch_items.batch_id
            )
        """
    )


def downgrade() -> None:
    op.drop_column(
        "generation_batch_items", "seedvr2_execution_account_ids_json"
    )
    op.drop_column(
        "generation_batch_items", "runninghub_execution_account_ids_json"
    )
