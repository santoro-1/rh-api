from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    User,
    VoiceCreationStatus,
    VoiceCreationTask,
)
from app.services.security import encrypt_secret
from app.services.speech import voice_jobs
from app.services.speech.accounts import credential_fingerprint
from app.services.storage import to_relative_data_path, voice_creation_dir
from scripts import cleanup_files
from tests.conftest import create_user


class _FakeVoiceClient:
    def __init__(self) -> None:
        self.uploads = 0
        self.preview_calls = 0

    def upload_clone_audio(self, path):
        assert path.is_file()
        self.uploads += 1
        return 7000 + self.uploads

    def clone_voice_with_preview(self, file_id, voice_id, **kwargs):
        assert file_id == 7001
        assert kwargs["preview_text"] == "这是一段试听文案。"
        self.preview_calls += 1
        return voice_id, b"ID3voice-preview", "audio/mpeg"


def test_clone_preview_and_save_are_owned_by_voice_jobs(monkeypatch):
    create_user("voice-jobs-user")
    settings = get_settings()
    source = settings.data_dir / "voice-jobs-source.mp3"
    source.write_bytes(b"ID3reference")

    with SessionLocal() as db:
        user = db.query(User).filter_by(username="voice-jobs-user").one()
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret("voice-jobs-key"),
            credential_fingerprint=credential_fingerprint(
                "voice-jobs-key"
            ),
            requests_per_minute=60,
        )
        db.add(config)
        db.flush()
        task = VoiceCreationTask(
            id="voice-job-1",
            user_id=user.id,
            config_id=config.id,
            account_binding_id=config.account_binding_id,
            credential_fingerprint=config.credential_fingerprint,
            method="clone",
            name="可维护测试音色",
            preview_text="这是一段试听文案。",
            model="speech-2.8-turbo",
            noise_reduction=True,
            volume_normalization=True,
            source_a_relative_path=to_relative_data_path(source, settings),
            source_a_original_name="source.mp3",
            status=VoiceCreationStatus.CLONING.value,
            cost_confirmed_at=datetime.now(timezone.utc),
        )
        db.add(task)
        db.commit()

    fake_client = _FakeVoiceClient()
    monkeypatch.setattr(
        voice_jobs,
        "_make_client",
        lambda task: fake_client,
    )
    with SessionLocal() as db:
        voice_jobs.process_voice_task(db, "voice-job-1")
        task = db.get(VoiceCreationTask, "voice-job-1")
        assert task.status == VoiceCreationStatus.PREVIEW_READY.value
        assert task.preview_relative_path
        assert task.final_voice_id
        task.status = VoiceCreationStatus.SAVE_PENDING.value
        db.commit()

    with SessionLocal() as db:
        voice_jobs.process_voice_task(db, "voice-job-1")
        task = db.get(VoiceCreationTask, "voice-job-1")
        voice = db.query(MiniMaxVoiceAsset).one()
        assert task.status == VoiceCreationStatus.SAVED.value
        assert task.voice_asset_id == voice.id
        assert voice.voice_id == task.final_voice_id
        assert voice.is_saved is True
        assert fake_client.uploads == 1
        assert fake_client.preview_calls == 1


def test_cleanup_expires_only_stale_unsaved_voice_previews():
    create_user("voice-cleanup-user")
    settings = get_settings()
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        user = db.query(User).filter_by(username="voice-cleanup-user").one()
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret("voice-cleanup-key"),
            credential_fingerprint=credential_fingerprint(
                "voice-cleanup-key"
            ),
            requests_per_minute=60,
        )
        db.add(config)
        db.flush()
        for task_id, completed_at in (
            ("expired-preview", now - timedelta(hours=49)),
            ("recent-preview", now - timedelta(hours=47)),
        ):
            directory = voice_creation_dir(settings, user.id, task_id)
            directory.mkdir(parents=True)
            source = directory / "source.mp3"
            preview = directory / "preview.mp3"
            source.write_bytes(b"ID3reference")
            preview.write_bytes(b"ID3preview")
            db.add(
                VoiceCreationTask(
                    id=task_id,
                    user_id=user.id,
                    config_id=config.id,
                    account_binding_id=config.account_binding_id,
                    credential_fingerprint=config.credential_fingerprint,
                    method="clone",
                    name=task_id,
                    preview_text="试听文案",
                    model="speech-2.8-turbo",
                    source_a_relative_path=to_relative_data_path(
                        source, settings
                    ),
                    source_a_original_name="source.mp3",
                    preview_relative_path=to_relative_data_path(
                        preview, settings
                    ),
                    status=VoiceCreationStatus.PREVIEW_READY.value,
                    cost_confirmed_at=now,
                    completed_at=completed_at,
                )
            )
        db.commit()
        user_id = user.id

    cleanup_files.main()

    with SessionLocal() as db:
        expired = db.get(VoiceCreationTask, "expired-preview")
        recent = db.get(VoiceCreationTask, "recent-preview")
        assert expired.status == VoiceCreationStatus.EXPIRED.value
        assert expired.preview_relative_path is None
        assert expired.error_code == "PREVIEW_EXPIRED"
        assert recent.status == VoiceCreationStatus.PREVIEW_READY.value
        assert recent.preview_relative_path is not None
    assert not voice_creation_dir(
        settings, user_id, "expired-preview"
    ).exists()
    assert voice_creation_dir(
        settings, user_id, "recent-preview"
    ).exists()
