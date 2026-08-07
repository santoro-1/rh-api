from __future__ import annotations

import json
import re
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import get_settings
from app.database import get_db
from app.models import (
    AudioGenerationTask,
    AudioTaskStatus,
    GenerationTask,
    LongAudioProject,
    LongAudioProjectStatus,
    TaskStatus,
    User,
)
from app.routes.dependencies import get_page_admin
from app.services.resource_monitor import resource_snapshot
from app.web import templates


router = APIRouter(prefix="/admin", tags=["operations"])

# Stable service key -> rotating log filename. These keys are also used as
# browser cursor names, so changing one requires updating operations.js.
LOG_FILES = {
    "web": "web.log",
    "audio_worker": "audio_worker.log",
    "media_worker": "media_worker.log",
    "video_worker": "video_worker.log",
    "launcher": "launcher.log",
}
SERVICE_LABELS = {
    "web": "Web",
    "audio_worker": "语音 Worker",
    "media_worker": "媒体 Worker",
    "video_worker": "视频 Worker",
    "launcher": "本地总控",
}
# Successful background requests are useful in raw server logs but are not
# operator events and must not flood the live admin stream.
_DISPLAY_NOISE_PATTERNS = (
    re.compile(r'"GET /api/batches/[^ ]+ HTTP/[^"]+"\s+200\b'),
    re.compile(r'"GET /healthz(?:\?[^ ]*)? HTTP/[^"]+"\s+200\b'),
    re.compile(r'"GET /admin/operations/updates[^ ]* HTTP/[^"]+"\s+200\b'),
)
_ACCESS_STATUS_RE = re.compile(
    r'uvicorn\.access:.*"\S+\s+\S+\s+HTTP/[^"]+"\s+(?P<status>\d{3})\b'
)
_WEB_STARTUP_MARKERS = (
    "Started server process",
    "Application startup complete",
    "Uvicorn running on",
)
SOURCE_CHANNELS = {
    "all": "全部",
    "legacy_web": "旧网页",
    "new_workbench": "新工作台",
}


class LogChunk(TypedDict):
    """One incremental log response for a single service file."""

    cursor: int
    lines: list[str]


def _heartbeat(service: str) -> dict[str, object]:
    path = get_settings().runtime_dir / f"{service}.heartbeat.json"
    if not path.is_file():
        return {"online": False, "updatedAt": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(str(payload["updatedAt"]))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        maximum_age = max(get_settings().poll_interval_seconds * 3 + 5, 20)
        payload["online"] = (
            datetime.now(timezone.utc) - updated
        ).total_seconds() <= maximum_age
        return payload
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return {"online": False, "updatedAt": None}


def _is_visible_log_line(line: str) -> bool:
    if any(pattern.search(line) for pattern in _DISPLAY_NOISE_PATTERNS):
        return False
    if "[EVENT " in line:
        return True
    if re.search(r"\s(?:WARNING|ERROR|CRITICAL)\s", line):
        return True
    if any(marker in line for marker in _WEB_STARTUP_MARKERS):
        return True
    access_match = _ACCESS_STATUS_RE.search(line)
    return bool(
        access_match and int(access_match.group("status")) >= 400
    )


def _normalize_source_channel(value: str) -> str:
    value = value.strip().lower()
    return value if value in SOURCE_CHANNELS else "all"


def _matches_source_channel(line: str, source_channel: str) -> bool:
    if source_channel == "all":
        return True
    return bool(
        re.search(
            rf'"source_channel"\s*:\s*"{re.escape(source_channel)}"',
            line,
        )
    )


def _read_log_chunk(
    path: Path,
    cursor: int | None,
    *,
    maximum_lines: int = 120,
    maximum_bytes: int = 256 * 1024,
    source_channel: str = "all",
) -> LogChunk:
    """Read complete new lines after cursor without resetting page scroll."""

    if not path.is_file():
        return {"cursor": 0, "lines": []}
    try:
        file_size = path.stat().st_size
        initial_request = cursor is None
        read_start = (
            max(file_size - maximum_bytes, 0)
            if initial_request
            else max(int(cursor), 0)
        )
        # A smaller file means midnight rotation or replacement. Start at zero
        # and let the browser continue with the new file's cursor.
        if read_start > file_size:
            read_start = 0
        if file_size - read_start > maximum_bytes:
            read_start = file_size - maximum_bytes

        with path.open("rb") as stream:
            # A cursor returned by this function always points after a newline.
            # A caller-supplied/rotation cursor may land mid-line, so inspect
            # the preceding byte before deciding whether to discard a prefix.
            starts_on_line_boundary = True
            if read_start:
                stream.seek(read_start - 1)
                starts_on_line_boundary = stream.read(1) == b"\n"
            stream.seek(read_start)
            raw = stream.read()
        if read_start and raw and not starts_on_line_boundary:
            first_newline = raw.find(b"\n")
            if first_newline < 0:
                return {"cursor": read_start, "lines": []}
            raw = raw[first_newline + 1 :]
            read_start += first_newline + 1
        last_newline = raw.rfind(b"\n")
        if last_newline < 0:
            if not initial_request:
                return {"cursor": read_start, "lines": []}
            complete = raw
        elif initial_request and last_newline != len(raw) - 1:
            complete = raw
        else:
            complete = raw[: last_newline + 1]
        next_cursor = read_start + len(complete)
        lines = complete.decode("utf-8", errors="replace").splitlines()
        source_channel = _normalize_source_channel(source_channel)
        visible = [
            line
            for line in lines
            if _is_visible_log_line(line)
            and _matches_source_channel(line, source_channel)
        ]
        return {
            "cursor": next_cursor,
            "lines": visible[-maximum_lines:],
        }
    except OSError:
        return {"cursor": 0, "lines": []}


def _tail(path: Path, maximum_lines: int = 120) -> list[str]:
    """Compatibility wrapper used by tests and non-streaming callers."""

    return _read_log_chunk(
        path,
        None,
        maximum_lines=maximum_lines,
    )["lines"]


def _history_log_paths(path: Path, days: int) -> list[Path]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    candidates = [path, *path.parent.glob(f"{path.name}.*")]
    available: list[Path] = []
    for candidate in candidates:
        try:
            modified = datetime.fromtimestamp(
                candidate.stat().st_mtime,
                tz=timezone.utc,
            )
        except OSError:
            continue
        if candidate.is_file() and modified >= cutoff:
            available.append(candidate)
    return sorted(
        available,
        key=lambda candidate: candidate.stat().st_mtime,
        reverse=True,
    )


def _download_log_paths(days: int) -> list[tuple[Path, str]]:
    """Collect only known service logs within the configured retention window."""

    logs_dir = get_settings().logs_dir
    files: list[tuple[Path, str]] = []
    for service, filename in LOG_FILES.items():
        for path in _history_log_paths(logs_dir / filename, days):
            if path.is_symlink():
                continue
            files.append((path, f"{service}/{path.name}"))
    return files


def _search_log_history(
    *,
    service: str,
    account_tokens: tuple[str, ...],
    task_query: str,
    level: str,
    days: int,
    source_channel: str = "all",
    maximum_lines: int = 300,
) -> list[str]:
    selected_services = (
        [service] if service in LOG_FILES else list(LOG_FILES)
    )
    task_query = task_query.strip().lower()
    level = level.strip().upper()
    results: list[str] = []
    for service_key in selected_services:
        path = get_settings().logs_dir / LOG_FILES[service_key]
        for history_path in _history_log_paths(path, days):
            chunk = _read_log_chunk(
                history_path,
                None,
                maximum_lines=maximum_lines,
                maximum_bytes=min(get_settings().log_max_bytes, 1024 * 1024),
                source_channel=source_channel,
            )
            for line in reversed(chunk["lines"]):
                lowered = line.lower()
                if account_tokens and not any(
                    token.lower() in lowered for token in account_tokens
                ):
                    continue
                if task_query and task_query not in lowered:
                    continue
                if level and f" {level} " not in line:
                    continue
                results.append(f"[{service_key}] {line}")
                if len(results) >= maximum_lines:
                    return results
    return results


def _service_snapshot() -> dict[str, dict[str, object]]:
    services = {
        "web": {
            "online": True,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        },
        "audio_worker": _heartbeat("audio_worker"),
        "media_worker": _heartbeat("media_worker"),
        "video_worker": _heartbeat("video_worker"),
        "launcher": _heartbeat("launcher"),
    }
    return {
        key: services[key]
        for key, _label in _service_options()
    }


def _service_options() -> tuple[tuple[str, str], ...]:
    """Hide the Windows-only launcher from systemd production deployments."""

    keys = (
        ("web", "audio_worker", "media_worker", "video_worker")
        if get_settings().app_env == "production"
        else tuple(LOG_FILES)
    )
    return tuple((key, SERVICE_LABELS[key]) for key in keys)


def _visible_log_files() -> dict[str, str]:
    return {
        key: LOG_FILES[key]
        for key, _label in _service_options()
    }


def _queue_snapshot(db: Session) -> dict[str, object]:
    # status -> count maps retain the provider-facing states for diagnosis;
    # active values are the concise numbers rendered in the status cards.
    video_counts = dict(
        db.execute(
            select(GenerationTask.status, func.count()).group_by(
                GenerationTask.status
            )
        ).all()
    )
    audio_counts = dict(
        db.execute(
            select(AudioGenerationTask.status, func.count()).group_by(
                AudioGenerationTask.status
            )
        ).all()
    )
    media_counts = dict(
        db.execute(
            select(LongAudioProject.status, func.count()).group_by(
                LongAudioProject.status
            )
        ).all()
    )
    video_active_statuses = {
        TaskStatus.PENDING.value,
        TaskStatus.UPLOADING.value,
        TaskStatus.SUBMITTED.value,
        TaskStatus.RUNNING.value,
    }
    audio_active_statuses = {
        AudioTaskStatus.PENDING.value,
        AudioTaskStatus.CLONING.value,
        AudioTaskStatus.SYNTHESIZING.value,
        AudioTaskStatus.REMOTE_PENDING.value,
        AudioTaskStatus.AWAITING_REVIEW.value,
        AudioTaskStatus.ALIGNING.value,
        AudioTaskStatus.SEGMENTING.value,
        AudioTaskStatus.HANDOFF.value,
    }
    media_active_statuses = {
        LongAudioProjectStatus.PENDING_ANALYSIS.value,
        LongAudioProjectStatus.ANALYZING.value,
        LongAudioProjectStatus.PENDING_CUT.value,
        LongAudioProjectStatus.CUTTING.value,
    }
    return {
        "videoCounts": video_counts,
        "audioCounts": audio_counts,
        "mediaCounts": media_counts,
        "videoActive": sum(
            count
            for status, count in video_counts.items()
            if status in video_active_statuses
        ),
        "audioActive": sum(
            count
            for status, count in audio_counts.items()
            if status in audio_active_statuses
        ),
        "mediaActive": sum(
            count
            for status, count in media_counts.items()
            if status in media_active_statuses
        ),
    }


@router.get("/operations")
def operations_page(
    request: Request,
    account_id: int | None = Query(None, ge=1),
    task: str = Query("", max_length=100),
    service: str = Query("all", max_length=30),
    level: str = Query("", max_length=10),
    source_channel: str = Query("all", max_length=30),
    days: int = Query(7, ge=1, le=7),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    source_channel = _normalize_source_channel(source_channel)
    queue = _queue_snapshot(db)
    services = _service_snapshot()
    visible_log_files = _visible_log_files()
    logs = {
        name: _read_log_chunk(
            get_settings().logs_dir / filename,
            None,
            source_channel=source_channel,
        )
        for name, filename in visible_log_files.items()
    }
    users = db.scalars(select(User).order_by(User.username)).all()
    selected_user = db.get(User, account_id) if account_id else None
    account_tokens = (
        (
            f'"user_id":{selected_user.id}',
            f'"username":"{selected_user.username}"',
        )
        if selected_user
        else ()
    )
    history_lines = (
        _search_log_history(
            service=service,
            account_tokens=account_tokens,
            task_query=task,
            level=level,
            days=days,
            source_channel=source_channel,
        )
        if account_id
        or task.strip()
        or service != "all"
        or level
        or source_channel != "all"
        else []
    )
    return templates.TemplateResponse(
        request,
        "operations.html",
        {
            "current_user": current_user,
            "services": services,
            "service_options": _service_options(),
            "source_channel_options": SOURCE_CHANNELS.items(),
            "video_counts": queue["videoCounts"],
            "audio_counts": queue["audioCounts"],
            "media_counts": queue["mediaCounts"],
            "video_active": queue["videoActive"],
            "audio_active": queue["audioActive"],
            "media_active": queue["mediaActive"],
            "logs": logs,
            "resources": resource_snapshot(get_settings()),
            "users": users,
            "filters": {
                "accountId": account_id,
                "task": task,
                "service": service,
                "level": level,
                "days": days,
                "sourceChannel": source_channel,
            },
            "history_lines": history_lines,
            "history_searched": bool(
                account_id
                or task.strip()
                or service != "all"
                or level
                or source_channel != "all"
            ),
        },
    )


@router.get("/operations/updates")
def operations_updates(
    web: int | None = None,
    audio_worker: int | None = None,
    media_worker: int | None = None,
    video_worker: int | None = None,
    launcher: int | None = None,
    source_channel: str = Query("all", max_length=30),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    """Return new operator events and fresh cards without reloading the page."""

    del current_user
    source_channel = _normalize_source_channel(source_channel)
    cursors = {
        "web": web,
        "audio_worker": audio_worker,
        "media_worker": media_worker,
        "video_worker": video_worker,
        "launcher": launcher,
    }
    visible_log_files = _visible_log_files()
    return {
        "services": _service_snapshot(),
        "queue": _queue_snapshot(db),
        "resources": resource_snapshot(get_settings()),
        "logs": {
            service: _read_log_chunk(
                get_settings().logs_dir / filename,
                cursors[service],
                source_channel=source_channel,
            )
            for service, filename in visible_log_files.items()
        },
    }


@router.get("/operations/logs/download")
def download_operations_logs(
    days: int = Query(7, ge=1, le=7),
    current_user: User = Depends(get_page_admin),
):
    """Download retained raw service logs for offline administrator diagnosis."""

    del current_user
    log_files = _download_log_paths(days)
    if not log_files:
        raise HTTPException(status_code=404, detail="最近没有可下载的日志")

    archive_dir = get_settings().runtime_dir / "log-downloads"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"logs-{uuid.uuid4().hex}.zip"
    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            for path, archive_name in log_files:
                archive.write(path, arcname=archive_name)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"runninghub-video-logs-{timestamp}.zip",
        content_disposition_type="attachment",
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )
