from __future__ import annotations

import hashlib
from dataclasses import replace

from app.config import get_settings
from app.database import SessionLocal
from app.models import GenerationTask
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
    remote_settings = replace(get_settings(), media_processing_mode="remote")
    monkeypatch.setattr("app.routes.h3_page.get_settings", lambda: remote_settings)
    monkeypatch.setattr(
        "app.services.h3_workbench.get_alignment_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("带远程切点的 H3 prepare 不应由云端直连 ASR")
        ),
    )
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
    split_at = len(SCRIPT) // 2
    alignment = {
        "schema": "jyd.h3-safe-cut-alignment.v1",
        "source": "remote_media_node_funasr",
        "script_sha256": hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest(),
        "audio_sha256": hashlib.sha256(b"ID3final-audio").hexdigest(),
        "audio_batch_id": "uploaded-audio",
        "audio_item_id": audio_id,
        "audio_generation_version": 0,
        "ranges": [
            {
                "script_start": 0,
                "script_end": split_at,
                "start_us": 0,
                "end_us": 5_000_000,
            },
            {
                "script_start": split_at,
                "script_end": len(SCRIPT),
                "start_us": 5_000_000,
                "end_us": 10_000_000,
            },
        ],
    }

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
                    "audio_alignment": alignment,
                }
            ],
        },
    )
    assert prepared.status_code == 201, prepared.text
    payload = prepared.json()
    assert payload["status"] == "AWAITING_COST_CONFIRMATION"
    assert payload["fee_snapshot"]["estimated_paid_calls"] == 1
    draft_history = client.get("/batches")
    assert draft_history.status_code == 200
    assert "上传音频 H3 批次" not in draft_history.text
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
    history = client.get("/batches")
    assert history.status_code == 200
    assert "上传音频 H3 批次" in history.text
    assert "MiniMax H3 多参考生成" in history.text
    assert f'href="/batches/{payload["batch_id"]}"' in history.text
    detail = client.get(f'/batches/{payload["batch_id"]}')
    assert detail.status_code == 200
    assert "MiniMax H3 多参考生成" in detail.text
    with SessionLocal() as db:
        task = db.query(GenerationTask).one()
        assert task.workflow_type == "minimax_h3_ref2va"


def test_h3_page_audio_alignment_is_claimed_by_remote_media_node(
    client, monkeypatch
) -> None:
    username = "h3-remote-alignment-user"
    create_user(username, h3_access_enabled=True)
    login(client, username)
    audio_id = _stage(client, "audio", "final.mp3", b"ID3remote-audio", "audio/mpeg")
    settings = replace(
        get_settings(),
        media_processing_mode="remote",
        media_worker_token="remote-worker-test-token-0123456789abcdef",
        media_worker_lease_seconds=1800,
    )
    monkeypatch.setattr("app.routes.h3_page.get_settings", lambda: settings)
    monkeypatch.setattr("app.routes.media_worker_api.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.services.h3_workbench.get_alignment_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("远程模式下云端不应直接调用 ASR")
        ),
    )

    created = client.post(
        "/api/h3-page/audio-alignments",
        json={"audio_asset_id": audio_id, "script_text": SCRIPT},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "PENDING"
    job_id = created.json()["job_id"]

    claimed = client.post(
        "/api/media-worker/v1/jobs/claim",
        headers={
            "Authorization": "Bearer remote-worker-test-token-0123456789abcdef"
        },
        json={
            "workerId": "asr-node",
            "capabilities": ["h3_audio_alignment"],
        },
    )
    assert claimed.status_code == 200, claimed.text
    job = claimed.json()
    assert job["jobId"] == job_id
    assert job["action"] == "h3_audio_alignment"
    source = client.get(
        job["source"]["audioUrl"],
        headers={
            "Authorization": "Bearer remote-worker-test-token-0123456789abcdef"
        },
    )
    assert source.status_code == 200
    assert source.content == b"ID3remote-audio"

    split_at = len(SCRIPT) // 2
    completed = client.post(
        f"/api/media-worker/v1/h3-asr-jobs/{job_id}/complete",
        headers={
            "Authorization": "Bearer remote-worker-test-token-0123456789abcdef"
        },
        json={
            "leaseId": job["leaseId"],
            "alignment": {
                "schema": "runninghub.h3-audio-alignment-result.v1",
                "provider": "funasr_http",
                "matchRatio": 0.99,
                "tokens": [
                    {
                        "text": SCRIPT[:split_at],
                        "scriptStart": 0,
                        "scriptEnd": split_at,
                        "startSeconds": 0,
                        "endSeconds": 5,
                    },
                    {
                        "text": SCRIPT[split_at:],
                        "scriptStart": split_at,
                        "scriptEnd": len(SCRIPT),
                        "startSeconds": 5,
                        "endSeconds": 10,
                    },
                ],
            },
        },
    )
    assert completed.status_code == 200, completed.text
    result = client.get(f"/api/h3-page/audio-alignments/{job_id}")
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["status"] == "SUCCESS"
    assert payload["alignment"]["audio_item_id"] == audio_id
    assert payload["alignment"]["source"] == "remote_media_node_funasr"
