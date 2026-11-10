from __future__ import annotations

from pathlib import Path
import shutil
import sys
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import WebApiSettings, create_app  # noqa: E402


def test_h3_routes_use_existing_login_and_original_project(tmp_path: Path) -> None:
    root = tmp_path / f"h3-api-{uuid.uuid4().hex}"
    settings = WebApiSettings(
        storage_root=root / "storage",
        template_library_root=root / "templates",
        default_draft_root=root / "drafts",
        audio_library_root=root / "audio",
        admin_password="admin-pass",
        admin_session_secret="admin-secret",
        auth_authority=False,
        auth_server_url="http://127.0.0.1:8000",
        execution_mode="agent",
    )
    for directory in (
        settings.storage_root,
        settings.template_library_root,
        settings.default_draft_root,
        settings.audio_library_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    user = {"user_id": "h3-user", "username": "tester", "enabled": True}
    prepared_snapshot = {
        "batch_id": "remote-h3-batch",
        "status": "AWAITING_COST_CONFIRMATION",
        "fee_snapshot": {"segment_count": 1, "estimated_paid_calls": 1},
        "items": [
            {
                "item_id": "remote-h3-item",
                "row_id": "1",
                "status": "AWAITING_COST_CONFIRMATION",
                "segments": [],
            }
        ],
    }
    active_snapshot = {
        **prepared_snapshot,
        "status": "ACTIVE",
        "items": [
            {
                "item_id": "remote-h3-item",
                "row_id": "1",
                "status": "PENDING",
                "segments": [],
            }
        ],
    }
    upload_counter = iter(("cloud-image", "cloud-video"))

    try:
        with (
            patch("jyd_probe.auth_center.AuthCenterClient.verify", return_value=user),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.login",
                return_value={"access_token": "token-h3", "user": user},
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.list_h3_execution_accounts",
                return_value={
                    "accounts": [{"id": 7, "selectable": True}],
                    "default_selected_account_ids": [7],
                },
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.approve_h3_audio_source",
                return_value={
                    "audio_batch_id": "audio-batch",
                    "audio_item_id": "audio-item",
                    "audio_generation_version": 1,
                    "status": "SUCCESS",
                    "reviewed_at": "2026-08-23T00:00:00+00:00",
                },
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.upload_workbench_batch_asset",
                side_effect=lambda *_args, **_kwargs: {"asset_id": next(upload_counter)},
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.prepare_h3_batch",
                return_value=prepared_snapshot,
            ) as prepare_h3,
            patch(
                "jyd_probe.auth_center.AuthCenterClient.confirm_h3_batch",
                return_value=active_snapshot,
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.get_h3_batch",
                return_value=active_snapshot,
            ),
        ):
            app = create_app(settings)
            with TestClient(app) as client:
                login = client.post(
                    "/api/auth/login",
                    json={"username": "tester", "password": "pass123"},
                )
                assert login.status_code == 200, login.text
                project = client.post(
                    "/api/new/projects",
                    json={
                        "name": "H3 接口项目",
                        "items": [
                            {"row_key": "1", "script_text": "测试台词。"},
                            {"row_key": "2", "script_text": "不参与本次 H3。"},
                        ],
                    },
                ).json()
                project_id = project["project_id"]
                item_id = project["items"][0]["item_id"]
                untouched_item_id = project["items"][1]["item_id"]
                untouched_status = project["items"][1]["status"]
                store = app.state.project_store
                identity_path = root / "identity.png"
                identity_path.write_bytes(b"identity")
                image = store.register_input_image(
                    owner_user_id=user["user_id"],
                    project_id=project_id,
                    filename="identity.png",
                    content_type="image/png",
                    size_bytes=8,
                    sha256="a" * 64,
                    managed_path=str(identity_path),
                )
                store.apply_image_strategy(
                    user["user_id"], project_id, strategy="loop", reuse_count=1
                )
                audio_path = settings.storage_root / "voice.mp3"
                audio_path.write_bytes(b"audio")
                store.add_asset(
                    owner_user_id=user["user_id"],
                    project_id=project_id,
                    item_id=item_id,
                    asset_type="audio",
                    source_type="minimax",
                    status="READY",
                    filename="voice.mp3",
                    managed_path=str(audio_path),
                    external_ref={
                        "batch_id": "audio-batch",
                        "remote_item_id": "audio-item",
                        "generation_version": 1,
                    },
                    metadata={"provider_status": "AWAITING_REVIEW"},
                    make_current=True,
                )

                settings_response = client.put(
                    f"/api/new/projects/{project_id}/h3/settings",
                    json={
                        "identity_image_ids": [image["image_id"]],
                        "defaults": {
                            "continuity_mode": "fast",
                            "aspect_ratio": "9:16 (Portrait Widescreen)",
                            "megapixels": 1,
                            "generation_tail_seconds": 0.5,
                        },
                    },
                )
                assert settings_response.status_code == 200, settings_response.text
                reference = client.post(
                    f"/api/new/projects/{project_id}/items/{item_id}/h3/reference-video?filename=徐博士参考视频.mp4",
                    content=b"reference-video",
                    headers={"content-type": "video/mp4"},
                )
                assert reference.status_code == 201, reference.text
                assert reference.json()["items"][0]["inputs"]["h3_reference_video"]

                override = client.patch(
                    f"/api/new/projects/{project_id}/items/{item_id}/h3/overrides",
                    json={
                        "user_direction": "以参考视频的动态为主体。",
                        "continuity_mode": "soft_chain",
                        "megapixels": 0.8,
                    },
                )
                assert override.status_code == 200, override.text
                assert override.json()["items"][0]["settings"]["h3"]["overrides"] == {
                    "user_direction": "以参考视频的动态为主体。",
                    "continuity_mode": "soft_chain",
                    "megapixels": 0.8,
                }

                accounts = client.get("/api/new/h3/accounts")
                assert accounts.status_code == 200, accounts.text
                reviewed = client.post(
                    f"/api/new/projects/{project_id}/h3/audio-review",
                    json={"item_ids": [item_id]},
                )
                assert reviewed.status_code == 200, reviewed.text
                assert reviewed.json()["reviewed_item_ids"] == [item_id]
                prepared = client.post(
                    f"/api/new/projects/{project_id}/h3/prepare",
                    json={
                        "selected_account_ids": [7],
                        "item_ids": [item_id],
                        "idempotency_key": "h3-api-quote",
                    },
                )
                assert prepared.status_code == 201, prepared.text
                assert prepare_h3.call_args.args[1]["rows"][0]["overrides"] == {
                    "user_direction": "以参考视频的动态为主体。",
                    "continuity_mode": "soft_chain",
                    "megapixels": 0.8,
                }
                assert prepared.json()["project"]["project_id"] == project_id
                assert prepared.json()["project"]["items"][0]["item_id"] == item_id
                untouched = next(
                    item
                    for item in prepared.json()["project"]["items"]
                    if item["item_id"] == untouched_item_id
                )
                assert untouched["status"] == untouched_status
                assert untouched["settings"].get("h3", {}).get("remote_item_id") is None
                assert untouched["settings"].get("h3", {}).get("remote_status") is None

                rejected = client.post(
                    f"/api/new/projects/{project_id}/h3/confirm",
                    json={"cost_confirmed": False},
                )
                assert rejected.status_code == 409
                confirmed = client.post(
                    f"/api/new/projects/{project_id}/h3/confirm",
                    json={"cost_confirmed": True, "batch_id": "remote-h3-batch"},
                )
                assert confirmed.status_code == 200, confirmed.text
                assert confirmed.json()["project"]["items"][0]["status"] == "H3_RUNNING"
                h3_audio_path = root / "h3-authoritative.wav"
                h3_audio_path.write_bytes(b"h3-authoritative-audio")
                store.add_asset(
                    owner_user_id=user["user_id"],
                    project_id=project_id,
                    item_id=item_id,
                    asset_type="audio",
                    source_type="h3",
                    status="READY",
                    filename="h3-authoritative.wav",
                    managed_path=str(h3_audio_path),
                    make_current=True,
                )
                detail = client.get(f"/api/new/projects/{project_id}")
                assert detail.status_code == 200, detail.text
                detail_item = detail.json()["items"][0]
                assert detail_item["outputs"]["audio"]["source_type"] == "h3"
                assert detail_item["outputs"]["minimax_audio"]["source_type"] == "minimax"
                signature = "f" * 64
                h3_master_path = settings.storage_root / "h3-master-av.mp4"
                h3_master_path.write_bytes(b"h3-master-with-audio")
                h3_segment_path = settings.storage_root / "segments" / "segment-001.mp4"
                h3_segment_path.parent.mkdir(parents=True, exist_ok=True)
                h3_segment_path.write_bytes(b"h3-source-segment")
                store.add_asset(
                    owner_user_id=user["user_id"],
                    project_id=project_id,
                    item_id=item_id,
                    asset_type="h3_master_av",
                    source_type="h3",
                    status="READY",
                    filename="h3-master-av.mp4",
                    managed_path=str(h3_master_path),
                    metadata={"h3_segment_signature": signature},
                )
                silent_base_path = settings.storage_root / "h3-base-video-silent.mp4"
                silent_base_path.write_bytes(b"silent-base-video")
                store.add_asset(
                    owner_user_id=user["user_id"],
                    project_id=project_id,
                    item_id=item_id,
                    asset_type="base_video",
                    source_type="h3",
                    status="READY",
                    filename="h3-base-video-silent.mp4",
                    managed_path=str(silent_base_path),
                    metadata={
                        "h3_segment_signature": signature,
                        "source_segment_ids": ["remote-h3-segment-1"],
                    },
                    make_current=True,
                )
                audible_preview = client.get(
                    f"/api/new/projects/{project_id}/items/{item_id}/preview-video"
                )
                assert audible_preview.status_code == 200, audible_preview.text
                assert audible_preview.content == b"h3-master-with-audio"
                editable_base = client.get(
                    f"/api/new/projects/{project_id}/items/{item_id}/base-video"
                )
                assert editable_base.status_code == 200, editable_base.text
                assert editable_base.content == b"silent-base-video"
                segment_preview = client.get(
                    f"/api/new/projects/{project_id}/items/{item_id}/h3-segments/1/preview"
                )
                assert segment_preview.status_code == 200, segment_preview.text
                assert segment_preview.content == b"h3-source-segment"
                missing_segment = client.get(
                    f"/api/new/projects/{project_id}/items/{item_id}/h3-segments/2/preview"
                )
                assert missing_segment.status_code == 404
                shared_preview = client.get(
                    f"/api/new/projects/{project_id}/items/{item_id}/audio"
                )
                assert shared_preview.status_code == 200, shared_preview.text
                assert shared_preview.content == b"audio"
                locked = client.patch(
                    f"/api/new/projects/{project_id}/items/{item_id}/h3/overrides",
                    json={"megapixels": 1.2},
                )
                assert locked.status_code == 409
                assert "正在生成" in locked.json()["detail"]
                synced = client.get(f"/api/new/projects/{project_id}/h3/status")
                assert synced.status_code == 200, synced.text
    finally:
        shutil.rmtree(root, ignore_errors=True)
