"""Add device identities, approvals and persistent proof replay protection.

Revision ID: 0049_workbench_devices
Revises: 0048_legacy_task_runninghub_pool
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0049_workbench_devices"
down_revision = "0048_legacy_task_runninghub_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add only independent tables. Never rebuild users or existing task parents.
    op.create_table(
        "workbench_device_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_device_control_singleton"),
        sa.CheckConstraint(
            "mode IN ('OFF','OBSERVE','ENFORCE')", name="ck_device_control_mode"
        ),
    )
    op.execute(
        "INSERT INTO workbench_device_control (id, mode, revision) VALUES (1, 'OFF', 1)"
    )
    op.create_table(
        "workbench_devices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("thumbprint", sa.String(43), nullable=False, unique=True),
        sa.Column("public_jwk_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("protection_report", sa.String(32), nullable=False),
        sa.Column("protection_verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE','SUSPENDED','REVOKED')", name="ck_device_status"
        ),
    )
    op.create_table(
        "workbench_device_policies",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("max_devices", sa.Integer(), nullable=False),
        sa.Column("allow_software", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "max_devices BETWEEN 0 AND 1000", name="ck_device_policy_quota"
        ),
    )
    op.create_table(
        "workbench_device_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "device_id",
            sa.String(36),
            sa.ForeignKey("workbench_devices.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("client_version", sa.String(80), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.UniqueConstraint("user_id", "device_id", name="uq_device_grant_user_device"),
        sa.CheckConstraint(
            "status IN ('PENDING','ACTIVE','REJECTED','SUSPENDED','REVOKED')",
            name="ck_device_grant_status",
        ),
    )
    op.create_index(
        "ix_workbench_device_grants_user_id", "workbench_device_grants", ["user_id"]
    )
    op.create_index(
        "ix_workbench_device_grants_device_id", "workbench_device_grants", ["device_id"]
    )
    op.create_table(
        "workbench_device_challenges",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("thumbprint", sa.String(43), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("consumed_at", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_workbench_device_challenges_user_id",
        "workbench_device_challenges",
        ["user_id"],
    )
    op.create_index(
        "ix_workbench_device_challenges_expires_at",
        "workbench_device_challenges",
        ["expires_at"],
    )
    op.create_table(
        "workbench_device_proof_replays",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_workbench_device_proof_replays_expires_at",
        "workbench_device_proof_replays",
        ["expires_at"],
    )
    op.create_table(
        "workbench_device_audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("subject_user_id", sa.Integer(), nullable=True),
        sa.Column("device_id", sa.String(36), nullable=True),
        sa.Column("grant_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_workbench_device_audit_events_subject_user_id",
        "workbench_device_audit_events",
        ["subject_user_id"],
    )


def downgrade() -> None:
    # Downgrading below the licensing schema is destructive and must be explicit.
    # Normal application rollback keeps these tables; see the development guide.
    for table in (
        "workbench_device_audit_events",
        "workbench_device_proof_replays",
        "workbench_device_challenges",
        "workbench_device_grants",
        "workbench_device_policies",
        "workbench_devices",
        "workbench_device_control",
    ):
        op.drop_table(table)
