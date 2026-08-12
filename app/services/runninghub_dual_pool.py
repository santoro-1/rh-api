from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BATCH_EXECUTION_MODE_DUAL_POOL_V1,
    BATCH_EXECUTION_MODE_SAME_ACCOUNT_V1,
    BATCH_SOURCE_NEW_WORKBENCH,
    GenerationBatch,
    RunningHubDualPoolGrant,
    RunningHubPoolRuntimeControl,
    User,
)


VALID_EXECUTION_MODES = {
    BATCH_EXECUTION_MODE_SAME_ACCOUNT_V1,
    BATCH_EXECUTION_MODE_DUAL_POOL_V1,
}


class RunningHubDualPoolError(ValueError):
    pass


class RunningHubExecutionModeConflictError(RunningHubDualPoolError):
    pass


def dual_pool_grant_for_user(
    db: Session, user_id: int
) -> RunningHubDualPoolGrant | None:
    return db.scalar(
        select(RunningHubDualPoolGrant).where(
            RunningHubDualPoolGrant.user_id == user_id
        )
    )


def user_has_dual_pool_entitlement(db: Session, user: User) -> bool:
    if not user.is_active:
        return False
    grant = dual_pool_grant_for_user(db, user.id)
    if grant is None or not grant.is_enabled:
        return False
    return bool(user.is_admin or grant.allow_non_admin)


def dual_pool_runtime_control(db: Session) -> RunningHubPoolRuntimeControl | None:
    return db.get(RunningHubPoolRuntimeControl, 1)


def dual_pool_runtime_enabled(db: Session) -> bool:
    """Read the web-managed switch, falling back to env before its first save."""

    control = dual_pool_runtime_control(db)
    if control is not None:
        return control.dual_pool_enabled
    return get_settings().runninghub_dual_pool_enabled


def set_dual_pool_runtime_enabled(
    db: Session, *, enabled: bool, updated_by_user_id: int
) -> RunningHubPoolRuntimeControl:
    control = dual_pool_runtime_control(db)
    if control is None:
        control = RunningHubPoolRuntimeControl(id=1)
        db.add(control)
    control.dual_pool_enabled = enabled
    control.updated_by_user_id = updated_by_user_id
    db.flush()
    return control


def resolve_execution_mode(
    db: Session,
    *,
    user: User,
    source_channel: str,
    workflow_type: str,
    dual_pool_enabled: bool | None = None,
) -> str:
    """Resolve a new operation mode without trusting a client-side flag."""

    enabled = (
        dual_pool_runtime_enabled(db)
        if dual_pool_enabled is None
        else dual_pool_enabled
    )
    if (
        not enabled
        or source_channel != BATCH_SOURCE_NEW_WORKBENCH
        or workflow_type != "digital_human"
        or not user_has_dual_pool_entitlement(db, user)
    ):
        return BATCH_EXECUTION_MODE_SAME_ACCOUNT_V1
    return BATCH_EXECUTION_MODE_DUAL_POOL_V1


def batch_execution_mode(batch: GenerationBatch) -> str:
    """Interpret historical NULL rows as the frozen current same-account path."""

    if batch.execution_mode is None:
        return BATCH_EXECUTION_MODE_SAME_ACCOUNT_V1
    if batch.execution_mode not in VALID_EXECUTION_MODES:
        raise RunningHubDualPoolError("RunningHub 执行模式快照已损坏")
    return batch.execution_mode


def bind_batch_execution_mode(
    db: Session, batch: GenerationBatch, execution_mode: str
) -> str:
    """Atomically lock the branch for a newly created 4A operation."""

    if execution_mode not in VALID_EXECUTION_MODES:
        raise RunningHubDualPoolError("不支持的 RunningHub 执行模式")
    if batch.execution_mode is not None:
        if batch.execution_mode != execution_mode:
            raise RunningHubExecutionModeConflictError(
                "该画面生成操作的 RunningHub 执行模式已锁定，不能修改"
            )
        return execution_mode
    result = db.execute(
        update(GenerationBatch)
        .where(
            GenerationBatch.id == batch.id,
            GenerationBatch.execution_mode.is_(None),
        )
        .values(execution_mode=execution_mode)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        batch.execution_mode = execution_mode
        return execution_mode
    db.refresh(batch, attribute_names=["execution_mode"])
    if batch.execution_mode != execution_mode:
        raise RunningHubExecutionModeConflictError(
            "该画面生成操作的 RunningHub 执行模式已锁定，不能修改"
        )
    return execution_mode


def set_dual_pool_grant(
    db: Session,
    *,
    user: User,
    is_enabled: bool,
    allow_non_admin: bool = False,
    note: str | None = None,
) -> RunningHubDualPoolGrant:
    """Persist entitlement by immutable user ID; callers never authorize by username."""

    clean_note = (note or "").strip() or None
    if clean_note is not None and len(clean_note) > 500:
        raise RunningHubDualPoolError("双账号池授权备注不能超过 500 个字符")
    if allow_non_admin and user.is_admin:
        allow_non_admin = False
    grant = dual_pool_grant_for_user(db, user.id)
    if grant is None:
        grant = RunningHubDualPoolGrant(user_id=user.id)
        db.add(grant)
    grant.is_enabled = is_enabled
    grant.allow_non_admin = allow_non_admin
    grant.note = clean_note
    db.flush()
    return grant
