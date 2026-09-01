from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import AudioGenerationTask, GenerationBatch, GenerationTask, User, VoiceCreationTask
from app.services.device_auth import service
from app.services.device_auth.models import (
    WorkbenchDeviceGrant,
    WorkbenchDeviceOperation,
    WorkbenchDeviceWorkBinding,
)
from app.services.device_auth.errors import DeviceAuthError
from app.services.device_auth.queued_work import h3_task_resource
from app.services.h3_workbench import confirm_h3_workbench_batch
from app.services.speech import voice_jobs
from app.workers import audio_worker, task_worker
from tests.conftest import create_user
from tests.test_device_auth import (
    ring,
    register,
    approve,
    exchange,
    device_key,
    auth_headers,
    account_token,
)
from tests.test_h3_quote_lifecycle import pending_quote, cancel
from tests.test_h3_worker_runtime import _FakeH3RunningHub


def mode(value):
    with SessionLocal() as db:
        control = service.lock_control(db)
        control.mode = value
        control.revision += 1
        db.commit()


@pytest.fixture
def licensed(client, ring, pending_quote):
    legacy, quote, username = pending_quote
    admin = create_user("admission-admin", is_admin=True)
    key = device_key()
    registration = register(client, key, legacy).json()
    approve(admin, registration)
    access = exchange(client, key, legacy).json()["access_token"]
    mode("ENFORCE")
    return {
        "legacy": legacy,
        "quote": quote,
        "username": username,
        "admin": admin,
        "key": key,
        "access": access,
        "registration": registration,
    }


def post(client, licensed, path, **payload):
    return client.post(
        path,
        json=payload,
        headers=auth_headers(
            client,
            licensed["key"],
            licensed["access"],
            "request",
            path=path,
            bound=True,
        ),
    )


def confirm(client, licensed):
    response = post(
        client,
        licensed,
        f"/api/workbench/h3-batches/{licensed['quote']['batch_id']}/confirm",
        cost_confirmed=True,
    )
    assert response.status_code == 200, response.text
    return response.json()


def change_grant(licensed, action):
    with SessionLocal() as db:
        from app.services.device_auth.models import WorkbenchDeviceGrant

        grant = db.get(WorkbenchDeviceGrant, licensed["registration"]["grant_id"])
        service.change_grant(
            db,
            actor_id=licensed["admin"].id,
            grant_id=grant.id,
            expected_revision=grant.revision,
            action=action,
            now=int(time.time()),
        )
        db.commit()


def first_task(db):
    return db.scalars(
        select(GenerationTask)
        .where(GenerationTask.workflow_type == "minimax_h3_ref2va")
        .order_by(GenerationTask.created_at)
    ).first()


def h3_operations(db):
    return db.query(WorkbenchDeviceOperation).filter(
        WorkbenchDeviceOperation.operation_kind.like("h3.%")
    )


def test_unbound_new_work_blocked_but_account_reads_and_cancel_remain(
    client, pending_quote
):
    token, quote, username = pending_quote
    mode("ENFORCE")
    response = client.post(
        "/api/workbench/h3-batches/prepare",
        json={"access_token": token, "device_id": "forged", "mode": "OFF"},
    )
    assert (
        response.status_code == 401
        and response.json()["code"] == "DEVICE_BOUND_TOKEN_REQUIRED"
    )
    path = f"/api/workbench/h3-batches/{quote['batch_id']}"
    assert client.post(path, json={"access_token": token}).status_code == 200
    assert (
        client.post(
            path + "/confirm", json={"access_token": token, "cost_confirmed": True}
        ).status_code
        == 401
    )
    # The same service boundary protects cookie/legacy-page and non-HTTP callers.
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        with pytest.raises(DeviceAuthError):
            confirm_h3_workbench_batch(db, user, quote["batch_id"], cost_confirmed=True)
        db.rollback()
        assert db.query(GenerationTask).count() == 0
        assert h3_operations(db).count() == 0
        assert db.get(GenerationBatch, quote["batch_id"]).h3_config.confirmed_at is None
    assert cancel(client, token, quote).status_code == 200


def test_bound_confirmation_persists_all_segments_once_and_never_tokens(
    client, licensed
):
    confirmed = confirm(client, licensed)
    confirm(client, licensed)
    with SessionLocal() as db:
        operation = h3_operations(db).one()
        assert operation.grant_id == licensed["registration"]["grant_id"]
        assert operation.operation_kind == "h3.generate"
        assert operation.admission_mode == "ENFORCE"
        bindings = db.query(WorkbenchDeviceWorkBinding).all()
        segment_ids = {
            s["segment_id"] for item in confirmed["items"] for s in item["segments"]
        }
        assert {b.resource_id for b in bindings} == segment_ids
        assert {b.operation_id for b in bindings} == {operation.id}
        assert len(segment_ids) == 2  # includes the not-yet-created soft-chain task
        saved = json.dumps(
            {c.name: getattr(operation, c.name) for c in operation.__table__.columns}
        )
        assert licensed["access"] not in saved and licensed["legacy"] not in saved


def test_proof_is_consumed_even_if_business_fails_and_body_cannot_switch_account(
    client, licensed
):
    path = "/api/workbench/h3-batches/no-such-batch"
    headers = auth_headers(
        client, licensed["key"], licensed["access"], "request", path=path, bound=True
    )
    assert client.post(path, json={}, headers=headers).status_code == 404
    assert client.post(path, json={}, headers=headers).status_code == 401
    other = create_user("admission-other", h3_access_enabled=True)
    response = post(client, licensed, path, access_token=account_token(other))
    assert (
        response.status_code == 401
        and response.json()["code"] == "AMBIGUOUS_ACCOUNT_TOKEN"
    )


def test_revoked_device_can_read_download_and_recover_confirm_receipt_not_new_work(
    client, licensed
):
    confirmed = confirm(client, licensed)
    change_grant(licensed, "revoke")
    confirm(client, licensed)  # existing receipt, not a fresh paid confirmation
    path = f"/api/workbench/h3-batches/{confirmed['batch_id']}"
    assert post(client, licensed, path).status_code == 200
    download = confirmed["items"][0]["input_raw_cues_download_url"]
    response = client.get(
        download,
        headers=auth_headers(
            client,
            licensed["key"],
            licensed["access"],
            "request",
            path=download,
            method="GET",
            bound=True,
        ),
    )
    assert response.status_code == 200, response.text
    response = post(client, licensed, "/api/workbench/h3-batches/prepare")
    assert response.status_code == 403 and response.json()["code"] == "DEVICE_REVOKED"
    with SessionLocal() as db:
        assert h3_operations(db).count() == 1


def test_paid_retry_cannot_disguise_as_download_only_and_wait_does_not_reset_task(
    client, licensed
):
    confirmed = confirm(client, licensed)
    with SessionLocal() as db:
        task = first_task(db)
        task.status = "FAILED"
        task.error_code = "PROVIDER_FAILED"
        task.runninghub_task_id = "paid-old-remote"
        segment_id, task_id = task.segment.id, task.id
        db.commit()
    change_grant(licensed, "revoke")
    prefix = f"/api/workbench/h3-segments/{segment_id}/retry"
    quote = post(client, licensed, prefix + "/prepare").json()
    assert quote["estimated_paid_calls"] == 1
    response = post(
        client,
        licensed,
        prefix + "/confirm",
        request_key="paid-retry",
        quote_token=quote["quote_token"],
        cost_confirmed=True,
        retry_scope="download_only",
    )
    assert response.status_code == 403 and response.json()["code"] == "DEVICE_REVOKED"
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert (task.status, task.error_code, task.runninghub_task_id) == (
            "FAILED",
            "PROVIDER_FAILED",
            "paid-old-remote",
        )
        assert h3_operations(db).count() == 1
        assert "_h3_manual_retry" not in json.loads(task.input_payload)
        task.status = "DOWNLOAD_FAILED"
        db.commit()
    quote = post(client, licensed, prefix + "/prepare").json()
    assert quote["estimated_paid_calls"] == 0
    response = post(
        client,
        licensed,
        prefix + "/confirm",
        request_key="download-retry",
        quote_token=quote["quote_token"],
        cost_confirmed=False,
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task.runninghub_task_id == "paid-old-remote" and task.status == "RUNNING"
        assert h3_operations(db).count() == 1


def test_queued_work_rechecks_current_grant_and_resumes_without_second_confirmation(
    client, licensed, monkeypatch
):
    confirm(client, licensed)
    change_grant(licensed, "suspend")
    fake = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) is None
        task = first_task(db)
        assert task.status == "PENDING" and task.runninghub_task_id is None
        assert not task.runninghub_attempts and task.execution_account_id is None
        binding = db.get(WorkbenchDeviceWorkBinding, h3_task_resource(task))
        assert binding.blocked_code == "DEVICE_SUSPENDED"
        assert h3_operations(db).count() == 1
    change_grant(licensed, "resume")
    # Device authorization revalidation uses the DB, not an old HTTP token/revision.
    with SessionLocal() as db:
        task_id = task_worker.claim_next_pending_task(db)
        assert task_id is not None
        task_worker.process_task(db, task_id)
        assert (
            db.get(GenerationTask, task_id).runninghub_task_id == "h3-remote-task-001"
        )
        assert h3_operations(db).count() == 1
    assert fake.submissions == 1


@pytest.mark.parametrize("when", ["before_dispatch", "during_upload"])
def test_revocation_after_claim_stops_submit_and_releases_only_unpaid_reservation(
    client, licensed, monkeypatch, when
):
    confirm(client, licensed)
    fake = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task_id = task_worker.claim_next_pending_task(db)
        if when == "before_dispatch":
            change_grant(licensed, "revoke")
        else:
            original = fake.upload_file
            revoked = []

            def upload(path):
                if not revoked:
                    change_grant(licensed, "revoke")
                    revoked.append(True)
                return original(path)

            monkeypatch.setattr(fake, "upload_file", upload)
        task_worker.process_task(db, task_id)
        task = db.get(GenerationTask, task_id)
        assert task.status == "PENDING" and task.execution_account_id is None
        assert task.runninghub_task_id is None and task.error_code is None
        assert task.runninghub_attempts[-1].status == "AUTHORIZATION_WAIT"
        assert task.runninghub_attempts[-1].execution_account_id is not None
        assert (
            db.get(WorkbenchDeviceWorkBinding, h3_task_resource(task)).blocked_code
            == "DEVICE_REVOKED"
        )
    assert fake.submissions == 0


def test_accepted_remote_task_is_still_polled_after_revocation(
    client, licensed, monkeypatch
):
    confirm(client, licensed)
    fake = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task_id = task_worker.claim_next_pending_task(db)
        task_worker.process_task(db, task_id)
        account_id = db.get(GenerationTask, task_id).execution_account_id
    change_grant(licensed, "revoke")
    calls = []
    monkeypatch.setattr(
        task_worker,
        "_handle_remote_status",
        lambda db, task, client, workflow: calls.append(
            (task.runninghub_task_id, task.execution_account_id)
        ),
    )
    with SessionLocal() as db:
        task_worker.process_task(db, task_id)
        assert (
            db.get(GenerationTask, task_id).runninghub_task_id == "h3-remote-task-001"
        )
    assert calls == [("h3-remote-task-001", account_id)]
    assert fake.submissions == 1


def test_off_mode_operation_does_not_turn_into_approved_work_when_enforcement_enabled(
    client, pending_quote
):
    legacy, quote, _ = pending_quote
    response = client.post(
        f"/api/workbench/h3-batches/{quote['batch_id']}/confirm",
        json={"access_token": legacy, "cost_confirmed": True},
    )
    assert response.status_code == 200, response.text
    mode("ENFORCE")
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) is None
        task = first_task(db)
        assert (
            db.get(WorkbenchDeviceWorkBinding, h3_task_resource(task)).blocked_code
            == "DEVICE_ADMISSION_REQUIRED"
        )
    mode("OBSERVE")
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) is not None


def test_business_failure_rolls_back_operation_without_releasing_consumed_proof(
    client, licensed, monkeypatch
):
    from app.services.h3_workbench import H3WorkbenchError

    def fail(*args, **kwargs):
        raise H3WorkbenchError("simulated business validation failure")

    monkeypatch.setattr("app.services.h3_workbench._confirm_h3_workbench_batch", fail)
    path = f"/api/workbench/h3-batches/{licensed['quote']['batch_id']}/confirm"
    headers = auth_headers(
        client, licensed["key"], licensed["access"], "request", path=path, bound=True
    )
    response = client.post(path, json={"cost_confirmed": True}, headers=headers)
    assert response.status_code == 400
    with SessionLocal() as db:
        assert h3_operations(db).count() == 0
        assert db.query(WorkbenchDeviceWorkBinding).filter(
            WorkbenchDeviceWorkBinding.resource_kind.in_(
                {"generation_segment", "generation_task"}
            )
        ).count() == 0
        assert db.query(GenerationTask).count() == 0
        assert (
            db.get(GenerationBatch, licensed["quote"]["batch_id"]).status
            == "AWAITING_COST_CONFIRMATION"
        )
    assert (
        client.post(path, json={"cost_confirmed": True}, headers=headers).status_code
        == 401
    )


def test_explicit_paid_retry_creates_new_binding_once(client, licensed):
    confirm(client, licensed)
    with SessionLocal() as db:
        task = first_task(db)
        task.status = "FAILED"
        task.segment.status = "FAILED"
        segment_id = task.segment.id
        original = db.get(
            WorkbenchDeviceWorkBinding, ("generation_segment", segment_id)
        ).operation_id
        db.commit()
    prefix = f"/api/workbench/h3-segments/{segment_id}/retry"
    quote = post(client, licensed, prefix + "/prepare").json()
    payload = {
        "request_key": "approved-paid-retry",
        "quote_token": quote["quote_token"],
        "cost_confirmed": True,
    }
    response = post(client, licensed, prefix + "/confirm", **payload)
    assert response.status_code == 200, response.text
    assert post(client, licensed, prefix + "/confirm", **payload).status_code == 200
    with SessionLocal() as db:
        assert h3_operations(db).count() == 2
        binding = db.get(WorkbenchDeviceWorkBinding, ("generation_segment", segment_id))
        assert binding.operation_id != original
        assert (
            db.get(WorkbenchDeviceOperation, binding.operation_id).operation_kind
            == "h3.retry"
        )


def test_worker_checks_current_scope_and_preserves_uncertain_submission(
    client, licensed, monkeypatch
):
    confirm(client, licensed)
    with SessionLocal() as db:
        grant = db.get(WorkbenchDeviceGrant, licensed["registration"]["grant_id"])
        grant.scopes_json = '["local:draft"]'
        grant.revision += 1
        db.commit()
        assert task_worker.claim_next_pending_task(db) is None
        task = first_task(db)
        assert (
            db.get(WorkbenchDeviceWorkBinding, h3_task_resource(task)).blocked_code
            == "DEVICE_SCOPE_DENIED"
        )
        task.error_code = "SUBMIT_OUTCOME_UNKNOWN"
        task.status = "UPLOADING"
        from app.services.runninghub_attempts import ensure_reserved_task_attempt

        attempt = ensure_reserved_task_attempt(task)
        attempt.status = "SUBMIT_UNKNOWN"
        old_account = task.execution_account_id
        task_id = task.id
        db.commit()
    monkeypatch.setattr(
        task_worker,
        "_make_client",
        lambda config: pytest.fail("uncertain paid submit must not dispatch"),
    )
    with SessionLocal() as db:
        task_worker.process_task(db, task_id)
        task = db.get(GenerationTask, task_id)
        assert task.error_code == "SUBMIT_OUTCOME_UNKNOWN"
        assert task.status == "UPLOADING" and task.execution_account_id == old_account
        assert task.runninghub_attempts[-1].status == "SUBMIT_UNKNOWN"


def test_generic_retry_helpers_cannot_borrow_original_task_device(client, licensed):
    from app.config import get_settings
    from app.services.task_management import (
        prepare_task_retry,
        prepare_successful_segment_regeneration,
    )

    confirm(client, licensed)
    with SessionLocal() as db:
        task = first_task(db)
        task.status = "FAILED"
        task.error_code = "PROVIDER_FAILED"
        task.runninghub_task_id = "original-paid-remote"
        db.commit()
        with pytest.raises(DeviceAuthError, match="校验本机授权"):
            prepare_task_retry(task, get_settings())
        assert (
            task.status == "FAILED"
            and task.runninghub_task_id == "original-paid-remote"
        )
        task.status = "SUCCESS"
        db.commit()
        with pytest.raises(DeviceAuthError):
            prepare_successful_segment_regeneration(task, get_settings())
        assert (
            task.status == "SUCCESS"
            and task.runninghub_task_id == "original-paid-remote"
        )
        task.status = "DOWNLOAD_FAILED"
        db.commit()
        # Existing result recovery remains available through the shared helper.
        prepare_task_retry(task, get_settings())
        db.commit()
        assert (
            task.runninghub_task_id == "original-paid-remote"
            and task.status == "RUNNING"
        )


def test_worker_applies_same_admission_to_new_workbench_tasks(client, licensed):
    confirm(client, licensed)
    with SessionLocal() as db:
        task = first_task(db)
        batch = task.segment.batch_item.batch
        task.workflow_type = "digital_human"
        batch.workflow_type = "digital_human"
        batch.source_channel = "new_workbench"
        task_id = task.id
        db.commit()
    change_grant(licensed, "suspend")
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) is None
        task = db.get(GenerationTask, task_id)
        binding = db.get(WorkbenchDeviceWorkBinding, h3_task_resource(task))
        assert binding.blocked_code == "DEVICE_SUSPENDED"
        assert task.status == "PENDING" and task.runninghub_task_id is None
    change_grant(licensed, "resume")
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == task_id


def test_unbound_new_workbench_waits_but_legacy_website_keeps_old_contract(
    client, licensed
):
    confirm(client, licensed)
    with SessionLocal() as db:
        task = first_task(db)
        batch = task.segment.batch_item.batch
        task.workflow_type = "digital_human"
        batch.workflow_type = "digital_human"
        batch.source_channel = "new_workbench"
        task_id = task.id
        for sibling in db.scalars(
            select(GenerationTask).where(GenerationTask.id != task_id)
        ):
            sibling.status = "SUCCESS"
        binding = db.get(WorkbenchDeviceWorkBinding, h3_task_resource(task))
        db.delete(binding)
        db.commit()
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) is None
        task = db.get(GenerationTask, task_id)
        assert db.get(WorkbenchDeviceWorkBinding, h3_task_resource(task)).blocked_code == "DEVICE_ADMISSION_REQUIRED"
        task.segment.batch_item.batch.source_channel = "legacy_web"
        db.delete(db.get(WorkbenchDeviceWorkBinding, h3_task_resource(task)))
        db.commit()
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == task_id


def test_audio_retry_binds_device_and_worker_rechecks_current_grant(
    client, licensed
):
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        task.status = "FAILED"
        task.batch_item.audio_status = "FAILED"
        task.batch_item.status = "AUDIO_FAILED"
        task_id = task.id
        batch_id = task.batch_item.batch_id
        item_id = task.batch_item_id
        db.commit()
    path = f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/retry"
    response = post(client, licensed, path, cost_confirmed=True, speed=0.95)
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        binding = db.get(
            WorkbenchDeviceWorkBinding,
            ("audio_generation_task", task_id),
        )
        operation = db.get(WorkbenchDeviceOperation, binding.operation_id)
        assert operation.operation_kind == "workbench.audio.retry"
        assert operation.thumbprint
    change_grant(licensed, "suspend")
    with SessionLocal() as db:
        assert audio_worker.claim_next_pending_task(db) is None
        binding = db.get(
            WorkbenchDeviceWorkBinding,
            ("audio_generation_task", task_id),
        )
        assert binding.blocked_code == "DEVICE_SUSPENDED"
    change_grant(licensed, "resume")
    with SessionLocal() as db:
        assert audio_worker.claim_next_pending_task(db) == task_id


def test_voice_creation_binds_device_and_worker_rechecks_current_grant(
    client, licensed, monkeypatch
):
    monkeypatch.setattr(
        "app.services.speech.voice_studio.inspect_audio_duration",
        lambda path: 18.0,
    )
    path = "/api/workbench/voice-creations"
    headers = auth_headers(
        client,
        licensed["key"],
        licensed["access"],
        "request",
        path=path,
        method="POST",
        bound=True,
    )
    response = client.post(
        path,
        data={
            "method": "clone",
            "name": "设备准入声音",
            "preview_text": "这是一段设备授权声音试听。",
            "model": "speech-2.8-turbo",
            "cost_confirmed": "true",
        },
        files={"source_a": ("sample.mp3", b"ID3voice-sample", "audio/mpeg")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["task_id"]
    with SessionLocal() as db:
        task = db.get(VoiceCreationTask, task_id)
        assert task.source_channel == "new_workbench"
        binding = db.get(
            WorkbenchDeviceWorkBinding,
            ("voice_creation_task", task_id),
        )
        operation = db.get(WorkbenchDeviceOperation, binding.operation_id)
        assert operation.operation_kind == "workbench.voice.create"
        assert operation.thumbprint
    change_grant(licensed, "suspend")
    with SessionLocal() as db:
        assert voice_jobs.claim_next_voice_task(db) is None
        binding = db.get(
            WorkbenchDeviceWorkBinding,
            ("voice_creation_task", task_id),
        )
        assert binding.blocked_code == "DEVICE_SUSPENDED"
    change_grant(licensed, "resume")
    with SessionLocal() as db:
        assert voice_jobs.claim_next_voice_task(db) == task_id
