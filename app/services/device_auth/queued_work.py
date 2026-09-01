"""Durable, server-owned admission for work that executes after its HTTP request.

No token/private key is stored here. A queued operation may outlive its short HTTP
token, but each paid dispatch still checks the *current* grant and policy. An
admin pause can therefore stop the queue without destroying paid-task history.
"""

from __future__ import annotations

import hashlib
import time
from typing import Iterable

from sqlalchemy.orm import Session

from app.models import (
    BATCH_SOURCE_H3_WORKBENCH,
    BATCH_SOURCE_NEW_WORKBENCH,
    GenerationTask,
)
from . import service
from .admission import WorkbenchIdentity, require_new_work
from .errors import DeviceAuthError
from .models import (
    WorkbenchDeviceAuditEvent,
    WorkbenchDeviceOperation,
    WorkbenchDeviceWorkBinding,
)
from .protocol import canonical_json


def bind_new_operation(
    db: Session,
    *,
    user_id: int,
    identity: WorkbenchIdentity | None,
    operation_kind: str,
    request_snapshot: dict,
    resources: Iterable[tuple[str, str]],
) -> WorkbenchDeviceOperation:
    """Call inside the same transaction as the business mutation, after ownership.

    Idempotent receipt reads must return before this function. Do not accept an
    operation ID or claims from JSON; identity is the verified request context.
    """
    service.lock_control(db)
    require_new_work(db, user_id=user_id, identity=identity)
    if operation_kind not in {
        "h3.generate",
        "h3.retry",
        "h3.regenerate",
        "h3.resume_authorization",
        "workbench.composition",
        "workbench.composition.retry",
        "workbench.enhancement.backfill",
        "workbench.audio.generate",
        "workbench.audio.retry",
        "workbench.voice.create",
        "workbench.voice.save",
    }:
        raise ValueError("unknown admitted operation")
    claims = identity.claims if identity and identity.claims else {}
    operation = WorkbenchDeviceOperation(
        user_id=user_id,
        device_id=claims.get("device_id"),
        grant_id=claims.get("grant_id"),
        thumbprint=identity.thumbprint if identity else None,
        grant_revision=claims.get("grant_revision"),
        policy_revision=claims.get("policy_revision"),
        scope="cloud:generate",
        operation_kind=operation_kind,
        request_digest=hashlib.sha256(
            canonical_json(request_snapshot).encode("utf-8")
        ).hexdigest(),
        admission_mode=service.current_mode(db),
    )
    db.add(operation)
    db.flush()
    for kind, resource_id in set(resources):
        if (
            kind
            not in {
                "generation_segment",
                "generation_task",
                "audio_generation_task",
                "voice_creation_task",
            }
            or not resource_id
            or len(resource_id) > 64
        ):
            raise ValueError("invalid admitted resource")
        binding = db.get(
            WorkbenchDeviceWorkBinding, (kind, resource_id), populate_existing=True
        )
        if binding is None:
            binding = WorkbenchDeviceWorkBinding(
                resource_kind=kind, resource_id=resource_id, user_id=user_id
            )
            db.add(binding)
        elif binding.user_id != user_id:
            raise DeviceAuthError(
                "DEVICE_ACCOUNT_MISMATCH", "操作授权与任务所属账号不一致", 403
            )
        binding.operation_id = operation.id
        binding.blocked_code = None
        binding.blocked_at = None
        binding.last_phase = "admitted"
    return operation


def task_resource(task: GenerationTask) -> tuple[str, str]:
    # Future soft-chain tasks inherit the original segment's admission. Even a
    # malformed/old task without a segment must not escape strong-mode checks.
    return (
        ("generation_segment", task.segment.id)
        if task.segment is not None
        else ("generation_task", task.id)
    )


def h3_task_resource(task: GenerationTask) -> tuple[str, str]:
    """Compatibility name retained for recovery/tests; resources are generic."""
    return task_resource(task)


def task_source_channel(task: GenerationTask) -> str | None:
    item = task.segment.batch_item if task.segment is not None else task.batch_item
    return (
        item.batch.source_channel
        if item is not None and item.batch is not None
        else None
    )


def task_requires_device_admission(task: GenerationTask) -> bool:
    return task_source_channel(task) in {
        BATCH_SOURCE_H3_WORKBENCH,
        BATCH_SOURCE_NEW_WORKBENCH,
    }


def resource_is_admitted(
    db: Session,
    *,
    user_id: int,
    resource: tuple[str, str],
    phase: str,
) -> bool:
    """Return False for an authorization wait, without rewriting provider state.

    Callers invoke this only before a provider submission that has no confirmed
    remote receipt yet. Existing remote IDs remain queryable in the worker's
    recovery branch. Legacy website submissions retain their separate website
    authorization contract. Callers commit audit/wait state and never submit
    after False.
    """
    if phase not in {"queue", "dispatch", "submit"}:
        raise ValueError("invalid admission phase")
    mode = service.current_mode(db)
    if mode != "OFF" and phase == "submit":
        # Serialize the final admission decision with admin state changes. The
        # worker commits this decision immediately before the provider call;
        # revocation cannot cancel an already-started external request.
        mode = service.lock_control(db).mode
    if mode not in {"OFF", "OBSERVE", "ENFORCE"}:
        raise DeviceAuthError("DEVICE_CONTROL_INVALID", "设备授权控制状态异常", 503)
    key = resource
    binding = db.get(WorkbenchDeviceWorkBinding, key, populate_existing=True)
    if mode == "OFF":
        if binding and binding.blocked_code:
            binding.blocked_code = None
            binding.blocked_at = None
            binding.last_phase = phase
        return True
    operation = (
        db.get(WorkbenchDeviceOperation, binding.operation_id)
        if binding and binding.operation_id
        else None
    )
    reason = None
    try:
        service._active_user(db, user_id)
        if binding is not None and binding.user_id != user_id:
            raise DeviceAuthError("DEVICE_ACCOUNT_MISMATCH", "任务账号与授权记录不一致")
        if operation is None or not operation.thumbprint:
            raise DeviceAuthError(
                "DEVICE_ADMISSION_REQUIRED", "请在获准工作台重新确认待执行任务"
            )
        if operation.user_id != user_id or operation.scope != "cloud:generate":
            raise DeviceAuthError(
                "DEVICE_ACCOUNT_MISMATCH", "任务账号或权限与授权记录不一致"
            )
        device, grant, _ = service.require_active_grant(
            db,
            user_id=user_id,
            thumbprint=operation.thumbprint,
            scope="cloud:generate",
            now=int(time.time()),
        )
        if (device.id, grant.id) != (operation.device_id, operation.grant_id):
            raise DeviceAuthError(
                "DEVICE_ADMISSION_REQUIRED", "任务绑定的授权关系已改变"
            )
        # Revisions are retained for audit, not a stale permanent allowance.
        # Check the current grant/scope/policy rather than an expired HTTP JWT;
        # benign rename/quota updates do not require users to re-pay a batch.
    except DeviceAuthError as exc:
        reason = exc.code
    if binding is None:
        binding = WorkbenchDeviceWorkBinding(
            resource_kind=key[0], resource_id=key[1], user_id=user_id
        )
        db.add(binding)
    old_reason = binding.blocked_code
    if old_reason != reason:
        db.add(
            WorkbenchDeviceAuditEvent(
                subject_user_id=user_id,
                device_id=operation.device_id if operation else None,
                grant_id=operation.grant_id if operation else None,
                action="device.work_wait" if reason else "device.work_resumed",
                details_json=canonical_json(
                    {
                        "resource_kind": key[0],
                        "resource_id": key[1],
                        "operation_id": operation.id if operation else None,
                        "reason": reason,
                        "mode": mode,
                        "phase": phase,
                    }
                ),
            )
        )
        binding.blocked_at = int(time.time()) if reason else None
    binding.blocked_code = reason
    binding.last_phase = phase
    return reason is None or mode == "OBSERVE"


def task_is_admitted(db: Session, task: GenerationTask, *, phase: str) -> bool:
    """Check one not-yet-submitted task created by a protected workbench."""
    if not task_requires_device_admission(task) or task.runninghub_task_id:
        return True
    return resource_is_admitted(
        db,
        user_id=task.user_id,
        resource=task_resource(task),
        phase=phase,
    )


def inherit_operation_binding(
    db: Session,
    *,
    user_id: int,
    source: tuple[str, str],
    targets: Iterable[tuple[str, str]],
) -> None:
    """Attach child queue resources to the already-admitted parent operation."""
    parent = db.get(WorkbenchDeviceWorkBinding, source, populate_existing=True)
    if parent is None or parent.user_id != user_id or not parent.operation_id:
        raise DeviceAuthError(
            "DEVICE_ADMISSION_REQUIRED", "付费任务缺少设备准入记录", 403
        )
    for kind, resource_id in set(targets):
        if kind not in {"generation_segment", "generation_task"} or not resource_id:
            raise ValueError("invalid inherited admitted resource")
        binding = db.get(
            WorkbenchDeviceWorkBinding,
            (kind, resource_id),
            populate_existing=True,
        )
        if binding is None:
            binding = WorkbenchDeviceWorkBinding(
                resource_kind=kind,
                resource_id=resource_id,
                user_id=user_id,
            )
            db.add(binding)
        elif binding.user_id != user_id:
            raise DeviceAuthError(
                "DEVICE_ACCOUNT_MISMATCH", "子任务与设备准入账号不一致", 403
            )
        binding.operation_id = parent.operation_id
        binding.blocked_code = None
        binding.blocked_at = None
        binding.last_phase = "admitted"


def h3_task_is_admitted(db: Session, task: GenerationTask, *, phase: str) -> bool:
    """Compatibility wrapper for callers migrating to generic task admission."""
    return task_is_admitted(db, task, phase=phase)
