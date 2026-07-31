"""Add leases and metrics for pull-based remote media workers."""

from alembic import op
import sqlalchemy as sa


revision = "0013_remote_media_worker"
down_revision = "0012_long_audio_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("long_audio_projects") as batch_op:
        batch_op.add_column(
            sa.Column("remote_lease_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("remote_worker_id", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "remote_lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "remote_last_heartbeat_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("remote_metrics_json", sa.Text(), nullable=True)
        )
        batch_op.create_index(
            "ix_long_audio_projects_remote_lease_id",
            ["remote_lease_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_long_audio_projects_remote_lease_expires_at",
            ["remote_lease_expires_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("long_audio_projects") as batch_op:
        batch_op.drop_index(
            "ix_long_audio_projects_remote_lease_expires_at"
        )
        batch_op.drop_index("ix_long_audio_projects_remote_lease_id")
        batch_op.drop_column("remote_metrics_json")
        batch_op.drop_column("remote_last_heartbeat_at")
        batch_op.drop_column("remote_lease_expires_at")
        batch_op.drop_column("remote_worker_id")
        batch_op.drop_column("remote_lease_id")
