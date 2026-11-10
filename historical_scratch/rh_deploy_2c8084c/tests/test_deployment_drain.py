from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from app.config import get_settings
from app.routes import media_worker_api
from app.services.deployment_drain import (
    deployment_drain_path,
    is_deployment_draining,
    read_deployment_drain,
)
from app.workers import audio_worker, task_worker


def _write_marker(*, expires_at: float) -> None:
    marker = deployment_drain_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "token": "test-token",
                "commit": "test-commit",
                "expiresAtEpoch": expires_at,
            }
        ),
        encoding="utf-8",
    )


def test_active_drain_blocks_writes_but_keeps_reads_and_auth_available(
    client, monkeypatch
):
    _write_marker(expires_at=time.time() + 300)

    blocked_api = client.post("/api/drain-test")
    assert blocked_api.status_code == 503
    assert blocked_api.json()["code"] == "DEPLOYMENT_DRAINING"
    assert blocked_api.headers["retry-after"] == "15"

    blocked_page = client.post("/drain-test")
    assert blocked_page.status_code == 503
    assert "暂停新建或修改任务" in blocked_page.text

    assert client.get("/api/drain-test").status_code == 404
    assert client.get("/login").status_code == 200
    assert client.post("/login").status_code != 503
    assert client.post("/logout").status_code != 503
    worker_token = "drain-test-worker-token-0123456789abcdef"
    remote_settings = replace(
        get_settings(),
        media_processing_mode="remote",
        media_worker_token=worker_token,
    )
    monkeypatch.setattr(media_worker_api, "get_settings", lambda: remote_settings)
    monkeypatch.setattr(
        media_worker_api,
        "_claim_next",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("draining node must not claim")
        ),
    )
    media_claim = client.post(
        "/api/media-worker/v1/jobs/claim",
        headers={"Authorization": f"Bearer {worker_token}"},
        json={"workerId": "drain-test", "capabilities": ["analysis", "cut"]},
    )
    assert media_claim.status_code == 204
    assert media_claim.headers.get("X-Deployment-Draining") is None


def test_expired_or_invalid_drain_marker_is_fail_open(client):
    _write_marker(expires_at=time.time() - 1)
    assert is_deployment_draining() is False
    assert read_deployment_drain() is None
    assert client.post("/api/drain-test").status_code == 404

    deployment_drain_path().write_text("not-json", encoding="utf-8")
    assert is_deployment_draining() is False


def test_video_worker_finishes_active_checks_without_claiming_pending(monkeypatch):
    monkeypatch.setattr(task_worker, "is_deployment_draining", lambda: True)
    monkeypatch.setattr(
        task_worker,
        "claim_next_pending_task",
        lambda _db: (_ for _ in ()).throw(AssertionError("must not claim")),
    )
    monkeypatch.setattr(
        task_worker,
        "process_pending_video_merges",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not merge")),
    )
    assert task_worker.run_once() == 0


def test_audio_worker_polls_remote_but_does_not_claim_new_work(monkeypatch):
    monkeypatch.setattr(audio_worker, "is_deployment_draining", lambda: True)
    monkeypatch.setattr(
        audio_worker,
        "claim_next_voice_task",
        lambda _db: (_ for _ in ()).throw(AssertionError("must not claim voice")),
    )
    monkeypatch.setattr(
        audio_worker,
        "claim_next_pending_task",
        lambda _db: (_ for _ in ()).throw(AssertionError("must not claim audio")),
    )
    assert audio_worker.run_once() == 0


def test_deploy_script_uses_owned_expiring_drain_marker():
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "deploy-update.ps1"
    ).read_text(encoding="utf-8")
    assert "deployment-drain.json" in script
    assert "expiresAtEpoch" in script
    assert "Wait-QueuesDrained" in script
    assert "'UPLOADING','SUBMITTED','RUNNING'" in script
    assert "'CLONING','SYNTHESIZING','REMOTE_PENDING','ALIGNING','SEGMENTING','HANDOFF'" in script
    assert "'CLONING','SYNTHESIZING','SAVING'" in script
    assert "'ANALYZING','CUTTING'" in script
    assert "'MERGING'" in script
    assert "generation_batch_items" in script
    assert "bi.merged_video_status = 'MERGE_PENDING'" in script
    assert "LEFT JOIN generation_tasks AS gt ON gt.segment_id = gs.id" in script
    assert "gt.id IS NULL OR gt.status <> 'SUCCESS'" in script
    assert "grep -Fq '\"token\":\"$script:DrainToken\"'" in script
    assert "Disable-DeploymentDrain" in script
