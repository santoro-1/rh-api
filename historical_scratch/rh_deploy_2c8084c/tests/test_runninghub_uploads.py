from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import GenerationTask
from app.services import runninghub_uploads
from app.workflows.base import WorkflowAsset


def _task_with_failure(exception_message: str) -> GenerationTask:
    return GenerationTask(
        id="remote-file-retry-task",
        workflow_type="ltx_lip_sync",
        runninghub_attempt_history=json.dumps(
            [
                {
                    "taskId": "remote-failed-id",
                    "status": "FAILED",
                    "failedReason": {"exception_message": exception_message},
                }
            ]
        ),
    )


def test_detects_only_latest_runninghub_missing_mp4_failure():
    task = _task_with_failure(
        "Error opening input file "
        "/workspace/ComfyUI/input/openapi/deadbeef.mp4. No such file or directory"
    )
    assert runninghub_uploads.latest_attempt_lost_remote_mp4(task) is True

    task.runninghub_attempt_history = json.dumps(
        [
            json.loads(task.runninghub_attempt_history)[0],
            {
                "taskId": "newer-oom-id",
                "status": "FAILED",
                "failedReason": {"exception_message": "Task crashed (OOM_KILLED)."},
            },
        ]
    )
    assert runninghub_uploads.latest_attempt_lost_remote_mp4(task) is False


def test_missing_remote_mp4_retry_uses_stream_copy_without_touching_source(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source.mp4"
    source_bytes = b"original-mp4-bytes"
    source.write_bytes(source_bytes)
    task = _task_with_failure(
        "No such file or directory: /workspace/ComfyUI/input/openapi/deadbeef.mp4"
    )
    asset = WorkflowAsset("video", "video", "uploads/source.mp4", "source.mp4")
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        Path(command[-1]).write_bytes(b"remuxed-mp4-bytes")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runninghub_uploads.subprocess, "run", fake_run)

    prepared = runninghub_uploads.prepare_runninghub_retry_upload(
        task,
        asset,
        source,
        tmp_path / "retry",
    )

    assert prepared != source
    assert prepared.read_bytes() == b"remuxed-mp4-bytes"
    assert source.read_bytes() == source_bytes
    command = captured["command"]
    assert command[command.index("-c") + 1] == "copy"
    assert "comment=runninghub-missing-object-retry-1" in command


def test_normal_upload_keeps_original_path(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")
    task = _task_with_failure("Task crashed (OOM_KILLED).")
    asset = WorkflowAsset("video", "video", "uploads/source.mp4", "source.mp4")

    assert (
        runninghub_uploads.prepare_runninghub_retry_upload(
            task,
            asset,
            source,
            tmp_path,
        )
        == source
    )


def test_remux_failure_does_not_modify_source(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.mp4"
    source_bytes = b"original"
    source.write_bytes(source_bytes)
    task = _task_with_failure(
        "No such file or directory: /workspace/ComfyUI/input/openapi/deadbeef.mp4"
    )
    asset = WorkflowAsset("video", "video", "uploads/source.mp4", "source.mp4")
    monkeypatch.setattr(
        runninghub_uploads.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="invalid container",
        ),
    )

    with pytest.raises(runninghub_uploads.RunningHubUploadPreparationError):
        runninghub_uploads.prepare_runninghub_retry_upload(
            task,
            asset,
            source,
            tmp_path / "retry",
        )
    assert source.read_bytes() == source_bytes
