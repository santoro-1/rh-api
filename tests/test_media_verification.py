from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import uuid

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import MediaAsset, MediaVerificationJob
from app.routes import cloud_media_jobs
from app.services.media_verification import enqueue
from tests.conftest import create_user


TOKEN = "cloud-verifier-token-isolated-1234567890"
DATA = b"sample-media-bytes"
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def worker_enabled(monkeypatch):
    settings = replace(
        get_settings(), media_cloud_worker_enabled=True,
        media_cloud_worker_token=TOKEN, media_cloud_worker_lease_seconds=900,
        media_cloud_worker_max_active=2,
    )
    monkeypatch.setattr(cloud_media_jobs, "get_settings", lambda: settings)
    return settings


def _asset(username: str, *, kind: str = "video") -> str:
    user = create_user(username)
    asset_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        asset = MediaAsset(
            id=asset_id, user_id=user.id, kind=kind, source_channel="web_legacy",
            asset_role="input_video", upload_key=f"upload-{asset_id}",
            original_name="input.mp4", size_bytes=len(DATA), expected_sha256=SHA,
            object_key=f"media/users/{user.id}/{asset_id}/source",
            multipart_upload_id="upload", part_size=8 * 1024 * 1024,
            status="UPLOADED", created_at=now, updated_at=now,
            expires_at=now + timedelta(days=1),
        )
        db.add(asset)
        enqueue(db, asset)
        enqueue(db, asset)
        db.commit()
    return asset_id


def _post(client, path: str, payload: dict | None = None, token: str = TOKEN):
    return client.post(
        f"/api/cloud-media/jobs{path}", json=payload or {},
        headers={"Authorization": f"Bearer {token}"},
    )


def _claim(client):
    response = _post(client, "/claim")
    assert response.status_code == 200, response.text
    return response.json()["job"]


def _complete(client, job: dict, **changes):
    body = {
        "lease_id": job["lease_id"], "outcome": "verified",
        "observed_size": len(DATA), "observed_sha256": SHA,
        "detected_kind": "video",
    }
    body.update(changes)
    return _post(client, f"/{job['job_id']}/complete", body)


def test_01_worker_disabled_by_default(client):
    assert _post(client, "/claim").status_code == 503


def test_02_token_required(client, worker_enabled):
    assert _post(client, "/claim", token="wrong").status_code == 401


def test_03_enqueue_is_idempotent(client, worker_enabled):
    asset_id = _asset("verify-enqueue")
    with SessionLocal() as db:
        assert db.query(MediaVerificationJob).filter_by(asset_id=asset_id).count() == 1


def test_04_claim_contains_metadata_but_no_credentials_or_bytes(client, worker_enabled):
    _asset("verify-claim")
    job = _claim(client)
    assert job["size_bytes"] == len(DATA)
    assert job["expected_sha256"] == SHA
    assert job["object_key"].startswith("media/users/")
    assert "access_key" not in job and "file" not in job


def test_05_wrong_lease_cannot_complete(client, worker_enabled):
    _asset("verify-lease")
    job = _claim(client)
    assert _complete(client, job, lease_id="forged").status_code == 409


def test_06_heartbeat_extends_valid_lease(client, worker_enabled):
    _asset("verify-heartbeat")
    job = _claim(client)
    response = _post(client, f"/{job['job_id']}/heartbeat", {"lease_id": job["lease_id"]})
    assert response.status_code == 200
    assert response.json()["lease_expires_at"]


def test_07_valid_receipt_makes_asset_ready(client, worker_enabled):
    asset_id = _asset("verify-ready")
    job = _claim(client)
    assert _complete(client, job).json()["status"] == "READY"
    with SessionLocal() as db:
        asset = db.get(MediaAsset, asset_id)
        assert asset.verified_sha256 == SHA


def test_08_digest_mismatch_rejected_before_use(client, worker_enabled):
    _asset("verify-digest")
    job = _claim(client)
    assert _complete(client, job, observed_sha256="b" * 64).json()["status"] == "REJECTED"


def test_09_media_type_mismatch_rejected(client, worker_enabled):
    _asset("verify-kind")
    job = _claim(client)
    assert _complete(client, job, detected_kind="image").json()["status"] == "REJECTED"


def test_10_retryable_error_returns_job_to_queue(client, worker_enabled):
    _asset("verify-retry")
    job = _claim(client)
    response = _complete(client, job, outcome="retryable_error", error_code="WORKER_ERROR")
    assert response.json()["status"] == "UPLOADED"
    assert _claim(client)["lease_id"] != job["lease_id"]


def test_11_third_retryable_error_rejects(client, worker_enabled):
    _asset("verify-exhaust")
    for attempt in range(3):
        job = _claim(client)
        response = _complete(client, job, outcome="retryable_error", error_code="WORKER_ERROR")
        assert response.json()["status"] == ("REJECTED" if attempt == 2 else "UPLOADED")
    assert _claim(client) is None


def test_12_replayed_success_is_idempotent(client, worker_enabled):
    _asset("verify-replay")
    job = _claim(client)
    assert _complete(client, job).json()["status"] == "READY"
    assert _complete(client, job).json()["status"] == "READY"


def test_13_max_active_limits_claims(client, worker_enabled):
    for number in range(3):
        _asset(f"verify-cap-{number}")
    assert _claim(client)
    assert _claim(client)
    assert _claim(client) is None


def test_14_invalid_size_type_rejected(client, worker_enabled):
    _asset("verify-type")
    job = _claim(client)
    assert _complete(client, job, observed_size=True).status_code == 400


def test_15_expired_lease_cannot_complete(client, worker_enabled):
    _asset("verify-expired")
    job = _claim(client)
    with SessionLocal() as db:
        record = db.get(MediaVerificationJob, job["job_id"])
        record.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert _complete(client, job).status_code == 409
    assert _claim(client)["lease_id"] != job["lease_id"]


def test_16_stream_verifier_detects_checksum_and_format(monkeypatch):
    from app import cloud_media_verifier

    class Bucket:
        def get_object(self, key):
            assert key.endswith("/source")
            return io.BytesIO(DATA)

    job = {
        "object_key": "media/users/1/id/source", "size_bytes": len(DATA),
        "expected_sha256": SHA, "kind": "video",
    }
    monkeypatch.setattr(cloud_media_verifier, "_detected_kind", lambda path: "video")
    assert cloud_media_verifier.verify_object(job, Bucket(), 1024)["outcome"] == "verified"
    assert cloud_media_verifier.verify_object(
        {**job, "expected_sha256": "b" * 64}, Bucket(), 1024
    )["error_code"] == "SHA_MISMATCH"
    monkeypatch.setattr(cloud_media_verifier, "_detected_kind", lambda path: "image")
    assert cloud_media_verifier.verify_object(job, Bucket(), 1024)["error_code"] == "FORMAT_INVALID"


def test_17_replayed_failure_receipts_do_not_consume_more_attempts(client, worker_enabled):
    _asset("verify-failure-replay")
    job = _claim(client)
    body = {"outcome": "invalid", "error_code": "FORMAT_INVALID"}
    assert _complete(client, job, **body).json()["status"] == "REJECTED"
    assert _complete(client, job, **body).json()["status"] == "REJECTED"
    assert _complete(client, job, **{**body, "error_code": "SHA_MISMATCH"}).status_code == 409


def test_18_replayed_retryable_receipt_is_stable(client, worker_enabled):
    _asset("verify-retry-replay")
    job = _claim(client)
    body = {"outcome": "retryable_error", "error_code": "WORKER_ERROR"}
    assert _complete(client, job, **body).json()["status"] == "UPLOADED"
    assert _complete(client, job, **body).json()["status"] == "UPLOADED"
