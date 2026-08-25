from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import uuid
import zipfile

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    BATCH_SOURCE_LTX_WORKBENCH,
    AudioGenerationAttempt,
    AudioGenerationTask,
    AudioTaskStatus,
    GenerationBatch,
    GenerationTask,
    LongAudioProject,
    LtxPreparationJob,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    TaskStatus,
    User,
    VoiceAssetStatus,
)
from app.services.ltx_workbench import compile_ltx_prompt
from app.services.security import encrypt_secret
from app.services.speech.accounts import credential_fingerprint
from app.services.storage import to_relative_data_path
from app.services.workflow_configs import save_workflow_config
from tests.conftest import create_user
from app.workers import task_worker


WORKER_TOKEN = "ltx-workbench-remote-worker-token"
SCRIPT = "方法永远比困难多。"


def _enable_ltx(username: str) -> None:
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        config = save_workflow_config(
            user,
            "ltx_lip_sync",
            ai_app_id="ltx-workbench-test",
            instance_type="plus",
            default_prompt="不会用于独立工作台",
            is_enabled=True,
        )
        db.add(config)
        db.commit()


def _token(client, username: str) -> str:
    response = client.post(
        "/api/auth/center/login",
        json={"username": username, "password": "password123"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _stage(client, token: str, kind: str, name: str, content_type: str) -> str:
    suffix = Path(name).suffix.lower()
    content = {
        ".mp4": b"\x00\x00\x00\x18ftypisomvideo-payload",
        ".mov": b"\x00\x00\x00\x18ftypqt  video-payload",
        ".mp3": b"ID3audio-payload",
        ".wav": b"RIFF\x10\x00\x00\x00WAVEaudio-payload",
    }[suffix]
    response = client.post(
        "/api/workbench/batch-assets",
        data={"access_token": token, "kind": kind},
        files={"file": (name, content, content_type)},
    )
    assert response.status_code == 201, response.text
    return response.json()["asset_id"]


def _configure_minimax(username: str) -> str:
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        secret = f"ltx-minimax-{username}"
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret(secret),
            credential_fingerprint=credential_fingerprint(secret),
            base_url="https://api.minimax.io",
            requests_per_minute=20,
        )
        db.add(config)
        db.flush()
        voice = MiniMaxVoiceAsset(
            id=f"voice-{username}",
            user_id=user.id,
            config_id=config.id,
            name="LTX 测试音色",
            voice_id=f"provider-{username}",
            account_binding_id=config.account_binding_id,
            credential_fingerprint=config.credential_fingerprint,
            status=VoiceAssetStatus.ACTIVE.value,
            method="clone",
            is_saved=True,
        )
        db.add(voice)
        db.commit()
        return voice.id


def _finished_audio(client, token: str, username: str) -> tuple[str, str]:
    voice_id = _configure_minimax(username)
    created = client.post(
        "/api/workbench/audio-batches",
        json={
            "access_token": token,
            "name": "LTX MiniMax 声音",
            "request_key": f"ltx-audio-{username}",
            "correlation_id": f"ltx-{username}",
            "rows": [{"row_id": "ROW-001", "speech_script": SCRIPT}],
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
        output = get_settings().outputs_dir / f"{username}-ltx-audio.mp3"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ID3generated-audio")
        subtitles = get_settings().outputs_dir / f"{username}-ltx-cues.json"
        subtitles.write_text(
            json.dumps(
                [{"text": SCRIPT, "start_seconds": 0, "end_seconds": 20}],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        task.output_path = to_relative_data_path(output, get_settings())
        task.subtitle_path = to_relative_data_path(subtitles, get_settings())
        task.status = AudioTaskStatus.AWAITING_REVIEW.value
        task.batch_item.audio_status = "AWAITING_REVIEW"
        task.batch_item.status = "AWAITING_AUDIO_REVIEW"
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
        db.commit()
    return batch_id, item_id


def _row(video_asset_id: str, audio_batch_id: str, audio_item_id: str) -> dict[str, object]:
    return {
        "row_id": "ROW-001",
        "script_text": SCRIPT,
        "video_asset_id": video_asset_id,
        "audio_batch_id": audio_batch_id,
        "audio_item_id": audio_item_id,
        "audio_generation_version": 1,
    }


def _create_batch(client, monkeypatch, username: str = "ltx-workbench-user"):
    create_user(username)
    _enable_ltx(username)
    token = _token(client, username)
    video_id = _stage(client, token, "video", "source.mov", "video/quicktime")
    audio_batch_id, audio_item_id = _finished_audio(client, token, username)
    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_audio_duration", lambda path: 20.0
    )
    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_media_duration", lambda path: 22.0
    )
    response = client.post(
        "/api/workbench/ltx-batches",
        json={
            "access_token": token,
            "name": "独立对口型批次",
            "request_key": "ltx-request-001",
            "cost_confirmed": True,
            "rows": [_row(video_id, audio_batch_id, audio_item_id)],
        },
    )
    assert response.status_code == 201, response.text
    return token, response.json()


def _single_segment_archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("audio/segment-001.mp3", b"ID3segment")
        bundle.writestr(
            "video/segment-001.mp4", b"\x00\x00\x00\x18ftypisomsegment"
        )
    return output.getvalue()


def test_fixed_prompt_is_compiled_only_from_original_script() -> None:
    assert compile_ltx_prompt(SCRIPT) == f"一个人用中文说：“{SCRIPT}”"


def test_ltx_workbench_validates_full_row_duration_and_creates_idempotently(
    client, monkeypatch
) -> None:
    create_user("ltx-row-user")
    _enable_ltx("ltx-row-user")
    token = _token(client, "ltx-row-user")
    video_id = _stage(client, token, "video", "source.mp4", "video/mp4")
    audio_batch_id, audio_item_id = _finished_audio(
        client, token, "ltx-row-user"
    )
    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_audio_duration", lambda path: 20.0
    )
    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_media_duration", lambda path: 19.0
    )
    rejected = client.post(
        "/api/workbench/ltx-batches/validate",
        json={
            "access_token": token,
            "rows": [_row(video_id, audio_batch_id, audio_item_id)],
        },
    )
    assert rejected.status_code == 400
    assert "源视频时长不足" in rejected.json()["detail"]

    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_media_duration", lambda path: 20.0
    )
    custom_prompt = _row(video_id, audio_batch_id, audio_item_id)
    custom_prompt["prompt"] = "允许用户改提示词"
    prompt_rejected = client.post(
        "/api/workbench/ltx-batches/validate",
        json={"access_token": token, "rows": [custom_prompt]},
    )
    assert prompt_rejected.status_code == 400
    assert "不能自定义" in prompt_rejected.json()["detail"]

    payload = {
        "access_token": token,
        "name": "完整行时长测试",
        "request_key": "ltx-idempotent-row",
        "cost_confirmed": True,
        "rows": [_row(video_id, audio_batch_id, audio_item_id)],
    }
    created = client.post("/api/workbench/ltx-batches", json=payload)
    assert created.status_code == 201, created.text
    repeated = client.post("/api/workbench/ltx-batches", json=payload)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["batch_id"] == created.json()["batch_id"]
    assert created.json()["source_channel"] == BATCH_SOURCE_LTX_WORKBENCH
    assert "prompt" not in created.text.lower()

    with SessionLocal() as db:
        batch = db.get(GenerationBatch, created.json()["batch_id"])
        preparation = db.query(LtxPreparationJob).one()
        source_audio = db.query(AudioGenerationTask).one()
        assert batch is not None
        assert preparation.duration_seconds == 20.0
        assert preparation.video_duration_seconds == 20.0
        assert preparation.script_text == SCRIPT
        assert preparation.alignment_provider == "minimax_sentence_timestamp"
        assert source_audio.status == AudioTaskStatus.SUCCESS.value
        assert source_audio.batch_item.segments == []
        assert source_audio.batch_item.generation_task is None
        assert max(
            segment["endSeconds"] - segment["startSeconds"]
            for segment in json.loads(preparation.segment_plan_json)
        ) <= 20


def test_ltx_reuses_the_same_minimax_version_after_h3_review(
    client, monkeypatch
) -> None:
    create_user("ltx-after-h3-user")
    _enable_ltx("ltx-after-h3-user")
    token = _token(client, "ltx-after-h3-user")
    video_id = _stage(client, token, "video", "source.mp4", "video/mp4")
    audio_batch_id, audio_item_id = _finished_audio(
        client, token, "ltx-after-h3-user"
    )
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).filter_by(
            batch_item_id=audio_item_id
        ).one()
        task.status = AudioTaskStatus.SUCCESS.value
        task.reviewed_at = datetime.now(timezone.utc)
        task.batch_item.audio_status = "AUDIO_APPROVED_H3"
        task.batch_item.status = "AUDIO_APPROVED_H3"
        task.attempts[-1].status = "APPROVED"
        db.commit()
    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_audio_duration", lambda path: 20.0
    )
    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_media_duration", lambda path: 22.0
    )

    validated = client.post(
        "/api/workbench/ltx-batches/validate",
        json={
            "access_token": token,
            "rows": [_row(video_id, audio_batch_id, audio_item_id)],
        },
    )

    assert validated.status_code == 200, validated.text


def test_minimax_timestamp_ltx_skips_asr_and_preserves_video_timeline(
    client, monkeypatch
) -> None:
    token, created = _create_batch(client, monkeypatch)
    settings = replace(
        get_settings(),
        media_processing_mode="remote",
        media_worker_token=WORKER_TOKEN,
        media_worker_lease_seconds=1800,
    )
    monkeypatch.setattr("app.routes.media_worker_api.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.services.long_audio.inspect_audio_duration", lambda path: 20.0
    )
    monkeypatch.setattr(
        "app.services.long_audio.inspect_media_duration", lambda path: 22.0
    )
    claim = client.post(
        "/api/media-worker/v1/jobs/claim",
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        json={"workerId": "ltx-test-worker", "capabilities": ["analysis", "cut"]},
    )
    assert claim.status_code == 200, claim.text
    job = claim.json()
    assert job["action"] == "cut"
    assert len(job["segments"]) == 1
    completed = client.post(
        f"/api/media-worker/v1/jobs/{job['jobId']}/cut",
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        data={"leaseId": job["leaseId"]},
        files={
            "archive": (
                "segments.zip",
                _single_segment_archive(),
                "application/zip",
            )
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "COMPLETED"

    item_id = created["items"][0]["item_id"]
    status = client.post(
        f"/api/workbench/ltx-items/{item_id}",
        json={"access_token": token},
    )
    assert status.status_code == 200, status.text
    item = status.json()
    assert item["preparation"]["aligned_script"]["schema"] == (
        "ltx.aligned-script.v1"
    )
    assert item["segments"][0]["script_text"] == SCRIPT

    with SessionLocal() as db:
        project = db.get(LongAudioProject, job["jobId"])
        task = db.query(GenerationTask).one()
        assert project is not None
        assert task.status == TaskStatus.PENDING.value
        assert task.seedvr2_enabled is True
        assert task.prompt == f"一个人用中文说：“{SCRIPT}”"
        assert Path(task.image_path).suffix == ".mp4"
        assert Path(task.audio_path).suffix == ".mp3"
        assert "prompt_prefix" not in item["preparation"]["aligned_script"]

        task.status = TaskStatus.RUNNING.value
        task.runninghub_task_id = "oom-ltx-task"
        db.commit()

    class OomRunningHub:
        def query_task(self, task_id):
            assert task_id == "oom-ltx-task"
            return {
                "taskId": task_id,
                "status": "FAILED",
                "errorCode": "805",
                "errorMessage": "工作流运行失败",
                "failedReason": {
                    "exception_type": "CRASH",
                    "exception_message": "Task crashed (OOM_KILLED).",
                    "current_inputs": "[]",
                    "traceback": "[]",
                },
                "usage": {"taskCostTime": "0"},
            }

    monkeypatch.setattr(task_worker, "_make_client", lambda config: OomRunningHub())
    with SessionLocal() as db:
        task_worker.process_task(db, db.query(GenerationTask).one().id)
        task = db.query(GenerationTask).one()
        assert task.status == TaskStatus.FAILED.value
        assert task.runninghub_auto_retry_count == 0
        assert task.runninghub_auto_retry_after is None
        assert "OOM_KILLED" in task.error_message
        assert "不会自动重复付费" in task.error_message

    failed_status = client.post(
        f"/api/workbench/ltx-items/{item_id}",
        json={"access_token": token},
    ).json()
    segment = failed_status["segments"][0]
    missing_key = client.post(
        f"/api/workbench/ltx-items/{item_id}/segments/1/retry",
        json={"access_token": token, "cost_confirmed": True},
    )
    assert missing_key.status_code == 422
    retry_payload = {
        "access_token": token,
        "cost_confirmed": True,
        "request_key": "manual-retry-001",
    }
    retried = client.post(
        f"/api/workbench/ltx-items/{item_id}/segments/1/retry",
        json=retry_payload,
    )
    assert retried.status_code == 200, retried.text
    repeated = client.post(
        f"/api/workbench/ltx-segments/{segment['segment_id']}/retry",
        json=retry_payload,
    )
    assert repeated.status_code == 200, repeated.text
    with SessionLocal() as db:
        task = db.query(GenerationTask).one()
        assert task.status == TaskStatus.PENDING.value
        assert json.loads(task.input_payload)["_ltx_manual_retry"]["request_key"] == (
            "manual-retry-001"
        )
