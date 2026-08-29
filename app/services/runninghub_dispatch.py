from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    BATCH_SOURCE_H3_WORKBENCH,
    BATCH_SOURCE_LEGACY_WEB,
    BATCH_SOURCE_NEW_WORKBENCH,
    GenerationBatch,
    GenerationTask,
    RunningHubConfig,
    RunningHubExecutionAccount,
    TaskStatus,
)
from app.services.runninghub_pool import (
    backfill_runninghub_config_fingerprints,
    credential_active_count_subquery,
    credential_active_task_count,
    execution_account_configuration_ready,
    task_execution_account_snapshot,
)


SLOT_OCCUPYING_TASK_STATUSES = (
    TaskStatus.UPLOADING.value,
    TaskStatus.SUBMITTED.value,
    TaskStatus.RUNNING.value,
)


@dataclass(frozen=True)
class DispatchReservation:
    """Safe fields describing one locally reserved RunningHub slot."""

    task_id: str
    uses_pool: bool
    execution_account_id: int | None
    execution_account_label: str | None
    concurrency_limit: int
    occupied_before_reservation: int


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def task_batch(task: GenerationTask) -> GenerationBatch | None:
    item = task.batch_item or (task.segment.batch_item if task.segment else None)
    return item.batch if item else None


def task_uses_execution_pool(task: GenerationTask) -> bool:
    """Return whether this task is inside the strictly gated pool path."""

    batch = task_batch(task)
    if not task_execution_account_snapshot(task):
        return False
    return bool(
        (
            task.workflow_type == "digital_human"
            and (
                batch is None
                or batch.source_channel
                in {BATCH_SOURCE_LEGACY_WEB, BATCH_SOURCE_NEW_WORKBENCH}
            )
        )
        or (
            batch is not None
            and batch.source_channel == BATCH_SOURCE_H3_WORKBENCH
            and task.workflow_type == "minimax_h3_ref2va"
        )
    )


def task_is_legacy_web_digital_human(task: GenerationTask) -> bool:
    """Identify old-web digital-human work without including other workbenches."""

    if task.workflow_type != "digital_human":
        return False
    batch = task_batch(task)
    return batch is None or batch.source_channel == BATCH_SOURCE_LEGACY_WEB


def task_account_limit(
    task: GenerationTask,
    account: RunningHubExecutionAccount,
) -> int | None:
    if task.workflow_type == "minimax_h3_ref2va":
        if not execution_account_configuration_ready(account):
            return None
        return max(int(account.max_concurrent_tasks), 1)
    if not execution_account_configuration_ready(account):
        return None
    return max(int(account.max_concurrent_tasks), 1)


def prepare_legacy_credential_fingerprints(db: Session) -> None:
    """Ensure old single-account tasks participate in real-key capacity counts."""

    if backfill_runninghub_config_fingerprints(db):
        db.commit()


def reserve_pool_task(
    db: Session,
    task: GenerationTask,
    *,
    remote_active_counts: Mapping[int, int] | None = None,
) -> DispatchReservation | None:
    """Atomically bind and reserve one eligible pool account for a pending task.

    The conditional UPDATE contains the credential-wide active-count check. It
    therefore remains safe if two workers both selected the same final slot
    from stale reads: only one update can turn a PENDING row into UPLOADING.
    """

    if not task_uses_execution_pool(task):
        return None
    selected_ids = task_execution_account_snapshot(task)
    if task.execution_account_id is not None:
        # Until per-attempt switching is implemented, an already bound retry
        # stays on its recorded account instead of overwriting history.
        selected_ids = [
            account_id
            for account_id in selected_ids
            if account_id == task.execution_account_id
        ]
    if not selected_ids:
        return None

    now = datetime.now(timezone.utc)
    accounts = list(
        db.scalars(
            select(RunningHubExecutionAccount).where(
                RunningHubExecutionAccount.id.in_(selected_ids),
                RunningHubExecutionAccount.is_enabled.is_(True),
            )
        ).all()
    )
    candidates: list[tuple[float, datetime, int, int, RunningHubExecutionAccount]] = []
    observed = remote_active_counts or {}
    for account in accounts:
        limit = task_account_limit(task, account)
        if limit is None:
            continue
        if account.cooldown_until and _as_utc(account.cooldown_until) > now:
            continue
        occupied = credential_active_task_count(db, account.credential_fingerprint)
        effective_load = max(occupied, int(observed.get(account.id, 0)))
        last_used = (
            _as_utc(account.last_used_at)
            if account.last_used_at
            else datetime.min.replace(tzinfo=timezone.utc)
        )
        candidates.append(
            (effective_load / limit, last_used, account.id, occupied, account)
        )
    candidates.sort(key=lambda value: (value[0], value[1], value[2]))

    for _, _, _, occupied, account in candidates:
        limit = task_account_limit(task, account)
        if limit is None:
            continue
        result = db.execute(
            update(GenerationTask)
            .where(
                GenerationTask.id == task.id,
                GenerationTask.status == TaskStatus.PENDING.value,
                credential_active_count_subquery(
                    account.credential_fingerprint
                )
                < limit,
            )
            .values(
                status=TaskStatus.UPLOADING.value,
                execution_account_id=account.id,
                error_code=None,
                error_message=None,
            )
        )
        if result.rowcount != 1:
            continue
        account.last_used_at = now
        db.commit()
        db.expire_all()
        return DispatchReservation(
            task_id=task.id,
            uses_pool=True,
            execution_account_id=account.id,
            execution_account_label=account.label,
            concurrency_limit=limit,
            occupied_before_reservation=occupied,
        )
    db.rollback()
    return None


def reserve_legacy_task(
    db: Session,
    task: GenerationTask,
) -> DispatchReservation | None:
    """Reserve the existing single-account path without duplicating real-key slots."""

    config = task.user.runninghub_config if task.user else None
    if not config or not config.api_key_encrypted or not config.credential_fingerprint:
        # Preserve the historical behavior for missing/corrupt single-account
        # configuration: claim once so process_task can persist a visible
        # CONFIGURATION_ERROR instead of leaving the row PENDING forever.
        result = db.execute(
            update(GenerationTask)
            .where(
                GenerationTask.id == task.id,
                GenerationTask.status == TaskStatus.PENDING.value,
            )
            .values(
                status=TaskStatus.UPLOADING.value,
                error_code=None,
                error_message=None,
            )
        )
        db.commit()
        if result.rowcount != 1:
            return None
        db.expire_all()
        return DispatchReservation(
            task_id=task.id,
            uses_pool=False,
            execution_account_id=None,
            execution_account_label=None,
            concurrency_limit=1,
            occupied_before_reservation=0,
        )
    limit = max(int(config.max_concurrent_tasks), 1)
    occupied = credential_active_task_count(db, config.credential_fingerprint)
    result = db.execute(
        update(GenerationTask)
        .where(
            GenerationTask.id == task.id,
            GenerationTask.status == TaskStatus.PENDING.value,
            credential_active_count_subquery(config.credential_fingerprint) < limit,
        )
        .values(
            status=TaskStatus.UPLOADING.value,
            error_code=None,
            error_message=None,
        )
    )
    db.commit()
    if result.rowcount != 1:
        return None
    db.expire_all()
    return DispatchReservation(
        task_id=task.id,
        uses_pool=False,
        execution_account_id=None,
        execution_account_label=None,
        concurrency_limit=limit,
        occupied_before_reservation=occupied,
    )


def release_unsubmitted_pool_reservation(task: GenerationTask) -> None:
    """Release only a pool binding that cannot yet represent a paid task."""

    if task.runninghub_task_id is not None:
        raise ValueError("已有 RunningHub 远程任务 ID，不能释放执行账号绑定")
    task.execution_account_id = None
    task.execution_account = None


def mark_execution_account_healthy(
    account: RunningHubExecutionAccount,
    *,
    clear_cooldown: bool = True,
) -> None:
    account.health_status = "HEALTHY"
    account.health_checked_at = datetime.now(timezone.utc)
    account.health_error_code = None
    if clear_cooldown:
        account.cooldown_until = None


def cool_execution_account(
    account: RunningHubExecutionAccount,
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
