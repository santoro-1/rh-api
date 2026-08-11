from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid
from typing import Any

from app.models import (
    GenerationTask,
    GenerationTaskAttempt,
    GenerationTaskEnhancement,
    GenerationTaskEnhancementAttempt,
    RunningHubExecutionAccount,
)


SUBMIT_OUTCOME_UNKNOWN = "SUBMIT_OUTCOME_UNKNOWN"
SUBMIT_UNKNOWN_ATTEMPT_STATUS = "SUBMIT_UNKNOWN"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def latest_task_attempt(task: GenerationTask) -> GenerationTaskAttempt | None:
    return task.runninghub_attempts[-1] if task.runninghub_attempts else None


def task_attempt_for_remote_id(
    task: GenerationTask,
    remote_task_id: str | None = None,
) -> GenerationTaskAttempt | None:
    clean_remote_id = str(remote_task_id or task.runninghub_task_id or "").strip()
    if clean_remote_id:
        for attempt in reversed(task.runninghub_attempts):
            if attempt.remote_task_id == clean_remote_id:
                return attempt
        return None
    return latest_task_attempt(task)


def ensure_reserved_task_attempt(task: GenerationTask) -> GenerationTaskAttempt:
    """Return the current unpaid reservation or create the next immutable attempt."""

    latest = latest_task_attempt(task)
    if (
        latest is not None
        and latest.remote_task_id is None
        and latest.finished_at is None
        and latest.status in {"RESERVED", "UPLOADING"}
        and latest.execution_account_id == task.execution_account_id
    ):
        return latest
    attempt = GenerationTaskAttempt(
        id=str(uuid.uuid4()),
        generation_task_id=task.id,
        attempt_number=(latest.attempt_number + 1 if latest is not None else 1),
        execution_account_id=task.execution_account_id,
        status="RESERVED",
    )
    task.runninghub_attempts.append(attempt)
    return attempt


def finish_task_attempt(
    task: GenerationTask,
    *,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    failed_reason: dict[str, Any] | None = None,
    finished: bool = True,
) -> GenerationTaskAttempt | None:
    attempt = task_attempt_for_remote_id(task)
    if attempt is None:
        return None
    attempt.status = status
    attempt.error_code = error_code
    attempt.error_message = error_message
    attempt.failed_reason_json = (
        json.dumps(failed_reason, ensure_ascii=False)
        if failed_reason is not None
        else None
    )
    attempt.finished_at = utcnow() if finished else None
    return attempt


def task_has_uncertain_submission(task: GenerationTask) -> bool:
    latest = latest_task_attempt(task)
    return bool(
        task.error_code == SUBMIT_OUTCOME_UNKNOWN
        or (
            latest is not None
            and latest.status == SUBMIT_UNKNOWN_ATTEMPT_STATUS
            and latest.remote_task_id is None
        )
    )


def task_execution_account_for_remote(
    task: GenerationTask,
) -> RunningHubExecutionAccount | None:
    attempt = task_attempt_for_remote_id(task)
    if attempt is not None and attempt.execution_account is not None:
        return attempt.execution_account
    return task.execution_account


def latest_enhancement_attempt(
    enhancement: GenerationTaskEnhancement,
) -> GenerationTaskEnhancementAttempt | None:
    return enhancement.attempts[-1] if enhancement.attempts else None


def enhancement_attempt_for_remote_id(
    enhancement: GenerationTaskEnhancement,
) -> GenerationTaskEnhancementAttempt | None:
    clean_remote_id = str(enhancement.remote_task_id or "").strip()
    if clean_remote_id:
        for attempt in reversed(enhancement.attempts):
            if attempt.remote_task_id == clean_remote_id:
                return attempt
    return latest_enhancement_attempt(enhancement)


def enhancement_has_uncertain_submission(
    enhancement: GenerationTaskEnhancement,
) -> bool:
    latest = latest_enhancement_attempt(enhancement)
    return bool(
        latest is not None
        and latest.status == SUBMIT_UNKNOWN_ATTEMPT_STATUS
        and latest.remote_task_id is None
    )


def enhancement_execution_account_for_remote(
    task: GenerationTask,
    enhancement: GenerationTaskEnhancement,
) -> RunningHubExecutionAccount | None:
    attempt = enhancement_attempt_for_remote_id(enhancement)
    if attempt is not None and attempt.execution_account is not None:
        return attempt.execution_account
    if enhancement.execution_account is not None:
        return enhancement.execution_account
    return task_execution_account_for_remote(task)
