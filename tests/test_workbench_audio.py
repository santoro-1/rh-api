from __future__ import annotations

import json
import uuid

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AudioGenerationAttempt,
    AudioGenerationTask,
    AudioTaskStatus,
    GenerationBatch,
    GenerationTask,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    User,
    VoiceAssetStatus,
)
from app.services.security import encrypt_secret
from app.services.speech.accounts import credential_fingerprint
from app.services.storage import to_relative_data_path
from tests.conftest import create_user


OFFICIAL_ITEMS = [
    {
        "voice_id": "Chinese (Mandarin)_Reliable_Executive",
        "voice_name": "Reliable Executive",
        "description": ["Chinese Mandarin"],
    },
    {
        "voice_id": "Chinese (Mandarin)_Warm_Girl",
        "voice_name": "Warm Girl",
        "description": ["Chinese Mandarin"],
    },
    {
        "voice_id": "Chinese (Mandarin)_Unrestrained_Young_Man",
        "voice_name": "Unrestrained Young Man",
        "description": ["Chinese Mandarin"],
    },
]


def _account(username: str) -> None:
    create_user(username)
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        secret = "workbench-minimax-key"
        db.add(
            MiniMaxConfig(
                user=user,
                api_key_encrypted=encrypt_secret(secret),
                credential_fingerprint=credential_fingerprint(secret),
                base_url="https://api.minimax.io",
                requests_per_minute=20,
            )
        )
        db.commit()


def _token(client, username: str) -> str:
    response = client.post(
        "/api/auth/center/login",
        json={"username": username, "password": "password123"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def test_workbench_audio_routes_reject_legacy_batches(client):
    _account("workbench-source-boundary-user")
    token = _token(client, "workbench-source-boundary-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="workbench-source-boundary-user").one()
        db.add(
            GenerationBatch(
                id="legacy-audio-route-batch",
                user_id=user.id,
                name="旧网页声音批次",
                workflow_type="digital_human",
                audio_mode="minimax",
                request_key="legacy-audio-route-batch",
                status="ACTIVE",
                total_items=0,
            )
        )
        db.commit()

    response = client.post(
        "/api/workbench/audio-batches/legacy-audio-route-batch",
        json={"access_token": token},
    )
    assert response.status_code == 404


def test_workbench_bootstraps_three_real_system_voices(client, monkeypatch):
    _account("workbench-voice-user")
    monkeypatch.setattr(
        "app.services.speech.workbench_voices.MiniMaxClient.list_voices",
        lambda self, voice_type="system": OFFICIAL_ITEMS,
    )
    token = _token(client, "workbench-voice-user")

    response = client.post("/api/workbench/voices", json={"access_token": token})

    assert response.status_code == 200, response.text
    voices = response.json()["voices"]
    assert {voice["provider_voice_id"] for voice in voices} == {
        item["voice_id"] for item in OFFICIAL_ITEMS
    }
    assert all(voice["source"] == "official" for voice in voices)
    with SessionLocal() as db:
        assert db.query(MiniMaxVoiceAsset).count() == 3


def test_official_voice_preview_requires_confirmation_and_is_cached(client, monkeypatch):
    _account("workbench-preview-user")
    monkeypatch.setattr(
        "app.services.speech.workbench_voices.MiniMaxClient.list_voices",
        lambda self, voice_type="system": OFFICIAL_ITEMS,
    )
    calls = []

    def fake_synthesize(self, **payload):
        calls.append(payload)
        return b"ID3official-preview", {"trace_id": "preview-1"}

    monkeypatch.setattr(
        "app.services.speech.workbench_voices.MiniMaxClient.synthesize_voice",
        fake_synthesize,
    )
    token = _token(client, "workbench-preview-user")
    voices = client.post("/api/workbench/voices", json={"access_token": token})
    voice_id = voices.json()["voices"][0]["voice_asset_id"]

    denied = client.post(
        f"/api/workbench/voices/{voice_id}/preview",
        json={
            "access_token": token,
            "preview_text": "官方声音试听。",
            "cost_confirmed": False,
        },
    )
    assert denied.status_code == 409
    assert calls == []

    created = client.post(
        f"/api/workbench/voices/{voice_id}/preview",
        json={
            "access_token": token,
            "preview_text": "官方声音试听。",
            "cost_confirmed": True,
        },
    )
    assert created.status_code == 200, created.text
    assert len(calls) == 1
    downloaded = client.get(
        f"/api/workbench/voices/{voice_id}/preview",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"ID3official-preview"

    cached = client.post(
        f"/api/workbench/voices/{voice_id}/preview",
        json={
            "access_token": token,
            "preview_text": "不会再次调用。",
            "cost_confirmed": True,
        },
    )
    assert cached.status_code == 200
    assert len(calls) == 1


def test_workbench_voice_clone_reuses_existing_voice_queue(client, monkeypatch):
    _account("workbench-clone-user")
    token = _token(client, "workbench-clone-user")
    monkeypatch.setattr(
        "app.services.speech.voice_studio.inspect_audio_duration",
        lambda path: 18.0,
    )

    response = client.post(
        "/api/workbench/voice-creations",
        data={
            "access_token": token,
            "method": "clone",
            "name": "工作台克隆音色",
            "preview_text": "这是一段声音克隆试听。",
            "model": "speech-2.8-turbo",
            "cost_confirmed": "true",
        },
        files={"source_a": ("sample.mp3", b"ID3voice-sample", "audio/mpeg")},
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "PENDING"


def test_saved_custom_voice_requires_explicit_activation_and_can_be_deleted(
    client, monkeypatch
):
    _account("workbench-activate-user")
    token = _token(client, "workbench-activate-user")
    voice_id = str(uuid.uuid4())
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="workbench-activate-user").one()
        config = user.minimax_config
        db.add(
            MiniMaxVoiceAsset(
                id=voice_id,
                user_id=user.id,
                config_id=config.id,
                name="待激活克隆音色",
                voice_id="WorkbenchActivateVoice01",
                account_binding_id=config.account_binding_id,
                credential_fingerprint=config.credential_fingerprint,
                status=VoiceAssetStatus.READY.value,
                method="clone",
                is_saved=True,
            )
        )
        db.commit()

    calls = []

    def fake_synthesize(self, **payload):
        calls.append(payload)
        return b"ID3activation-preview", {"trace_id": "activation-1"}

    monkeypatch.setattr(
        "app.services.speech.workbench_voices.MiniMaxClient.synthesize_voice",
        fake_synthesize,
    )
    denied = client.post(
        f"/api/workbench/voices/{voice_id}/activate",
        json={"access_token": token, "cost_confirmed": False},
    )
    assert denied.status_code == 409
    assert calls == []

    activated = client.post(
        f"/api/workbench/voices/{voice_id}/activate",
        json={"access_token": token, "cost_confirmed": True},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["activated"] is True
    assert activated.json()["selectable"] is True
    assert calls[0]["voice_id"] == "WorkbenchActivateVoice01"

    repeated = client.post(
        f"/api/workbench/voices/{voice_id}/activate",
        json={"access_token": token, "cost_confirmed": True},
    )
    assert repeated.status_code == 200
    assert len(calls) == 1

    deleted = client.post(
        f"/api/workbench/voices/{voice_id}/delete",
        json={"access_token": token},
    )
    assert deleted.status_code == 200, deleted.text
    with SessionLocal() as db:
        assert db.get(MiniMaxVoiceAsset, voice_id).is_saved is False


def test_workbench_audio_batch_stops_at_review_and_exposes_audio(client, monkeypatch):
    _account("workbench-audio-user")
    monkeypatch.setattr(
        "app.services.speech.workbench_voices.MiniMaxClient.list_voices",
        lambda self, voice_type="system": OFFICIAL_ITEMS,
    )
    token = _token(client, "workbench-audio-user")
    voice_response = client.post(
        "/api/workbench/voices", json={"access_token": token}
    )
    voice_id = voice_response.json()["voices"][0]["voice_asset_id"]
    created = client.post(
        "/api/workbench/audio-batches",
        json={
            "access_token": token,
            "name": "WB-20260804 声音批次",
            "request_key": "wb-audio-project-1",
            "correlation_id": "workbench-correlation-001",
            "rows": [
                {
                    "row_id": "1",
                    "speech_script": "第一条真实语音测试脚本。",
                    "prompt": "人物自然说话",
                }
            ],
            "speech_options": {
                "voiceAssetId": voice_id,
                "model": "speech-2.8-hd",
                "speed": 1,
                "volume": 1,
                "pitch": 0,
                "languageBoost": "Chinese",
                "outputFormat": "mp3",
                "costConfirmed": True,
            },
        },
    )
    assert created.status_code == 201, created.text
    batch_id = created.json()["batch_id"]
    assert created.json()["correlation_id"] == "workbench-correlation-001"
    assert created.json()["source_channel"] == "new_workbench"
    item_id = created.json()["items"][0]["item_id"]
    with SessionLocal() as db:
        batch = db.get(GenerationBatch, batch_id)
        task = db.query(AudioGenerationTask).one()
        assert batch.review_required is True
        assert batch.source_channel == "new_workbench"
        assert batch.correlation_id == "workbench-correlation-001"
        assert db.query(GenerationTask).count() == 0
        assert task.primary_kind is None
        assert task.primary_path is None
        assert task.primary_original_name is None
        output = get_settings().outputs_dir / "workbench-audio.mp3"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ID3generated-audio")
        subtitles = get_settings().outputs_dir / "workbench-audio.json"
        subtitles.write_text(
            json.dumps(
                [{"text": "第一条。", "start_seconds": 0, "end_seconds": 1.2}],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        task.output_path = to_relative_data_path(output, get_settings())
        task.subtitle_path = to_relative_data_path(subtitles, get_settings())
        db.add(
            AudioGenerationAttempt(
                id=str(uuid.uuid4()),
                audio_task_id=task.id,
                version=task.generation_version,
                output_path=task.output_path,
                subtitle_path=task.subtitle_path,
                status="READY",
            )
        )
        task.status = AudioTaskStatus.AWAITING_REVIEW.value
        task.batch_item.audio_status = "AWAITING_REVIEW"
        task.batch_item.status = "AWAITING_AUDIO_REVIEW"
        db.commit()

    status = client.post(
        f"/api/workbench/audio-batches/{batch_id}",
        json={"access_token": token},
    )
    assert status.status_code == 200
    row = status.json()["items"][0]
    assert row["status"] == "AWAITING_REVIEW"
    assert row["audio_ready"] is True
    assert row["captions"]["source"] == "minimax_timestamps"
    audio = client.get(
        f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/audio",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert audio.status_code == 200
    assert audio.content == b"ID3generated-audio"

    staged = client.post(
        "/api/workbench/batch-assets",
        data={"access_token": token, "kind": "image"},
        files={"file": ("person.png", b"\x89PNG\r\n\x1a\npayload", "image/png")},
    )
    assert staged.status_code == 201, staged.text
    staged_id = staged.json()["asset_id"]

    started = client.post(
        f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition",
        json={
            "access_token": token,
            "idempotency_key": "composition-project-1",
            "cost_confirmed": True,
            "image_asset_id": staged_id,
            "correlation_id": "workbench-correlation-001",
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["composition"]["status"] == "COMPOSITION_QUEUED"
    repeated = client.post(
        f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition",
        json={
            "access_token": token,
            "idempotency_key": "composition-project-1",
            "cost_confirmed": True,
            "image_asset_id": staged_id,
            "correlation_id": "workbench-correlation-001",
        },
    )
    assert repeated.status_code == 200, repeated.text
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        assert task.status == AudioTaskStatus.PENDING.value
        assert task.reviewed_at is not None
        assert task.primary_kind == "image"
        assert task.primary_path
        assert task.primary_original_name == "person.png"
        assert json.loads(task.video_parameters_json)["timing_mode"] == "exact_timestamps"
        base = get_settings().outputs_dir / "workbench-base.mp4"
        base.write_bytes(b"normalized-base-video")
        task.batch_item.merged_video_path = to_relative_data_path(base, get_settings())
        task.batch_item.merged_video_status = "PREVIEW_READY"
        db.commit()

    manifest = client.post(
        f"/api/workbench/tasks/{item_id}", json={"access_token": token}
    )
    assert manifest.status_code == 200
    assert manifest.json()["composition"]["status"] == "BASE_VIDEO_READY"
    base_video = client.get(
        f"/api/workbench/tasks/{item_id}/base-video",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert base_video.status_code == 200
    assert base_video.content == b"normalized-base-video"
