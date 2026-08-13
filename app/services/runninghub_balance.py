from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    RunningHubCredentialBalance,
    RunningHubExecutionAccount,
    SeedVR2ExecutionAccount,
)
from app.services.runninghub import (
    RunningHubAccountStatus,
    RunningHubClient,
    RunningHubError,
)
from app.services.security import decrypt_secret


BALANCE_CACHE_SECONDS = 60
BALANCE_RETRY_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _cache_row(
    db: Session, credential_fingerprint: str
) -> RunningHubCredentialBalance:
    row = db.get(RunningHubCredentialBalance, credential_fingerprint)
    if row is None:
        row = RunningHubCredentialBalance(
            credential_fingerprint=credential_fingerprint,
            balance_status="UNKNOWN",
        )
        db.add(row)
    return row


def save_account_status(
    db: Session,
    *,
    credential_fingerprint: str,
    status: RunningHubAccountStatus,
) -> RunningHubCredentialBalance:
    row = _cache_row(db, credential_fingerprint)
    row.balance_status = "AVAILABLE" if status.remain_coins is not None else "UNKNOWN"
    row.remain_coins = _decimal_text(status.remain_coins)
    row.remain_money = _decimal_text(status.remain_money)
    row.currency = status.currency
    row.api_type = status.api_type
    row.remote_current_task_count = status.current_task_count
    row.checked_at = _now()
    row.error_code = None
    row.error_message = None
    row.retry_after = None
    db.flush()
    return row


def save_balance_error(
    db: Session,
    *,
    credential_fingerprint: str,
    error: RunningHubError,
) -> RunningHubCredentialBalance:
    row = _cache_row(db, credential_fingerprint)
    code = str(error.error_code or "BALANCE_QUERY_FAILED")[:100]
    normalized = code.upper()
    message = str(error).replace("\r", " ").replace("\n", " ")[:500]
    if normalized in {"401", "403", "INVALID_CREDENTIAL", "INVALID_API_KEY"}:
        row.balance_status = "AUTH_INVALID"
    else:
        row.balance_status = "TEMPORARY_ERROR"
    row.error_code = code
    row.error_message = message
    row.retry_after = _now() + timedelta(seconds=BALANCE_RETRY_SECONDS)
    db.flush()
    return row


def balance_summary(
    db: Session, credential_fingerprint: str | None
) -> dict[str, Any]:
    row = (
        db.get(RunningHubCredentialBalance, credential_fingerprint)
        if credential_fingerprint
        else None
    )
    if row is None:
        return {
            "status": "UNKNOWN",
            "remain_coins": None,
            "remain_money": None,
            "currency": None,
            "api_type": None,
            "remote_current_task_count": None,
            "checked_at": None,
            "stale": True,
        }
    checked_at = row.checked_at
    if checked_at is not None and checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    stale = bool(
        checked_at is None
        or (_now() - checked_at).total_seconds() > BALANCE_CACHE_SECONDS
    )
    return {
        "status": row.balance_status,
        "remain_coins": row.remain_coins,
        "remain_money": row.remain_money,
        "currency": row.currency,
        "api_type": row.api_type,
        "remote_current_task_count": row.remote_current_task_count,
        "checked_at": checked_at.isoformat() if checked_at else None,
        "stale": stale,
    }


def persist_client_account_status(
    db: Session,
    client: RunningHubClient,
    credential_fingerprint: str | None,
) -> None:
    status = getattr(client, "last_account_status", None)
    if credential_fingerprint and isinstance(status, RunningHubAccountStatus):
        save_account_status(
            db,
            credential_fingerprint=credential_fingerprint,
            status=status,
        )


def refresh_pool_account_balance(
    db: Session,
    account: RunningHubExecutionAccount | SeedVR2ExecutionAccount,
) -> RunningHubCredentialBalance:
    api_key = decrypt_secret(account.api_key_encrypted)
    app_id = (
        account.digital_human_ai_app_id
        if isinstance(account, RunningHubExecutionAccount)
        else account.seedvr2_ai_app_id
    )
    client = RunningHubClient(api_key, account.base_url, app_id)
    try:
        status = client.get_account_status()
    except RunningHubError as exc:
        return save_balance_error(
            db,
            credential_fingerprint=account.credential_fingerprint,
            error=exc,
        )
    return save_account_status(
        db,
        credential_fingerprint=account.credential_fingerprint,
        status=status,
    )
