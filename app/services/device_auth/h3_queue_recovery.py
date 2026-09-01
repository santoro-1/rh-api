"""Explicit owner-approved rebinding of existing, unsubmitted H3 work.

Never resets task/provider state, creates a generation task, or calls a provider.
The review digest is a concurrency check, not a license or a bearer credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

from sqlalchemy import select

from app.models import GenerationBatch, H3BatchConfig
from app.services.runninghub_attempts import (
    latest_task_attempt,
    task_has_uncertain_submission,
)
from . import service
from .admission import WorkbenchIdentity
from .errors import DeviceAuthError
from .models import (
    WorkbenchDeviceAuditEvent,
    WorkbenchDeviceOperation,
    WorkbenchDeviceWorkBinding,
)
from .protocol import canonical_json
from .queued_work import bind_new_operation

SCHEMA = "runninghub.h3-authorization-recovery.v1"
KIND = "h3.resume_authorization"


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _owned_batch(db, user, batch_id):
    from app.services.h3_workbench import get_h3_batch

    batch = get_h3_batch(db, user, batch_id)
    if batch is None or batch.h3_config is None:
        raise DeviceAuthError("H3_BATCH_NOT_FOUND", "H3 批次不存在", 404)
    return batch


def _strict_identity(db, user_id, identity):
    # Rebinding must prove a current grant even while rollout is OFF/OBSERVE.
    if (
        not isinstance(identity, WorkbenchIdentity)
        or not identity.claims
        or not identity.thumbprint
    ):
        raise DeviceAuthError(
            "DEVICE_BOUND_TOKEN_REQUIRED", "请先校验本机设备授权", 401
        )
    claims = identity.claims
    if (
        identity.user_id != user_id
        or claims["user_id"] != user_id
        or claims["cnf"]["jkt"] != identity.thumbprint
    ):
        raise DeviceAuthError("DEVICE_ACCOUNT_MISMATCH", "任务与设备账号不一致", 403)
    now = int(time.time())
    if now >= claims["exp"]:
        raise DeviceAuthError("AUTH_REFRESH_REQUIRED", "请刷新本机授权后继续", 401)
    service.validate_bound_claims(
        db, claims=dict(claims), scope="cloud:generate", now=now
    )


def _binding_reason(db, user_id, binding):
    if binding is not None and binding.user_id != user_id:
        raise DeviceAuthError(
            "DEVICE_ACCOUNT_MISMATCH", "任务授权关联异常，请联系管理员", 403
        )
    operation = (
        db.get(WorkbenchDeviceOperation, binding.operation_id)
        if binding and binding.operation_id
        else None
    )
    if operation is not None and (
        operation.user_id != user_id or operation.scope != "cloud:generate"
    ):
        raise DeviceAuthError(
            "DEVICE_ACCOUNT_MISMATCH", "任务授权记录异常，请联系管理员", 403
        )
    if operation is None or not operation.thumbprint:
        return "DEVICE_ADMISSION_REQUIRED"
    try:
        device, grant, _ = service.require_active_grant(
            db,
            user_id=user_id,
            thumbprint=operation.thumbprint,
            scope="cloud:generate",
            now=int(time.time()),
        )
        if (device.id, grant.id) != (operation.device_id, operation.grant_id):
            return "DEVICE_ADMISSION_REQUIRED"
    except DeviceAuthError as exc:
        return exc.code
    return None


def _review(db, user, batch):
    resources, rows, versions = [], [], []
    if batch.h3_config.confirmed_at is not None and batch.status != "CANCELLED":
        for item in batch.items:
            if item.status == "CANCELLED":
                continue
            for segment in sorted(item.segments, key=lambda value: value.segment_index):
                if segment.h3_config is None or segment.status in {
                    "CANCELLED",
                    "SUCCESS",
                    "FAILED",
                }:
                    continue
                task = segment.generation_task
                latest = latest_task_attempt(task) if task else None
                if task is not None:
                    if (
                        task.user_id != user.id
                        or task.workflow_type != "minimax_h3_ref2va"
                    ):
                        raise DeviceAuthError(
                            "DEVICE_ACCOUNT_MISMATCH", "分段任务归属异常", 403
                        )
                    if (
                        task.status != "PENDING"
                        or task.runninghub_task_id
                        or task.runninghub_submitted_at
                        or task_has_uncertain_submission(task)
                        or task.result_path
                        or (
                            latest
                            and latest.finished_at is None
                            and (
                                latest.remote_task_id
                                or latest.submitted_at
                                or latest.status
                                in {"RESERVED", "UPLOADING", "SUBMIT_UNKNOWN"}
                            )
                        )
                    ):
                        continue
                elif not (
                    segment.status == "WAITING_DEPENDENCY"
                    and segment.segment_index > 0
                    and segment.h3_config.continuity_mode == "soft_chain"
                ):
                    continue
                key = ("generation_segment", segment.id)
                binding = db.get(
                    WorkbenchDeviceWorkBinding, key, populate_existing=True
                )
                reason = _binding_reason(db, user.id, binding)
                if reason is None:
                    continue
                resources.append(key)
                rows.append(
                    {
                        "segment_id": segment.id,
                        "row_id": item.row_key,
                        "segment_number": segment.segment_index + 1,
                        "code": reason,
                    }
                )
                versions.append(
                    {
                        "segment_id": segment.id,
                        "input": segment.h3_config.input_sha256,
                        "segment_status": segment.status,
                        "task_id": task.id if task else None,
                        "task_input": _digest(task.input_payload) if task else None,
                        "task_status": task.status if task else None,
                        "attempt": latest.id if latest else None,
                        "operation_id": binding.operation_id if binding else None,
                        "reason": reason,
                    }
                )
    snapshot = {
        "batch_id": batch.id,
        "contract": batch.h3_config.input_sha256,
        "fee": batch.h3_config.fee_snapshot_json,
        "resources": versions,
    }
    return {
        "schema": SCHEMA,
        "batch_id": batch.id,
        "name": batch.name,
        "segment_count": len(resources),
        "segments": rows,
        "review_token": _digest(snapshot),
        "can_resume": bool(resources),
        "notice": "仅继续本批次已确认但尚未提交的分段，按原计划产生生成费用；已提交或提交结果未知的任务不重发。",
    }, resources


def prepare_recovery(db, user, batch_id):
    service._active_user(db, user.id)
    batch = _owned_batch(db, user, batch_id)
    return _review(db, user, batch)[0]


def list_waiting_batches(db, user, *, after_id=""):
    service._active_user(db, user.id)
    if after_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", after_id):
        raise DeviceAuthError("INVALID_RECOVERY_CURSOR", "待恢复列表游标无效", 422)
    if service.current_mode(db) != "ENFORCE":
        return {"schema": SCHEMA, "batches": [], "next_cursor": None}
    # Stable ID pagination, not an unbounded scan or a silent latest-N cutoff.
    from app.models import BATCH_SOURCE_H3_WORKBENCH

    ids = list(
        db.scalars(
            select(GenerationBatch.id)
            .join(H3BatchConfig)
            .where(
                GenerationBatch.user_id == user.id,
                GenerationBatch.workflow_type == "minimax_h3_ref2va",
                GenerationBatch.source_channel == BATCH_SOURCE_H3_WORKBENCH,
                H3BatchConfig.confirmed_at.is_not(None),
                GenerationBatch.status.not_in(["CANCELLED", "SUCCESS"]),
                GenerationBatch.id > after_id,
            )
            .order_by(GenerationBatch.id)
            .limit(51)
        )
    )
    batches = []
    for batch_id in ids[:50]:
        review = prepare_recovery(db, user, batch_id)
        if review["can_resume"]:
            batches.append(
                {key: review[key] for key in ("batch_id", "name", "segment_count")}
            )
    return {
        "schema": SCHEMA,
        "batches": batches,
        "next_cursor": ids[49] if len(ids) > 50 else None,
    }


def resume_recovery(
    db, user, batch_id, *, identity, resume_confirmed, request_key, review_token
):
    if resume_confirmed is not True:
        raise DeviceAuthError(
            "H3_RESUME_CONFIRMATION_REQUIRED", "请明确确认继续原分段及原计划费用", 409
        )
    if not isinstance(request_key, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{8,100}", request_key
    ):
        raise DeviceAuthError("INVALID_RECOVERY_REQUEST_KEY", "恢复请求标识无效", 422)
    if not isinstance(review_token, str) or not re.fullmatch(
        r"[a-f0-9]{64}", review_token
    ):
        raise DeviceAuthError("INVALID_RECOVERY_REVIEW", "请重新查看待恢复分段", 422)
    try:
        # Locks concurrent resumes and authorization/admin changes across processes.
        # SQLite also serializes task writes; this mutation never resets task state.
        service.lock_control(db)
        service._active_user(db, user.id)
        db.expire_all()
        batch = _owned_batch(db, user, batch_id)
        request_snapshot = {"batch_id": batch_id, "request_key": request_key}
        digest = _digest(request_snapshot)
        old = db.scalar(
            select(WorkbenchDeviceOperation).where(
                WorkbenchDeviceOperation.user_id == user.id,
                WorkbenchDeviceOperation.operation_kind == KIND,
                WorkbenchDeviceOperation.request_digest == digest,
            )
        )
        if old is not None:
            audit = db.get(WorkbenchDeviceAuditEvent, old.id)
            receipt = (
                json.loads(audit.details_json)
                if audit and audit.action == "device.h3_resume"
                else {}
            )
            if receipt.get("review_token") != review_token:
                raise DeviceAuthError(
                    "H3_RECOVERY_REQUEST_CONFLICT",
                    "相同请求标识不能用于另一份恢复确认",
                    409,
                )
            db.commit()
            return {
                "schema": SCHEMA,
                "batch_id": batch_id,
                "operation_id": old.id,
                "segment_count": receipt["segment_count"],
                "already_applied": True,
            }
        _strict_identity(db, user.id, identity)
        review, resources = _review(db, user, batch)
        if not resources or not hmac.compare_digest(
            review_token, review["review_token"]
        ):
            raise DeviceAuthError(
                "H3_RECOVERY_REVIEW_CHANGED",
                "待恢复分段或授权状态已变化，请重新查看并确认",
                409,
            )
        operation = bind_new_operation(
            db,
            user_id=user.id,
            identity=identity,
            operation_kind=KIND,
            request_snapshot=request_snapshot,
            resources=resources,
        )
        # Paired immutable audit doubles as the idempotent receipt. No tokens or
        # paths are stored, and the original operation remains available for audit.
        db.add(
            WorkbenchDeviceAuditEvent(
                id=operation.id,
                actor_user_id=user.id,
                subject_user_id=user.id,
                device_id=operation.device_id,
                grant_id=operation.grant_id,
                action="device.h3_resume",
                details_json=canonical_json(
                    {
                        "batch_id": batch_id,
                        "review_token": review_token,
                        "segment_count": len(resources),
                        "segment_ids": [value[1] for value in resources],
                    }
                ),
            )
        )
        result = {
            "schema": SCHEMA,
            "batch_id": batch_id,
            "operation_id": operation.id,
            "segment_count": len(resources),
            "already_applied": False,
        }
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
