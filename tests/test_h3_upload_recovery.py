from __future__ import annotations

import hashlib
import json
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import GenerationTask
from app.services.h3 import upload_recovery as recovery
from app.services.runninghub import RunningHubError
from app.services.runninghub_uploads import prepare_runninghub_retry_upload
from app.services.task_management import TaskManagementError, prepare_task_retry
from app.workers import task_worker
from app.workflows.base import WorkflowAsset
from tests.conftest import login
from tests.test_h3_worker_runtime import _FakeH3RunningHub, _prepare_confirmed_h3_task


def chunk(kind: bytes, value: bytes) -> bytes:
    return (
        struct.pack(">I", len(value))
        + kind
        + value
        + struct.pack(">I", zlib.crc32(kind + value))
    )


def png() -> bytes:
    return (
        recovery.PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0))
        + chunk(b"gAMA", struct.pack(">I", 45455))
        + chunk(b"tEXt", b"Author\0original metadata")
        + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\xff\0\xff\0\x80"))
        + chunk(b"IEND", b"")
    )


def chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    parts = []
    pos = 8
    while pos < len(data):
        size = struct.unpack(">I", data[pos : pos + 4])[0]
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + size]
        assert (
            zlib.crc32(kind + body)
            == struct.unpack(">I", data[pos + 8 + size : pos + 12 + size])[0]
        )
        parts.append((kind, body))
        pos += size + 12
    return parts


def failure(filename: str) -> dict:
    return {
        "taskId": "old-paid-remote",
        "status": "FAILED",
        "failedReason": {
            "node_name": "LoadImage",
            "node_id": "97",
            "exception_type": "av.error.FileNotFoundError",
            "exception_message": f"[Errno 2] No such file or directory: '/workspace/ComfyUI/input/{filename}'",
        },
    }


def task_fixture(tmp_path: Path):
    source = tmp_path / "original.png"
    source.write_bytes(png())
    remote_name = f"openapi/{hashlib.sha256(png()).hexdigest()}.png"
    task = GenerationTask(
        id="png-retry-task",
        workflow_type="minimax_h3_ref2va",
        status="PENDING",
        created_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        input_payload=json.dumps(
            {"assets": {"frozen": "unchanged"}, "parameters": {"prompt": "keep"}}
        ),
        runninghub_attempt_history=json.dumps([failure(remote_name)]),
    )
    asset = WorkflowAsset("identity_image_1", "image", "original.png", "图3.png")
    return task, asset, source, remote_name


def test_png_retry_preserves_every_original_chunk_and_pixels(tmp_path):
    task, asset, source, old_name = task_fixture(tmp_path)
    original_payload = task.input_payload
    prepared = prepare_runninghub_retry_upload(task, asset, source, tmp_path)
    assert prepared != source
    original = chunks(source.read_bytes())
    refreshed = chunks(prepared.read_bytes())
    assert refreshed[:-2] + refreshed[-1:] == original
    assert refreshed[-2][0] == b"tEXt"
    assert refreshed[-2][1].startswith(b"RunningHub-Retry\0")
    assert source.read_bytes() == png()
    assert recovery.sha256_file(prepared) != recovery.sha256_file(source)
    assert task.input_payload == original_payload
    assert zlib.decompress(dict(refreshed)[b"IDAT"]) == zlib.decompress(
        dict(original)[b"IDAT"]
    )


def test_retry_copy_is_stable_on_recovery_but_changes_after_next_remote_failure(
    tmp_path,
):
    task, asset, source, old_name = task_fixture(tmp_path)
    a, b, c = [tmp_path / x for x in ("a", "b", "c")]
    for d in (a, b, c):
        d.mkdir()
    first = recovery.prepare_png_retry(task, asset, source, a)
    assert (
        first.read_bytes()
        == recovery.prepare_png_retry(task, asset, source, b).read_bytes()
    )
    recovery.start_upload_receipt(task, "attempt-1")
    refreshed_name = f"openapi/{recovery.sha256_file(first)}.png"
    recovery.record_upload(task, asset, source, first, refreshed_name)
    recovery.bind_upload_receipt(task, "new-paid-remote")
    task.runninghub_task_id = "new-paid-remote"
    next_failure = failure(refreshed_name)
    next_failure["taskId"] = task.runninghub_task_id
    next_failure["uploadReceipt"] = recovery.failure_upload_receipt(task)
    task.runninghub_attempt_history = json.dumps([next_failure])
    assert (
        first.read_bytes()
        != recovery.prepare_png_retry(task, asset, source, c).read_bytes()
    )


@pytest.mark.parametrize(
    "change",
    [
        "oom",
        "newer_success",
        "wrong_node",
        "wrong_type",
        "local_path",
        "jpg",
        "bad_json",
        "wrong_image",
        "other_workflow",
    ],
)
def test_recovery_does_not_match_unrelated_failures(tmp_path, change):
    task, asset, source, old_name = task_fixture(tmp_path)
    entry = failure(old_name)
    reason = entry["failedReason"]
    if change == "oom":
        reason["exception_message"] = "OOM_KILLED"
    elif change == "wrong_node":
        reason["node_name"] = "LoadVideo"
    elif change == "wrong_type":
        reason["exception_type"] = "ValueError"
    elif change == "local_path":
        reason["exception_message"] = "No such file or directory: C:/images/old.png"
    elif change == "jpg":
        reason["exception_message"] = reason["exception_message"].replace(
            ".png", ".jpg"
        )
    elif change == "wrong_image":
        source.write_bytes(png() + b"different")
    elif change == "other_workflow":
        task.workflow_type = "digital_human"
    history = [entry, {"status": "SUCCESS"}] if change == "newer_success" else [entry]
    task.runninghub_attempt_history = (
        "broken JSON" if change == "bad_json" else json.dumps(history)
    )
    assert prepare_runninghub_retry_upload(task, asset, source, tmp_path) == source


@pytest.mark.parametrize(
    "corrupt",
    [
        b"not-png",
        png()[:-5],
        png() + b"trailing",
        png().replace(b"original metadata", b"tampered metadata"),
    ],
)
def test_malformed_png_stops_without_modifying_original(tmp_path, corrupt):
    task, asset, source, _ = task_fixture(tmp_path)
    source.write_bytes(corrupt)
    task.runninghub_attempt_history = json.dumps(
        [failure(f"openapi/{recovery.sha256_file(source)}.png")]
    )
    with pytest.raises(recovery.H3UploadRecoveryError):
        recovery.prepare_png_retry(task, asset, source, tmp_path)
    assert source.read_bytes() == corrupt


def test_continuity_anchor_and_other_slots_are_selected_by_exact_receipt(tmp_path):
    task, asset, source, _ = task_fixture(tmp_path)
    opaque_name = "openapi/provider-object.png"
    f = failure(opaque_name)
    f["failedReason"]["node_id"] = "178"
    f["uploadReceipt"] = {
        "remote_task_id": f["taskId"],
        "assets": {
            "continuity_anchor": {
                "remote_filename": opaque_name,
                "source_sha256": recovery.sha256_file(source),
            }
        },
    }
    task.runninghub_attempt_history = json.dumps([f])
    anchor = WorkflowAsset("continuity_anchor", "image", "original.png", "anchor.png")
    assert recovery.prepare_png_retry(task, asset, source, tmp_path) == source
    assert recovery.prepare_png_retry(task, anchor, source, tmp_path) != source


@pytest.mark.parametrize(
    "remote_name",
    [
        "https://host/image.png?token=SECRET",
        "../secret.png",
        "/absolute.png",
        "openapi/a.png\nAPIKEY=SECRET",
    ],
)
def test_upload_receipt_rejects_unsafe_provider_filename(tmp_path, remote_name):
    task, asset, source, _ = task_fixture(tmp_path)
    recovery.start_upload_receipt(task, "attempt")
    with pytest.raises(recovery.H3UploadRecoveryError) as exc:
        recovery.record_upload(task, asset, source, source, remote_name)
    assert "SECRET" not in str(exc.value)
    assert "SECRET" not in task.input_payload


def test_unknown_missing_image_does_not_guess_or_submit(tmp_path):
    task, asset, source, _ = task_fixture(tmp_path)
    task.runninghub_attempt_history = json.dumps([failure("openapi/unknown.png")])
    recovery.start_upload_receipt(task, "attempt")
    recovery.record_upload(task, asset, source, source, "openapi/new.png")
    with pytest.raises(recovery.H3UploadRecoveryError, match="无法安全定位"):
        recovery.ensure_recovered_uploads(task)


@pytest.mark.parametrize(
    "outcome", ["accepted", "same_bad_key", "upload_failed", "submit_unknown"]
)
def test_h3_worker_recovers_only_failed_segment_and_never_blindly_resubmits(
    client, monkeypatch, outcome, caplog
):
    username = "h3-png-" + outcome
    _prepare_confirmed_h3_task(client, monkeypatch, username)
    with SessionLocal() as db:
        tasks = db.query(GenerationTask).order_by(GenerationTask.created_at).all()
        target, sibling = tasks
        target_id, sibling_id = target.id, sibling.id
        original_payload = json.loads(target.input_payload)
        source = (
            get_settings().data_dir
            / original_payload["assets"]["identity_image_1"]["path"]
        )
        source.write_bytes(png())
        old_name = f"openapi/{recovery.sha256_file(source)}.png"
        target.status = "FAILED"
        target.runninghub_task_id = "old-paid-remote"
        target.runninghub_attempt_history = json.dumps([failure(old_name)])
        target.runninghub_failed_reason = json.dumps(failure(old_name)["failedReason"])
        sibling.status = "SUCCESS"
        sibling.runninghub_task_id = "successful-sibling-remote"
        sibling.result_path = "outputs/keep-sibling.mp4"
        sibling_file = get_settings().data_dir / sibling.result_path
        sibling_file.parent.mkdir(parents=True, exist_ok=True)
        sibling_file.write_bytes(b"successful-result")
        db.commit()
        original_history = target.runninghub_attempt_history
        prepare_task_retry(target, get_settings())
        db.commit()
        assert target.runninghub_failed_reason is None
        assert target.runninghub_attempt_history == original_history

    class Fake(_FakeH3RunningHub):
        def upload_file(self, path):
            if path.name.startswith("h3-retry-"):
                if outcome == "upload_failed":
                    raise RunningHubError("mock upload unavailable", retry_safe=True)
                if outcome == "same_bad_key":
                    return old_name
            return f"openapi/{recovery.sha256_file(path)}{path.suffix}"

        def submit_task(self, payload):
            if outcome == "submit_unknown":
                raise RunningHubError(
                    "mock response lost", submission_outcome_unknown=True
                )
            return super().submit_task(payload)

    fake = Fake()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    caplog.set_level("INFO")
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == target_id
        task_worker.process_task(db, target_id)
        db.expire_all()
        task = db.get(GenerationTask, target_id)
        new_payload = json.loads(task.input_payload)
        assert new_payload["assets"] == original_payload["assets"]
        assert new_payload["parameters"] == original_payload["parameters"]
        assert source.read_bytes() == png()
        sibling = db.get(GenerationTask, sibling_id)
        assert (sibling.status, sibling.runninghub_task_id) == (
            "SUCCESS",
            "successful-sibling-remote",
        )
        assert sibling_file.read_bytes() == b"successful-result"
        if outcome == "accepted":
            assert task.status == "SUBMITTED"
            assert fake.submissions == 1
            graph = json.loads(fake.last_payload["workflow"])
            assert graph["97"]["inputs"]["image"] != old_name
            assert (
                graph["83"]["inputs"]["text"]
                == original_payload["parameters"]["prompt"]
            )
            receipt = new_payload[recovery.RECEIPT_KEY]
            assert receipt["remote_task_id"] == task.runninghub_task_id
            assert receipt["execution_account_id"] == task.execution_account_id
            record = receipt["assets"]["identity_image_1"]
            assert record["source_sha256"] != record["upload_sha256"]
            assert record["remote_filename"] == graph["97"]["inputs"]["image"]
            assert "video.asset_uploaded" in caplog.text
            assert "h3-runninghub-" + username not in caplog.text
            remote_failure = failure(graph["97"]["inputs"]["image"])
            fake.query_task = lambda task_id: {**remote_failure, "errorCode": "805"}
            task_worker.process_task(db, target_id)
            assert task.status == "FAILED"
            assert task.runninghub_auto_retry_count == 0
            assert task.runninghub_auto_retry_after is None
            assert (
                json.loads(task.runninghub_attempt_history)[-1]["uploadReceipt"]
                == receipt
            )
            assert fake.submissions == 1
        else:
            assert fake.submissions == 0
            assert task.runninghub_task_id is None
            if outcome == "same_bad_key":
                assert task.status == "FAILED"
                assert task.error_code == "H3_INPUT_RECOVERY_FAILED"
                assert task.runninghub_auto_retry_count == 0
                assert task.runninghub_auto_retry_after is None
            elif outcome == "upload_failed":
                assert task.status == "PENDING"
            else:
                assert task.status == "FAILED"
                with pytest.raises(TaskManagementError, match="禁止盲目重提"):
                    prepare_task_retry(task, get_settings())


def test_h3_retry_rejects_stale_request_after_another_request_and_worker_claim(
    client, monkeypatch
):
    _prepare_confirmed_h3_task(client, monkeypatch, "h3-retry-race")
    with SessionLocal() as db:
        task = db.query(GenerationTask).first()
        task_id = task.id
        task.status = "FAILED"
        db.commit()
    with SessionLocal() as stale, SessionLocal() as winner:
        stale_task = stale.get(GenerationTask, task_id)
        active_task = winner.get(GenerationTask, task_id)
        prepare_task_retry(active_task, get_settings())
        winner.commit()
        active_task.status = "SUBMITTED"
        active_task.runninghub_task_id = "already-accepted"
        winner.commit()
        assert stale_task.status == "FAILED"
        with pytest.raises(TaskManagementError, match="状态已变化"):
            prepare_task_retry(stale_task, get_settings())
        stale.rollback()
        winner.expire_all()
        assert (
            winner.get(GenerationTask, task_id).runninghub_task_id == "already-accepted"
        )


def test_legacy_h3_retry_double_click_is_rejected(client, monkeypatch):
    username = "h3-legacy-double-click"
    _prepare_confirmed_h3_task(client, monkeypatch, username)
    with SessionLocal() as db:
        task = db.query(GenerationTask).first()
        task.status = "FAILED"
        task_id = task.id
        db.commit()
    login(client, username)
    assert (
        client.post(f"/tasks/{task_id}/retry", follow_redirects=False).status_code
        == 303
    )
    assert (
        client.post(f"/tasks/{task_id}/retry", follow_redirects=False).status_code
        == 409
    )


def test_h3_confirmation_replay_keeps_same_retry_receipt(client, monkeypatch):
    from tests.test_h3_workbench import _token

    username = "h3-retry-confirm-replay"
    _prepare_confirmed_h3_task(client, monkeypatch, username)
    token = _token(client, username)
    with SessionLocal() as db:
        task = db.query(GenerationTask).first()
        task.status = "FAILED"
        segment_id = task.segment_id
        task_id = task.id
        db.commit()
    preview = client.post(
        f"/api/workbench/h3-segments/{segment_id}/retry/prepare",
        json={"access_token": token},
    )
    assert preview.status_code == 200
    request = {
        "access_token": token,
        "request_key": "same-confirm-click",
        "quote_token": preview.json()["quote_token"],
        "cost_confirmed": True,
    }
    url = f"/api/workbench/h3-segments/{segment_id}/retry/confirm"
    first = client.post(url, json=request)
    second = client.post(url, json=request)
    assert first.status_code == second.status_code == 200
    assert first.json()["retry"] == second.json()["retry"]
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task.status == "PENDING"
        assert task.runninghub_task_id is None
        assert not task.runninghub_attempts


def test_upload_receipt_from_different_remote_attempt_cannot_select_image(tmp_path):
    task, asset, source, old_name = task_fixture(tmp_path)
    f = failure("openapi/opaque-file.png")
    f["uploadReceipt"] = {
        "remote_task_id": "unrelated-remote",
        "assets": {
            asset.name: {
                "remote_filename": "openapi/opaque-file.png",
                "source_sha256": recovery.sha256_file(source),
            }
        },
    }
    task.runninghub_attempt_history = json.dumps([f])
    assert recovery.prepare_png_retry(task, asset, source, tmp_path) == source
