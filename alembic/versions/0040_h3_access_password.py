"""add encrypted H3 workflow access password

Revision ID: 0040_h3_access_password
Revises: 0039_h3_workbench_snapshots
Create Date: 2026-08-22
"""

from alembic import op
import sqlalchemy as sa


revision = "0040_h3_access_password"
down_revision = "0039_h3_workbench_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runninghub_h3_capabilities",
        sa.Column("access_password_encrypted", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(
        "runninghub_h3_capabilities",
        "access_password_encrypted",
    )
