from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    BATCH_SOURCE_LTX_WORKBENCH,
    GenerationBatch,
    GenerationTask,
    LongAudioProject,
    LtxPreparationJob,
    TaskStatus,
    User,
)
from app.services.ltx_workbench import compile_ltx_prompt
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


def _row(video_asset_id: str, audio_asset_id: str) -> dict[str, str]:
    return {
        "row_id": "ROW-001",
        "script_text": SCRIPT,
        "video_asset_id": video_asset_id,
        "audio_asset_id": audio_asset_id,
    }


def _create_batch(client, monkeypatch, username: str = "ltx-workbench-user"):
    create_user(username)
    _enable_ltx(username)
    token = _token(client, username)
    video_id = _stage(client, token, "video", "source.mov", "video/quicktime")
    audio_id = _stage(client, token, "audio", "speech.wav", "audio/wav")
    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_audio_duration", lambda path: 30.0
    )
    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_media_duration", lambda path: 32.0
    )
    response = client.post(
        "/api/workbench/ltx-batches",
        json={
            "access_token": token,
            "name": "独立对口型批次",
            "request_key": "ltx-request-001",
            "cost_confirmed": True,
            "rows": [_row(video_id, audio_id)],
        },
    )
    assert response.status_code == 201, response.text
    return token, response.json()


def test_fixed_prompt_is_compiled_only_from_original_script() -> None:
    assert compile_ltx_prompt(SCRIPT) == f"一个人用中文说：“{SCRIPT}”"


def test_ltx_workbench_validates_full_row_duration_and_creates_idempotently(
    client, monkeypatch
) -> None:
    create_user("ltx-row-user")
    _enable_ltx("ltx-row-user")
    token = _token(client, "ltx-row-user")
    video_id = _stage(client, token, "video", "source.mp4", "video/mp4")
    audio_id = _stage(client, token, "audio", "speech.mp3", "audio/mpeg")
    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_audio_duration", lambda path: 30.0
    )
    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_media_duration", lambda path: 29.0
    )
    rejected = client.post(
        "/api/workbench/ltx-batches/validate",
        json={"access_token": token, "rows": [_row(video_id, audio_id)]},
    )
    assert rejected.status_code == 400
    assert "源视频时长不足" in rejected.json()["detail"]

    monkeypatch.setattr(
        "app.services.ltx_workbench.inspect_media_duration", lambda path: 30.0
    )
    custom_prompt = _row(video_id, audio_id)
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
        "rows": [_row(video_id, audio_id)],
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
        assert batch is not None
        assert preparation.duration_seconds == 30.0
        assert preparation.video_duration_seconds == 30.0
        assert preparation.script_text == SCRIPT


def test_short_unsegmented_ltx_analysis_preserves_uploaded_media_and_timeline(
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
        "app.services.long_audio.inspect_audio_duration", lambda path: 30.0
    )
    monkeypatch.setattr(
        "app.services.long_audio.inspect_media_duration", lambda path: 32.0
    )
    claim = client.post(
        "/api/media-worker/v1/jobs/claim",
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        json={"workerId": "ltx-test-worker", "capabilities": ["analysis", "cut"]},
    )
    assert claim.status_code == 200, claim.text
    job = claim.json()
    assert job["action"] == "analysis"
    completed = client.post(
        f"/api/media-worker/v1/jobs/{job['jobId']}/analysis",
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        json={
            "leaseId": job["leaseId"],
            "provider": "funasr_http",
            "segments": [
                {
                    "index": 1,
                    "startSeconds": 0,
                    "endSeconds": 30,
                    "scriptText": SCRIPT,
                    "alignmentMethod": "asr_timestamp",
                }
            ],
            "alignment": {
                "matchRatio": 1.0,
                "tokens": [
                    {
                        "text": SCRIPT,
                        "scriptStart": 0,
                        "scriptEnd": len(SCRIPT),
                        "startSeconds": 0,
                        "endSeconds": 30,
                        "confidence": 0.99,
                    }
                ],
            },
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
        assert Path(task.image_path).suffix == ".mov"
        assert Path(task.audio_path).suffix == ".wav"
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
