from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from threading import Barrier

import jwt
import pytest

from app.database import SessionLocal
from app.models import GenerationBatch, GenerationTask, User
from app.services.device_auth.admission import WorkbenchIdentity
from app.services.device_auth.errors import DeviceAuthError
from app.services.device_auth.h3_queue_recovery import KIND, resume_recovery
from app.services.device_auth.models import (
    WorkbenchDeviceAuditEvent,
    WorkbenchDeviceOperation,
    WorkbenchDeviceWorkBinding,
)
from app.services.runninghub_attempts import (
    ensure_reserved_task_attempt,
    finish_task_attempt,
)
from app.workers import task_worker
from tests.conftest import create_user
from tests.test_device_auth import ring, account_token
from tests.test_device_work_admission import (
    licensed,
    mode,
    post,
    first_task,
    change_grant,
    h3_operations,
)
from tests.test_h3_quote_lifecycle import pending_quote
from tests.test_h3_worker_runtime import _FakeH3RunningHub


@pytest.fixture
def legacy_queue(client, licensed):
    mode("OFF")
    prefix = f"/api/workbench/h3-batches/{licensed['quote']['batch_id']}"
    response = client.post(
        prefix + "/confirm",
        json={"access_token": licensed["legacy"], "cost_confirmed": True},
    )
    assert response.status_code == 200, response.text
    mode("ENFORCE")
    licensed["prefix"] = prefix + "/authorization"
    return licensed


def review(client, state):
    response = post(client, state, state["prefix"] + "/prepare")
    assert response.status_code == 200, response.text
    return response.json()


def resume(client, state, checked, **overrides):
    return post(
        client,
        state,
        state["prefix"] + "/resume",
        **{
            "resume_confirmed": True,
            "request_key": "resume-request-01",
            "review_token": checked["review_token"],
            **overrides,
        },
    )


def task_snapshot(db):
    return {
        task.id: {c.name: getattr(task, c.name) for c in task.__table__.columns}
        for task in db.query(GenerationTask).all()
    }


def test_old_queue_resume_binds_future_segments_preserves_tasks_and_submits_once(
    client, legacy_queue, monkeypatch
):
    checked = review(client, legacy_queue)
    assert checked["segment_count"] == 2
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) is None
        before = task_snapshot(db)
        old_operations = {op.id for op in h3_operations(db)}
    waiting = client.post(
        "/api/workbench/h3-authorization-waiting",
        json={"access_token": legacy_queue["legacy"]},
    )
    assert waiting.status_code == 200 and waiting.headers["cache-control"] == "no-store"
    assert [batch["batch_id"] for batch in waiting.json()["batches"]] == [
        checked["batch_id"]
    ]
    result = resume(client, legacy_queue, checked)
    assert result.status_code == 200, result.text
    assert not result.json()["already_applied"]
    assert resume(client, legacy_queue, checked).json()["already_applied"]
    assert not review(client, legacy_queue)["can_resume"]
    with SessionLocal() as db:
        assert task_snapshot(db) == before
        operations = h3_operations(db).all()
        assert len(operations) == len(old_operations) + 1
        op = next(op for op in operations if op.operation_kind == KIND)
        assert {
            binding.operation_id
            for binding in db.query(WorkbenchDeviceWorkBinding).filter(
                WorkbenchDeviceWorkBinding.resource_kind.in_(
                    {"generation_segment", "generation_task"}
                )
            )
        } == {op.id}
        assert op.grant_id == legacy_queue["registration"]["grant_id"]
        audit = db.get(WorkbenchDeviceAuditEvent, op.id)
        assert json.loads(audit.details_json)["segment_count"] == 2
        assert (
            legacy_queue["access"] not in audit.details_json
            and legacy_queue["legacy"] not in audit.details_json
        )
    fake = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    monkeypatch.setattr(task_worker, "_handle_remote_status", lambda *args: None)
    with SessionLocal() as db:
        task_id = task_worker.claim_next_pending_task(db)
        assert task_id in before
        task_worker.process_task(db, task_id)
        task_worker.process_task(db, task_id)
        assert (
            db.get(GenerationTask, task_id).runninghub_task_id == "h3-remote-task-001"
        )
    assert fake.submissions == 1


@pytest.mark.parametrize("rollout", ["OFF", "OBSERVE", "ENFORCE"])
def test_resume_requires_real_bound_identity_even_in_compatibility_modes(
    client, legacy_queue, rollout
):
    checked = review(client, legacy_queue)
    mode(rollout)
    response = client.post(
        legacy_queue["prefix"] + "/resume",
        json={
            "access_token": legacy_queue["legacy"],
            "resume_confirmed": True,
            "request_key": "resume-request-01",
            "review_token": checked["review_token"],
            "device_id": legacy_queue["registration"]["device_id"],
            "mode": "OFF",
        },
    )
    assert (
        response.status_code == 401
        and response.json()["code"] == "DEVICE_BOUND_TOKEN_REQUIRED"
    )
    with SessionLocal() as db:
        assert h3_operations(db).count() == 1


@pytest.mark.parametrize("confirmation", [None, False, 1, "true"])
def test_resume_needs_explicit_boolean_confirmation(client, legacy_queue, confirmation):
    checked = review(client, legacy_queue)
    response = resume(client, legacy_queue, checked, resume_confirmed=confirmation)
    assert response.status_code == 409
    assert review(client, legacy_queue)["segment_count"] == 2


def test_other_account_cannot_see_or_rebind_batch(client, legacy_queue):
    other = create_user("queue-recovery-other", h3_access_enabled=True)
    token = account_token(other)
    response = client.post(
        "/api/workbench/h3-authorization-waiting", json={"access_token": token}
    )
    assert response.json()["batches"] == []
    checked = review(client, legacy_queue)
    for endpoint, extra in [
        ("prepare", {}),
        (
            "resume",
            {
                "resume_confirmed": True,
                "request_key": "other-request-01",
                "review_token": checked["review_token"],
            },
        ),
    ]:
        result = client.post(
            legacy_queue["prefix"] + "/" + endpoint,
            json={"access_token": token, **extra},
        )
        assert result.status_code == 404


@pytest.mark.parametrize(
    "unsafe", ["remote", "uncertain", "uploading", "cancelled", "failed", "reserved"]
)
def test_review_and_resume_never_reset_unsafe_or_finished_task(
    client, legacy_queue, unsafe
):
    with SessionLocal() as db:
        task = first_task(db)
        target = task.segment.id
        if unsafe == "remote":
            task.runninghub_task_id = "previous-paid-id"
        elif unsafe == "uncertain":
            task.error_code = "SUBMIT_OUTCOME_UNKNOWN"
        elif unsafe == "reserved":
            ensure_reserved_task_attempt(task)
        else:
            task.status = unsafe.upper()
        db.commit()
        before = task_snapshot(db)
    checked = review(client, legacy_queue)
    assert target not in {segment["segment_id"] for segment in checked["segments"]}
    if checked["can_resume"]:
        assert resume(client, legacy_queue, checked).status_code == 200
    with SessionLocal() as db:
        assert task_snapshot(db) == before
        old_binding = db.get(WorkbenchDeviceWorkBinding, ("generation_segment", target))
        assert (
            db.get(WorkbenchDeviceOperation, old_binding.operation_id).operation_kind
            == "h3.generate"
        )


def test_completed_old_attempt_does_not_strand_explicitly_queued_retry(
    client, legacy_queue
):
    with SessionLocal() as db:
        task = first_task(db)
        attempt = ensure_reserved_task_attempt(task)
        attempt.remote_task_id = "old-failed-paid-id"
        attempt.submitted_at = datetime.now(timezone.utc)
        finish_task_attempt(task, status="FAILED")
        db.commit()
        before = task_snapshot(db)
    checked = review(client, legacy_queue)
    assert checked["segment_count"] == 2
    assert resume(client, legacy_queue, checked).status_code == 200
    with SessionLocal() as db:
        assert task_snapshot(db) == before
        assert (
            first_task(db).runninghub_attempts[-1].remote_task_id
            == "old-failed-paid-id"
        )


def test_stale_review_and_changed_state_cannot_rebind_blindly(client, legacy_queue):
    checked = review(client, legacy_queue)
    with SessionLocal() as db:
        first_task(db).status = "UPLOADING"
        db.commit()
    response = resume(client, legacy_queue, checked)
    assert (
        response.status_code == 409
        and response.json()["code"] == "H3_RECOVERY_REVIEW_CHANGED"
    )
    with SessionLocal() as db:
        assert h3_operations(db).count() == 1


def test_revoke_stops_new_resume_but_receipt_recovery_never_creates_work(
    client, legacy_queue
):
    checked = review(client, legacy_queue)
    assert resume(client, legacy_queue, checked).status_code == 200
    change_grant(legacy_queue, "revoke")
    assert resume(client, legacy_queue, checked).json()["already_applied"]
    refreshed_review = review(client, legacy_queue)
    denied = resume(
        client, legacy_queue, refreshed_review, request_key="new-resume-after-revoke"
    )
    assert denied.status_code == 403 and denied.json()["code"] == "DEVICE_REVOKED"
    conflict = resume(client, legacy_queue, refreshed_review)
    assert (
        conflict.status_code == 409
        and conflict.json()["code"] == "H3_RECOVERY_REQUEST_CONFLICT"
    )
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) is None
        assert h3_operations(db).count() == 2


def test_parallel_identical_resume_has_one_operation_and_receipt(client, legacy_queue):
    checked = review(client, legacy_queue)
    claims = jwt.decode(legacy_queue["access"], options={"verify_signature": False})
    identity = WorkbenchIdentity(claims["user_id"], claims["cnf"]["jkt"], claims)
    barrier = Barrier(2)

    def run(_):
        with SessionLocal() as db:
            user = db.get(User, identity.user_id)
            barrier.wait(timeout=10)
            return resume_recovery(
                db,
                user,
                checked["batch_id"],
                identity=identity,
                resume_confirmed=True,
                request_key="parallel-resume-01",
                review_token=checked["review_token"],
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, range(2)))
    assert sorted(result["already_applied"] for result in outcomes) == [False, True]
    assert len({result["operation_id"] for result in outcomes}) == 1


def test_failed_binding_rolls_back_without_consuming_business_receipt(
    client, legacy_queue, monkeypatch
):
    checked = review(client, legacy_queue)
    from app.services.device_auth import h3_queue_recovery

    original = h3_queue_recovery.bind_new_operation

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise DeviceAuthError("TEST_ROLLBACK", "模拟事务失败", 409)

    monkeypatch.setattr(h3_queue_recovery, "bind_new_operation", fail)
    assert resume(client, legacy_queue, checked).status_code == 409
    with SessionLocal() as db:
        assert h3_operations(db).count() == 1
    monkeypatch.setattr(h3_queue_recovery, "bind_new_operation", original)
    assert resume(client, legacy_queue, checked).status_code == 200


def test_list_paginates_without_silently_hiding_older_batches(client, legacy_queue):
    from app.models import H3BatchConfig

    with SessionLocal() as db:
        original = db.get(GenerationBatch, legacy_queue["quote"]["batch_id"])
        data = {c.name: getattr(original, c.name) for c in original.__table__.columns}
        config = {
            c.name: getattr(original.h3_config, c.name)
            for c in H3BatchConfig.__table__.columns
        }
        for index in range(51):
            batch_id = original.id + f"-{index:03d}"
            db.add(
                GenerationBatch(
                    **{**data, "id": batch_id, "request_key": f"page-{index}"}
                )
            )
            db.add(H3BatchConfig(**{**config, "batch_id": batch_id}))
        db.commit()
    path = "/api/workbench/h3-authorization-waiting"
    response = post(client, legacy_queue, path)
    assert response.status_code == 200
    first = response.json()
    assert first["next_cursor"]
    assert first["batches"][0]["batch_id"] == legacy_queue["quote"]["batch_id"]
    second = post(client, legacy_queue, path, after_id=first["next_cursor"]).json()
    assert second["next_cursor"] is None and second["batches"] == []


def test_real_workbench_transport_recovers_same_cloud_tasks(
    client, legacy_queue, ring, monkeypatch
):
    import io
    from urllib.error import HTTPError
    from urllib.parse import urlsplit
    from tests.test_device_auth_client_contract import TestIdentity, ServerTransport
    from jyd_probe.auth_center import AuthCenterClient
    from jyd_probe.device_authorization import DeviceAuthorizationSession
    from jyd_probe.device_authorization_routes import DeviceSessionRegistry
    from jyd_probe.device_auth_protocol import TrustedIssuer
    from jyd_probe.device_business_transport import DeviceBusinessProofs

    with SessionLocal() as db:
        user = db.query(User).filter_by(username=legacy_queue["username"]).one()
        user_id = user.id
    identity = TestIdentity()
    identity.key = legacy_queue["key"]
    session = DeviceAuthorizationSession(
        user_id=user_id,
        login_token=legacy_queue["legacy"],
        trust=TrustedIssuer(
            ring.config.origin, ring.config.environment, ring.verification_keys
        ),
        identity=identity,
        transport=ServerTransport(client),
    )
    session.refresh()
    registry = DeviceSessionRegistry(
        "http://testserver", session_factory=lambda **_: session
    )
    sent = []

    class Response:
        def __init__(self, response):
            self.stream, self.status, self.headers = (
                io.BytesIO(response.content),
                response.status_code,
                response.headers,
            )

        def read(self, *args):
            return self.stream.read(*args)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def request(req, **_):
        path = urlsplit(req.full_url).path
        body = json.loads(req.data)
        sent.append((path, body, dict(req.header_items())))
        response = client.request(
            req.method, path, json=body, headers=dict(req.header_items())
        )
        if response.status_code >= 400:
            raise HTTPError(
                req.full_url,
                response.status_code,
                "denied",
                {},
                io.BytesIO(response.content),
            )
        return Response(response)

    monkeypatch.setattr("jyd_probe.auth_center._device_urlopen", request)
    monkeypatch.setattr("jyd_probe.auth_center.urlopen", request)
    transport = AuthCenterClient("http://testserver")
    transport.device_header_provider = DeviceBusinessProofs(
        registry, account_resolver=transport.verify
    )
    token, batch_id = legacy_queue["legacy"], legacy_queue["quote"]["batch_id"]
    assert (
        transport.list_h3_authorization_waiting(token)["batches"][0]["batch_id"]
        == batch_id
    )
    checked = transport.prepare_h3_authorization_recovery(token, batch_id)
    payload = {
        "resume_confirmed": True,
        "request_key": "real-client-resume",
        "review_token": checked["review_token"],
    }
    first = transport.resume_h3_authorization_recovery(token, batch_id, **payload)
    second = transport.resume_h3_authorization_recovery(token, batch_id, **payload)
    assert first["operation_id"] == second["operation_id"] and second["already_applied"]
    requests = [
        (body, headers)
        for path, body, headers in sent
        if path.endswith("/authorization/resume")
    ]
    assert len(requests) == 2
    assert all(
        body == payload and headers["Authorization"].startswith("DPoP ")
        for body, headers in requests
    )
    assert requests[0][1]["Dpop"] != requests[1][1]["Dpop"]
    assert identity.created == 0
    registry.close()
