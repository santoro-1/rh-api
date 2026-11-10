"""add canonical two-line title branch to content analysis

Revision ID: 0030_content_analysis_title
Revises: 0029_seedvr2_video_enhancement
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa


revision = "0030_content_analysis_title"
down_revision = "0029_seedvr2_video_enhancement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("content_analysis_caches") as batch_op:
        batch_op.add_column(
            sa.Column(
                "title_analysis_status",
                sa.String(length=20),
                server_default="FAILED",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("title_json", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("title_error_code", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("title_error_summary", sa.String(length=500), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("content_analysis_caches") as batch_op:
        batch_op.drop_column("title_error_summary")
        batch_op.drop_column("title_error_code")
        batch_op.drop_column("title_json")
        batch_op.drop_column("title_analysis_status")
