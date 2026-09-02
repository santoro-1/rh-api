from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models import ArkAnalysisOperation
from app.services.ark_operation_ledger import (
    ArkOperationConflict,
    claim_ark_operation,
    expire_stale_ark_operations,
    mark_ark_operation_running,
    mark_ark_operation_succeeded,
    release_unadmitted_ark_operation,
)
from tests.conftest import create_user


def test_operation_id_replay_must_keep_same_request_identity() -> None:
    user = create_user("ledger-identity", with_config=False)
    with SessionLocal() as db:
        first = claim_ark_operation(
            db,
            operation_id="operation-identity",
            user_id=user.id,
            kind="content",
            business_key="business-a",
            request_sha256="a" * 64,
        )
        replay = claim_ark_operation(
            db,
            operation_id="operation-identity",
            user_id=user.id,
            kind="content",
            business_key="business-a",
            request_sha256="a" * 64,
        )
        assert replay.id == first.id
        with pytest.raises(ArkOperationConflict) as captured:
            claim_ark_operation(
                db,
                operation_id="operation-identity",
                user_id=user.id,
                kind="content",
                business_key="business-b",
                request_sha256="b" * 64,
            )
        assert captured.value.code == "ARK_OPERATION_ID_CONFLICT"


def test_restart_expires_active_operation_without_automatic_replay() -> None:
    user = create_user("ledger-restart", with_config=False)
    with SessionLocal() as db:
        claim_ark_operation(
            db,
            operation_id="operation-running",
            user_id=user.id,
            kind="visual",
            business_key="business-running",
            request_sha256="c" * 64,
        )
        mark_ark_operation_running(db, "operation-running")
        record = db.query(ArkAnalysisOperation).filter_by(
            operation_id="operation-running"
        ).one()
        record.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()
        assert expire_stale_ark_operations(db, stale_seconds=0) == 1
        with pytest.raises(ArkOperationConflict) as captured:
            claim_ark_operation(
                db,
                operation_id="operation-running",
                user_id=user.id,
                kind="visual",
                business_key="business-running",
                request_sha256="c" * 64,
            )
        assert captured.value.code == "ARK_OPERATION_REPLAY_BLOCKED"


def test_partial_result_is_replayable_from_existing_cache_link() -> None:
    user = create_user("ledger-partial", with_config=False)
    with SessionLocal() as db:
        claim_ark_operation(
            db,
            operation_id="operation-partial",
            user_id=user.id,
            kind="content",
            business_key="business-partial",
            request_sha256="d" * 64,
        )
        mark_ark_operation_succeeded(
            db,
            "operation-partial",
            cache_kind="content",
            cache_id=17,
            status="PARTIAL",
        )
        replay = claim_ark_operation(
            db,
            operation_id="operation-partial",
            user_id=user.id,
            kind="content",
            business_key="business-partial",
            request_sha256="d" * 64,
        )
        assert replay.status == "PARTIAL"
        assert replay.cache_id == 17


def test_unadmitted_queue_rejection_can_retry_with_same_operation_id() -> None:
    user = create_user("ledger-unadmitted", with_config=False)
    with SessionLocal() as db:
        first = claim_ark_operation(
            db,
            operation_id="operation-unadmitted",
            user_id=user.id,
            kind="content",
            business_key="business-unadmitted",
            request_sha256="e" * 64,
        )
        release_unadmitted_ark_operation(db, first.operation_id)
        retry = claim_ark_operation(
            db,
            operation_id="operation-unadmitted",
            user_id=user.id,
            kind="content",
            business_key="business-unadmitted",
            request_sha256="e" * 64,
        )
        assert retry.status == "QUEUED"
        assert db.query(ArkAnalysisOperation).count() == 1
