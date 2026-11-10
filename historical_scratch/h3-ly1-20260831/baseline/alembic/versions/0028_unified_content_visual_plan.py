"""add visual branch to unified content analysis cache

Revision ID: 0028_unified_content_visual_plan
Revises: 0027_audio_primary_sha256
"""

from alembic import op
import sqlalchemy as sa


revision = "0028_unified_content_visual_plan"
down_revision = "0027_audio_primary_sha256"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("content_analysis_caches") as batch_op:
        batch_op.drop_constraint("uq_content_analysis_cache_key", type_="unique")
        batch_op.add_column(
            sa.Column(
                "visual_catalog_version",
                sa.String(length=128),
                server_default="none",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "visual_context_sha256",
                sa.String(length=64),
                server_default="0" * 64,
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "visual_analysis_status",
                sa.String(length=20),
                server_default="FAILED",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("visual_plan_json", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("visual_error_code", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("visual_error_summary", sa.String(length=500), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_content_analysis_cache_key",
            [
                "user_id",
                "script_sha256",
                "schema_version",
                "prompt_version",
                "model",
                "visual_catalog_version",
                "visual_context_sha256",
            ],
        )


def downgrade() -> None:
    with op.batch_alter_table("content_analysis_caches") as batch_op:
        batch_op.drop_constraint("uq_content_analysis_cache_key", type_="unique")
        batch_op.drop_column("visual_error_summary")
        batch_op.drop_column("visual_error_code")
        batch_op.drop_column("visual_plan_json")
        batch_op.drop_column("visual_analysis_status")
        batch_op.drop_column("visual_context_sha256")
        batch_op.drop_column("visual_catalog_version")
        batch_op.create_unique_constraint(
            "uq_content_analysis_cache_key",
            [
                "user_id",
                "script_sha256",
                "schema_version",
                "prompt_version",
                "model",
            ],
        )
