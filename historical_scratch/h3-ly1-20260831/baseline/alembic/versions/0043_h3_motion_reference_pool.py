"""freeze per-segment H3 motion references

Revision ID: 0043_h3_motion_reference_pool
Revises: 0042_h3_loop_anchor_mode
Create Date: 2026-08-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0043_h3_motion_reference_pool"
down_revision = "0042_h3_loop_anchor_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("h3_segment_configs") as batch_op:
        batch_op.add_column(
            sa.Column("motion_reference_index", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("motion_reference_path", sa.String(length=500), nullable=True)
        )
        batch_op.add_column(
            sa.Column("motion_reference_sha256", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("h3_segment_configs") as batch_op:
        batch_op.drop_column("motion_reference_sha256")
        batch_op.drop_column("motion_reference_path")
        batch_op.drop_column("motion_reference_index")
