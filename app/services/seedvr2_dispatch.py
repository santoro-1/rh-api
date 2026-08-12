from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import (
    BATCH_EXECUTION_MODE_DUAL_POOL_V1,
    EnhancementStatus,
    GenerationTask,
    GenerationTaskEnhancement,
    GenerationTaskEnhancementAttempt,
    SeedVR2ExecutionAccount,
)
from app.services.seedvr2_pool import seedvr2_batch_account_snapshot


ACTIVE_SEEDVR2_STATUSES = {
    EnhancementStatus.PENDING.value,
    EnhancementStatus.UPLOADING.value,
    EnhancementStatus.SUBMITTED.value,
    EnhancementStatus.RUNNING.value,
}


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def task_batch(task: GenerationTask):
    item = task.batch_item or (task.segment.batch_item if task.segment else None)
    return item.batch if item is not None else None


def task_uses_dual_pool(task: GenerationTask) -> bool:
    batch = task_batch(task)
    return bool(batch and batch.execution_mode == BATCH_EXECUTION_MODE_DUAL_POOL_V1)


def seedvr2_activity_filter(account_id: int):
    uncertain_attempt = (
        select(GenerationTaskEnhancementAttempt.id)
        .where(
            GenerationTaskEnhancementAttempt.enhancement_id
            == GenerationTaskEnhancement.id,
            GenerationTaskEnhancementAttempt.seedvr2_execution_account_id
            == account_id,
            GenerationTaskEnhancementAttempt.status == "SUBMIT_UNKNOWN",
            GenerationTaskEnhancementAttempt.remote_task_id.is_(None),
        )
        .exists()
    )
    return (
        GenerationTaskEnhancement.seedvr2_execution_account_id == account_id,
        or_(
            GenerationTaskEnhancement.status.in_(ACTIVE_SEEDVR2_STATUSES),
            uncertain_attempt,
        ),
    )


def seedvr2_active_count(db: Session, account_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(GenerationTaskEnhancement.id)).where(
                *seedvr2_activity_filter(account_id)
            )
        )
        or 0
    )


def reserve_seedvr2_account(
    db: Session, task: GenerationTask, enhancement: GenerationTaskEnhancement
) -> SeedVR2ExecutionAccount | None:
    """Atomically bind one stage account from the immutable operation snapshot."""

    if enhancement.seedvr2_execution_account_id is not None:
        return enhancement.seedvr2_execution_account
    batch = task_batch(task)
    if batch is None or batch.execution_mode != BATCH_EXECUTION_MODE_DUAL_POOL_V1:
        return None
    snapshot = seedvr2_batch_account_snapshot(batch) or []
    now = datetime.now(timezone.utc)
    accounts = list(
        db.scalars(
            select(SeedVR2ExecutionAccount).where(
                SeedVR2ExecutionAccount.id.in_(snapshot),
                SeedVR2ExecutionAccount.is_enabled.is_(True),
            )
        ).all()
    )
    eligible = [
        account for account in accounts
        if account.health_status not in {"UNHEALTHY", "ERROR"}
        and not (account.cooldown_until and _as_utc(account.cooldown_until) > now)
    ]
    counts = {account.id: seedvr2_active_count(db, account.id) for account in eligible}
    eligible.sort(key=lambda account: (
        counts[account.id] / account.max_concurrent_tasks,
        _as_utc(account.last_used_at) if account.last_used_at else datetime.min.replace(tzinfo=timezone.utc),
        account.id,
    ))
    for account in eligible:
        if counts[account.id] >= account.max_concurrent_tasks:
            continue
        active_count = select(func.count(GenerationTaskEnhancement.id)).where(
            *seedvr2_activity_filter(account.id)
        ).scalar_subquery()
        result = db.execute(
            update(GenerationTaskEnhancement)
            .where(
                GenerationTaskEnhancement.id == enhancement.id,
                GenerationTaskEnhancement.seedvr2_execution_account_id.is_(None),
                active_count < account.max_concurrent_tasks,
            )
            .values(seedvr2_execution_account_id=account.id)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            enhancement.seedvr2_execution_account_id = account.id
            enhancement.seedvr2_execution_account = account
            account.last_used_at = now
            db.flush()
            return account
        db.refresh(enhancement)
        if enhancement.seedvr2_execution_account_id is not None:
            return enhancement.seedvr2_execution_account
    return None


def release_seedvr2_account_for_new_attempt(
    enhancement: GenerationTaskEnhancement,
) -> None:
    if enhancement.remote_task_id is not None:
        raise ValueError("已有远程 SeedVR2 任务 ID，不能更换执行账号")
    enhancement.seedvr2_execution_account_id = None
    enhancement.seedvr2_execution_account = None


def cool_seedvr2_account(
    account: SeedVR2ExecutionAccount,
    *,
    error_code: str,
    cooldown_seconds: float,
    unhealthy: bool,
) -> None:
    now = datetime.now(timezone.utc)
    account.health_status = "UNHEALTHY" if unhealthy else "HEALTHY"
    account.health_checked_at = now
    account.health_error_code = error_code[:100]
    account.cooldown_until = now + timedelta(seconds=max(cooldown_seconds, 0.0))


def mark_seedvr2_account_healthy(
    account: SeedVR2ExecutionAccount,
    *,
    clear_cooldown: bool = True,
) -> None:
    account.health_status = "HEALTHY"
    account.health_checked_at = datetime.now(timezone.utc)
    account.health_error_code = None
    if clear_cooldown:
        account.cooldown_until = None
