from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    GenerationTask,
    LongAudioProject,
    LongAudioProjectStatus,
    User,
)
from app.services.workflow_configs import save_workflow_config
from tests.conftest import create_user, login


TOKEN = "remote-worker-test-token-0123456789abcdef"
SCRIPT = "今天是星期四。我要吃肯德基。但是我下班很晚。"


def _configure_ltx(username: str) -> None:
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        config = save_workflow_config(
            user,
            "ltx_lip_sync",
            ai_app_id="ltx-remote-worker-test",
            instance_type="default",
            default_prompt="测试",
            is_enabled=True,
        )
        db.add(config)
        db.commit()


def _remote_settings(monkeypatch):
    settings = replace(
        get_settings(),
        media_processing_mode="remote",
        media_worker_token=TOKEN,
        media_worker_lease_seconds=1800,
    )
    monkeypatch.setattr(
        "app.routes.media_worker_api.get_settings",
        lambda: settings,
    )
    return settings


def _create_project(client, monkeypatch) -> str:
    create_user("remote-media-user")
    _configure_ltx("remote-media-user")
    login(client, "remote-media-user")
    monkeypatch.setattr(
        "app.services.long_audio.inspect_audio_duration",
        lambda path: 30.0 if "segment-" in Path(path).name else 90.0,
    )
    monkeypatch.setattr(
        "app.services.long_audio.inspect_media_duration",
        lambda path: 30.0 if "segment-" in Path(path).name else 100.0,
    )
    response = client.post(
        "/api/long-audio-projects",
        data={
            "name": "远程节点测试",
            "scriptText": SCRIPT,
            "promptPrefix": "一名人物用中文说",
            "instanceType": "default",
            "alignmentProvider": "funasr_http",
        },
        files={
            "customAudio": ("long.mp3", b"ID3long-audio", "audio/mpeg"),
            "sourceVideo": (
                "source.mp4",
                b"\x00\x00\x00\x18ftypisomvideo",
                "video/mp4",
            ),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["projectId"]


def _worker_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _segments() -> list[dict[str, object]]:
    return [
        {
            "index": 1,
            "startSeconds": 0,
            "endSeconds": 30,
            "scriptText": "今天是星期四。",
            "alignmentMethod": "asr_timestamp",
        },
        {
            "index": 2,
            "startSeconds": 30,
            "endSeconds": 60,
            "scriptText": "我要吃肯德基。",
            "alignmentMethod": "asr_timestamp",
        },
        {
            "index": 3,
            "startSeconds": 60,
            "endSeconds": 90,
            "scriptText": "但是我下班很晚。",
            "alignmentMethod": "asr_timestamp",
        },
    ]


def _segment_archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
        for index in range(1, 4):
            bundle.writestr(
                f"audio/segment-{index:03d}.mp3",
                b"ID3segment",
            )
            bundle.writestr(
                f"video/segment-{index:03d}.mp4",
                b"\x00\x00\x00\x18ftypisomsegment",
            )
    return output.getvalue()


def test_remote_worker_claim_analysis_review_and_cut_handoff(client, monkeypatch):
    _remote_settings(monkeypatch)
    project_id = _create_project(client, monkeypatch)

    unauthorized = client.post(
        "/api/media-worker/v1/jobs/claim",
        json={"workerId": "intruder", "capabilities": ["analysis"]},
    )
    assert unauthorized.status_code == 401

    claim = client.post(
        "/api/media-worker/v1/jobs/claim",
        headers=_worker_headers(),
        json={"workerId": "san-laptop", "capabilities": ["analysis", "cut"]},
    )
    assert claim.status_code == 200, claim.text
    analysis_job = claim.json()
    assert analysis_job["jobId"] == project_id
    assert analysis_job["action"] == "analysis"
    lease_id = analysis_job["leaseId"]

    source = client.get(
        analysis_job["source"]["audioUrl"],
        headers=_worker_headers(),
    )
    assert source.status_code == 200
    assert source.content == b"ID3long-audio"

    heartbeat = client.post(
        f"/api/media-worker/v1/jobs/{project_id}/heartbeat",
        headers=_worker_headers(),
        json={
            "leaseId": lease_id,
            "metrics": {"phase": "asr", "workerTreeRssMb": 2200},
        },
    )
    assert heartbeat.status_code == 200

    analyzed = client.post(
        f"/api/media-worker/v1/jobs/{project_id}/analysis",
        headers=_worker_headers(),
        json={
            "leaseId": lease_id,
            "provider": "funasr_http",
            "segments": _segments(),
            "metrics": {
                "phase": "analysis_completed",
                "elapsedSeconds": 24.5,
            },
        },
    )
    assert analyzed.status_code == 200, analyzed.text

    project_payload = client.get(
        f"/api/long-audio-projects/{project_id}"
    ).json()
    assert project_payload["status"] == LongAudioProjectStatus.REVIEW.value
    assert project_payload["remoteWorkerId"] == "san-laptop"
    assert project_payload["remoteMetrics"]["elapsedSeconds"] == 24.5

    confirmed = client.post(
        f"/api/long-audio-projects/{project_id}/confirm"
    )
    assert confirmed.status_code == 200

    cut_claim = client.post(
        "/api/media-worker/v1/jobs/claim",
        headers=_worker_headers(),
        json={"workerId": "san-laptop", "capabilities": ["analysis", "cut"]},
    )
    assert cut_claim.status_code == 200
    cut_job = cut_claim.json()
    assert cut_job["action"] == "cut"
    assert len(cut_job["segments"]) == 3

    unsafe_archive = io.BytesIO()
    with zipfile.ZipFile(unsafe_archive, "w") as bundle:
        bundle.writestr("../outside.mp4", b"not-allowed")
    rejected = client.post(
        f"/api/media-worker/v1/jobs/{project_id}/cut",
        headers=_worker_headers(),
        data={"leaseId": cut_job["leaseId"]},
        files={
            "archive": (
                "unsafe.zip",
                unsafe_archive.getvalue(),
                "application/zip",
            )
        },
    )
    assert rejected.status_code == 400

    completed = client.post(
        f"/api/media-worker/v1/jobs/{project_id}/cut",
        headers=_worker_headers(),
        data={
            "leaseId": cut_job["leaseId"],
            "metrics": '{"phase":"cut_completed","elapsedSeconds":31.2}',
        },
        files={
            "archive": (
                "segments.zip",
                _segment_archive(),
                "application/zip",
            )
        },
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["batchId"]

    with SessionLocal() as db:
        project = db.get(LongAudioProject, project_id)
        tasks = (
            db.query(GenerationTask)
            .filter(GenerationTask.segment_id.is_not(None))
            .all()
        )
        assert project is not None
        assert project.status == LongAudioProjectStatus.COMPLETED.value
        assert project.remote_lease_id is None
        assert project.remote_worker_id == "san-laptop"
        assert len(tasks) == 3


def test_expired_remote_lease_is_reclaimed(client, monkeypatch):
    _remote_settings(monkeypatch)
    project_id = _create_project(client, monkeypatch)
    first = client.post(
        "/api/media-worker/v1/jobs/claim",
        headers=_worker_headers(),
        json={"workerId": "first-worker", "capabilities": ["analysis"]},
    ).json()

    with SessionLocal() as db:
        project = db.get(LongAudioProject, project_id)
        assert project is not None
        project.remote_lease_expires_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)
        db.commit()

    second_response = client.post(
        "/api/media-worker/v1/jobs/claim",
        headers=_worker_headers(),
        json={"workerId": "second-worker", "capabilities": ["analysis"]},
    )
    assert second_response.status_code == 200
    second = second_response.json()
    assert second["jobId"] == project_id
    assert second["leaseId"] != first["leaseId"]

    stale = client.post(
        f"/api/media-worker/v1/jobs/{project_id}/analysis",
        headers=_worker_headers(),
        json={
            "leaseId": first["leaseId"],
            "provider": "funasr_http",
            "segments": _segments(),
        },
    )
    assert stale.status_code == 409
