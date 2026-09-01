"""Additive licensing records. No task ownership or provider state lives here."""

from __future__ import annotations

import time
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def now_seconds() -> int:
    return int(time.time())


class WorkbenchDeviceControl(Base):
    __tablename__ = "workbench_device_control"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_device_control_singleton"),
        CheckConstraint(
            "mode IN ('OFF','OBSERVE','ENFORCE')", name="ck_device_control_mode"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(10), default="OFF", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class WorkbenchDevice(Base):
    __tablename__ = "workbench_devices"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE','SUSPENDED','REVOKED')", name="ck_device_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    thumbprint: Mapped[str] = mapped_column(String(43), unique=True, nullable=False)
    public_jwk_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="ACTIVE", nullable=False)
    protection_report: Mapped[str] = mapped_column(String(32), nullable=False)
    # Hardware provenance is NOT attested by self-reported provider names.
    protection_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[int] = mapped_column(
        Integer, default=now_seconds, nullable=False
    )
    last_seen_at: Mapped[int] = mapped_column(
        Integer, default=now_seconds, nullable=False
    )


class WorkbenchDevicePolicy(Base):
    __tablename__ = "workbench_device_policies"
    __table_args__ = (
        CheckConstraint(
            "max_devices BETWEEN 0 AND 1000", name="ck_device_policy_quota"
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    max_devices: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    allow_software: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class WorkbenchDeviceGrant(Base):
    __tablename__ = "workbench_device_grants"
    __table_args__ = (
        UniqueConstraint("user_id", "device_id", name="uq_device_grant_user_device"),
        CheckConstraint(
            "status IN ('PENDING','ACTIVE','REJECTED','SUSPENDED','REVOKED')",
            name="ck_device_grant_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("workbench_devices.id"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(12), default="PENDING", nullable=False)
    label: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    client_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    scopes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[int] = mapped_column(
        Integer, default=now_seconds, nullable=False
    )
    updated_at: Mapped[int] = mapped_column(
        Integer, default=now_seconds, nullable=False
    )


class WorkbenchDeviceChallenge(Base):
    __tablename__ = "workbench_device_challenges"

    # Store only a digest of the challenge. Purpose separates registration and use.
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    thumbprint: Mapped[str] = mapped_column(String(43), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    consumed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WorkbenchDeviceProofReplay(Base):
    __tablename__ = "workbench_device_proof_replays"

    # Digest of key thumbprint + jti, not the JWT itself; unique across workers.
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[int] = mapped_column(Integer, index=True, nullable=False)


class WorkbenchDeviceAuditEvent(Base):
    __tablename__ = "workbench_device_audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Deliberately not cascading FKs: retain minimal audit after account removal.
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subject_user_id: Mapped[int | None] = mapped_column(
        Integer, index=True, nullable=True
    )
    device_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    grant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    details_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[int] = mapped_column(
        Integer, default=now_seconds, nullable=False
    )


class WorkbenchDeviceOperation(Base):
    """Server-created authorization snapshot; never a bearer credential."""

    __tablename__ = "workbench_device_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    device_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    grant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    thumbprint: Mapped[str | None] = mapped_column(String(43), nullable=True)
    grant_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    admission_mode: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[int] = mapped_column(
        Integer, default=now_seconds, nullable=False
    )


class WorkbenchDeviceWorkBinding(Base):
    """Current operation and authorization wait state, apart from provider status.

    Resource IDs are resolved from owned database objects, never from a caller's
    assertion of an admission ID. Operations remain as history on paid retries.
    """

    __tablename__ = "workbench_device_work_bindings"
    __table_args__ = (
        CheckConstraint(
            "resource_kind IN ("
            "'generation_segment','generation_task',"
            "'audio_generation_task','voice_creation_task'"
            ")",
            name="ck_device_work_resource_kind",
        ),
    )

    resource_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("workbench_device_operations.id", ondelete="CASCADE"), nullable=True
    )
    blocked_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blocked_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_phase: Mapped[str | None] = mapped_column(String(16), nullable=True)
