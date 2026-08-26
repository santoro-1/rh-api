from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AudioGenerationAttempt,
    AudioGenerationTask,
    AudioTaskStatus,
    GenerationBatch,
    GenerationSegment,
    GenerationTask,
    TaskStatus,
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


def test_workbench_audio_retry_uses_requested_speed(client, monkeypatch):
    _account("workbench-speed-retry-user")
    monkeypatch.setattr(
        "app.services.speech.workbench_voices.MiniMaxClient.list_voices",
        lambda self, voice_type="system": OFFICIAL_ITEMS,
    )
    token = _token(client, "workbench-speed-retry-user")
    voices = client.post("/api/workbench/voices", json={"access_token": token})
    voice_id = voices.json()["voices"][0]["voice_asset_id"]
    created = client.post(
        "/api/workbench/audio-batches",
        json={
            "access_token": token,
            "name": "语速重试测试",
            "request_key": "workbench-speed-retry-1",
            "rows": [{"row_id": "1", "speech_script": "语速重试测试。"}],
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
    item_id = created.json()["items"][0]["item_id"]
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        output = get_settings().outputs_dir / "workbench-speed-retry.mp3"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ID3speed-retry")
        task.output_path = to_relative_data_path(output, get_settings())
        db.add(
            AudioGenerationAttempt(
                id=str(uuid.uuid4()),
                audio_task_id=task.id,
                version=task.generation_version,
                output_path=task.output_path,
                status="READY",
            )
        )
        task.status = AudioTaskStatus.AWAITING_REVIEW.value
        task.batch_item.audio_status = "AWAITING_REVIEW"
        task.batch_item.status = "AWAITING_AUDIO_REVIEW"
        db.commit()

    invalid = client.post(
        f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/retry",
        json={"access_token": token, "cost_confirmed": True, "speed": 0.1},
    )
    assert invalid.status_code == 422
    retried = client.post(
        f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/retry",
        json={"access_token": token, "cost_confirmed": True, "speed": 0.9},
    )
    assert retried.status_code == 200, retried.text
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        assert task.speed == 0.9
        assert task.status == AudioTaskStatus.PENDING.value
        assert task.generation_version == 2


def test_workbench_composition_reports_failed_approved_audio_as_audio_stage(
    client, monkeypatch
):
    _account("workbench-audio-stage-failure-user")
    monkeypatch.setattr(
        "app.services.speech.workbench_voices.MiniMaxClient.list_voices",
        lambda self, voice_type="system": OFFICIAL_ITEMS,
    )
    token = _token(client, "workbench-audio-stage-failure-user")
    voices = client.post("/api/workbench/voices", json={"access_token": token})
    voice_id = voices.json()["voices"][0]["voice_asset_id"]
    created = client.post(
        "/api/workbench/audio-batches",
        json={
            "access_token": token,
            "name": "音频失败阶段测试",
            "request_key": "workbench-audio-stage-failure-1",
            "rows": [{"row_id": "1", "speech_script": "音频失败阶段测试。"}],
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
    item_id = created.json()["items"][0]["item_id"]
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        output = get_settings().outputs_dir / "failed-approved-audio.mp3"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ID3approved-audio")
        task.output_path = to_relative_data_path(output, get_settings())
        task.reviewed_at = datetime.now(timezone.utc)
        task.status = AudioTaskStatus.FAILED.value
        task.error_code = "CONFIGURATION_ERROR"
        task.error_message = "MiniMax 账号配置缺失或已更换"
        task.batch_item.audio_status = "FAILED"
        task.batch_item.status = "AUDIO_FAILED"
        db.add(
            AudioGenerationAttempt(
                id=str(uuid.uuid4()),
                audio_task_id=task.id,
                version=task.generation_version,
                output_path=task.output_path,
                status="APPROVED",
            )
        )
        db.commit()

    manifest = client.post(
        f"/api/workbench/tasks/{item_id}", json={"access_token": token}
    )
    assert manifest.status_code == 200, manifest.text
    composition = manifest.json()["composition"]
    assert composition["status"] == "COMPOSITION_FAILED"
    assert composition["failure_stage"] == "audio"
    assert composition["error_code"] == "CONFIGURATION_ERROR"


def test_h3_approved_audio_rejects_obsolete_digital_human_handoff(
    client, monkeypatch
):
    _account("workbench-h3-only-user")
    monkeypatch.setattr(
        "app.services.speech.workbench_voices.MiniMaxClient.list_voices",
        lambda self, voice_type="system": OFFICIAL_ITEMS,
    )
    token = _token(client, "workbench-h3-only-user")
    voices = client.post("/api/workbench/voices", json={"access_token": token})
    voice_id = voices.json()["voices"][0]["voice_asset_id"]
    created = client.post(
        "/api/workbench/audio-batches",
        json={
            "access_token": token,
            "name": "多参考交接边界",
            "request_key": "workbench-h3-only-1",
            "rows": [{"row_id": "1", "speech_script": "只走多参考。"}],
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
    item_id = created.json()["items"][0]["item_id"]
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).filter_by(batch_item_id=item_id).one()
        task.status = AudioTaskStatus.SUCCESS.value
        task.reviewed_at = datetime.now(timezone.utc)
        task.primary_kind = "image"
        task.primary_path = "uploads/obsolete-person.png"
        task.batch_item.audio_status = "AUDIO_APPROVED_H3"
        task.batch_item.status = "AUDIO_APPROVED_H3"
        db.commit()

    rejected = client.post(
        f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition",
        json={
            "access_token": token,
            "idempotency_key": "obsolete-standard-handoff",
            "cost_confirmed": True,
        },
    )
    assert rejected.status_code == 409
    assert "只支持多参考" in rejected.json()["detail"]

    manifest = client.post(
        f"/api/workbench/tasks/{item_id}", json={"access_token": token}
    )
    assert manifest.status_code == 200, manifest.text
    composition = manifest.json()["composition"]
    assert composition["status"] == "COMPOSITION_FAILED"
    assert composition["error_code"] == "NEW_WORKBENCH_H3_ONLY"
    assert composition["failure_stage"] == "handoff"


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


def test_workbench_imports_existing_clone_voice_without_paid_synthesis(
    client, monkeypatch
):
    _account("workbench-import-voice-user")
    token = _token(client, "workbench-import-voice-user")
    synthesize_calls = []

    monkeypatch.setattr(
        "app.services.speech.workbench_voices.MiniMaxClient.list_voices",
        lambda self, voice_type="system": (
            [{"voice_id": "ImportedCloneVoice01", "voice_name": "抽卡克隆音色"}]
            if voice_type == "voice_cloning"
            else OFFICIAL_ITEMS
        ),
    )
    monkeypatch.setattr(
        "app.services.speech.workbench_voices.MiniMaxClient.synthesize_voice",
        lambda self, **payload: synthesize_calls.append(payload),
    )

    imported = client.post(
        "/api/workbench/voices/import",
        json={
            "access_token": token,
            "voice_id": "ImportedCloneVoice01",
            "name": "我的抽卡音色",
            "already_activated": False,
        },
    )

    assert imported.status_code == 201, imported.text
    assert imported.json()["provider_voice_id"] == "ImportedCloneVoice01"
    assert imported.json()["selectable"] is False
    assert imported.json()["activation_required"] is True
    assert synthesize_calls == []
    with SessionLocal() as db:
        voice = db.query(MiniMaxVoiceAsset).filter_by(
            voice_id="ImportedCloneVoice01"
        ).one()
        assert voice.name == "我的抽卡音色"
        assert voice.status == VoiceAssetStatus.READY.value
        assert voice.is_saved is True

    activated_import = client.post(
        "/api/workbench/voices/import",
        json={
            "access_token": token,
            "voice_id": "ImportedCloneVoice01",
            "name": "我的抽卡音色",
            "already_activated": True,
        },
    )
    assert activated_import.status_code == 200, activated_import.text
    assert activated_import.json()["activated"] is True
    assert activated_import.json()["selectable"] is True
    assert synthesize_calls == []
    with SessionLocal() as db:
        assert db.query(MiniMaxVoiceAsset).filter_by(
            voice_id="ImportedCloneVoice01"
        ).one().status == VoiceAssetStatus.ACTIVE.value

    missing = client.post(
        "/api/workbench/voices/import",
        json={
            "access_token": token,
            "voice_id": "MissingCloneVoice01",
            "name": "不存在的音色",
        },
    )
    assert missing.status_code == 400
    assert "没有这个 voice_id" in missing.json()["detail"]


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
            "resolution": "1024",
            "rows": [
                {
                    "row_id": "1",
                    "speech_script": "第一条真实语音测试脚本。",
                    "prompt": "人物自然地说话",
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
        # Simulate an audio row created before new-workbench SeedVR2 was
        # persisted in the immutable video parameter snapshot.
        video_parameters = json.loads(task.video_parameters_json)
        video_parameters.pop("seedvr2_enabled", None)
        task.video_parameters_json = json.dumps(
            video_parameters, ensure_ascii=False
        )
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
    mastered_paths = []
    monkeypatch.setattr(
        "app.routes.workbench.ensure_generated_speech_mastered",
        lambda path: mastered_paths.append(path) or True,
    )
    audio = client.get(
        f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/audio",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert mastered_paths == [output]
    assert audio.status_code == 200
    assert audio.content == b"ID3generated-audio"

    first_image = b"\x89PNG\r\n\x1a\npayload"
    first_image_sha256 = hashlib.sha256(first_image).hexdigest()
    staged = client.post(
        "/api/workbench/batch-assets",
        data={"access_token": token, "kind": "image"},
        files={"file": ("person.png", first_image, "image/png")},
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
            "image_sha256": first_image_sha256,
            "correlation_id": "workbench-correlation-001",
            "resolution": "2048",
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["composition"]["status"] == "COMPOSITION_QUEUED"
    with SessionLocal() as db:
        stored_task = db.query(AudioGenerationTask).filter_by(batch_item_id=item_id).one()
        video_parameters = json.loads(stored_task.video_parameters_json)
        assert video_parameters["resolution"] == "2048"
        assert video_parameters["seedvr2_enabled"] is True
    repeated = client.post(
        f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition",
        json={
            "access_token": token,
            "idempotency_key": "composition-project-1",
            "cost_confirmed": True,
            "image_asset_id": staged_id,
            "image_sha256": first_image_sha256,
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
        assert task.primary_sha256 == first_image_sha256
        video_parameters = json.loads(task.video_parameters_json)
        assert video_parameters["timing_mode"] == "exact_timestamps"
        assert video_parameters["generation_tail_seconds"] == 2.0
        assert video_parameters["prompt"] == "测试提示词"
        base = get_settings().outputs_dir / "workbench-base.mp4"
        base.write_bytes(b"normalized-base-video")
        task.batch_item.merged_video_path = to_relative_data_path(base, get_settings())
        task.batch_item.merged_video_status = "PREVIEW_READY"
        segment = GenerationSegment(
            id="old-image-segment",
            batch_item_id=task.batch_item_id,
            segment_index=1,
            script_text="第一条。",
            start_seconds=0.0,
            end_seconds=1.2,
            audio_path=task.output_path,
            prompt="测试提示词",
            status="TASK_CREATED",
        )
        db.add(segment)
        db.flush()
        db.add(
            GenerationTask(
                id="old-image-video-task",
                user_id=task.user_id,
                segment_id=segment.id,
                workflow_type="digital_human",
                image_path=task.primary_path,
                audio_path=task.output_path,
                image_original_name=task.primary_original_name,
                audio_original_name="workbench-audio.mp3",
                audio_duration_seconds=1.2,
                start_seconds=0.0,
                end_seconds=1.2,
                prompt="测试提示词",
                status=TaskStatus.CANCELLED.value,
                error_code="REMOTE_TASK_NOT_FOUND",
                error_message="RunningHub 任务已被手动取消",
            )
        )
        task.status = AudioTaskStatus.SUCCESS.value
        task.batch_item.status = "SEGMENTS_CREATED"
        db.commit()

    replacement_image = b"\x89PNG\r\n\x1a\nreplacement-payload"
    replacement_sha256 = hashlib.sha256(replacement_image).hexdigest()
    replacement_staged = client.post(
        "/api/workbench/batch-assets",
        data={"access_token": token, "kind": "image"},
        files={"file": ("replacement.png", replacement_image, "image/png")},
    )
    assert replacement_staged.status_code == 201, replacement_staged.text
    replaced = client.post(
        f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition",
        json={
            "access_token": token,
            "idempotency_key": "composition-project-1-new-image",
            "cost_confirmed": True,
            "image_asset_id": replacement_staged.json()["asset_id"],
            "image_sha256": replacement_sha256,
            "correlation_id": "workbench-correlation-001",
            "resolution": "1024",
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["composition"]["status"] == "COMPOSITION_QUEUED"
    assert replaced.json()["composition"]["image_sha256"] == replacement_sha256
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        assert task.primary_original_name == "replacement.png"
        assert task.primary_sha256 == replacement_sha256
        assert task.status == AudioTaskStatus.PENDING.value
        assert json.loads(task.video_parameters_json)["resolution"] == "1024"
        assert task.output_path
        assert task.subtitle_path
        assert task.batch_item.merged_video_path is None
        assert task.batch_item.merged_video_status == "MERGE_PENDING"
        assert task.batch_item.segments == []
        assert db.query(GenerationTask).count() == 0

    manifest = client.post(
        f"/api/workbench/tasks/{item_id}", json={"access_token": token}
    )
    assert manifest.status_code == 200
    assert manifest.json()["composition"]["status"] == "COMPOSITION_QUEUED"
    assert manifest.json()["composition"]["image_sha256"] == replacement_sha256
    base_video = client.get(
        f"/api/workbench/tasks/{item_id}/base-video",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert base_video.status_code == 409
