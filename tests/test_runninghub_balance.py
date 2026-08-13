from __future__ import annotations

from decimal import Decimal

from app.database import SessionLocal
from app.models import RunningHubCredentialBalance
from app.services.runninghub import RunningHubAccountStatus, RunningHubError
from app.services.runninghub_balance import (
    balance_summary,
    save_account_status,
    save_balance_error,
)


def test_balance_cache_preserves_decimal_text_and_safe_summary():
    with SessionLocal() as db:
        row = save_account_status(
            db,
            credential_fingerprint="a" * 64,
            status=RunningHubAccountStatus(
                current_task_count=2,
                remain_coins=Decimal("138.5400"),
                remain_money=Decimal("9.90"),
                currency="CNY",
                api_type="NORMAL",
            ),
        )
        db.commit()
        assert row.remain_coins == "138.54"
        assert row.remain_money == "9.9"

        summary = balance_summary(db, "a" * 64)
        assert summary["status"] == "AVAILABLE"
        assert summary["remain_coins"] == "138.54"
        assert summary["remain_money"] == "9.9"
        assert summary["remote_current_task_count"] == 2
        assert summary["checked_at"]


def test_temporary_balance_error_keeps_last_successful_amount():
    fingerprint = "b" * 64
    with SessionLocal() as db:
        save_account_status(
            db,
            credential_fingerprint=fingerprint,
            status=RunningHubAccountStatus(
                current_task_count=0,
                remain_coins=Decimal("88.25"),
                remain_money=None,
                currency=None,
                api_type=None,
            ),
        )
        save_balance_error(
            db,
            credential_fingerprint=fingerprint,
            error=RunningHubError("临时网络错误", error_code="429"),
        )
        db.commit()

        row = db.get(RunningHubCredentialBalance, fingerprint)
        assert row is not None
        assert row.balance_status == "TEMPORARY_ERROR"
        assert row.remain_coins == "88.25"
        assert row.retry_after is not None


def test_unknown_balance_summary_contains_no_credential_data():
    with SessionLocal() as db:
        summary = balance_summary(db, "c" * 64)
    assert summary == {
        "status": "UNKNOWN",
        "remain_coins": None,
        "remain_money": None,
        "currency": None,
        "api_type": None,
        "remote_current_task_count": None,
        "checked_at": None,
        "stale": True,
    }
