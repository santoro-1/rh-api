from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from app.models import GenerationTask
from app.services.processes import hidden_creation_flags
from app.workflows.base import WorkflowAsset


logger = logging.getLogger(__name__)


class RunningHubUploadPreparationError(ValueError):
    """A local retry copy could not be prepared without touching the source."""


def _attempt_history(task: GenerationTask) -> list[dict]:
    if not task.runninghub_attempt_history:
        return []
    try:
        value = json.loads(task.runninghub_attempt_history)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return (
        [entry for entry in value if isinstance(entry, dict)]
        if isinstance(value, list)
        else []
    )


def latest_attempt_lost_remote_mp4(task: GenerationTask) -> bool:
    """Return whether RunningHub's latest attempt lost an uploaded MP4 object."""

    history = _attempt_history(task)
    if not history:
        return False
    failure_text = json.dumps(history[-1], ensure_ascii=False).lower()
    return (
        "no such file or directory" in failure_text
        and "/input/openapi/" in failure_text
        and ".mp4" in failure_text
    )


def prepare_runninghub_retry_upload(
    task: GenerationTask,
    asset: WorkflowAsset,
    source: Path,
    work_dir: Path,
) -> Path:
    """Create a byte-distinct, stream-copy MP4 after a remote object-loss failure.

    RunningHub names uploaded objects by content. Re-uploading identical bytes can
    therefore keep returning the same broken object key. A metadata-only remux
    preserves encoded streams, frame rate and duration while forcing a new key.
    The persisted source is never modified.
    """

    if (
        task.workflow_type != "ltx_lip_sync"
        or asset.kind != "video"
        or source.suffix.lower() != ".mp4"
        or not latest_attempt_lost_remote_mp4(task)
    ):
        return source

    failure_count = len(_attempt_history(task))
    work_dir.mkdir(parents=True, exist_ok=True)
    destination = work_dir / f"runninghub-retry-{failure_count:03d}.mp4"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        "-map_metadata",
        "0",
        "-metadata",
        f"comment=runninghub-missing-object-retry-{failure_count}",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
            creationflags=hidden_creation_flags(),
        )
    except FileNotFoundError as exc:
        raise RunningHubUploadPreparationError(
            "服务器未安装 ffmpeg，无法刷新 RunningHub 丢失的视频素材"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RunningHubUploadPreparationError(
            "刷新 RunningHub 丢失的视频素材超时，原视频未被修改"
        ) from exc
    if (
        completed.returncode != 0
        or not destination.is_file()
        or destination.stat().st_size <= 0
    ):
        diagnostic = (completed.stderr or completed.stdout or "").strip()
        logger.warning("RunningHub 重试视频无损重封装失败：%s", diagnostic[-1000:])
        raise RunningHubUploadPreparationError(
            "刷新 RunningHub 丢失的视频素材失败，原视频未被修改"
        )
    return destination
