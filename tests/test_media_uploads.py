from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import MediaAsset, MediaVerificationJob
from app.routes import media_assets as routes
from app.services.media_oss import UploadedPart
from app.services.media_oss import OssMediaStore
from tests.conftest import create_user, login


class FakeStore:
    def __init__(self) -> None:
        self.parts: dict[str, list[UploadedPart]] = {}
        self.sizes: dict[str, int] = {}
        self.completed = 0
        self.raise_after_complete = False

    def initiate(self, key: str) -> str:
        self.parts[key] = []
        return "fake-multipart-id"

    def sign_part(self, key: str, upload_id: str, number: int) -> str:
        return f"https://oss.example/{key}?partNumber={number}&uploadId={upload_id}"

    def list_parts(self, key: str, upload_id: str) -> list[UploadedPart]:
        return self.parts[key]

    def complete(self, key: str, upload_id: str, parts: list[UploadedPart]) -> None:
        self.completed += 1
        self.sizes[key] = sum(part.size for part in parts)
        if self.raise_after_complete:
            raise TimeoutError("Complete reply lost")

    def object_size(self, key: str) -> int:
        return self.sizes.get(key, -1)


@pytest.fixture
def oss_enabled(monkeypatch):
    store = FakeStore()
    settings = replace(get_settings(), media_oss_enabled=True)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "_store", lambda: store)
    return store


def _request(key: str = "media-test-key-001", **changes):
    payload = {
        "kind": "video",
        "source_channel": "web_legacy",
        "asset_role": "input_video",
        "upload_key": key,
        "filename": "reference.mp4",
        "size_bytes": 17 * 1024 * 1024,
        "source_sha256": "a" * 64,
    }
    payload.update(changes)
    return payload


def _start(client, payload=None):
    return client.post("/api/media-assets/uploads", json=payload or _request())


def test_01_disabled_by_default(client):
    create_user("media-disabled")
    login(client, "media-disabled")
    assert _start(client).status_code == 503


def test_02_create_is_metadata_only(client, oss_enabled):
    create_user("media-create")
    login(client, "media-create")
    response = _start(client)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "UPLOADING"
    assert response.json()["part_count"] == 3
    with SessionLocal() as db:
        asset = db.get(MediaAsset, response.json()["asset_id"])
        assert asset.object_key.startswith("media/users/")
        assert asset.expected_sha256 == "a" * 64
        assert not hasattr(asset, "relative_path")


def test_03_same_key_resumes_without_new_upload(client, oss_enabled):
    create_user("media-resume")
    login(client, "media-resume")
    first = _start(client).json()
    second = _start(client).json()
    assert first["asset_id"] == second["asset_id"]
    assert len(oss_enabled.parts) == 1


def test_04_same_key_different_digest_rejected(client, oss_enabled):
    create_user("media-conflict")
    login(client, "media-conflict")
    _start(client)
    response = _start(client, _request(source_sha256="b" * 64))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MEDIA_KEY_CONFLICT"


def test_05_asset_is_user_isolated(client, oss_enabled):
    create_user("media-owner")
    create_user("media-other")
    login(client, "media-owner")
    asset_id = _start(client).json()["asset_id"]
    client.get("/logout")
    login(client, "media-other")
    assert client.get(f"/api/media-assets/uploads/{asset_id}").status_code == 404


def test_06_size_limit_before_oss_initiate(client, oss_enabled):
    create_user("media-limit")
    login(client, "media-limit")
    response = _start(client, _request(size_bytes=501 * 1024 * 1024))
    assert response.status_code == 413
    assert oss_enabled.parts == {}


def test_07_only_missing_parts_are_signed(client, oss_enabled):
    create_user("media-parts")
    login(client, "media-parts")
    asset_id = _start(client).json()["asset_id"]
    key = next(iter(oss_enabled.parts))
    oss_enabled.parts[key] = [UploadedPart(1, 8 * 1024 * 1024, "etag-1")]
    response = client.post(f"/api/media-assets/uploads/{asset_id}/parts", json={"start": 1, "count": 3})
    assert response.status_code == 200
    assert response.json()["uploaded_parts"] == [1]
    assert [part["number"] for part in response.json()["parts"]] == [2, 3]


def test_08_incomplete_or_wrong_parts_cannot_complete(client, oss_enabled):
    create_user("media-incomplete")
    login(client, "media-incomplete")
    asset_id = _start(client).json()["asset_id"]
    url = f"/api/media-assets/uploads/{asset_id}/complete"
    assert client.post(url).status_code == 409
    key = next(iter(oss_enabled.parts))
    oss_enabled.parts[key] = [
        UploadedPart(1, 8 * 1024 * 1024, "a"),
        UploadedPart(2, 7 * 1024 * 1024, "b"),
        UploadedPart(3, 1 * 1024 * 1024, "c"),
    ]
    assert client.post(url).status_code == 422
    assert oss_enabled.completed == 0


def test_09_complete_timeout_is_reconciled_and_not_ready(client, oss_enabled):
    create_user("media-complete")
    login(client, "media-complete")
    asset_id = _start(client).json()["asset_id"]
    key = next(iter(oss_enabled.parts))
    oss_enabled.parts[key] = [
        UploadedPart(1, 8 * 1024 * 1024, "a"),
        UploadedPart(2, 8 * 1024 * 1024, "b"),
        UploadedPart(3, 1 * 1024 * 1024, "c"),
    ]
    oss_enabled.raise_after_complete = True
    url = f"/api/media-assets/uploads/{asset_id}/complete"
    assert client.post(url).json()["status"] == "UPLOADED"
    assert client.post(url).json()["status"] == "UPLOADED"
    assert oss_enabled.completed == 1
    with SessionLocal() as db:
        assert db.query(MediaVerificationJob).filter_by(asset_id=asset_id).count() == 1


def test_10_channel_and_checksum_spoof_rejected(client, oss_enabled):
    create_user("media-guard")
    login(client, "media-guard")
    assert _start(client, _request(source_channel="h3_workbench")).status_code == 403
    assert _start(client, _request(source_sha256="not-a-digest")).status_code == 400
    assert oss_enabled.parts == {}


def test_11_oss_v4_part_signature_contains_only_its_part(monkeypatch):
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "test-key-id")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "test-key-secret")
    settings = replace(
        get_settings(), media_oss_enabled=True,
        media_oss_bucket="testbucket",
        media_oss_public_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        media_oss_internal_endpoint="https://oss-cn-hangzhou-internal.aliyuncs.com",
    )
    store = OssMediaStore(settings)
    url = store.sign_part("media/users/1/id/source", "upload-test-id", 2)
    assert "partNumber=2" in url
    assert "uploadId=upload-test-id" in url
    assert "oss-cn-hangzhou.aliyuncs.com" in url
    assert "x-oss-signature-version=OSS4-HMAC-SHA256" in url


def test_12_active_local_h3_key_is_not_rebound_to_oss(client, oss_enabled):
    create_user("media-h3-mixed", h3_access_enabled=True)
    login(client, "media-h3-mixed")
    content = b"\x89PNG\r\n\x1a\n" + b"mixed-protocol"
    key = "mixed-h3-upload-key"
    old = client.post(
        "/api/h3-page/assets",
        data={
            "kind": "image", "asset_role": "reference_image",
            "upload_key": key, "transport_profile": "original-v1",
        },
        files={"file": ("face.png", content, "image/png")},
    )
    assert old.status_code == 201
    response = _start(client, _request(
        key, kind="image", source_channel="h3_web",
        asset_role="reference_image", filename="face.png",
        size_bytes=len(content), source_sha256=hashlib.sha256(content).hexdigest(),
    ))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MEDIA_LEGACY_KEY_EXISTS"
    assert oss_enabled.parts == {}


def test_13_oss_cannot_enable_without_isolated_configuration(monkeypatch):
    monkeypatch.setenv("MEDIA_OSS_ENABLED", "true")
    monkeypatch.delenv("MEDIA_OSS_BUCKET", raising=False)
    with pytest.raises(ValueError, match="杭州 Bucket"):
        Settings.from_environment()
