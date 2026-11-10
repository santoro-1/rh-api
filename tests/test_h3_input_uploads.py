from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models import StagedAsset, User
from app.services.batch_assets import StagedAssetError, load_available_assets
from app.services.storage import _validate_content_signature
from tests.conftest import create_user, login
from tests.test_h3_workbench import _token


PNG = b"\x89PNG\r\n\x1a\n" + b"h3-image"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"h3-video"


class _TrackedFile(io.BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


class _TrackedPath:
    def __init__(self, content: bytes) -> None:
        self.stream = _TrackedFile(content)

    def open(self, mode: str):
        assert mode == "rb"
        return self.stream


def test_content_signature_reads_only_first_32_bytes() -> None:
    path = _TrackedPath(PNG + b"x" * (2 * 1024 * 1024))
    _validate_content_signature(path, "image")  # type: ignore[arg-type]
    assert path.stream.read_sizes == [32]


def _upload_web_image(client, *, upload_key: str, content: bytes = PNG):
    return client.post(
        "/api/h3-page/assets",
        data={
            "kind": "image",
            "asset_role": "reference_image",
            "upload_key": upload_key,
            "transport_profile": "original-v1",
        },
        files={"file": ("人物图.png", content, "image/png")},
    )


def test_mock_01_web_image_upload_records_original_digest(client) -> None:
    create_user("h3-web-upload", h3_access_enabled=True)
    login(client, "h3-web-upload")
    upload_key = "h3-web-upload-key-001"
    first = _upload_web_image(client, upload_key=upload_key)
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "uploaded"
    digest = hashlib.sha256(PNG).hexdigest()
    assert first.json()["source_sha256"] == first.json()["transport_sha256"] == digest
    lookup = client.post(
        "/api/h3-page/assets/lookup",
        json={"upload_key": upload_key},
    )
    assert lookup.status_code == 200
    assert lookup.json()["asset_id"] == first.json()["asset_id"]
    with SessionLocal() as db:
        asset = db.get(StagedAsset, first.json()["asset_id"])
        assert asset.source_channel == "h3_web"
        assert asset.asset_role == "reference_image"
        assert asset.source_sha256 == asset.transport_sha256 == digest


def test_mock_03_same_key_same_content_is_reused(client) -> None:
    create_user("h3-web-reuse", h3_access_enabled=True)
    login(client, "h3-web-reuse")
    upload_key = "h3-web-reuse-key-001"
    first = _upload_web_image(client, upload_key=upload_key)
    assert first.status_code == 201, first.text

    for _ in range(9):
        repeated = _upload_web_image(client, upload_key=upload_key)
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "reused"
        assert repeated.json()["asset_id"] == first.json()["asset_id"]
    with SessionLocal() as db:
        assert db.query(StagedAsset).filter_by(upload_key=upload_key).count() == 1


def test_mock_04_same_key_different_content_conflicts_without_overwrite(client) -> None:
    create_user("h3-web-conflict", h3_access_enabled=True)
    login(client, "h3-web-conflict")
    upload_key = "h3-web-conflict-key-001"
    first = _upload_web_image(client, upload_key=upload_key)
    assert first.status_code == 201, first.text
    original_digest = first.json()["source_sha256"]

    conflict = _upload_web_image(
        client,
        upload_key=upload_key,
        content=PNG + b"different",
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "H3_UPLOAD_KEY_CONFLICT"
    with SessionLocal() as db:
        asset = db.get(StagedAsset, first.json()["asset_id"])
        assert db.query(StagedAsset).filter_by(upload_key=upload_key).count() == 1
        assert asset.source_sha256 == asset.transport_sha256 == original_digest


def test_mock_05_upload_key_and_lookup_are_user_isolated(client) -> None:
    upload_key = "shared-looking-upload-key"
    create_user("h3-owner-a", h3_access_enabled=True)
    create_user("h3-owner-b", h3_access_enabled=True)
    login(client, "h3-owner-a")
    first = client.post(
        "/api/h3-page/assets",
        data={
            "kind": "video",
            "asset_role": "reference_video",
            "upload_key": upload_key,
            "transport_profile": "original-v1",
        },
        files={"file": ("reference.mp4", MP4, "video/mp4")},
    )
    assert first.status_code == 201, first.text
    client.get("/logout")

    login(client, "h3-owner-b")
    foreign_lookup = client.post(
        "/api/h3-page/assets/lookup", json={"upload_key": upload_key}
    )
    assert foreign_lookup.json() == {"found": False}
    second = client.post(
        "/api/h3-page/assets",
        data={
            "kind": "video",
            "asset_role": "reference_video",
            "upload_key": upload_key,
            "transport_profile": "original-v1",
        },
        files={"file": ("reference.mp4", MP4, "video/mp4")},
    )
    assert second.status_code == 201, second.text
    assert first.json()["asset_id"] != second.json()["asset_id"]


def test_mock_02_workbench_video_upload_ignores_forged_source(client) -> None:
    create_user("h3-workbench-upload", h3_access_enabled=True)
    token = _token(client, "h3-workbench-upload")
    digest = hashlib.sha256(MP4).hexdigest()
    response = client.post(
        "/api/workbench/h3-assets",
        data={
            "access_token": token,
            "kind": "video",
            "asset_role": "reference_video",
            "upload_key": "h3-workbench-upload-key-001",
            "source_sha256": digest,
            "transport_profile": "original-v1",
            "source_channel": "h3_web",
        },
        files={"file": ("参考视频.mp4", MP4, "video/mp4")},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["source_sha256"] == payload["transport_sha256"] == digest
    lookup = client.post(
        "/api/workbench/h3-assets/lookup",
        json={"access_token": token, "upload_key": "h3-workbench-upload-key-001"},
    )
    assert lookup.status_code == 200
    assert lookup.json()["asset_id"] == payload["asset_id"]
    with SessionLocal() as db:
        asset = db.get(StagedAsset, payload["asset_id"])
        assert asset.source_channel == "h3_workbench"


def test_mock_06_consumed_and_expired_assets_cannot_reach_prepare(client) -> None:
    owner = create_user("h3-lifecycle-upload", h3_access_enabled=True)
    login(client, "h3-lifecycle-upload")
    base_form = {
        "kind": "image",
        "asset_role": "reference_image",
        "transport_profile": "original-v1",
    }
    consumed = client.post(
        "/api/h3-page/assets",
        data={**base_form, "upload_key": "consumed-upload-key"},
        files={"file": ("consumed.png", PNG, "image/png")},
    ).json()
    expired = client.post(
        "/api/h3-page/assets",
        data={**base_form, "upload_key": "expired-upload-key"},
        files={"file": ("expired.png", PNG, "image/png")},
    ).json()
    with SessionLocal() as db:
        db.get(StagedAsset, consumed["asset_id"]).consumed_at = datetime.now(timezone.utc)
        db.get(StagedAsset, expired["asset_id"]).expires_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        db.commit()
    consumed_lookup = client.post(
        "/api/h3-page/assets/lookup", json={"upload_key": "consumed-upload-key"}
    )
    expired_lookup = client.post(
        "/api/h3-page/assets/lookup", json={"upload_key": "expired-upload-key"}
    )
    assert consumed_lookup.json() == {"found": False}
    assert expired_lookup.json() == {"found": False}
    with SessionLocal() as db:
        assert db.get(StagedAsset, consumed["asset_id"]) is not None
        assert db.get(StagedAsset, expired["asset_id"]) is None
        user = db.get(User, owner.id)
        with pytest.raises(StagedAssetError, match="已过期或已被使用"):
            load_available_assets(db, user, [consumed["asset_id"]])
        with pytest.raises(StagedAssetError, match="已过期或已被使用"):
            load_available_assets(db, user, [expired["asset_id"]])


def test_legacy_batch_asset_endpoints_remain_compatible(client) -> None:
    create_user("legacy-asset-user")
    login(client, "legacy-asset-user")
    page = client.post(
        "/api/batch-assets",
        data={"kind": "image"},
        files={"file": ("legacy.png", PNG, "image/png")},
    )
    assert page.status_code == 201
    token = _token(client, "legacy-asset-user")
    workbench = client.post(
        "/api/workbench/batch-assets",
        data={"access_token": token, "kind": "video"},
        files={"file": ("legacy.mp4", MP4, "video/mp4")},
    )
    assert workbench.status_code == 201
