from __future__ import annotations

import json

from app.database import SessionLocal
from app.models import (
    AudioGenerationAttempt,
    AudioGenerationTask,
    GenerationSegment,
    GenerationTask,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    User,
    VoiceAssetStatus,
)
from app.services.security import encrypt_secret
from app.services.speech.accounts import credential_fingerprint
from app.workers import audio_worker
from tests.async_speech_fakes import make_async_speech_bundle
from tests.conftest import create_user, login


def _stage(client, kind: str, name: str, content: bytes, mime: str) -> str:
    response = client.post(
        "/api/batch-assets",
        data={"kind": kind},
        files={"file": (name, content, mime)},
    )
    assert response.status_code == 201, response.text
    return response.json()["assetId"]


class FakeMiniMaxClient:
    def __init__(self) -> None:
        self.uploads = 0
        self.clones: list[str] = []
        self.submissions = 0
        self.queries = 0
        self.downloads = 0
        self.submission_kwargs = {}
        self.bundle = make_async_speech_bundle(
            b"ID3generated-audio",
            [(0.0, 8.5, "欢迎使用完整的语音和视频生成流程。")],
        )

    def upload_clone_audio(self, path):
        self.uploads += 1
        return 1000 + self.uploads

    def clone_voice(self, file_id, voice_id):
        self.clones.append(voice_id)
        return voice_id

    def create_async_speech_task(self, **kwargs):
        self.submission_kwargs = kwargs
        self.submissions += 1
        return "async-task-1", "async-file-1", {}

    def query_async_speech_task(self, task_id):
        assert task_id == "async-task-1"
        self.queries += 1
        if self.queries == 1:
            return "processing", "async-file-1", {}
        return "success", "async-file-1", {}

    def download_file_content(self, file_id):
        assert file_id == "async-file-1"
        self.downloads += 1
        return self.bundle


def _write_fake_segment(source, target, **kwargs):
    del source, kwargs
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"ID3segment")


def test_audio_worker_activates_voices_and_hands_off_to_video_queue(
    client, monkeypatch
):
    create_user("audio-worker-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="audio-worker-user").one()
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret("test-minimax-key"),
            credential_fingerprint=credential_fingerprint(
                "test-minimax-key"
            ),
            base_url="https://api.minimax.io",
            requests_per_minute=60,
        )
        db.add(config)
        db.flush()
        voice = MiniMaxVoiceAsset(
            id="saved-worker-voice",
            user_id=user.id,
            config_id=config.id,
            name="保存音色",
            voice_id="provider-saved-worker-voice",
            account_binding_id=config.account_binding_id,
            credential_fingerprint=config.credential_fingerprint,
            status=VoiceAssetStatus.READY.value,
            method="clone",
            is_saved=True,
        )
        db.add(voice)
        db.commit()
    login(client, "audio-worker-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 20.0,
    )
    monkeypatch.setattr(audio_worker, "inspect_audio_duration", lambda path: 8.5)
    monkeypatch.setattr(
        audio_worker,
        "cut_audio_segment",
        _write_fake_segment,
    )

    image_id = _stage(
        client,
        "image",
        "person.png",
        b"\x89PNG\r\n\x1a\npayload",
        "image/png",
    )
    payload = {
        "name": "自动衔接批次",
        "workflowType": "digital_human",
        "audioMode": "minimax",
        "requestKey": "audio-worker-batch",
        "assetIds": [image_id],
        "batchParameters": {"person_mode": "单人", "resolution": "768"},
        "speechOptions": {
            "voiceAssetId": "saved-worker-voice",
            "pronunciationTones": (
                '["燕少飞/(yan4)(shao3)(fei1)", "omg/oh my god"]'
            ),
            "costConfirmed": True,
        },
        "rows": [
            {
                "row_id": "row-001",
                "image_file": "person.png",
                "speech_script": "欢迎使用完整的语音和视频生成流程。",
                "prompt": "人物自然地说话。",
            }
        ],
    }
    created = client.post("/api/batches", json=payload)
    assert created.status_code == 201, created.text

    fake_client = FakeMiniMaxClient()
    monkeypatch.setattr(audio_worker, "_make_client", lambda task: fake_client)
    assert audio_worker.run_once() == 1
    with SessionLocal() as db:
        submitted = db.query(AudioGenerationTask).one()
        assert submitted.status == "REMOTE_PENDING"
        assert submitted.provider_task_id == "async-task-1"
        assert db.query(GenerationTask).count() == 0
        assert audio_worker.recover_interrupted_tasks(db) == 0
    assert audio_worker.run_once() == 1
    with SessionLocal() as db:
        polling = db.query(AudioGenerationTask).one()
        assert polling.status == "REMOTE_PENDING"
        assert db.query(GenerationTask).count() == 0
    assert audio_worker.run_once() == 1

    with SessionLocal() as db:
        audio_task = db.query(AudioGenerationTask).one()
        video_task = db.query(GenerationTask).one()
        segment = db.query(GenerationSegment).one()
        voices = db.query(MiniMaxVoiceAsset).all()
        task_input = json.loads(video_task.input_payload)
        assert audio_task.status == "SUCCESS"
        assert video_task.status == "PENDING"
        assert task_input["parameters"]["end_time"] == "0:09"
        assert task_input["parameters"]["person_mode"] == "1"
        assert segment.generation_task.id == video_task.id
        assert segment.script_text.startswith("欢迎使用")
        assert all(voice.status == "ACTIVE" for voice in voices)
        assert all(voice.expires_at is not None for voice in voices)
        assert fake_client.uploads == 0
        assert fake_client.submissions == 1
        assert fake_client.submission_kwargs["pronunciation_tones"] == [
            "燕少飞/(yan4)(shao3)(fei1)",
            "omg/oh my god",
        ]
        assert fake_client.queries == 2
        assert fake_client.downloads == 1


def test_optional_audio_review_can_regenerate_then_approve(
    client, monkeypatch
):
    create_user("audio-review-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="audio-review-user").one()
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret("review-minimax-key"),
            credential_fingerprint=credential_fingerprint(
                "review-minimax-key"
            ),
            base_url="https://api.minimax.io",
            requests_per_minute=60,
        )
        db.add(config)
        db.flush()
        db.add(
            MiniMaxVoiceAsset(
                id="saved-review-voice",
                user_id=user.id,
                config_id=config.id,
                name="审核测试音色",
                voice_id="provider-saved-review-voice",
                account_binding_id=config.account_binding_id,
                credential_fingerprint=config.credential_fingerprint,
                status=VoiceAssetStatus.READY.value,
                method="clone",
                is_saved=True,
            )
        )
        db.commit()
    login(client, "audio-review-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 20.0,
    )
    monkeypatch.setattr(audio_worker, "inspect_audio_duration", lambda path: 8.5)
    monkeypatch.setattr(audio_worker, "cut_audio_segment", _write_fake_segment)
    image_id = _stage(
        client,
        "image",
        "review-person.png",
        b"\x89PNG\r\n\x1a\npayload",
        "image/png",
    )
    created = client.post(
        "/api/batches",
        json={
            "name": "语音审核批次",
            "workflowType": "digital_human",
            "audioMode": "minimax",
            "requestKey": "audio-review-batch",
            "assetIds": [image_id],
            "batchParameters": {
                "person_mode": "单人",
                "resolution": "768",
            },
            "speechOptions": {
                "voiceAssetId": "saved-review-voice",
                "reviewRequired": True,
                "costConfirmed": True,
            },
            "rows": [
                {
                    "row_id": "review-001",
                    "speech_script": "这是需要试听审核的完整口播脚本。",
                    "prompt": "人物自然地说话。",
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    batch_id = created.json()["batchId"]

    fake_client = FakeMiniMaxClient()
    monkeypatch.setattr(audio_worker, "_make_client", lambda task: fake_client)
    assert audio_worker.run_once() == 1
    assert audio_worker.run_once() == 1
    assert audio_worker.run_once() == 1

    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        attempt = db.query(AudioGenerationAttempt).one()
        item_id = task.batch_item_id
        first_attempt_id = attempt.id
        first_output_path = attempt.output_path
        assert task.status == "AWAITING_REVIEW"
        assert task.batch_item.batch.review_required is True
        assert attempt.status == "READY"
        assert db.query(GenerationTask).count() == 0

    status = client.get(f"/api/batches/{batch_id}")
    assert status.status_code == 200
    assert status.json()["awaitingReview"] == 1
    page = client.get(f"/batches/{batch_id}")
    assert "重新生成（再次计费）" in page.text
    assert client.get(
        f"/batches/{batch_id}/items/{item_id}/audio"
    ).status_code == 200

    regenerated = client.post(
        f"/batches/{batch_id}/items/{item_id}/regenerate-audio",
        follow_redirects=False,
    )
    assert regenerated.status_code == 303
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        first_attempt = db.query(AudioGenerationAttempt).one()
        assert task.status == "PENDING"
        assert task.generation_version == 2
        assert task.provider_task_id is None
        assert first_attempt.status == "REJECTED"
        assert first_attempt.output_path == first_output_path

    assert client.get(
        f"/batches/{batch_id}/items/{item_id}/audio-attempts/"
        f"{first_attempt_id}"
    ).status_code == 200
    assert audio_worker.run_once() == 1
    assert audio_worker.run_once() == 1

    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        attempts = db.query(AudioGenerationAttempt).order_by(
            AudioGenerationAttempt.version
        ).all()
        assert task.status == "AWAITING_REVIEW"
        assert [attempt.status for attempt in attempts] == [
            "REJECTED",
            "READY",
        ]
        assert attempts[0].output_path != attempts[1].output_path

    approved = client.post(
        f"/batches/{batch_id}/items/{item_id}/approve-audio",
        follow_redirects=False,
    )
    assert approved.status_code == 303
    assert audio_worker.run_once() == 1

    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        attempts = db.query(AudioGenerationAttempt).order_by(
            AudioGenerationAttempt.version
        ).all()
        assert task.status == "SUCCESS"
        assert task.reviewed_at is not None
        assert attempts[1].status == "APPROVED"
        assert db.query(GenerationTask).count() == 1
