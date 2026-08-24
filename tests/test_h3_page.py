from __future__ import annotations

import json

from app.database import SessionLocal
from app.models import GenerationTask
from app.services.h3.segmentation import H3TimestampedSegment
from tests.conftest import create_user, login
from tests.test_h3_workbench import (
    _enable_h3_pool,
    _fake_motion_references,
    _token,
)


SCRIPT = "真正的优势，是把复杂的事情长期做对。"


def _stage(client, kind: str, name: str, content: bytes, content_type: str) -> str:
    response = client.post(
        "/api/batch-assets",
        data={"kind": kind},
        files={"file": (name, content, content_type)},
    )
    assert response.status_code == 201, response.text
    return response.json()["assetId"]


def test_h3_is_integrated_into_batch_page_only_for_entitled_users(client) -> None:
    create_user("plain-page-user")
    login(client, "plain-page-user")
    page = client.get("/generate/batch?workflow=minimax_h3_ref2va")
    assert page.status_code == 200
    assert '<option value="minimax_h3_ref2va">' not in page.text

    client.get("/logout")
    create_user("h3-page-user", h3_access_enabled=True)
    login(client, "h3-page-user")
    page = client.get("/generate/batch?workflow=minimax_h3_ref2va")
    assert page.status_code == 200
    assert "MiniMax H3 多参考生成" in page.text
    assert 'id="h3-audio-files"' in page.text
    assert "逐条成品音频" in page.text


def test_uploaded_audio_h3_prepare_and_confirm_are_separate_steps(
    client, monkeypatch
) -> None:
    username = "h3-uploaded-audio-user"
    create_user(username, h3_access_enabled=True)
    login(client, username)
    account_id = _enable_h3_pool(username)
    video_id = _stage(
        client,
        "video",
        "reference.mp4",
        b"\x00\x00\x00\x18ftypisomh3-video",
        "video/mp4",
    )
    audio_id = _stage(client, "audio", "final.mp3", b"ID3final-audio", "audio/mpeg")

    monkeypatch.setattr(
        "app.services.h3_workbench.inspect_audio_duration", lambda path: 10.0
    )
    monkeypatch.setattr(
        "app.services.h3_workbench.split_h3_motion_reference",
        _fake_motion_references,
    )
    monkeypatch.setattr(
        "app.services.h3_workbench._plan_uploaded_h3_audio",
        lambda *args, **kwargs: (
            [
                H3TimestampedSegment(
                    index=0,
                    script_text=SCRIPT,
                    start_seconds=0.0,
                    end_seconds=10.0,
                    boundary_strength="strong",
                )
            ],
            json.dumps(
                [{"text": SCRIPT, "start_seconds": 0.0, "end_seconds": 10.0}],
                ensure_ascii=False,
            ),
        ),
    )

    prepared = client.post(
        "/api/h3-page/batches/prepare",
        json={
            "name": "上传音频 H3 批次",
            "request_key": "uploaded-h3-page-001",
            "reference_image_asset_ids": [],
            "selected_account_ids": [account_id],
            "defaults": {
                "continuity_mode": "fast",
                "generation_tail_seconds": 0.5,
                "resolution": {
                    "aspect_ratio": "9:16 (Portrait Widescreen)",
                    "megapixels": 1,
                    "multiple": 32,
                },
            },
            "rows": [
                {
                    "row_id": "H3-001",
                    "script_text": SCRIPT,
                    "video_asset_id": video_id,
                    "audio_asset_id": audio_id,
                }
            ],
        },
    )
    assert prepared.status_code == 201, prepared.text
    payload = prepared.json()
    assert payload["status"] == "AWAITING_COST_CONFIRMATION"
    assert payload["fee_snapshot"]["estimated_paid_calls"] == 1
    audio_url = payload["items"][0]["input_audio_download_url"]
    assert audio_url.endswith("/audio")
    audio_download = client.get(
        audio_url,
        headers={"Authorization": f"Bearer {_token(client, username)}"},
    )
    assert audio_download.status_code == 200
    assert audio_download.content == b"ID3final-audio"
    with SessionLocal() as db:
        assert db.query(GenerationTask).count() == 0

    confirmed = client.post(
        f"/api/h3-page/batches/{payload['batch_id']}/confirm",
        json={"cost_confirmed": True},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "ACTIVE"
    with SessionLocal() as db:
        task = db.query(GenerationTask).one()
        assert task.workflow_type == "minimax_h3_ref2va"
