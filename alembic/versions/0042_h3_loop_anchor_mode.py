"""add H3 loop-anchor continuity mode

Revision ID: 0042_h3_loop_anchor_mode
Revises: 0041_user_h3_access
Create Date: 2026-08-24
"""

from alembic import op


revision = "0042_h3_loop_anchor_mode"
down_revision = "0041_user_h3_access"
branch_labels = None
depends_on = None


_TABLE_CONSTRAINTS = (
    ("h3_batch_configs", "ck_h3_batch_continuity_mode"),
    ("h3_item_configs", "ck_h3_item_continuity_mode"),
    ("h3_segment_configs", "ck_h3_segment_continuity_mode"),
)


def _replace_constraints(expression: str) -> None:
    for table_name, constraint_name in _TABLE_CONSTRAINTS:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(constraint_name, type_="check")
            batch_op.create_check_constraint(constraint_name, expression)


def upgrade() -> None:
    _replace_constraints("continuity_mode IN ('loop_anchor', 'fast', 'soft_chain')")


def downgrade() -> None:
    for table_name, _constraint_name in _TABLE_CONSTRAINTS:
        op.execute(
            f"UPDATE {table_name} SET continuity_mode = 'fast' "
            "WHERE continuity_mode = 'loop_anchor'"
        )
    _replace_constraints("continuity_mode IN ('fast', 'soft_chain')")
