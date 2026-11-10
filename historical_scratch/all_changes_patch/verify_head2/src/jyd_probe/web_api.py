from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
from itertools import combinations, product
import json
import logging
from math import gcd
import mimetypes
import os
from pathlib import Path, PurePosixPath
import queue as queue_module
import re
import secrets
import shutil
import subprocess
import sys
import threading
import traceback
import uuid
from typing import Any
from urllib.parse import quote, urlparse
import zipfile

from fastapi import (
    Body,
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .audio_catalog import AudioCatalog, CombinedAudioCatalog
from .asset_admin import AssetAdminCatalog
from .browser_preview import BrowserPreviewError, browser_preview_path
from .caption_alignment import FunASRCaptionAligner
from .auth_center import (
    AuthCenterClient,
    AuthCenterError,
    AuthHandoffStore,
    create_local_workbench_handoff,
)
from .admin_auth import AdminAuth
from .draft_crypto import is_plain_json_file
from .draft_transfer import import_transfer_package
from .excel_batch import parse_excel_batch_workbook
from .music_matching import MusicProfileMatcher
from .logging_config import log_event
from .h3_handoff import H3HandoffError, import_h3_handoff
from .project_store import ProjectRevisionConflict, ProjectStore
from .user_templates import UserTemplateStore
from .project_audio import ProjectAudioCoordinator
from .project_content_analysis import ProjectContentAnalysisCoordinator
from .project_composition import (
    ProjectCompositionCoordinator,
    ProjectCompositionStartDispatcher,
)
from .project_h3 import ProjectH3Coordinator
from .project_ltx import (
    LtxWorkbenchClient,
    LtxWorkbenchError,
    ProjectLtxCoordinator,
)
from .project_diagnostics import build_project_diagnostic_archive
from .project_inputs import detect_project_image, parse_project_script_file
from .project_export_naming import (
    audio_export_filename,
    composition_export_filename,
    project_item_export_stem,
    segment_export_filename,
    variant_export_filename,
)
from .project_music import ProjectMusicSelector
from .project_postprocess import (
    ProjectPostprocessCoordinator,
    normalize_cover_title,
    normalize_top_title,
)
from .project_results import ProjectResultLibrary
from .project_variants import ProjectVariantCoordinator
from .project_video_source import project_segment_boundaries
from .project_visual_analysis import ProjectVisualAnalysisCoordinator
from .render_job import run_render_job
from .runtime_paths import detect_jianying_draft_root, libraries_root, project_root, resource_path
from .runtime_control import RuntimeControl
from .semantic_visuals import load_semantic_visual_catalog
from .unified_visual_plan import refresh_saved_visual_plans_for_catalog
from .layout_profiles import (
    DEFAULT_LAYOUT_PROFILE,
    LAYOUT_PROFILE_FONT_IDENTITY,
    layout_profile,
    normalize_layout_profile,
    public_layout_profiles,
)
from .subtitles import (
    build_caption_cues,
    caption_cues_from_payload,
    cues_to_srt,
    parse_srt_cues,
    validate_caption_cues,
)
from .template_library import TemplateLibrary, summarize_draft_data
from .task_store import SQLiteTaskStore
from .ui_automation_thread import initialize_ui_automation_in_current_thread
from .user_auth import UserAuth


PROJECT_ROOT = project_root()
workbench_logger = logging.getLogger(__name__)
render_logger = logging.getLogger("jyd_probe.render")
LIBRARIES_ROOT = libraries_root()
FRONTEND_ROOT = resource_path("apps", "processor", "frontend")
NEW_FRONTEND_ROOT = FRONTEND_ROOT / "new"
LAN_WEBSITE_ORIGIN_REGEX = (
    r"^https?://(?:"
    r"localhost|127(?:\.\d{1,3}){3}|"
    r"10(?:\.\d{1,3}){3}|"
    r"192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.(?:local|lan))?"
    r")(?:\:\d{1,5})?$"
)
TEXT_STYLE_LIBRARY_ROOT = Path(
    os.environ.get("JYD_TEXT_STYLE_LIBRARY_ROOT", LIBRARIES_ROOT / "text_style_library")
).expanduser().resolve()
FONT_LIBRARY_ROOT = Path(
    os.environ.get("JYD_FONT_LIBRARY_ROOT", LIBRARIES_ROOT / "font_library")
).expanduser().resolve()
SYSTEM_FONT_ROOT = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
EFFECT_LIBRARY_ROOT = Path(
    os.environ.get("JYD_EFFECT_LIBRARY_ROOT", LIBRARIES_ROOT / "effect_library")
).expanduser().resolve()
TEXT_EFFECT_LIBRARY_ROOT = Path(
    os.environ.get("JYD_TEXT_EFFECT_LIBRARY_ROOT", LIBRARIES_ROOT / "text_effect_library")
).expanduser().resolve()
TEXT_TEMPLATE_LIBRARY_ROOT = Path(
    os.environ.get("JYD_TEXT_TEMPLATE_LIBRARY_ROOT", LIBRARIES_ROOT / "text_template_library")
).expanduser().resolve()
STICKER_LIBRARY_ROOT = Path(
    os.environ.get("JYD_STICKER_LIBRARY_ROOT", LIBRARIES_ROOT / "sticker_library")
).expanduser().resolve()
CORNER_STICKER_LIBRARY_ROOT = Path(
    os.environ.get(
        "JYD_CORNER_STICKER_LIBRARY_ROOT", LIBRARIES_ROOT / "corner_sticker_library"
    )
).expanduser().resolve()
SEMANTIC_VISUAL_LIBRARY_ROOT = Path(
    os.environ.get(
        "JYD_SEMANTIC_VISUAL_LIBRARY_ROOT",
        LIBRARIES_ROOT / "semantic_visual_library",
    )
).expanduser().resolve()
NEW_WORKBENCH_H3_ONLY_DETAIL = (
    "新版工作台只支持多参考生成，普通数字人和视频对口型入口已停用"
)


@dataclass(frozen=True)
class WebApiSettings:
    storage_root: Path
    template_library_root: Path
    default_draft_root: Path
    audio_library_root: Path
    result_library_root: Path | None = None
    media_retention_hours: int = 24
    template_retention_hours: int = 48
    draft_retention_hours: int = 48
    completed_output_retention_hours: int = 72
    failed_output_retention_hours: int = 24
    metadata_retention_days: int = 30
    asset_trash_retention_days: int = 7
    cleanup_interval_minutes: int = 30
    admin_username: str = "admin"
    admin_password: str = "admin123"
    admin_session_secret: str = ""
    admin_session_hours: int = 12
    admin_cookie_secure: bool = False
    admin_cookie_name: str = "jyd_admin_session"
    site_username: str = "operator"
    site_password: str = "operator123"
    site_session_secret: str = ""
    site_cookie_name: str = "jyd_site_session"
    execution_mode: str = "embedded"
    agent_token: str = ""
    agent_lease_seconds: int = 180
    max_active_jobs: int = 500
    max_video_upload_bytes: int = 2 * 1024 * 1024 * 1024
    max_audio_upload_bytes: int = 200 * 1024 * 1024
    max_draft_import_bytes: int = 5 * 1024 * 1024 * 1024
    database_path: Path | None = None
    allow_local_file_access: bool = False
    personal_library_root: Path | None = None
    bootstrap_site_user: bool = True
    auth_server_url: str = "http://127.0.0.1:8000"
    shared_processor_url: str = ""
    ltx_workbench_url: str = ""
    auth_authority: bool = False
    auth_timeout_seconds: int = 15
    asr_base_url: str = ""
    asr_timeout_seconds: int = 1800
    asr_shared_token: str = ""
    asr_required: bool = False
    enable_legacy_new_workbench_video_engines: bool = False


class RenderJobQueue:
    def __init__(self, settings: WebApiSettings):
        self.settings = settings
        self.execution_mode = settings.execution_mode.strip().lower() or "embedded"
        if self.execution_mode not in {"embedded", "agent"}:
            raise ValueError("execution_mode 只能是 embedded 或 agent")
        audio_roots = [settings.audio_library_root]
        if settings.personal_library_root is not None:
            audio_roots.append(settings.personal_library_root / "audio_library")
        self.audio_catalog = CombinedAudioCatalog(audio_roots)
        self.store = SQLiteTaskStore(
            settings.database_path or (settings.storage_root / "control.db")
        )
        self._queue: queue_module.Queue[str] = queue_module.Queue()
        self._pending: list[str] = []
        self._lock = threading.Lock()
        self._mark_interrupted_jobs()
        self._import_legacy_records()
        self._worker: threading.Thread | None = None
        if self.execution_mode == "embedded":
            self.store.register_agent(
                "embedded-local",
                {"name": "本机内置处理机", "hostname": os.environ.get("COMPUTERNAME", "")},
            )
            self._worker = threading.Thread(
                target=self._worker_loop, name="jyd-render-worker", daemon=True
            )
            self._worker.start()
            for job_id in self.store.pending_job_ids():
                if job_id not in self._pending:
                    self._pending.append(job_id)
                self._queue.put(job_id)

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_queue_capacity(1)
        job_id = uuid.uuid4().hex
        job = _prepare_render_job_payload(self.settings, self.audio_catalog, payload, job_id)
        job_dir = _job_dir(self.settings, job_id)
        job_dir.mkdir(parents=True, exist_ok=False)

        now = _now()
        _write_json(job_dir / "job.json", job)

        with self._lock:
            self._pending.append(job_id)
            queue_position = len(self._pending)

        status = {
            "job_id": job_id,
            "status": "pending",
            "created_at": now,
            "queued_at": now,
            "queue_position": queue_position,
            "queue_size": queue_position,
        }
        _write_json(job_dir / "status.json", status)
        self.store.add_job(job_id, job, status)
        if self.execution_mode == "embedded":
            self._queue.put(job_id)
        return status

    def submit_batch(
        self,
        payloads: list[dict[str, Any]],
        variants: list[dict[str, Any]],
        *,
        temporary_template_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not payloads or len(payloads) != len(variants):
            raise ValueError("批量任务参数为空或组合信息不匹配")
        self._ensure_queue_capacity(len(payloads))

        batch_id = uuid.uuid4().hex
        temporary_ids = _validate_batch_once_template_ids(
            self.settings,
            payloads,
            temporary_template_ids or [],
        )
        prepared: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for index, (payload, variant) in enumerate(zip(payloads, variants), start=1):
            job_id = uuid.uuid4().hex
            job = _prepare_render_job_payload(self.settings, self.audio_catalog, payload, job_id)
            job.setdefault("batch", {})
            job["batch"].update(
                {
                    "batch_id": batch_id,
                    "index": index,
                    "total": len(payloads),
                    "variant": variant,
                }
            )
            prepared.append((job_id, job, variant))

        now = _now()
        batch_dir = _batch_dir(self.settings, batch_id)
        batch_dir.mkdir(parents=True, exist_ok=False)
        batch_record = {
            "batch_id": batch_id,
            "status": "pending",
            "created_at": now,
            "total": len(prepared),
            "combination_filter": _batch_combination_filter(variants),
            "temporary_template_ids": temporary_ids,
            "jobs": [
                {"job_id": job_id, "index": index, "variant": variant}
                for index, (job_id, _, variant) in enumerate(prepared, start=1)
            ],
        }
        _write_json(batch_dir / "batch.json", batch_record)
        self.store.add_batch(batch_record)
        _claim_batch_once_templates(self.settings, temporary_ids, batch_id)

        with self._lock:
            first_position = len(self._pending) + 1
            for job_id, job, variant in prepared:
                job_dir = _job_dir(self.settings, job_id)
                job_dir.mkdir(parents=True, exist_ok=False)
                _write_json(job_dir / "job.json", job)
                self._pending.append(job_id)
                status = {
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "batch_index": job["batch"]["index"],
                    "batch_total": len(prepared),
                    "variant": variant,
                    "status": "pending",
                    "created_at": now,
                    "queued_at": now,
                    "queue_position": len(self._pending),
                    "queue_size": len(self._pending),
                }
                _write_json(job_dir / "status.json", status)
                self.store.add_job(job_id, job, status)

        if self.execution_mode == "embedded":
            for job_id, _, _ in prepared:
                self._queue.put(job_id)

        return {
            "batch_id": batch_id,
            "status": "pending",
            "created_at": now,
            "total": len(prepared),
            "combination_filter": _batch_combination_filter(variants),
            "temporary_template_ids": temporary_ids,
            "queue_position": first_position,
            "job_ids": [item[0] for item in prepared],
        }

    def get_batch_status(self, batch_id: str) -> dict[str, Any]:
        batch_path = _batch_dir(self.settings, batch_id) / "batch.json"
        if not batch_path.exists():
            raise HTTPException(status_code=404, detail=f"批次不存在: {batch_id}")

        batch = _read_json(batch_path)
        jobs: list[dict[str, Any]] = []
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
        raw_statuses: list[dict[str, Any]] = []
        for item in batch.get("jobs", []):
            if not isinstance(item, dict) or not item.get("job_id"):
                continue
            status = self.get_status(str(item["job_id"]))
            raw_statuses.append(status)
            current = str(status.get("status", "failed"))
            counts[current] = counts.get(current, 0) + 1
            jobs.append(
                {
                    "job_id": status.get("job_id"),
                    "index": item.get("index"),
                    "variant": item.get("variant", {}),
                    "status": current,
                    "queue_position": status.get("queue_position"),
                    "error": status.get("error", ""),
                    "result": status.get("result", {}),
                    "output_deleted": bool(status.get("output_deleted", False)),
                    "expires_at": status.get("expires_at"),
                }
            )

        finished = counts.get("completed", 0) + counts.get("failed", 0) + counts.get("cancelled", 0)
        if counts.get("running", 0):
            overall = "running"
        elif finished >= len(jobs) and jobs:
            if counts.get("failed", 0):
                overall = "failed"
            elif counts.get("cancelled", 0):
                overall = "cancelled"
            else:
                overall = "completed"
        else:
            overall = "pending"
        timing = _estimate_batch_timing(raw_statuses)
        return {
            **batch,
            "status": overall,
            "counts": counts,
            "finished": finished,
            **timing,
            "jobs": jobs,
        }

    def list_recent_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(50, int(limit or 20)))
        batches_root = self.settings.storage_root / "batches"
        if not batches_root.exists():
            return []

        candidates: list[dict[str, Any]] = []
        batch_paths = sorted(
            batches_root.glob("*/batch.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[: max(50, safe_limit * 5)]
        for batch_path in batch_paths:
            try:
                batch = _read_json(batch_path)
                batch_id = str(batch.get("batch_id", "")).strip()
                if not batch_id:
                    continue
                status = self.get_batch_status(batch_id)
            except Exception:
                continue
            available_outputs = sum(
                1
                for job in status.get("jobs", [])
                if job.get("status") == "completed"
                and isinstance(job.get("result"), dict)
                and job["result"].get("exported")
                and not job.get("output_deleted")
            )
            candidates.append(
                {
                    "batch_id": batch_id,
                    "created_at": status.get("created_at", ""),
                    "status": status.get("status", ""),
                    "total": status.get("total", 0),
                    "finished": status.get("finished", 0),
                    "counts": status.get("counts", {}),
                    "available_outputs": available_outputs,
                }
            )
        candidates.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return candidates[:safe_limit]

    def delete_batch_record(self, batch_id: str) -> dict[str, Any]:
        batch_dir = _batch_dir(self.settings, batch_id)
        batch_path = batch_dir / "batch.json"
        if not batch_path.is_file():
            raise HTTPException(status_code=404, detail=f"批次不存在: {batch_id}")

        batch = _read_json(batch_path)
        job_ids = [
            str(item.get("job_id", ""))
            for item in batch.get("jobs", [])
            if isinstance(item, dict) and item.get("job_id")
        ]
        statuses: list[tuple[str, Path, dict[str, Any]]] = []
        active: list[str] = []
        for job_id in job_ids:
            status_path = _job_dir(self.settings, job_id) / "status.json"
            if not status_path.is_file():
                continue
            status = self.get_status(job_id)
            if status.get("status") in {"pending", "running"}:
                active.append(job_id)
            statuses.append((job_id, status_path, status))
        if active:
            raise HTTPException(
                status_code=409,
                detail=f"批次仍有 {len(active)} 个排队中或运行中的任务，请先等待完成或取消",
            )

        _cleanup_finished_batch_once_templates(
            self.settings,
            batch_id,
            keep_failed=False,
            force=True,
        )

        deletion = {"deleted_files": 0, "deleted_directories": 0, "deleted_bytes": 0}
        for job_id, status_path, status in statuses:
            result = status.get("result", {})
            job_path = status_path.parent / "job.json"
            job = _read_json(job_path) if job_path.is_file() else {}
            artifacts = _delete_managed_job_artifacts(
                self.settings,
                result if isinstance(result, dict) else {},
                job=job if isinstance(job, dict) else {},
            )
            _merge_deletion_report(deletion, artifacts)
            deletion["deleted_bytes"] += _directory_size(status_path.parent)
            if status_path.parent.is_dir():
                shutil.rmtree(status_path.parent)
                deletion["deleted_directories"] += 1

        deletion["deleted_bytes"] += _directory_size(batch_dir)
        if batch_dir.is_dir():
            shutil.rmtree(batch_dir)
            deletion["deleted_directories"] += 1
        if hasattr(self, "store"):
            self.store.delete_batch(batch_id)
        return {
            "batch_id": batch_id,
            "deleted_jobs": len(statuses),
            **deletion,
        }

    def cancel_batch(self, batch_id: str) -> dict[str, Any]:
        batch_path = _batch_dir(self.settings, batch_id) / "batch.json"
        if not batch_path.exists():
            raise HTTPException(status_code=404, detail=f"批次不存在: {batch_id}")

        batch = _read_json(batch_path)
        if hasattr(self, "store"):
            cancelled_ids = self.store.cancel_batch(batch_id)
            with self._lock:
                self._pending = [item for item in self._pending if item not in cancelled_ids]
            for job_id in cancelled_ids:
                status = self.store.get_status(job_id)
                if status is None:
                    continue
                status.update(
                    {
                        "expires_at": _expiry_after(self.settings.failed_output_retention_hours),
                        "metadata_expires_at": _expiry_after(
                            self.settings.metadata_retention_days * 24
                        ),
                    }
                )
                self.store.set_status(job_id, status)
                _write_json(_job_dir(self.settings, job_id) / "status.json", status)
            result = self.get_batch_status(batch_id)
            result["cancelled_now"] = len(cancelled_ids)
            self._cleanup_batch_once_templates(batch_id)
            return result
        cancelled = 0
        with self._lock:
            for item in batch.get("jobs", []):
                if not isinstance(item, dict) or not item.get("job_id"):
                    continue
                job_id = str(item["job_id"])
                if job_id not in self._pending:
                    continue
                status_path = _job_dir(self.settings, job_id) / "status.json"
                status = _read_json(status_path)
                if status.get("status") != "pending":
                    continue
                self._pending.remove(job_id)
                status.update(
                    {
                        "status": "cancelled",
                        "finished_at": _now(),
                        "expires_at": _expiry_after(self.settings.failed_output_retention_hours),
                        "metadata_expires_at": _expiry_after(self.settings.metadata_retention_days * 24),
                        "error": "用户取消了尚未开始的任务",
                    }
                )
                status.pop("queue_position", None)
                status.pop("queue_size", None)
                _write_json(status_path, status)
                cancelled += 1

        result = self.get_batch_status(batch_id)
        result["cancelled_now"] = cancelled
        self._cleanup_batch_once_templates(batch_id)
        return result

    def retry_failed_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.get_batch_status(batch_id)
        payloads: list[dict[str, Any]] = []
        variants: list[dict[str, Any]] = []
        for item in batch.get("jobs", []):
            if item.get("status") != "failed":
                continue
            job_id = str(item.get("job_id", ""))
            job = deepcopy(_read_json(_job_dir(self.settings, job_id) / "job.json"))
            job.pop("batch", None)
            job.pop("output_mp4", None)
            output = job.get("output")
            if isinstance(output, dict):
                for key in ("mp4_path", "output_mp4", "draft_name", "output_name"):
                    output.pop(key, None)
            payloads.append(job)
            variants.append(deepcopy(item.get("variant", {})))

        if not payloads:
            raise HTTPException(status_code=400, detail="这个批次没有可重试的失败任务")
        temporary_ids = [
            str(item)
            for item in batch.get("temporary_template_ids", [])
            if str(item).strip()
        ]
        if temporary_ids:
            result = self.submit_batch(
                payloads,
                variants,
                temporary_template_ids=temporary_ids,
            )
        else:
            result = self.submit_batch(payloads, variants)
        result["retried_from_batch_id"] = batch_id
        return result

    def create_batch_download(self, batch_id: str, job_ids: list[str]) -> dict[str, Any]:
        batch = self.get_batch_status(batch_id)
        requested = set(job_ids)
        if not requested:
            raise HTTPException(status_code=400, detail="请至少选择一个已完成的视频")
        batch_job_ids = {str(item.get("job_id", "")) for item in batch.get("jobs", [])}
        unknown = requested - batch_job_ids
        if unknown:
            raise HTTPException(status_code=400, detail=f"任务不属于当前批次: {sorted(unknown)[0]}")

        selected: list[tuple[Path, str]] = []
        used_names: dict[str, int] = {}
        for item in batch.get("jobs", []):
            job_id = str(item.get("job_id", ""))
            if job_id not in requested:
                continue
            status = self.get_status(job_id)
            result = status.get("result", {})
            if status.get("status") != "completed" or not isinstance(result, dict) or not result.get("exported"):
                raise HTTPException(status_code=400, detail=f"任务还没有可下载的 MP4: {job_id}")
            if status.get("output_deleted"):
                raise HTTPException(status_code=410, detail=f"任务输出已删除: {job_id}")
            path = Path(str(result.get("output_mp4", "")))
            if not path.is_file():
                raise HTTPException(status_code=404, detail=f"MP4 文件不存在: {path}")
            base_name = _safe_download_stem(_job_display_name(status) or path.stem)
            sequence = used_names.get(base_name, 0) + 1
            used_names[base_name] = sequence
            archive_name = f"{base_name}.mp4" if sequence == 1 else f"{base_name}-{sequence:02d}.mp4"
            selected.append((path, archive_name))

        download_id = uuid.uuid4().hex
        archive_root = self.settings.storage_root / "batch_downloads"
        archive_root.mkdir(parents=True, exist_ok=True)
        archive_path = archive_root / f"{download_id}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for path, archive_name in selected:
                archive.write(path, arcname=archive_name)
        return {
            "batch_id": batch_id,
            "download_id": download_id,
            "count": len(selected),
            "size": archive_path.stat().st_size,
            "url": f"/api/batch-downloads/{download_id}",
        }

    def delete_batch_outputs(self, batch_id: str, job_ids: list[str]) -> dict[str, Any]:
        batch = self.get_batch_status(batch_id)
        requested = set(job_ids)
        if not requested:
            raise HTTPException(status_code=400, detail="请至少选择一个结果")
        batch_job_ids = {str(item.get("job_id", "")) for item in batch.get("jobs", [])}
        unknown = requested - batch_job_ids
        if unknown:
            raise HTTPException(status_code=400, detail=f"任务不属于当前批次: {sorted(unknown)[0]}")

        deleted: list[str] = []
        skipped: list[dict[str, str]] = []
        for job_id in job_ids:
            status_path = _job_dir(self.settings, job_id) / "status.json"
            status = _read_json(status_path)
            if status.get("status") != "completed":
                skipped.append({"job_id": job_id, "reason": "任务尚未完成"})
                continue
            result = status.get("result", {})
            if not isinstance(result, dict):
                skipped.append({"job_id": job_id, "reason": "任务没有输出结果"})
                continue
            if result.get("external_output"):
                skipped.append({"job_id": job_id, "reason": "本机指定目录中的文件不由系统删除"})
                continue

            deletion = _delete_managed_job_artifacts(self.settings, result)

            status["output_deleted"] = True
            status["output_deleted_at"] = _now()
            status["output_delete_reason"] = "user_requested"
            _write_json(status_path, status)
            if deletion["deleted_files"] or deletion["deleted_directories"]:
                deleted.append(job_id)
            else:
                skipped.append({"job_id": job_id, "reason": "输出不存在或不在受管目录内"})
        return {"batch_id": batch_id, "deleted": deleted, "skipped": skipped}

    def get_status(self, job_id: str) -> dict[str, Any]:
        status_path = _job_dir(self.settings, job_id) / "status.json"
        if not status_path.exists():
            raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")

        stored_status = self.store.get_status(job_id) if hasattr(self, "store") else None
        status = stored_status or _read_json(status_path)
        if stored_status is not None:
            _write_json(status_path, status)
        status = _recover_legacy_export_success(self.settings, job_id, status, status_path)
        if status.get("status") == "pending":
            position = self._queue_position(job_id)
            if position is not None:
                status["queue_position"] = position
                status["queue_size"] = self.pending_count()
        return status

    def pending_count(self) -> int:
        if hasattr(self, "store"):
            return self.store.pending_count()
        with self._lock:
            return len(self._pending)

    def _queue_position(self, job_id: str) -> int | None:
        if hasattr(self, "store"):
            return self.store.queue_position(job_id)
        with self._lock:
            try:
                return self._pending.index(job_id) + 1
            except ValueError:
                return None

    def _worker_loop(self) -> None:
        with initialize_ui_automation_in_current_thread():
            while True:
                signal_job_id = self._queue.get()
                try:
                    claimed = self.store.claim_job(
                        "embedded-local", lease_seconds=self.settings.agent_lease_seconds
                    )
                    if claimed is not None:
                        self._run_job(str(claimed["job_id"]), already_claimed=True)
                finally:
                    self._queue.task_done()

    def _run_job(self, job_id: str, *, already_claimed: bool = False) -> None:
        with self._lock:
            if job_id in self._pending:
                self._pending.remove(job_id)

        job_dir = _job_dir(self.settings, job_id)
        status_path = job_dir / "status.json"
        previous = _read_json(status_path)
        if previous.get("status") == "cancelled":
            return
        if already_claimed and hasattr(self, "store"):
            running_status = self.store.get_status(job_id) or previous
        else:
            running_status = {
                "job_id": job_id,
                **_batch_status_fields(previous),
                "status": "running",
                "created_at": previous.get("created_at", _now()),
                "queued_at": previous.get("queued_at", previous.get("created_at", _now())),
                "started_at": _now(),
            }
        job_payload = _read_json(job_dir / "job.json")
        _write_json(status_path, running_status)
        observability = job_payload.get("observability", {})
        if not isinstance(observability, dict):
            observability = {}
        event_context = {
            key: observability.get(key)
            for key in ("project_id", "item_id", "operation_id", "correlation_id")
        }
        log_event(
            render_logger,
            "render.job_started",
            "本地渲染任务开始",
            component="render",
            job_id=job_id,
            **event_context,
        )
        print(f"[render-job] 开始任务 job_id={job_id}", flush=True)

        try:
            result = run_render_job(job_payload)
            result_data = result.as_dict()
            output_config = job_payload.get("output", {})
            if isinstance(output_config, dict) and output_config.get("external_output"):
                result_data["external_output"] = True
            completed_status = {
                **running_status,
                "status": "completed",
                "finished_at": _now(),
                "expires_at": _expiry_after(self.settings.completed_output_retention_hours),
                "draft_expires_at": _expiry_after(self.settings.draft_retention_hours),
                "metadata_expires_at": _expiry_after(self.settings.metadata_retention_days * 24),
                "result": result_data,
            }
            _write_json(status_path, completed_status)
            if hasattr(self, "store"):
                if already_claimed:
                    self.store.finish_job("embedded-local", job_id, result=result_data)
                self.store.set_status(job_id, completed_status)
            print(
                f"[render-job] 任务完成 job_id={job_id} exported={result.exported} "
                f"output={result.output_mp4 or result.output_draft_dir}",
                flush=True,
            )
            log_event(
                render_logger,
                "render.job_completed",
                "本地渲染任务完成",
                component="render",
                job_id=job_id,
                exported=bool(result.exported),
                **event_context,
            )
        except Exception as exc:
            failed_status = {
                **running_status,
                "status": "failed",
                "finished_at": _now(),
                "expires_at": _expiry_after(self.settings.failed_output_retention_hours),
                "draft_expires_at": _expiry_after(self.settings.draft_retention_hours),
                "metadata_expires_at": _expiry_after(self.settings.metadata_retention_days * 24),
                "error": str(exc),
            }
            _write_json(status_path, failed_status)
            if hasattr(self, "store"):
                if already_claimed:
                    self.store.finish_job("embedded-local", job_id, error=str(exc))
                self.store.set_status(job_id, failed_status)
            print(
                f"[render-job] 任务失败 job_id={job_id}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            traceback.print_exc(file=sys.stderr)
            log_event(
                render_logger,
                "render.job_failed",
                "本地渲染任务失败",
                level=logging.ERROR,
                component="render",
                job_id=job_id,
                error_type=type(exc).__name__,
                **event_context,
            )
        finally:
            _extend_job_media_expiration(self.settings, job_payload)
            batch_id = str(job_payload.get("batch", {}).get("batch_id", "")).strip()
            if batch_id:
                self._cleanup_batch_once_templates(batch_id)

    def _ensure_queue_capacity(self, adding: int) -> None:
        current = self.store.active_count() if hasattr(self, "store") else len(self._pending)
        if current + max(0, int(adding)) > self.settings.max_active_jobs:
            raise HTTPException(
                status_code=429,
                detail=f"任务队列已达到上限 {self.settings.max_active_jobs}，请等待现有任务完成",
            )

    def _import_legacy_records(self) -> None:
        jobs_root = self.settings.storage_root / "jobs"
        if not jobs_root.exists():
            return
        batches_root = self.settings.storage_root / "batches"
        if batches_root.exists():
            for batch_path in batches_root.glob("*/batch.json"):
                try:
                    self.store.add_batch(_read_json(batch_path))
                except Exception:
                    continue
        for status_path in jobs_root.glob("*/status.json"):
            job_path = status_path.parent / "job.json"
            if not job_path.is_file():
                continue
            try:
                self.store.import_legacy_job(
                    status_path.parent.name,
                    _read_json(job_path),
                    _read_json(status_path),
                    replace_existing=self.execution_mode == "embedded",
                )
            except Exception:
                continue

    def register_agent(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.register_agent(agent_id, payload)

    def heartbeat_agent(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.heartbeat_agent(agent_id, payload)

    def list_agents(self) -> list[dict[str, Any]]:
        if self.execution_mode == "embedded":
            try:
                self.store.heartbeat_agent("embedded-local", {"state": "embedded"})
            except KeyError:
                pass
        return self.store.list_agents()

    def claim_agent_job(self, agent_id: str) -> dict[str, Any] | None:
        if self.execution_mode != "agent":
            raise HTTPException(
                status_code=409,
                detail="中央服务当前使用内置处理模式；请设置 JYD_EXECUTION_MODE=agent",
            )
        claimed = self.store.claim_job(
            agent_id, lease_seconds=self.settings.agent_lease_seconds
        )
        if claimed is not None:
            _write_json(
                _job_dir(self.settings, str(claimed["job_id"])) / "status.json",
                claimed["status"],
            )
        return claimed

    def heartbeat_agent_job(
        self, agent_id: str, job_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        status = self.store.heartbeat_job(
            agent_id,
            job_id,
            payload,
            lease_seconds=self.settings.agent_lease_seconds,
        )
        _write_json(_job_dir(self.settings, job_id) / "status.json", status)
        return status

    def finish_agent_job(
        self,
        agent_id: str,
        job_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        status = self.store.finish_job(agent_id, job_id, result=result, error=error)
        status.update(
            {
                "expires_at": _expiry_after(
                    self.settings.completed_output_retention_hours
                    if result is not None
                    else self.settings.failed_output_retention_hours
                ),
                "draft_expires_at": _expiry_after(self.settings.draft_retention_hours),
                "metadata_expires_at": _expiry_after(
                    self.settings.metadata_retention_days * 24
                ),
            }
        )
        self.store.set_status(job_id, status)
        _write_json(_job_dir(self.settings, job_id) / "status.json", status)
        job_path = _job_dir(self.settings, job_id) / "job.json"
        if job_path.is_file():
            _extend_job_media_expiration(self.settings, _read_json(job_path))
        batch_id = str(status.get("batch_id", "")).strip()
        if batch_id:
            self._cleanup_batch_once_templates(batch_id)
        return status

    def _cleanup_batch_once_templates(
        self,
        batch_id: str,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return _cleanup_finished_batch_once_templates(
            self.settings, batch_id, now=now, force=force
        )

    def _mark_interrupted_jobs(self) -> None:
        if getattr(self, "execution_mode", "embedded") == "agent":
            return
        jobs_root = self.settings.storage_root / "jobs"
        if not jobs_root.exists():
            return
        for status_path in jobs_root.glob("*/status.json"):
            try:
                status = _read_json(status_path)
            except Exception:
                continue
            if status.get("status") not in {"pending", "running"}:
                continue
            status["status"] = "failed"
            status["finished_at"] = _now()
            status["expires_at"] = _expiry_after(self.settings.failed_output_retention_hours)
            status["draft_expires_at"] = _expiry_after(self.settings.draft_retention_hours)
            status["metadata_expires_at"] = _expiry_after(self.settings.metadata_retention_days * 24)
            status["error"] = "服务重启，内存队列中的任务已中断，请重新提交。"
            _write_json(status_path, status)


class StorageLifecycleManager:
    def __init__(
        self,
        settings: WebApiSettings,
        task_store: SQLiteTaskStore | None = None,
        *,
        asset_admin: AssetAdminCatalog | None = None,
        audio_catalog: CombinedAudioCatalog | None = None,
    ):
        self.settings = settings
        self.task_store = task_store
        self.asset_admin = asset_admin or AssetAdminCatalog(settings.storage_root / "asset_admin.json")
        self.audio_catalog = audio_catalog
        self._stop_event = threading.Event()
        self._cleanup_lock = threading.Lock()
        self._last_report: dict[str, Any] | None = None
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="jyd-storage-cleanup",
            daemon=True,
        )

    def start(self) -> None:
        self.cleanup()
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker.is_alive():
            self._worker.join(timeout=2)

    def status(self) -> dict[str, Any]:
        temporary_roots = {
            "media": self.settings.storage_root / "media",
            "outputs": self.settings.storage_root / "outputs",
            "generated_video_drafts": self.settings.storage_root / "generated_video_drafts",
            "draft_imports": self.settings.storage_root / "draft_imports",
            "batch_downloads": self.settings.storage_root / "batch_downloads",
            "job_metadata": self.settings.storage_root / "jobs",
            "batch_metadata": self.settings.storage_root / "batches",
        }
        usage = {
            name: {"path": str(path), "bytes": _directory_size(path)}
            for name, path in temporary_roots.items()
        }
        return {
            "policy": {
                "media_retention_hours": self.settings.media_retention_hours,
                "template_retention_hours": self.settings.template_retention_hours,
                "draft_retention_hours": self.settings.draft_retention_hours,
                "completed_output_retention_hours": self.settings.completed_output_retention_hours,
                "failed_output_retention_hours": self.settings.failed_output_retention_hours,
                "metadata_retention_days": self.settings.metadata_retention_days,
                "asset_trash_retention_days": self.settings.asset_trash_retention_days,
                "orphan_zip_retention_hours": 24,
                "cleanup_interval_minutes": self.settings.cleanup_interval_minutes,
            },
            "temporary_usage": usage,
            "temporary_total_bytes": sum(item["bytes"] for item in usage.values()),
            "permanent_roots": {
                "templates": str(self.settings.template_library_root),
                "audio": str(self.settings.audio_library_root),
                "fonts": str(FONT_LIBRARY_ROOT),
                "text_styles": str(TEXT_STYLE_LIBRARY_ROOT),
                "effects": str(EFFECT_LIBRARY_ROOT),
                "text_effects": str(TEXT_EFFECT_LIBRARY_ROOT),
                "text_templates": str(TEXT_TEMPLATE_LIBRARY_ROOT),
                "stickers": str(STICKER_LIBRARY_ROOT),
                "asset_admin": str(self.settings.storage_root / "asset_admin.json"),
            },
            "last_cleanup": self._last_report,
        }

    def cleanup(self, *, dry_run: bool = False, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now()
        report: dict[str, Any] = {
            "started_at": current.isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "initialized_job_expirations": 0,
            "initialized_draft_expirations": 0,
            "initialized_media_expirations": 0,
            "initialized_template_expirations": 0,
            "initialized_metadata_expirations": 0,
            "expired_jobs": 0,
            "expired_drafts": 0,
            "deleted_files": 0,
            "deleted_directories": 0,
            "deleted_bytes": 0,
            "deleted_media": 0,
            "deleted_templates": 0,
            "purged_assets": 0,
            "skipped_active_assets": 0,
            "deleted_batch_once_templates": 0,
            "deleted_draft_import_packages": 0,
            "deleted_archives": 0,
            "deleted_job_metadata": 0,
            "deleted_batch_metadata": 0,
            "errors": [],
        }
        with self._cleanup_lock:
            self._cleanup_job_drafts(current, dry_run, report)
            self._cleanup_jobs(current, dry_run, report)
            self._cleanup_media(current, dry_run, report)
            self._cleanup_batch_once_templates(current, dry_run, report)
            self._cleanup_deleted_assets(current, dry_run, report)
            self._cleanup_templates(current, dry_run, report)
            self._cleanup_draft_import_packages(current, dry_run, report)
            self._cleanup_archives(current, dry_run, report)
            self._cleanup_metadata(current, dry_run, report)
            report["finished_at"] = datetime.now().isoformat(timespec="seconds")
            if not dry_run:
                self._last_report = report
        return report

    def _cleanup_batch_once_templates(
        self,
        now: datetime,
        dry_run: bool,
        report: dict[str, Any],
    ) -> None:
        batches_root = self.settings.storage_root / "batches"
        if not batches_root.exists():
            return
        for batch_path in batches_root.glob("*/batch.json"):
            try:
                batch = _read_json(batch_path)
                if not batch.get("temporary_template_ids"):
                    continue
                result = _cleanup_finished_batch_once_templates(
                    self.settings,
                    str(batch.get("batch_id") or batch_path.parent.name),
                    dry_run=dry_run,
                    now=now,
                )
                report["deleted_batch_once_templates"] += int(
                    result.get("deleted_templates", 0)
                )
                report["deleted_templates"] += int(result.get("deleted_templates", 0))
                _merge_deletion_report(report, result)
            except Exception as exc:
                _append_cleanup_error(report, batch_path, exc)

    def _cleanup_job_drafts(self, now: datetime, dry_run: bool, report: dict[str, Any]) -> None:
        jobs_root = self.settings.storage_root / "jobs"
        if not jobs_root.exists():
            return
        for status_path in jobs_root.glob("*/status.json"):
            try:
                status = _read_json(status_path)
                if str(status.get("status", "")) not in {"completed", "failed", "cancelled"}:
                    continue
                expires_at = _parse_timestamp(status.get("draft_expires_at"))
                if expires_at is None:
                    expires_at = now + timedelta(hours=self.settings.draft_retention_hours)
                    report["initialized_draft_expirations"] += 1
                    if not dry_run:
                        status["draft_expires_at"] = expires_at.isoformat(timespec="seconds")
                        self._persist_status(status_path, status)
                    continue
                if expires_at > now or status.get("draft_deleted"):
                    continue

                result = status.get("result", {})
                job_path = status_path.parent / "job.json"
                job = _read_json(job_path) if job_path.is_file() else {}
                deletion = _delete_managed_draft_artifacts(
                    self.settings,
                    result if isinstance(result, dict) else {},
                    job=job if isinstance(job, dict) else {},
                    dry_run=dry_run,
                )
                report["expired_drafts"] += 1
                _merge_deletion_report(report, deletion)
                if not dry_run:
                    status["draft_deleted"] = True
                    status["draft_deleted_at"] = now.isoformat(timespec="seconds")
                    status["draft_delete_reason"] = "retention_expired"
                    self._persist_status(status_path, status)
            except Exception as exc:
                _append_cleanup_error(report, status_path, exc)

    def _cleanup_jobs(self, now: datetime, dry_run: bool, report: dict[str, Any]) -> None:
        jobs_root = self.settings.storage_root / "jobs"
        if not jobs_root.exists():
            return
        for status_path in jobs_root.glob("*/status.json"):
            try:
                status = _read_json(status_path)
                current_status = str(status.get("status", ""))
                if current_status not in {"completed", "failed", "cancelled"}:
                    continue
                result = status.get("result", {})
                if isinstance(result, dict) and result.get("external_output"):
                    continue
                expires_at = _parse_timestamp(status.get("expires_at"))
                if expires_at is None:
                    retention = (
                        self.settings.completed_output_retention_hours
                        if current_status == "completed"
                        else self.settings.failed_output_retention_hours
                    )
                    expires_at = now + timedelta(hours=retention)
                    report["initialized_job_expirations"] += 1
                    if not dry_run:
                        status["expires_at"] = expires_at.isoformat(timespec="seconds")
                        self._persist_status(status_path, status)
                    continue
                if expires_at > now or status.get("output_deleted"):
                    continue
                report["expired_jobs"] += 1
                deletion = _delete_managed_job_artifacts(
                    self.settings,
                    result if isinstance(result, dict) else {},
                    job=_read_json(status_path.parent / "job.json")
                    if (status_path.parent / "job.json").is_file()
                    else {},
                    include_drafts=False,
                    dry_run=dry_run,
                )
                report["deleted_files"] += deletion["deleted_files"]
                report["deleted_directories"] += deletion["deleted_directories"]
                report["deleted_bytes"] += deletion["deleted_bytes"]
                if not dry_run:
                    status["output_deleted"] = True
                    status["output_deleted_at"] = now.isoformat(timespec="seconds")
                    status["output_delete_reason"] = "retention_expired"
                    self._persist_status(status_path, status)
            except Exception as exc:
                _append_cleanup_error(report, status_path, exc)

    def _cleanup_media(self, now: datetime, dry_run: bool, report: dict[str, Any]) -> None:
        records_root = self.settings.storage_root / "media" / "records"
        if not records_root.exists():
            return
        active_media_ids = _active_job_media_ids(self.settings)
        media_root = (self.settings.storage_root / "media").resolve()
        for record_path in records_root.glob("*.json"):
            try:
                record = _read_json(record_path)
                media_id = str(record.get("media_id", ""))
                expires_at = _parse_timestamp(record.get("expires_at"))
                if expires_at is None:
                    expires_at = now + timedelta(hours=self.settings.media_retention_hours)
                    report["initialized_media_expirations"] += 1
                    if not dry_run:
                        record["expires_at"] = expires_at.isoformat(timespec="seconds")
                        _write_json(record_path, record)
                    continue
                if expires_at > now or media_id in active_media_ids:
                    continue
                media_path = Path(str(record.get("path", ""))).resolve()
                if media_path.is_file() and _is_relative_to(media_path, media_root):
                    report["deleted_bytes"] += media_path.stat().st_size
                    report["deleted_files"] += 1
                    if not dry_run:
                        media_path.unlink()
                report["deleted_media"] += 1
                if not dry_run:
                    record_path.unlink(missing_ok=True)
            except Exception as exc:
                _append_cleanup_error(report, record_path, exc)

    def _cleanup_deleted_assets(
        self,
        now: datetime,
        dry_run: bool,
        report: dict[str, Any],
    ) -> None:
        cutoff = now - timedelta(days=self.settings.asset_trash_retention_days)
        audio_catalog = self.audio_catalog or CombinedAudioCatalog(
            _library_roots(self.settings, self.settings.audio_library_root, "audio_library")
        )
        groups = _raw_admin_asset_groups(self.settings, audio_catalog)
        sources = {
            (kind, str(item.get("identity", ""))): {**item, "kind": kind}
            for kind, items in groups.items()
            for item in items
            if isinstance(item, dict) and item.get("identity")
        }
        active_template_ids = _active_job_template_ids(self.settings)
        active_job_text = _active_job_reference_text(self.settings)
        for record in self.asset_admin.deleted_records():
            kind = str(record.get("kind", ""))
            identity = str(record.get("identity", ""))
            deleted_at = _parse_timestamp(record.get("deleted_at"))
            if deleted_at is None or deleted_at > cutoff:
                continue
            source = sources.get((kind, identity))
            try:
                if kind == "template" and identity in active_template_ids:
                    report["skipped_active_assets"] += 1
                    continue
                references = [identity]
                if source:
                    references.extend(
                        str(source.get(key, ""))
                        for key in ("path", "absolute_path", "root_dir")
                    )
                if any(value and value in active_job_text for value in references):
                    report["skipped_active_assets"] += 1
                    continue
                if source:
                    deletion = _purge_asset_storage(
                        self.settings,
                        audio_catalog,
                        source,
                        dry_run=dry_run,
                    )
                    _merge_deletion_report(report, deletion)
                report["purged_assets"] += 1
                if not dry_run:
                    self.asset_admin.mark_purged(kind, identity)
            except Exception as exc:
                _append_cleanup_error(
                    report,
                    Path(str(source.get("path", ""))) if source else self.asset_admin.path,
                    exc,
                )

    def _cleanup_templates(self, now: datetime, dry_run: bool, report: dict[str, Any]) -> None:
        library = TemplateLibrary(self.settings.template_library_root)
        active_template_ids = _active_job_template_ids(self.settings)
        trashed_template_ids = {
            str(item.get("identity", ""))
            for item in self.asset_admin.deleted_records()
            if item.get("kind") == "template"
        }
        library_root = self.settings.template_library_root.resolve()
        records_root = (self.settings.storage_root / "draft_imports" / "records").resolve()
        for record in library.list():
            if record.import_info.get("source") != "local_collector":
                continue
            if record.template_id in trashed_template_ids:
                continue
            try:
                expires_at = _parse_timestamp(record.expires_at)
                if expires_at is None:
                    expires_at = now + timedelta(hours=self.settings.template_retention_hours)
                    report["initialized_template_expirations"] += 1
                    if not dry_run:
                        meta = _read_json(record.meta_path)
                        meta["expires_at"] = expires_at.isoformat(timespec="seconds")
                        _write_json(record.meta_path, meta)
                    continue
                if expires_at > now or record.template_id in active_template_ids:
                    continue

                paths: list[tuple[Path, Path]] = [(record.root_dir.resolve(), library_root)]
                import_id = str(record.import_info.get("import_id", "")).strip()
                if import_id and import_id.isalnum():
                    paths.append(((records_root / import_id).resolve(), records_root))

                for path, allowed_root in paths:
                    if path == allowed_root or not _is_relative_to(path, allowed_root) or not path.exists():
                        continue
                    report["deleted_bytes"] += (
                        _directory_size(path) if path.is_dir() else path.stat().st_size
                    )
                    if path.is_dir():
                        report["deleted_directories"] += 1
                        if not dry_run:
                            shutil.rmtree(path)
                    else:
                        report["deleted_files"] += 1
                        if not dry_run:
                            path.unlink()
                report["deleted_templates"] += 1
            except Exception as exc:
                _append_cleanup_error(report, record.meta_path, exc)

    def _cleanup_draft_import_packages(
        self,
        now: datetime,
        dry_run: bool,
        report: dict[str, Any],
    ) -> None:
        incoming_root = self.settings.storage_root / "draft_imports" / "incoming"
        if not incoming_root.exists():
            return
        cutoff = now - timedelta(hours=self.settings.template_retention_hours)
        for package_path in incoming_root.glob("*.zip"):
            try:
                if datetime.fromtimestamp(package_path.stat().st_mtime) > cutoff:
                    continue
                report["deleted_bytes"] += package_path.stat().st_size
                report["deleted_files"] += 1
                report["deleted_draft_import_packages"] += 1
                if not dry_run:
                    package_path.unlink()
            except Exception as exc:
                _append_cleanup_error(report, package_path, exc)

    def _cleanup_archives(self, now: datetime, dry_run: bool, report: dict[str, Any]) -> None:
        archive_root = self.settings.storage_root / "batch_downloads"
        if not archive_root.exists():
            return
        cutoff = now - timedelta(hours=24)
        for archive_path in archive_root.glob("*.zip"):
            try:
                modified_at = datetime.fromtimestamp(archive_path.stat().st_mtime)
                if modified_at > cutoff:
                    continue
                report["deleted_bytes"] += archive_path.stat().st_size
                report["deleted_files"] += 1
                report["deleted_archives"] += 1
                if not dry_run:
                    archive_path.unlink()
            except Exception as exc:
                _append_cleanup_error(report, archive_path, exc)

    def _cleanup_metadata(self, now: datetime, dry_run: bool, report: dict[str, Any]) -> None:
        terminal_statuses = {"completed", "failed", "cancelled"}
        batches_root = self.settings.storage_root / "batches"
        if batches_root.exists():
            for batch_path in batches_root.glob("*/batch.json"):
                try:
                    batch = _read_json(batch_path)
                    job_ids = [
                        str(item.get("job_id", ""))
                        for item in batch.get("jobs", [])
                        if isinstance(item, dict) and item.get("job_id")
                    ]
                    statuses: list[tuple[Path, dict[str, Any]]] = []
                    for job_id in job_ids:
                        status_path = _job_dir(self.settings, job_id) / "status.json"
                        if not status_path.exists():
                            statuses = []
                            break
                        statuses.append((status_path, _read_json(status_path)))
                    if not statuses or any(item.get("status") not in terminal_statuses for _, item in statuses):
                        continue
                    expires_at = _parse_timestamp(batch.get("metadata_expires_at"))
                    if expires_at is None:
                        expires_at = now + timedelta(days=self.settings.metadata_retention_days)
                        report["initialized_metadata_expirations"] += 1
                        if not dry_run:
                            batch["metadata_expires_at"] = expires_at.isoformat(timespec="seconds")
                            _write_json(batch_path, batch)
                        continue
                    if expires_at > now:
                        continue
                    for status_path, status in statuses:
                        result = status.get("result", {})
                        deletion = _delete_managed_job_artifacts(
                            self.settings,
                            result if isinstance(result, dict) else {},
                            dry_run=dry_run,
                        )
                        _merge_deletion_report(report, deletion)
                        job_dir = status_path.parent
                        report["deleted_bytes"] += _directory_size(job_dir)
                        report["deleted_job_metadata"] += 1
                        if not dry_run:
                            shutil.rmtree(job_dir)
                    batch_dir = batch_path.parent
                    report["deleted_bytes"] += _directory_size(batch_dir)
                    report["deleted_batch_metadata"] += 1
                    if not dry_run:
                        shutil.rmtree(batch_dir)
                        if self.task_store is not None:
                            self.task_store.delete_batch(str(batch.get("batch_id") or batch_dir.name))
                except Exception as exc:
                    _append_cleanup_error(report, batch_path, exc)

        jobs_root = self.settings.storage_root / "jobs"
        if not jobs_root.exists():
            return
        for status_path in jobs_root.glob("*/status.json"):
            try:
                status = _read_json(status_path)
                if status.get("batch_id") or status.get("status") not in terminal_statuses:
                    continue
                expires_at = _parse_timestamp(status.get("metadata_expires_at"))
                if expires_at is None:
                    expires_at = now + timedelta(days=self.settings.metadata_retention_days)
                    report["initialized_metadata_expirations"] += 1
                    if not dry_run:
                        status["metadata_expires_at"] = expires_at.isoformat(timespec="seconds")
                        self._persist_status(status_path, status)
                    continue
                if expires_at > now:
                    continue
                result = status.get("result", {})
                deletion = _delete_managed_job_artifacts(
                    self.settings,
                    result if isinstance(result, dict) else {},
                    dry_run=dry_run,
                )
                _merge_deletion_report(report, deletion)
                job_dir = status_path.parent
                report["deleted_bytes"] += _directory_size(job_dir)
                report["deleted_job_metadata"] += 1
                if not dry_run:
                    job_id = job_dir.name
                    shutil.rmtree(job_dir)
                    if self.task_store is not None:
                        self.task_store.delete_job(job_id)
            except Exception as exc:
                _append_cleanup_error(report, status_path, exc)

    def _worker_loop(self) -> None:
        interval_seconds = max(60, self.settings.cleanup_interval_minutes * 60)
        while not self._stop_event.wait(interval_seconds):
            try:
                self.cleanup()
            except Exception:
                continue

    def _persist_status(self, path: Path, status: dict[str, Any]) -> None:
        _write_json(path, status)
        if self.task_store is not None:
            try:
                self.task_store.set_status(path.parent.name, status)
            except KeyError:
                pass


def _is_admin_protected_path(path: str) -> bool:
    if path in {"/admin", "/admin/login", "/local-admin/login"}:
        return False
    if path in {"/api/admin/login", "/api/admin/logout", "/api/admin/session"}:
        return False
    if path in {"/app/advanced", "/app/assets", "/openapi.json"}:
        return True
    if path.startswith(("/docs", "/redoc", "/api/admin/", "/api/storage", "/api/drafts")):
        return True
    if path.startswith("/api/batches/") and path.endswith("/delete-outputs"):
        return True
    return path in {
        "/api/agents",
        "/api/templates/import",
        "/api/audio-library/categories",
        "/api/audio-library/assign",
    }


def _is_site_protected_path(path: str) -> bool:
    if path.startswith("/api/runtime/pages"):
        return False
    if path == "/app/new/login":
        return False
    if path in {
        "/api/health",
        "/api/admin/login",
        "/api/admin/logout",
        "/api/admin/session",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/session",
        "/api/auth/handoff",
        "/api/auth/local-handoff",
        "/api/auth/center/login",
        "/api/auth/center/verify",
        "/api/auth/center/handoff",
        "/api/runtime/status",
        "/api/runtime/shutdown",
    }:
        return False
    if path.startswith("/api/agents/"):
        return False
    return (
        path == "/app"
        or path == "/app/new"
        or path.startswith("/app/new/")
        or path.startswith("/api/")
    )


def _safe_admin_next(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("/") and not candidate.startswith("//"):
        if _is_admin_protected_path(candidate) or _is_site_protected_path(candidate):
            return candidate
    return "/app/assets"


def _safe_site_next(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("/") and not candidate.startswith("//"):
        if _is_site_protected_path(candidate) and not _is_admin_protected_path(candidate):
            return candidate
    return "/app"


def _safe_peer_next(value: str, *, fallback: str = "/") -> str:
    candidate = value.strip()
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return fallback


def _workbench_environment(auth_server_url: str) -> str:
    hostname = (urlparse(auth_server_url).hostname or "").lower()
    return "local" if hostname in {"127.0.0.1", "localhost", "::1"} else "production"


def _default_ltx_workbench_url(auth_server_url: str) -> str:
    port = 8792 if _workbench_environment(auth_server_url) == "local" else 8791
    return f"http://127.0.0.1:{port}"


def default_settings() -> WebApiSettings:
    storage_root = Path(os.environ.get("JYD_WEB_STORAGE_ROOT", PROJECT_ROOT / "data" / "web_storage")).expanduser().resolve()
    database_path = Path(
        os.environ.get("JYD_DATABASE_PATH", storage_root / "control.db")
    ).expanduser().resolve()
    template_library_root = Path(
        os.environ.get("JYD_TEMPLATE_LIBRARY_ROOT", PROJECT_ROOT / "data" / "template_library")
    ).expanduser().resolve()
    default_draft_root = detect_jianying_draft_root(
        os.environ.get("JYD_WEB_DRAFT_ROOT", ""),
        fallback=PROJECT_ROOT / "runtime" / "web_drafts",
    )
    audio_library_root = Path(
        os.environ.get("JYD_AUDIO_LIBRARY_ROOT", LIBRARIES_ROOT / "audio_library")
    ).expanduser().resolve()
    return WebApiSettings(
        storage_root=storage_root,
        template_library_root=template_library_root,
        default_draft_root=default_draft_root,
        audio_library_root=audio_library_root,
        result_library_root=Path(
            os.environ.get("JYD_RESULT_LIBRARY_ROOT", "D:/auto")
        ).expanduser().resolve(),
        media_retention_hours=_env_positive_int("JYD_MEDIA_RETENTION_HOURS", 24),
        template_retention_hours=_env_positive_int("JYD_TEMPLATE_RETENTION_HOURS", 48),
        draft_retention_hours=_env_positive_int("JYD_DRAFT_RETENTION_HOURS", 48),
        completed_output_retention_hours=_env_positive_int("JYD_OUTPUT_RETENTION_HOURS", 72),
        failed_output_retention_hours=_env_positive_int("JYD_FAILED_RETENTION_HOURS", 24),
        metadata_retention_days=_env_positive_int("JYD_METADATA_RETENTION_DAYS", 30),
        asset_trash_retention_days=_env_positive_int("JYD_ASSET_TRASH_RETENTION_DAYS", 7),
        cleanup_interval_minutes=_env_positive_int("JYD_CLEANUP_INTERVAL_MINUTES", 30),
        admin_username=os.environ.get("JYD_ADMIN_USERNAME", "admin").strip() or "admin",
        admin_password=os.environ.get("JYD_ADMIN_PASSWORD", "admin123").strip()
        or "admin123",
        admin_session_secret=os.environ.get("JYD_ADMIN_SESSION_SECRET", "").strip(),
        admin_session_hours=_env_positive_int("JYD_ADMIN_SESSION_HOURS", 12),
        admin_cookie_secure=_as_bool(os.environ.get("JYD_ADMIN_COOKIE_SECURE", "false")),
        admin_cookie_name=os.environ.get("JYD_ADMIN_COOKIE_NAME", "jyd_admin_session").strip()
        or "jyd_admin_session",
        site_username=os.environ.get("JYD_SITE_USERNAME", "operator").strip() or "operator",
        site_password=os.environ.get("JYD_SITE_PASSWORD", "operator123").strip()
        or "operator123",
        site_session_secret=os.environ.get("JYD_SITE_SESSION_SECRET", "").strip(),
        site_cookie_name=os.environ.get("JYD_SITE_COOKIE_NAME", "jyd_site_session").strip()
        or "jyd_site_session",
        execution_mode=os.environ.get("JYD_EXECUTION_MODE", "embedded").strip().lower()
        or "embedded",
        agent_token=os.environ.get("JYD_AGENT_TOKEN", "").strip(),
        agent_lease_seconds=_env_positive_int("JYD_AGENT_LEASE_SECONDS", 180),
        max_active_jobs=_env_positive_int("JYD_MAX_ACTIVE_JOBS", 500),
        max_video_upload_bytes=_env_positive_int(
            "JYD_MAX_VIDEO_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024
        ),
        max_audio_upload_bytes=_env_positive_int(
            "JYD_MAX_AUDIO_UPLOAD_BYTES", 200 * 1024 * 1024
        ),
        max_draft_import_bytes=_env_positive_int(
            "JYD_MAX_DRAFT_IMPORT_BYTES", 5 * 1024 * 1024 * 1024
        ),
        database_path=database_path,
        allow_local_file_access=_as_bool(
            os.environ.get("JYD_ALLOW_LOCAL_FILE_ACCESS", "false")
        ),
        personal_library_root=Path(
            os.environ.get("JYD_PERSONAL_LIBRARY_ROOT", PROJECT_ROOT / "data" / "personal_libraries")
        ).expanduser().resolve(),
        bootstrap_site_user=(storage_root / "users.json").is_file()
        or (storage_root / "access_password.txt").is_file(),
        auth_server_url=os.environ.get(
            "JYD_AUTH_SERVER_URL", "http://127.0.0.1:8000"
        ).strip()
        or "http://127.0.0.1:8000",
        shared_processor_url=os.environ.get(
            "JYD_SHARED_PROCESSOR_URL", ""
        ).strip(),
        ltx_workbench_url=os.environ.get("JYD_LTX_WORKBENCH_URL", "").strip(),
        auth_authority=_as_bool(os.environ.get("JYD_AUTH_AUTHORITY", "false")),
        auth_timeout_seconds=_env_positive_int("JYD_AUTH_TIMEOUT_SECONDS", 15),
        asr_base_url=os.environ.get(
            "JYD_ASR_BASE_URL", "http://127.0.0.1:18084"
        ).strip(),
        asr_timeout_seconds=_env_positive_int("JYD_ASR_TIMEOUT_SECONDS", 1800),
        asr_shared_token=os.environ.get("JYD_ASR_SHARED_TOKEN", "").strip(),
        asr_required=_as_bool(os.environ.get("JYD_ASR_REQUIRED", "true")),
    )


def create_app(settings: WebApiSettings | None = None) -> FastAPI:
    # Windows may otherwise expose WOFF2 files as text/plain, which causes
    # browsers to reject the locally bundled Font Awesome icon font.
    mimetypes.add_type("font/woff2", ".woff2")
    settings = settings or default_settings()
    print(f"[JYD] 剪映草稿目录: {settings.default_draft_root}", flush=True)
    app = FastAPI(title="Jianying Render API", version="0.1.0")
    runtime_control = RuntimeControl()
    semantic_visual_catalog = load_semantic_visual_catalog(
        SEMANTIC_VISUAL_LIBRARY_ROOT
    )
    admin_auth = AdminAuth(
        settings.storage_root,
        username=settings.admin_username,
        password=settings.admin_password,
        session_secret=settings.admin_session_secret,
        session_hours=settings.admin_session_hours,
        secure_cookie=settings.admin_cookie_secure,
        cookie_name=settings.admin_cookie_name,
    )
    collector_auth = AdminAuth(
        settings.storage_root,
        username=settings.site_username,
        password=settings.site_password,
        session_secret=settings.site_session_secret or secrets.token_urlsafe(32),
        cookie_name="jyd_collector_unused",
        password_filename="access_password.txt",
        secret_filename="collector_access_secret.txt",
    )
    site_auth = UserAuth(
        settings.storage_root,
        initial_username=settings.site_username,
        initial_password=settings.site_password,
        session_secret=settings.site_session_secret,
        session_hours=settings.admin_session_hours,
        secure_cookie=settings.admin_cookie_secure,
        cookie_name=settings.site_cookie_name,
        create_initial=settings.auth_authority and settings.bootstrap_site_user,
    )
    auth_center = None if settings.auth_authority else AuthCenterClient(
        settings.auth_server_url, timeout_seconds=settings.auth_timeout_seconds
    )
    auth_handoffs = AuthHandoffStore(lifetime_seconds=60)
    template_import_tickets = AuthHandoffStore(lifetime_seconds=600)
    if admin_auth.generated_password:
        print(
            f"[JYD ADMIN] 初始管理员账号: {admin_auth.username}，"
            f"密码已写入: {admin_auth.password_file}"
        )
    if site_auth.initial_user_created:
        print(
            f"[JYD ACCESS] 已创建首个内测账号: {settings.site_username}，"
            f"账号库: {site_auth.users_path}"
        )
    asset_admin = AssetAdminCatalog(settings.storage_root / "asset_admin.json")
    agent_token = settings.agent_token or _load_or_create_text_secret(
        settings.storage_root / "agent_token.txt"
    )
    render_queue = RenderJobQueue(settings)
    project_store = ProjectStore(render_queue.store.path)
    ltx_workbench_client = None
    ltx_url = settings.ltx_workbench_url or _default_ltx_workbench_url(
        settings.auth_server_url
    )
    if runtime_control.manager_token and ltx_url:
        try:
            ltx_workbench_client = LtxWorkbenchClient(
                ltx_url, runtime_control.manager_token
            )
        except ValueError as exc:
            print(f"[JYD] LTX 引擎配置无效: {exc}", flush=True)
    user_template_store = UserTemplateStore(
        settings.storage_root / "user_templates",
        libraries_root=LIBRARIES_ROOT,
        max_template_bytes=settings.max_draft_import_bytes,
    )
    if project_store.startup_recovered_analysis_count:
        print(
            "[JYD] 已将 "
            f"{project_store.startup_recovered_analysis_count} 条中断或超时的分析恢复为可重试",
            flush=True,
        )
    visual_refresh = refresh_saved_visual_plans_for_catalog(
        project_store, semantic_visual_catalog
    )
    if visual_refresh["scanned"]:
        print(
            "[JYD] 新版素材目录本地刷新："
            f"重映射 {visual_refresh['remapped']} 条，"
            f"需显式重试 {visual_refresh['retryable']} 条，"
            f"本地刷新异常 {visual_refresh['failed']} 条",
            flush=True,
        )
    if settings.enable_legacy_new_workbench_video_engines:
        recovered_composition_starts = (
            project_store.recover_interrupted_composition_starts()
        )
        if recovered_composition_starts:
            print(
                f"[JYD] 已恢复 {recovered_composition_starts} 条中断的旧 4A 启动操作",
                flush=True,
            )
    else:
        retired_composition_starts = (
            project_store.retire_unstarted_legacy_composition_operations()
        )
        if retired_composition_starts:
            print(
                f"[JYD] 已停止 {retired_composition_starts} 条未进入云端的旧 4A 启动操作",
                flush=True,
            )
    composition_coordinator = (
        ProjectCompositionCoordinator(
            project_store,
            auth_center,
            storage_root=settings.storage_root,
            max_video_bytes=settings.max_video_upload_bytes,
        )
        if auth_center is not None
        else None
    )
    composition_start_dispatcher = (
        ProjectCompositionStartDispatcher(composition_coordinator, max_workers=4)
        if composition_coordinator is not None
        else None
    )
    project_result_library = ProjectResultLibrary(
        project_store,
        settings.result_library_root or (settings.storage_root / "result_library"),
    )
    storage_lifecycle = StorageLifecycleManager(
        settings,
        render_queue.store,
        asset_admin=asset_admin,
        audio_catalog=render_queue.audio_catalog,
    )
    storage_lifecycle.start()
    app.state.storage_lifecycle = storage_lifecycle
    app.state.admin_auth = admin_auth
    app.state.site_auth = site_auth
    app.state.auth_center = auth_center
    app.state.auth_handoffs = auth_handoffs
    app.state.template_import_tickets = template_import_tickets
    app.state.agent_token = agent_token
    app.state.project_store = project_store
    app.state.ltx_workbench_client = ltx_workbench_client
    app.state.user_template_store = user_template_store
    app.state.project_result_library = project_result_library
    app.state.composition_start_dispatcher = composition_start_dispatcher
    app.state.runtime_control = runtime_control

    @app.post("/api/runtime/pages")
    def runtime_page_open() -> dict[str, str]:
        return {"lease_id": runtime_control.open_page()}

    @app.post("/api/runtime/pages/{lease_id}")
    def runtime_page_heartbeat(lease_id: str) -> dict[str, bool]:
        if not runtime_control.touch_page(lease_id):
            raise HTTPException(status_code=404, detail="页面租约已失效")
        return {"ok": True}

    @app.post("/api/runtime/pages/{lease_id}/close")
    def runtime_page_close(lease_id: str) -> dict[str, bool]:
        runtime_control.close_page(lease_id)
        return {"ok": True}

    @app.get("/api/runtime/status")
    def runtime_status() -> dict[str, object]:
        return runtime_control.status()

    @app.post("/api/runtime/shutdown")
    def runtime_shutdown(request: Request) -> dict[str, bool]:
        token = request.headers.get("x-workbench-manager-token", "")
        if not runtime_control.request_shutdown(token):
            raise HTTPException(status_code=403, detail="无权关闭工作台")
        return {"ok": True}

    def is_managed_project_file(path: Path) -> bool:
        resolved = path.resolve()
        return _is_relative_to(resolved, settings.storage_root.resolve()) or _is_relative_to(
            resolved, project_result_library.root.resolve()
        )

    def require_agent_token(request: Request) -> None:
        authorization = request.headers.get("authorization", "")
        supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, agent_token):
            raise HTTPException(status_code=401, detail="处理机令牌无效")

    def has_local_file_access(request: Request) -> bool:
        client_host = request.client.host if request.client else ""
        return settings.allow_local_file_access and client_host in {
            "127.0.0.1", "::1", "localhost"
        }

    def require_local_file_access(request: Request) -> None:
        if not settings.allow_local_file_access:
            raise HTTPException(status_code=404, detail="当前服务未启用本机文件模式")
        if not has_local_file_access(request):
            raise HTTPException(status_code=403, detail="本机文件功能只能从当前电脑操作")

    def verify_site_token(token: str) -> dict[str, Any] | None:
        if settings.auth_authority:
            return site_auth.verify_token(token)
        if auth_center is None:
            return None
        return auth_center.verify(token)

    def set_site_token_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            settings.site_cookie_name,
            token,
            max_age=max(1, settings.admin_session_hours) * 3600,
            httponly=True,
            secure=settings.admin_cookie_secure,
            samesite="lax",
            path="/",
        )

    @app.middleware("http")
    async def require_admin_session(request: Request, call_next):
        path = request.url.path
        admin_required = _is_admin_protected_path(path)
        site_required = _is_site_protected_path(path)
        if not admin_required and not site_required:
            return await call_next(request)
        token = request.cookies.get(admin_auth.cookie_name, "")
        if admin_auth.verify_token(token):
            return await call_next(request)
        if not admin_required:
            site_token = request.cookies.get(site_auth.cookie_name, "")
            try:
                if verify_site_token(site_token) is not None:
                    return await call_next(request)
            except AuthCenterError as exc:
                log_event(
                    workbench_logger,
                    "auth_center.middleware_verify_failed",
                    "请求在进入工作台接口前无法完成数字人网站登录校验",
                    level=logging.ERROR,
                    component="workbench",
                    request_method=request.method,
                    request_path=path,
                    error_code=exc.error_code,
                    http_status=exc.status_code,
                    retryable=exc.retryable,
                    error_summary=str(exc).strip()[:500],
                )
                if path.startswith("/api/"):
                    return JSONResponse({"detail": str(exc)}, status_code=503)
                login_path = "/app/new/login" if path.startswith("/app/new") else "/login"
                return RedirectResponse(
                    f"{login_path}?next={quote(path, safe='/')}&center=offline", status_code=303
                )
        collector_token = request.headers.get("x-jyd-access-token", "")
        if path.startswith("/api/new/jianying-template-imports/"):
            return await call_next(request)
        if (
            path in {"/api/draft-imports", "/api/personal-assets/import"}
            and collector_token
            and hmac.compare_digest(collector_token, collector_auth.password)
        ):
            return await call_next(request)
        if path.startswith("/api/") or path == "/openapi.json":
            return JSONResponse({"detail": "需要管理员登录"}, status_code=401)
        next_path = quote(path, safe="/")
        login_path = (
            "/local-admin/login"
            if admin_required
            else ("/app/new/login" if path.startswith("/app/new") else "/login")
        )
        return RedirectResponse(f"{login_path}?next={next_path}", status_code=303)

    @app.on_event("shutdown")
    def stop_storage_lifecycle() -> None:
        if composition_start_dispatcher is not None:
            composition_start_dispatcher.shutdown()
        storage_lifecycle.stop()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:3000",
            "http://localhost:3000",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_origin_regex=LAN_WEBSITE_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if FRONTEND_ROOT.exists():
        app.mount("/app-static", StaticFiles(directory=str(FRONTEND_ROOT)), name="app-static")

    @app.get("/")
    def root() -> dict[str, Any]:
        return {
            "name": "Jianying Render API",
            "docs": "/docs",
            "app": "/app",
            "new_app": "/app/new",
            "health": "/api/health",
        }

    @app.get("/app")
    def frontend() -> FileResponse:
        index_path = FRONTEND_ROOT / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail=f"前端文件不存在: {index_path}")
        return FileResponse(index_path)

    def new_frontend_file(filename: str) -> FileResponse:
        index_path = NEW_FRONTEND_ROOT / filename
        if not index_path.exists():
            raise HTTPException(status_code=404, detail=f"新版前端文件不存在: {index_path}")
        return FileResponse(index_path)

    @app.get("/app/new")
    def new_frontend() -> FileResponse:
        return new_frontend_file("index.html")

    @app.get("/app/new/")
    def new_frontend_trailing_slash() -> RedirectResponse:
        return RedirectResponse("/app/new", status_code=303)

    @app.get("/app/new/generate")
    def new_generation_frontend() -> FileResponse:
        return new_frontend_file("index.html")

    @app.get("/app/new/gallery")
    def new_gallery_frontend() -> FileResponse:
        return new_frontend_file("gallery.html")

    @app.get("/app/new/voices")
    def new_voice_frontend() -> FileResponse:
        return new_frontend_file("voice-library.html")

    @app.get("/app/new/templates")
    def new_template_frontend() -> FileResponse:
        return new_frontend_file("templates.html")

    @app.get("/app/new/login")
    def new_login_frontend(request: Request):
        site_token = request.cookies.get(site_auth.cookie_name, "")
        admin_token = request.cookies.get(admin_auth.cookie_name, "")
        try:
            site_user = verify_site_token(site_token)
        except AuthCenterError:
            site_user = None
        if site_user is not None or admin_auth.verify_token(admin_token):
            next_path = _safe_site_next(request.query_params.get("next", "/app/new"))
            if not next_path.startswith("/app/new"):
                next_path = "/app/new"
            return RedirectResponse(next_path, status_code=303)
        return new_frontend_file("login.html")

    @app.get("/login")
    def site_login_frontend(request: Request):
        site_token = request.cookies.get(site_auth.cookie_name, "")
        admin_token = request.cookies.get(admin_auth.cookie_name, "")
        try:
            site_user = verify_site_token(site_token)
        except AuthCenterError:
            site_user = None
        if site_user is not None or admin_auth.verify_token(admin_token):
            return RedirectResponse("/app", status_code=303)
        index_path = FRONTEND_ROOT / "site-login.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail=f"内部登录页面不存在: {index_path}")
        return FileResponse(index_path)

    @app.post("/api/auth/login")
    def site_login(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        if settings.auth_authority:
            if not site_auth.list_users():
                raise HTTPException(status_code=403, detail="尚未配置内测账号，请管理员先进入 /admin 创建账号")
            user = site_auth.authenticate(username, password)
            if user is None:
                raise HTTPException(status_code=401, detail="账号或密码错误")
            token = site_auth.issue_token(user)
        else:
            try:
                remote = auth_center.login(username, password) if auth_center else None
            except AuthCenterError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            if remote is None:
                raise HTTPException(status_code=503, detail="统一账号中心未配置")
            user = remote["user"]
            token = remote["access_token"]
        next_path = _safe_site_next(str(payload.get("next", "")))
        response = JSONResponse({"ok": True, "next": next_path, "user": user})
        set_site_token_cookie(response, token)
        return response

    @app.get("/api/auth/session")
    def site_session(request: Request) -> dict[str, Any]:
        try:
            user = verify_site_token(request.cookies.get(settings.site_cookie_name, ""))
            center_online = True
        except AuthCenterError:
            user = None
            center_online = False
        admin_authenticated = admin_auth.verify_token(request.cookies.get(admin_auth.cookie_name, ""))
        workbench_environment = _workbench_environment(settings.auth_server_url)
        return {
            "authenticated": user is not None or admin_authenticated,
            "username": str(user.get("username", "")) if user else (admin_auth.username if admin_authenticated else ""),
            "user": user or {},
            "auth_authority": settings.auth_authority,
            "auth_server_url": settings.auth_server_url,
            "shared_processor_url": settings.shared_processor_url,
            "ltx_workbench_url": settings.ltx_workbench_url
            or _default_ltx_workbench_url(settings.auth_server_url),
            "ltx_workbench_handoff_url": (
                "/api/auth/handoff-to?target=ltx&next=%2F" if user else ""
            ),
            "workbench_environment": workbench_environment,
            "workbench_environment_label": (
                "本地测试" if workbench_environment == "local" else "生产环境"
            ),
            "auth_center_online": center_online,
        }

    def current_project_user(request: Request) -> dict[str, Any]:
        """Return the ordinary digital-human account behind this session.

        Local technical administrators may access maintenance pages, but they
        do not own ordinary users' projects and therefore cannot use the new
        project API without a digital-human account session.
        """

        token = request.cookies.get(settings.site_cookie_name, "")
        try:
            user = verify_site_token(token)
        except AuthCenterError as exc:
            log_event(
                workbench_logger,
                "auth_center.project_user_verify_failed",
                "项目接口内部无法完成数字人网站登录校验",
                level=logging.ERROR,
                component="workbench",
                request_method=request.method,
                request_path=request.url.path,
                error_code=exc.error_code,
                http_status=exc.status_code,
                retryable=exc.retryable,
                error_summary=str(exc).strip()[:500],
            )
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if not isinstance(user, dict):
            raise HTTPException(status_code=401, detail="请先使用数字人账号登录")
        user_id = str(user.get("user_id") or "").strip()
        if not user_id:
            raise HTTPException(status_code=401, detail="数字人账号缺少稳定用户编号")
        return {
            "user_id": user_id,
            "username": str(user.get("username") or "").strip(),
            "is_admin": user.get("is_admin") is True,
        }

    @app.post("/api/new/jianying-template-import-tickets", status_code=201)
    def issue_jianying_template_import_ticket(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            name = user_template_store.validate_new_name(
                user["user_id"], str(payload.get("name") or "")
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        ticket = template_import_tickets.issue(
            json.dumps(
                {
                    "schema": "jyd.template-import-ticket.v1",
                    "user_id": user["user_id"],
                    "name": name,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return {
            "ticket": ticket,
            "expires_in_seconds": template_import_tickets.lifetime_seconds,
            "upload_path": f"/api/new/jianying-template-imports/{ticket}",
        }

    @app.post("/api/new/jianying-template-imports/{ticket}", status_code=201)
    async def import_account_jianying_template(
        ticket: str, request: Request
    ) -> dict[str, Any]:
        ticket_payload = template_import_tickets.consume(ticket)
        if ticket_payload is None:
            raise HTTPException(status_code=401, detail="模板上传凭证无效、已使用或已过期")
        try:
            import_claim = json.loads(ticket_payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=401, detail="模板上传凭证格式错误") from exc
        if (
            not isinstance(import_claim, dict)
            or import_claim.get("schema") != "jyd.template-import-ticket.v1"
            or not str(import_claim.get("user_id") or "").strip()
        ):
            raise HTTPException(status_code=401, detail="模板上传凭证格式错误")

        incoming_root = settings.storage_root / "template_imports" / "incoming"
        incoming_root.mkdir(parents=True, exist_ok=True)
        package_path = incoming_root / f"{uuid.uuid4().hex}.zip"
        digest = hashlib.sha256()
        size = 0
        declared_size = request.headers.get("content-length", "").strip()
        if declared_size:
            try:
                if int(declared_size) > settings.max_draft_import_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "模板迁移包超过限制 "
                            f"{_format_bytes(settings.max_draft_import_bytes)}"
                        ),
                    )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Content-Length 不合法") from exc
        try:
            with package_path.open("wb") as stream:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > settings.max_draft_import_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                "模板迁移包超过限制 "
                                f"{_format_bytes(settings.max_draft_import_bytes)}"
                            ),
                        )
                    digest.update(chunk)
                    stream.write(chunk)
            if size <= 0:
                raise ValueError("上传的模板迁移包为空")
            expected_checksum = request.headers.get("x-package-sha256", "").strip().lower()
            actual_checksum = digest.hexdigest()
            if expected_checksum and expected_checksum != actual_checksum:
                raise ValueError("模板迁移包校验失败，请重新上传")
            template = user_template_store.import_transfer_package(
                str(import_claim["user_id"]),
                str(import_claim.get("name") or ""),
                package_path,
            )
            return {
                "status": "completed",
                "template": template,
                "upload_size_bytes": size,
                "package_checksum_sha256": actual_checksum,
                "website_url": "/app/new/templates",
            }
        except HTTPException:
            raise
        except (
            FileNotFoundError,
            OSError,
            RuntimeError,
            ValueError,
            zipfile.BadZipFile,
        ) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            _unlink_if_exists(package_path)

    @app.get("/api/new/jianying-templates")
    def list_jianying_templates(request: Request) -> dict[str, Any]:
        user = current_project_user(request)
        return {"templates": user_template_store.list(user["user_id"])}

    @app.get("/api/new/jianying-templates/{template_id}/cover")
    def get_jianying_template_cover(template_id: str, request: Request) -> FileResponse:
        user = current_project_user(request)
        try:
            cover = user_template_store.cover_path(user["user_id"], template_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if cover is None:
            raise HTTPException(status_code=404, detail="剪映模板没有封面")
        return FileResponse(cover)

    @app.post("/api/new/jianying-templates", status_code=201)
    def create_jianying_template(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return user_template_store.create(user["user_id"], str(payload.get("name") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/new/jianying-templates/{template_id}/draft-files")
    async def upload_jianying_template_draft_file(
        template_id: str, request: Request, path: str
    ) -> dict[str, Any]:
        user = current_project_user(request)
        content = await request.body()
        if len(content) > settings.max_draft_import_bytes:
            raise HTTPException(status_code=413, detail="单个草稿文件过大")
        try:
            return user_template_store.upload_draft_file(
                user["user_id"], template_id, path, content
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/new/jianying-templates/{template_id}/analyze")
    def analyze_jianying_template(template_id: str, request: Request) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return user_template_store.analyze(user["user_id"], template_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/new/jianying-templates/{template_id}/resource-files")
    async def upload_jianying_template_resource_file(
        template_id: str,
        request: Request,
        resource_key: str,
        path: str,
    ) -> dict[str, Any]:
        user = current_project_user(request)
        content = await request.body()
        if len(content) > settings.max_draft_import_bytes:
            raise HTTPException(status_code=413, detail="单个资源文件过大")
        try:
            return user_template_store.upload_resource_file(
                user["user_id"], template_id, resource_key, path, content
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/new/jianying-templates/{template_id}/resources/complete")
    def complete_jianying_template_resources(
        template_id: str, request: Request
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return user_template_store.complete_resources(user["user_id"], template_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.patch("/api/new/jianying-templates/{template_id}")
    def rename_jianying_template(
        template_id: str, request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return user_template_store.rename(
                user["user_id"], template_id, str(payload.get("name") or "")
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/new/jianying-templates/{template_id}")
    def delete_jianying_template(template_id: str, request: Request) -> dict[str, Any]:
        user = current_project_user(request)
        offset = 0
        while True:
            projects = project_store.list_projects(user["user_id"], limit=100, offset=offset)
            page = projects.get("projects", [])
            for project in page:
                binding = dict(project.get("settings") or {}).get("jianying_template")
                if isinstance(binding, dict) and binding.get("template_id") == template_id:
                    raise HTTPException(status_code=409, detail="模板正在被项目使用，请先取消选择")
            offset += len(page)
            if not page or offset >= int(projects.get("total", 0)):
                break
        try:
            user_template_store.delete(user["user_id"], template_id)
            return {"ok": True}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/new/projects/{project_id}/jianying-template")
    def select_project_jianying_template(
        project_id: str, request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            project = project_store.get_project(user["user_id"], project_id)
            project_settings = dict(project.get("settings") or {})
            template_id = str(payload.get("template_id") or "").strip()
            if template_id:
                binding = user_template_store.render_binding(user["user_id"], template_id)
                project_settings["jianying_template"] = {
                    "template_id": binding["template_id"],
                    "name": binding["name"],
                }
            else:
                project_settings.pop("jianying_template", None)
            return project_store.update_project(
                user["user_id"], project_id, settings=project_settings
            )
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/new/projects", status_code=201)
    def create_new_project(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_store.create_project(
                owner_user_id=user["user_id"],
                owner_username=user["username"],
                name=str(payload.get("name") or ""),
                items=payload.get("items") if isinstance(payload.get("items"), list) else [],
                settings=(
                    payload.get("settings")
                    if isinstance(payload.get("settings"), dict)
                    else {}
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/new/projects/import-h3-handoff", status_code=201)
    def import_h3_project_handoff(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        require_local_file_access(request)
        user = current_project_user(request)
        try:
            return import_h3_handoff(
                project_store,
                owner_user_id=user["user_id"],
                owner_username=user["username"],
                project_name=str(payload.get("name") or "H3 自动后期项目"),
                manifest_path=str(payload.get("manifest_path") or ""),
            )
        except H3HandoffError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/new/projects")
    def list_new_projects(
        request: Request, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        user = current_project_user(request)
        return project_store.list_projects(
            user["user_id"], limit=limit, offset=offset
        )

    @app.get("/api/new/projects/{project_id}")
    def get_new_project(project_id: str, request: Request) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc

    @app.get("/api/new/projects/{project_id}/diagnostics")
    def download_project_diagnostics(project_id: str, request: Request) -> FileResponse:
        user = current_project_user(request)
        try:
            project = project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        archive_path = build_project_diagnostic_archive(
            project,
            logs_root=settings.storage_root.parent / "logs",
            output_root=settings.storage_root / "diagnostic_downloads",
        )
        safe_project_no = re.sub(
            r"[^A-Za-z0-9_-]+",
            "-",
            str(project.get("project_no") or "project"),
        ).strip("-") or "project"
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"JYD-diagnostics-{safe_project_no}.zip",
            background=BackgroundTask(_unlink_if_exists, archive_path),
        )

    @app.patch("/api/new/projects/{project_id}")
    def update_new_project(
        project_id: str, request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            expected_revision = payload.get("expected_revision")
            return project_store.update_project(
                user["user_id"],
                project_id,
                name=payload.get("name") if "name" in payload else None,
                settings=payload.get("settings") if "settings" in payload else None,
                expected_revision=(
                    int(expected_revision) if expected_revision is not None else None
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except ProjectRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/new/projects/{project_id}/digital-human-settings")
    def update_new_project_digital_human_settings(
        project_id: str, request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_store.set_digital_human_resolution(
                user["user_id"],
                project_id,
                resolution=str(payload.get("resolution") or ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.patch("/api/new/projects/{project_id}/items/{item_id}")
    def update_new_project_item(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_store.update_item(
                user["user_id"],
                project_id,
                item_id,
                row_key=payload.get("row_key") if "row_key" in payload else None,
                script_text=(
                    payload.get("script_text") if "script_text" in payload else None
                ),
                settings=payload.get("settings") if "settings" in payload else None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/new/projects/{project_id}/items/{item_id}")
    def delete_new_project_item(
        project_id: str, item_id: str, request: Request
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            project, cleanup_paths = project_store.delete_item(
                user["user_id"], project_id, item_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        for managed_path in cleanup_paths:
            path = Path(managed_path).resolve()
            if is_managed_project_file(path):
                _unlink_if_exists(path)
        return project

    @app.get("/api/new/script-template")
    def download_new_script_template() -> FileResponse:
        path = NEW_FRONTEND_ROOT / "project-script-template.xlsx"
        if not path.exists():
            raise HTTPException(status_code=404, detail="脚本导入模板不存在")
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="数字人脚本导入模板.xlsx",
        )

    @app.post("/api/new/script-imports/preview")
    async def preview_new_project_scripts(
        request: Request, filename: str = ""
    ) -> dict[str, Any]:
        current_project_user(request)
        original_filename = filename or request.headers.get("x-filename", "")
        try:
            return parse_project_script_file(await request.body(), original_filename)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/new/projects/{project_id}/metadata-import")
    async def import_new_project_source_metadata(
        project_id: str, request: Request, filename: str = ""
    ) -> dict[str, Any]:
        user = current_project_user(request)
        original_filename = filename or request.headers.get("x-filename", "")
        content = await request.body()
        try:
            parsed = parse_project_script_file(content, original_filename)
            rows = parsed.get("rows") if isinstance(parsed.get("rows"), list) else []
            if not rows or any(
                not row.get("article_type") or not row.get("assigned_account")
                for row in rows
                if isinstance(row, dict)
            ):
                raise ValueError("回填分类信息必须使用包含文章类型、分配账号的四列表")
            project_store.import_source_metadata(
                user["user_id"], project_id, rows
            )
            safe_name = _safe_filename(original_filename)
            directory = (
                settings.storage_root
                / "projects"
                / user["user_id"]
                / project_id
                / "script_sources"
            ).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{uuid.uuid4().hex}-{safe_name}"
            target.write_bytes(content)
            try:
                project_store.add_script_source(
                    user["user_id"],
                    project_id,
                    filename=safe_name,
                    content_type=(
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        if Path(safe_name).suffix.lower() == ".xlsx"
                        else "text/csv"
                    ),
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    managed_path=str(target),
                    allow_active=True,
                )
            except Exception:
                _unlink_if_exists(target)
                raise
            return project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/new/projects/{project_id}/script-source")
    async def save_new_project_script_source(
        project_id: str, request: Request, filename: str = ""
    ) -> dict[str, Any]:
        user = current_project_user(request)
        original_filename = filename or request.headers.get("x-filename", "")
        content = await request.body()
        try:
            parsed = parse_project_script_file(content, original_filename)
            project = project_store.get_project(user["user_id"], project_id)
            current_rows = [
                {
                    "row_key": str(item.get("row_key") or ""),
                    "script_text": str(item.get("script_text") or ""),
                }
                for item in project.get("items", [])
            ]
            parsed_rows = [
                {
                    "row_key": str(item.get("row_key") or ""),
                    "script_text": str(item.get("script_text") or ""),
                }
                for item in parsed.get("rows", [])
            ]
            if parsed_rows != current_rows:
                raise ValueError("脚本源文件内容与当前项目脚本不一致")
            safe_name = _safe_filename(original_filename)
            directory = (
                settings.storage_root
                / "projects"
                / user["user_id"]
                / project_id
                / "script_sources"
            ).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{uuid.uuid4().hex}-{safe_name}"
            target.write_bytes(content)
            try:
                project_store.add_script_source(
                    user["user_id"],
                    project_id,
                    filename=safe_name,
                    content_type=(
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        if Path(safe_name).suffix.lower() == ".xlsx"
                        else "text/csv"
                    ),
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    managed_path=str(target),
                )
            except Exception:
                _unlink_if_exists(target)
                raise
            return project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/new/projects/{project_id}/inputs")
    def replace_new_project_inputs(
        project_id: str, request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_store.replace_inputs(
                user["user_id"],
                project_id,
                payload.get("items") if isinstance(payload.get("items"), list) else [],
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/new/projects/{project_id}/items", status_code=201)
    def append_new_project_item(
        project_id: str, request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_store.append_item(
                user["user_id"],
                project_id,
                row_key=str(payload.get("row_key") or ""),
                script_text=str(payload.get("script_text") or ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/new/projects/{project_id}/items/batch", status_code=201)
    def append_new_project_items(
        project_id: str, request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_store.append_items(
                user["user_id"],
                project_id,
                items=(
                    payload.get("items")
                    if isinstance(payload.get("items"), list)
                    else []
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/new/projects/{project_id}")
    def delete_new_project(project_id: str, request: Request) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            cleanup = project_store.delete_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        deleted_file_count = 0
        for path in cleanup["files"]:
            target = Path(path)
            if is_managed_project_file(target):
                _unlink_if_exists(target)
                deleted_file_count += 1

        result_root = project_result_library.root.resolve()
        deleted_directory_count = 0
        for path in cleanup["directories"]:
            target = Path(path).resolve()
            try:
                relative = target.relative_to(result_root)
            except ValueError:
                continue
            if len(relative.parts) != 2 or not relative.parts[1].isdigit():
                continue
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
                deleted_directory_count += 1
        return {
            "ok": True,
            "project_id": project_id,
            "deleted_file_count": deleted_file_count,
            "deleted_directory_count": deleted_directory_count,
        }

    @app.post("/api/new/projects/{project_id}/images", status_code=201)
    async def upload_new_project_image(
        project_id: str, request: Request, filename: str = ""
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        content = await request.body()
        original_filename = filename or request.headers.get("x-filename", "")
        try:
            content_type, detected_suffix = detect_project_image(
                content, original_filename
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        safe_name = _safe_filename(original_filename or f"image{detected_suffix}")
        digest = hashlib.sha256(content).hexdigest()
        stem = Path(safe_name).stem[:120] or "image"
        stored_filename = f"{uuid.uuid4().hex}_{stem}{detected_suffix}"
        directory = settings.storage_root / "new_projects" / project_id / "input_images"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / stored_filename
        path.write_bytes(content)
        try:
            return project_store.register_input_image(
                owner_user_id=user["user_id"],
                project_id=project_id,
                filename=safe_name,
                content_type=content_type,
                size_bytes=len(content),
                sha256=digest,
                managed_path=str(path),
            )
        except KeyError as exc:
            _unlink_if_exists(path)
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except ValueError as exc:
            _unlink_if_exists(path)
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/new/projects/{project_id}/images/{image_id}")
    def get_new_project_image(
        project_id: str, image_id: str, request: Request
    ) -> FileResponse:
        user = current_project_user(request)
        try:
            image = project_store.get_input_image(
                user["user_id"], project_id, image_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目图片不存在") from exc
        path = Path(str(image["managed_path"]))
        if not path.is_file():
            raise HTTPException(status_code=404, detail="项目图片文件不存在")
        return FileResponse(path, media_type=str(image["content_type"]))

    @app.delete("/api/new/projects/{project_id}/images/{image_id}")
    def delete_new_project_image(
        project_id: str, image_id: str, request: Request
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            image = project_store.remove_input_image(
                user["user_id"], project_id, image_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目图片不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _unlink_if_exists(Path(str(image["managed_path"])))
        return {"ok": True, "image_id": image_id}

    @app.put("/api/new/projects/{project_id}/image-mapping")
    def apply_new_project_image_mapping(
        project_id: str, request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            image_ids = payload.get("image_ids")
            if image_ids is not None and not isinstance(image_ids, list):
                raise ValueError("image_ids 必须是图片 ID 数组")
            return project_store.apply_image_strategy(
                user["user_id"],
                project_id,
                strategy=str(payload.get("strategy") or ""),
                reuse_count=int(payload.get("reuse_count") or 1),
                image_ids=image_ids,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/new/projects/{project_id}/image-mapping-scope")
    def set_new_project_image_mapping_scope(
        project_id: str, request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        item_ids = payload.get("item_ids")
        if not isinstance(item_ids, list):
            raise HTTPException(status_code=422, detail="item_ids 必须是脚本行 ID 数组")
        try:
            return project_store.set_image_mapping_scope(
                user["user_id"],
                project_id,
                item_ids=item_ids,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/new/projects/{project_id}/items/{item_id}/image")
    def replace_new_project_item_image(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_store.replace_item_image(
                user["user_id"],
                project_id,
                item_id,
                str(payload.get("image_id") or ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目、脚本行或图片不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def digital_human_access(request: Request) -> tuple[AuthCenterClient, str]:
        if settings.auth_authority or auth_center is None:
            log_event(
                workbench_logger,
                "auth_center.not_configured",
                "工作台内容分析入口没有可用的数字人网站客户端",
                level=logging.ERROR,
                component="workbench",
                request_method=request.method,
                request_path=request.url.path,
                auth_authority=bool(settings.auth_authority),
                auth_center_configured=auth_center is not None,
            )
            raise HTTPException(status_code=503, detail="工作台尚未连接数字人网站")
        token = request.cookies.get(settings.site_cookie_name, "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="请先使用数字人账号登录")
        return auth_center, token

    def project_ltx_coordinator() -> ProjectLtxCoordinator:
        client = app.state.ltx_workbench_client
        if not isinstance(client, LtxWorkbenchClient):
            raise HTTPException(
                status_code=503,
                detail=(
                    "视频对口型服务未随主工作台启动。请关闭当前工作台，"
                    "并通过“公域闪剪工作台”统一启动器重新打开"
                ),
            )
        return ProjectLtxCoordinator(
            project_store,
            client,
            storage_root=settings.storage_root,
        )

    def raise_ltx_error(exc: Exception) -> None:
        if isinstance(exc, LtxWorkbenchError):
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if isinstance(exc, KeyError):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise exc

    def project_audio_coordinator(client: AuthCenterClient) -> ProjectAudioCoordinator:
        return ProjectAudioCoordinator(
            project_store,
            client,
            storage_root=settings.storage_root,
            max_audio_bytes=settings.max_audio_upload_bytes,
            visual_catalog=semantic_visual_catalog,
        )

    def project_content_analysis_coordinator(
        client: AuthCenterClient,
    ) -> ProjectContentAnalysisCoordinator:
        _, bgm_assets = project_postprocess_resources()
        available_bgm = {
            str(item.get("identity") or ""): item
            for item in bgm_assets
            if item.get("identity") and item.get("available", True)
        }
        return ProjectContentAnalysisCoordinator(
            project_store,
            client,
            max_concurrency=10,
            music_selector=ProjectMusicSelector(
                MusicProfileMatcher(settings.audio_library_root), available_bgm
            ),
            visual_catalog=semantic_visual_catalog,
        )

    def project_visual_analysis_coordinator(
        client: AuthCenterClient,
    ) -> ProjectVisualAnalysisCoordinator:
        return ProjectVisualAnalysisCoordinator(
            project_store,
            client,
            semantic_visual_catalog,
            max_concurrency=10,
        )

    @app.get("/api/new/semantic-visuals/catalog")
    def new_semantic_visual_catalog(request: Request) -> dict[str, Any]:
        current_project_user(request)
        return semantic_visual_catalog.public_payload()

    @app.get("/api/new/semantic-visuals/{asset_id}/preview")
    def new_semantic_visual_preview(asset_id: str, request: Request) -> FileResponse:
        current_project_user(request)
        asset = semantic_visual_catalog.asset(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="语义图片素材不存在")
        preview_path = Path(asset["preview_path"])
        preview_type = mimetypes.guess_type(preview_path.name)[0] or "application/octet-stream"
        return FileResponse(preview_path, media_type=preview_type)

    @app.get("/api/new/semantic-visuals/{asset_id}/content")
    def new_semantic_visual_content(asset_id: str, request: Request) -> FileResponse:
        current_project_user(request)
        asset = semantic_visual_catalog.asset(asset_id)
        if asset is None or asset.get("media_type") != "video":
            raise HTTPException(status_code=404, detail="语义视频素材不存在")
        video_path = Path(asset["resource_path"])
        video_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
        return FileResponse(video_path, media_type=video_type)

    @app.get("/api/new/fixed-visuals/nameplate/preview")
    def new_fixed_nameplate_preview(request: Request) -> FileResponse:
        current_project_user(request)
        try:
            profile = layout_profile(request.query_params.get("layout_profile"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        preview_path = (
            semantic_visual_catalog.root
            / str(profile["nameplate"]["bundle"])
            / "resources"
            / "sticker"
            / "singleImage.png"
        ).resolve()
        if not preview_path.is_file():
            raise HTTPException(status_code=404, detail="固定人名牌素材不存在")
        return FileResponse(preview_path, media_type="image/png")

    @app.post("/api/new/projects/{project_id}/visual-analysis")
    def analyze_new_project_visuals(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        raw_item_ids = payload.get("item_ids")
        if raw_item_ids is not None and not isinstance(raw_item_ids, list):
            raise HTTPException(status_code=422, detail="item_ids 必须是数组")
        if isinstance(raw_item_ids, list) and not raw_item_ids:
            raise HTTPException(status_code=422, detail="item_ids 不能为空数组")
        if type(payload.get("force_refresh", False)) is not bool:
            raise HTTPException(status_code=422, detail="force_refresh 必须是布尔值")
        try:
            return project_content_analysis_coordinator(client).analyze(
                user["user_id"],
                project_id,
                token,
                item_ids=(
                    [str(item_id) for item_id in raw_item_ids]
                    if isinstance(raw_item_ids, list)
                    else None
                ),
                force_refresh=payload.get("force_refresh") is True,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/new/projects/{project_id}/items/{item_id}/visual-analysis/retry"
    )
    def retry_new_project_item_visual_analysis(
        project_id: str,
        item_id: str,
        request: Request,
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            return project_content_analysis_coordinator(client).analyze(
                user["user_id"],
                project_id,
                token,
                item_ids=[item_id],
                force_refresh=True,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put(
        "/api/new/projects/{project_id}/items/{item_id}/visual-overlays"
    )
    def update_new_project_item_visual_overlays(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        overlays = payload.get("overlays")
        if not isinstance(overlays, list):
            raise HTTPException(status_code=422, detail="overlays 必须是数组")
        normalized: list[dict[str, Any]] = []
        for raw in overlays:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=422, detail="语义贴图必须是对象")
            asset = semantic_visual_catalog.asset(str(raw.get("asset_id") or ""))
            concept_id = str(raw.get("concept_id") or "")
            if asset is None or concept_id not in asset["concept_ids"]:
                raise HTTPException(status_code=422, detail="语义贴图素材与概念不匹配")
            media_type = asset["media_type"]
            defaults = asset["defaults"]
            overlay = {
                    key: raw.get(key)
                    for key in (
                        "overlay_id",
                        "candidate_id",
                        "concept_id",
                        "asset_id",
                        "enabled",
                        "locked",
                        "corner",
                        "scale",
                        "opacity",
                        "start_us",
                        "duration_us",
                        "timing_source",
                        "timing_mode",
                        "sentence_char_start",
                        "sentence_char_end",
                        "sentence_text",
                        "phrase_char_start",
                        "phrase_char_end",
                        "phrase_text",
                        "list_index",
                        "list_size",
                        "segment_boundary_us",
                        "usage",
                    )
                } | {
                    "asset_name": asset["name"],
                    "preview_url": asset["preview_url"],
                    "media_type": media_type,
                    "renderer": asset["renderer"],
                    "resource_path": (
                        asset["resource"]["bundle"]
                        if media_type == "image"
                        else asset["resource"]["video"]
                    ),
                }
            if media_type == "video":
                source_start_us = int(
                    raw.get("source_start_us", defaults["source_start_us"])
                )
                available_us = max(
                    0,
                    int(asset["resource"].get("duration_us") or 0)
                    - source_start_us,
                )
                source_duration_us = min(
                    int(raw.get("source_duration_us", defaults["duration_us"])),
                    available_us,
                )
                overlay["duration_us"] = min(
                    int(overlay.get("duration_us") or 0), source_duration_us
                )
                overlay.update(
                    {
                        "source_start_us": source_start_us,
                        "source_duration_us": source_duration_us,
                        "loop_to_target": False,
                        "mute": raw.get("mute", defaults["mute"]) is not False,
                        "fit": str(raw.get("fit") or defaults["fit"]),
                    }
                )
            normalized.append(overlay)
        media_types = {item["media_type"] for item in normalized}
        media_policy = (
            "mixed"
            if media_types == {"image", "video"}
            else "video_only"
            if media_types == {"video"}
            else "image_only"
        )
        try:
            return project_store.update_item_visual_overlays(
                user["user_id"],
                project_id,
                item_id,
                overlays=normalized,
                expected_revision=int(payload.get("revision")),
                catalog_version=semantic_visual_catalog.catalog_version,
                library_id=(
                    semantic_visual_catalog.library_id or "jyd.semantic-visual-library.default"
                ),
                media_policy=media_policy,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except ProjectRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/new/projects/{project_id}/content-analysis")
    def analyze_new_project_content(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        raw_item_ids = payload.get("item_ids")
        if raw_item_ids is not None and not isinstance(raw_item_ids, list):
            raise HTTPException(status_code=422, detail="item_ids 必须是数组")
        if isinstance(raw_item_ids, list) and not raw_item_ids:
            raise HTTPException(status_code=422, detail="item_ids 不能为空数组")
        if type(payload.get("force_refresh", False)) is not bool:
            raise HTTPException(status_code=422, detail="force_refresh 必须是布尔值")
        try:
            return project_content_analysis_coordinator(client).analyze(
                user["user_id"],
                project_id,
                token,
                item_ids=(
                    [str(item_id) for item_id in raw_item_ids]
                    if isinstance(raw_item_ids, list)
                    else None
                ),
                force_refresh=payload.get("force_refresh") is True,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/new/projects/{project_id}/items/{item_id}/content-analysis/retry"
    )
    def retry_new_project_item_content_analysis(
        project_id: str,
        item_id: str,
        request: Request,
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            return project_content_analysis_coordinator(client).analyze(
                user["user_id"],
                project_id,
                token,
                item_ids=[item_id],
                force_refresh=True,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc

    def project_composition_coordinator(
        client: AuthCenterClient,
    ) -> ProjectCompositionCoordinator:
        if composition_coordinator is not None and client is auth_center:
            return composition_coordinator
        return ProjectCompositionCoordinator(
            project_store,
            client,
            storage_root=settings.storage_root,
            max_video_bytes=settings.max_video_upload_bytes,
        )

    def project_h3_coordinator(client: AuthCenterClient) -> ProjectH3Coordinator:
        caption_aligner = (
            FunASRCaptionAligner(
                settings.asr_base_url,
                timeout_seconds=settings.asr_timeout_seconds,
                shared_token=settings.asr_shared_token,
            )
            if settings.asr_base_url
            else None
        )
        return ProjectH3Coordinator(
            project_store,
            client,
            storage_root=settings.storage_root,
            max_video_bytes=settings.max_video_upload_bytes,
            caption_aligner=caption_aligner,
            require_precise_alignment=settings.asr_required,
        )

    @app.get("/api/new/h3/accounts")
    def list_new_h3_accounts(request: Request) -> dict[str, Any]:
        client, token = digital_human_access(request)
        try:
            return project_h3_coordinator(client).accounts(token)
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.put("/api/new/projects/{project_id}/h3/settings")
    def update_new_project_h3_settings(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        image_ids = payload.get("identity_image_ids")
        if not isinstance(image_ids, list):
            raise HTTPException(
                status_code=422, detail="identity_image_ids 必须是数组"
            )
        try:
            return project_store.set_h3_configuration(
                user["user_id"],
                project_id,
                identity_image_ids=image_ids,
                defaults=(
                    payload.get("defaults")
                    if isinstance(payload.get("defaults"), dict)
                    else {}
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/new/projects/{project_id}/generation-mode")
    def update_new_project_generation_mode(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        mode = str(payload.get("mode") or "").strip()
        if (
            mode != "minimax_h3_ref2va"
            and not settings.enable_legacy_new_workbench_video_engines
        ):
            raise HTTPException(status_code=409, detail=NEW_WORKBENCH_H3_ONLY_DETAIL)
        try:
            return project_store.set_generation_mode(
                user["user_id"], project_id, mode
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/new/projects/{project_id}/ltx/state")
    def get_new_project_ltx_state(
        project_id: str, request: Request
    ) -> dict[str, Any]:
        user = current_project_user(request)
        _, token = digital_human_access(request)
        try:
            return project_ltx_coordinator().state(
                user["user_id"], project_id, token
            )
        except Exception as exc:
            raise_ltx_error(exc)
            raise

    @app.put(
        "/api/new/projects/{project_id}/items/{item_id}/ltx/source-video",
        status_code=201,
    )
    async def upload_new_project_ltx_source_video(
        project_id: str,
        item_id: str,
        request: Request,
        filename: str = "",
    ) -> dict[str, Any]:
        user = current_project_user(request)
        if not settings.enable_legacy_new_workbench_video_engines:
            raise HTTPException(status_code=409, detail=NEW_WORKBENCH_H3_ONLY_DETAIL)
        _, token = digital_human_access(request)
        safe_name = _safe_filename(
            filename or request.headers.get("x-filename") or "source.mp4"
        )
        if Path(safe_name).suffix.lower() not in {
            ".mp4",
            ".mov",
            ".mkv",
            ".webm",
            ".avi",
        }:
            raise HTTPException(status_code=422, detail="视频格式不受支持")
        declared_size = request.headers.get("content-length", "").strip()
        if declared_size:
            try:
                if int(declared_size) > settings.max_video_upload_bytes:
                    raise HTTPException(status_code=413, detail="视频超过大小限制")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Content-Length 不合法") from exc
        temporary_dir = settings.storage_root / "temporary_uploads" / "ltx"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        temporary = temporary_dir / f"{uuid.uuid4().hex}{Path(safe_name).suffix.lower()}"
        size = 0
        try:
            with temporary.open("wb") as output:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > settings.max_video_upload_bytes:
                        raise HTTPException(status_code=413, detail="视频超过大小限制")
                    output.write(chunk)
            if size <= 0:
                raise HTTPException(status_code=422, detail="视频不能为空")
            return project_ltx_coordinator().upload_source_video(
                user["user_id"],
                project_id,
                item_id,
                token,
                temporary,
                filename=safe_name,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise_ltx_error(exc)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    @app.post("/api/new/projects/{project_id}/ltx/generate")
    def start_new_project_ltx(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        if not settings.enable_legacy_new_workbench_video_engines:
            raise HTTPException(status_code=409, detail=NEW_WORKBENCH_H3_ONLY_DETAIL)
        _, token = digital_human_access(request)
        item_ids = payload.get("item_ids")
        if item_ids is not None and not isinstance(item_ids, list):
            raise HTTPException(status_code=422, detail="item_ids 必须是数组")
        try:
            return project_ltx_coordinator().start(
                user["user_id"],
                project_id,
                token,
                item_ids=item_ids,
                cost_confirmed=payload.get("cost_confirmed") is True,
                force_new=payload.get("force_new") is True,
            )
        except Exception as exc:
            raise_ltx_error(exc)
            raise

    @app.post("/api/new/projects/{project_id}/ltx/refresh")
    def refresh_new_project_ltx(
        project_id: str, request: Request
    ) -> dict[str, Any]:
        user = current_project_user(request)
        if not settings.enable_legacy_new_workbench_video_engines:
            raise HTTPException(status_code=409, detail=NEW_WORKBENCH_H3_ONLY_DETAIL)
        _, token = digital_human_access(request)
        try:
            return project_ltx_coordinator().refresh(
                user["user_id"], project_id, token
            )
        except Exception as exc:
            raise_ltx_error(exc)
            raise

    @app.post(
        "/api/new/projects/{project_id}/items/{item_id}/ltx/retry"
    )
    def retry_new_project_ltx_item(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        del payload
        user = current_project_user(request)
        if not settings.enable_legacy_new_workbench_video_engines:
            raise HTTPException(status_code=409, detail=NEW_WORKBENCH_H3_ONLY_DETAIL)
        _, token = digital_human_access(request)
        try:
            return project_ltx_coordinator().retry(
                user["user_id"], project_id, item_id, token
            )
        except Exception as exc:
            raise_ltx_error(exc)
            raise

    @app.patch(
        "/api/new/projects/{project_id}/items/{item_id}/h3/overrides"
    )
    def update_new_project_h3_item_overrides(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_store.set_h3_item_overrides(
                user["user_id"], project_id, item_id, payload
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/new/projects/{project_id}/items/{item_id}/h3/reference-video",
        status_code=201,
    )
    async def upload_new_project_h3_reference_video(
        project_id: str,
        item_id: str,
        request: Request,
        filename: str = "",
    ) -> dict[str, Any]:
        user = current_project_user(request)
        content = await request.body()
        if not content:
            raise HTTPException(status_code=422, detail="H3 参考视频不能为空")
        if len(content) > settings.max_video_upload_bytes:
            raise HTTPException(status_code=413, detail="H3 参考视频超过大小限制")
        safe_name = _safe_filename(
            filename or request.headers.get("x-filename") or "reference.mp4"
        )
        if Path(safe_name).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
            raise HTTPException(status_code=422, detail="H3 参考视频格式不支持")
        directory = (
            settings.storage_root
            / "new_projects"
            / project_id
            / item_id
            / "h3_reference"
        )
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{uuid.uuid4().hex}_{safe_name}"
        target.write_bytes(content)
        try:
            return project_store.add_h3_reference_video(
                owner_user_id=user["user_id"],
                project_id=project_id,
                item_id=item_id,
                filename=safe_name,
                managed_path=str(target),
                metadata={
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
            )
        except KeyError as exc:
            _unlink_if_exists(target)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            _unlink_if_exists(target)
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/new/projects/{project_id}/h3/audio-review")
    def review_new_project_h3_audio(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        item_ids = payload.get("item_ids")
        if item_ids is not None and not isinstance(item_ids, list):
            raise HTTPException(status_code=422, detail="item_ids 必须是数组")
        try:
            return project_h3_coordinator(client).approve_audio(
                user["user_id"],
                project_id,
                token,
                item_ids=item_ids,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/new/projects/{project_id}/h3/prepare", status_code=201)
    def prepare_new_project_h3(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        selected = payload.get("selected_account_ids")
        if not isinstance(selected, list):
            raise HTTPException(
                status_code=422, detail="selected_account_ids 必须是数组"
            )
        item_ids = payload.get("item_ids")
        if item_ids is not None and not isinstance(item_ids, list):
            raise HTTPException(status_code=422, detail="item_ids 必须是数组")
        try:
            return project_h3_coordinator(client).prepare(
                user["user_id"],
                project_id,
                token,
                idempotency_key=str(payload.get("idempotency_key") or ""),
                selected_account_ids=selected,
                item_ids=item_ids,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/new/projects/{project_id}/h3/confirm")
    def confirm_new_project_h3(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        if payload.get("cost_confirmed") is not True:
            raise HTTPException(status_code=409, detail="请明确确认 H3 分段费用")
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            return project_h3_coordinator(client).confirm(
                user["user_id"],
                project_id,
                token,
                batch_id=str(payload.get("batch_id") or ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/new/projects/{project_id}/h3/status")
    def sync_new_project_h3(
        project_id: str, request: Request
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            return project_h3_coordinator(client).sync(
                user["user_id"], project_id, token
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/new/projects/{project_id}/h3/segments/{segment_id}/regeneration/prepare"
    )
    def prepare_new_project_h3_regeneration(
        project_id: str, segment_id: str, request: Request
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            return project_h3_coordinator(client).prepare_regeneration(
                user["user_id"], project_id, token, segment_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/new/projects/{project_id}/h3/segments/{segment_id}/regeneration/confirm"
    )
    def confirm_new_project_h3_regeneration(
        project_id: str,
        segment_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        if payload.get("cost_confirmed") is not True:
            raise HTTPException(status_code=409, detail="请明确确认 H3 级联重生成费用")
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            return project_h3_coordinator(client).confirm_regeneration(
                user["user_id"],
                project_id,
                token,
                segment_id,
                request_key=str(payload.get("request_key") or ""),
                quote_token=str(payload.get("quote_token") or ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/new/projects/{project_id}/h3/segments/{segment_id}/retry/prepare"
    )
    def prepare_new_project_h3_retry(
        project_id: str, segment_id: str, request: Request
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            return project_h3_coordinator(client).prepare_retry(
                user["user_id"], project_id, token, segment_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/new/projects/{project_id}/h3/segments/{segment_id}/retry/confirm"
    )
    def confirm_new_project_h3_retry(
        project_id: str,
        segment_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            return project_h3_coordinator(client).confirm_retry(
                user["user_id"],
                project_id,
                token,
                segment_id,
                request_key=str(payload.get("request_key") or ""),
                quote_token=str(payload.get("quote_token") or ""),
                cost_confirmed=payload.get("cost_confirmed") is True,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/new/projects/{project_id}/h3/segments/{segment_id}/cancel"
    )
    def cancel_new_project_h3_segment(
        project_id: str,
        segment_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            return project_h3_coordinator(client).cancel_segment(
                user["user_id"],
                project_id,
                token,
                segment_id,
                request_key=str(payload.get("request_key") or ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    default_subtitle_font_identity = "resource_id:7244518590332801592"

    def project_postprocess_resources() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        fonts = asset_admin.decorate(
            "font",
            _combined_library_items(
                settings, FONT_LIBRARY_ROOT, "font_library", _list_font_library
            ),
        )
        fonts = _list_system_fonts() + fonts
        available_fonts = [
            item
            for item in fonts
            if item.get("available")
            and item.get("enabled", True)
            and item.get("path")
            and Path(str(item["path"])).is_file()
            and Path(str(item["path"])).stat().st_size > 1024
        ]
        available_fonts.sort(
            key=lambda item: (
                str(item.get("identity") or "") != default_subtitle_font_identity,
                not str(item.get("identity") or "").startswith("system:simhei"),
                str(item.get("name") or "").casefold(),
            )
        )
        audio = _decorate_audio_snapshot(
            render_queue.audio_catalog.snapshot(), asset_admin
        ).get("assets", [])
        available_bgm = [
            item
            for item in audio
            if isinstance(item, dict)
            and item.get("identity")
            and item.get("enabled", True)
            and not item.get("deleted", False)
        ]
        return available_fonts, available_bgm

    def project_postprocess_coordinator() -> ProjectPostprocessCoordinator:
        fonts, bgm_assets = project_postprocess_resources()
        caption_aligner = (
            FunASRCaptionAligner(
                settings.asr_base_url,
                timeout_seconds=settings.asr_timeout_seconds,
                shared_token=settings.asr_shared_token,
            )
            if settings.asr_base_url
            else None
        )
        return ProjectPostprocessCoordinator(
            project_store,
            render_queue,
            storage_root=settings.storage_root,
            draft_root=settings.default_draft_root,
            fonts=fonts,
            bgm_assets=bgm_assets,
            music_matcher=MusicProfileMatcher(settings.audio_library_root),
            caption_aligner=caption_aligner,
            require_precise_alignment=settings.asr_required,
            semantic_visual_library_root=semantic_visual_catalog.root,
            semantic_visual_catalog=semantic_visual_catalog,
        )

    def project_variant_coordinator() -> ProjectVariantCoordinator:
        fonts, bgm_assets = project_postprocess_resources()
        effects = asset_admin.decorate(
            "effect",
            _combined_library_items(
                settings, EFFECT_LIBRARY_ROOT, "effect_library", _list_json_library
            ),
        )
        fullscreen_stickers = asset_admin.decorate(
            "sticker",
            _combined_bundle_items(
                settings, STICKER_LIBRARY_ROOT, "sticker_library",
                "sticker_manifest.json", "stickers", "jyd_probe.fullscreen_sticker.v1",
            ),
        )
        corner_stickers = asset_admin.decorate(
            "corner_sticker",
            _combined_bundle_items(
                settings, CORNER_STICKER_LIBRARY_ROOT, "corner_sticker_library",
                "sticker_manifest.json", "stickers", "jyd_probe.fullscreen_sticker.v1",
            ),
        )
        return ProjectVariantCoordinator(
            project_store,
            render_queue,
            storage_root=settings.storage_root,
            draft_root=settings.default_draft_root,
            fonts=fonts,
            bgm_assets=bgm_assets,
            effects=effects,
            fullscreen_stickers=fullscreen_stickers,
            corner_stickers=corner_stickers,
            result_library_root=project_result_library.root,
            semantic_visual_library_root=semantic_visual_catalog.root,
        )

    def selectable_voice_ids(library: dict[str, Any]) -> set[str]:
        return {
            str(item.get("voice_asset_id") or "")
            for item in library.get("voices", [])
            if isinstance(item, dict) and item.get("selectable") is not False
        }

    @app.get("/api/new/voices")
    def list_new_voices(request: Request) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            result = client.list_workbench_voices(token)
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        voices = result.get("voices") if isinstance(result.get("voices"), list) else []
        available_ids = selectable_voice_ids({"voices": voices})
        selectable_voices = [
            voice
            for voice in voices
            if isinstance(voice, dict) and voice.get("selectable") is not False
        ]
        preferences = project_store.get_voice_preferences(user["user_id"])
        if (
            preferences["default_voice_asset_id"] not in available_ids
            and selectable_voices
        ):
            preferences = project_store.set_voice_preferences(
                user["user_id"],
                default_voice_asset_id=str(
                    selectable_voices[0].get("voice_asset_id") or ""
                ),
            )
        return {
            "schema": "jyd.workbench-voices.v1",
            "voices": voices,
            "creation_tasks": result.get("creation_tasks", []),
            "preferences": preferences,
        }

    @app.put("/api/new/voices/default")
    def set_new_default_voice(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        voice_asset_id = str(payload.get("voice_asset_id") or "").strip()
        try:
            library = client.list_workbench_voices(token)
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        available = selectable_voice_ids(library)
        if voice_asset_id not in available:
            raise HTTPException(status_code=422, detail="音色未激活或不属于当前账号")
        try:
            preferences = project_store.set_voice_preferences(
                user["user_id"],
                default_voice_asset_id=voice_asset_id,
                voice_settings=(
                    payload.get("voice_settings")
                    if isinstance(payload.get("voice_settings"), dict)
                    else None
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "preferences": preferences}

    @app.post("/api/new/voices/import", status_code=201)
    def import_new_voice(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        client, token = digital_human_access(request)
        try:
            return client.import_workbench_voice(
                token,
                voice_id=str(payload.get("voice_id") or ""),
                name=str(payload.get("name") or ""),
                already_activated=payload.get("already_activated") is True,
            )
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/api/new/voices/{voice_asset_id}/preview")
    def create_new_voice_preview(
        voice_asset_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        client, token = digital_human_access(request)
        try:
            client.create_official_voice_preview(
                token,
                voice_asset_id,
                preview_text=str(
                    payload.get("preview_text")
                    or "你好，这是一段官方声音的试听内容。"
                ),
                cost_confirmed=payload.get("cost_confirmed") is True,
            )
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {
            "ok": True,
            "preview_url": f"/api/new/voices/{quote(voice_asset_id, safe='')}/preview",
        }

    @app.get("/api/new/voices/{voice_asset_id}/preview")
    def download_new_voice_preview(voice_asset_id: str, request: Request) -> FileResponse:
        client, token = digital_human_access(request)
        temporary = settings.storage_root / "temporary_downloads" / f"{uuid.uuid4().hex}.mp3"
        try:
            client.download_voice_preview(
                token,
                voice_asset_id,
                temporary,
                max_bytes=settings.max_audio_upload_bytes,
            )
        except AuthCenterError as exc:
            _unlink_if_exists(temporary)
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return FileResponse(
            temporary,
            media_type="audio/mpeg",
            filename="voice-preview.mp3",
            background=BackgroundTask(_unlink_if_exists, temporary),
        )

    @app.post("/api/new/voices/{voice_asset_id}/activate")
    def activate_new_voice(
        voice_asset_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        client, token = digital_human_access(request)
        try:
            return client.activate_workbench_voice(
                token,
                voice_asset_id,
                cost_confirmed=payload.get("cost_confirmed") is True,
            )
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.delete("/api/new/voices/{voice_asset_id}")
    def delete_new_voice(voice_asset_id: str, request: Request) -> dict[str, Any]:
        user = current_project_user(request)
        references = project_store.projects_using_voice(
            user["user_id"], voice_asset_id
        )
        if references:
            labels = "、".join(
                str(item.get("project_no") or item.get("name") or "项目")
                for item in references[:5]
            )
            raise HTTPException(
                status_code=409,
                detail=f"该音色仍被项目 {labels} 使用，请先更换项目音色",
            )
        client, token = digital_human_access(request)
        try:
            return client.delete_workbench_voice(token, voice_asset_id)
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/api/new/voice-creations", status_code=201)
    async def create_new_voice_creation(request: Request) -> dict[str, Any]:
        client, token = digital_human_access(request)
        form = await request.form()
        source_a = form.get("source_a")
        source_b = form.get("source_b")
        if source_a is None or not hasattr(source_a, "read"):
            raise HTTPException(status_code=422, detail="请上传声音样本 A")
        source_a_bytes = await source_a.read()
        if len(source_a_bytes) > settings.max_audio_upload_bytes:
            raise HTTPException(status_code=413, detail="声音样本 A 超过上传大小限制")
        source_b_bytes = None
        if source_b is not None and hasattr(source_b, "read"):
            source_b_bytes = await source_b.read()
            if len(source_b_bytes) > settings.max_audio_upload_bytes:
                raise HTTPException(status_code=413, detail="声音样本 B 超过上传大小限制")
        fields = {
            "method": str(form.get("method") or "clone"),
            "name": str(form.get("name") or ""),
            "preview_text": str(form.get("preview_text") or ""),
            "model": str(form.get("model") or "speech-2.8-turbo"),
            "weight_a": str(form.get("weight_a") or "50"),
            "noise_reduction": str(form.get("noise_reduction") or "false"),
            "volume_normalization": str(form.get("volume_normalization") or "false"),
            "cost_confirmed": str(form.get("cost_confirmed") or "false"),
        }
        try:
            return client.create_voice_creation(
                token,
                fields=fields,
                source_a_name=str(getattr(source_a, "filename", None) or "voice-a.mp3"),
                source_a=source_a_bytes,
                source_a_content_type=str(
                    getattr(source_a, "content_type", None) or "application/octet-stream"
                ),
                source_b_name=(
                    str(getattr(source_b, "filename", None) or "voice-b.mp3")
                    if source_b_bytes is not None
                    else None
                ),
                source_b=source_b_bytes,
                source_b_content_type=(
                    str(getattr(source_b, "content_type", None) or "application/octet-stream")
                    if source_b_bytes is not None
                    else None
                ),
            )
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/api/new/voice-creations/{task_id}/save")
    def save_new_voice_creation(task_id: str, request: Request) -> dict[str, Any]:
        client, token = digital_human_access(request)
        try:
            return client.save_voice_creation(token, task_id)
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/api/new/voice-creations/{task_id}/preview")
    def download_new_voice_creation_preview(task_id: str, request: Request) -> FileResponse:
        client, token = digital_human_access(request)
        temporary = settings.storage_root / "temporary_downloads" / f"{uuid.uuid4().hex}.mp3"
        try:
            client.download_voice_creation_preview(
                token,
                task_id,
                temporary,
                max_bytes=settings.max_audio_upload_bytes,
            )
        except AuthCenterError as exc:
            _unlink_if_exists(temporary)
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return FileResponse(
            temporary,
            media_type="audio/mpeg",
            filename="voice-creation-preview.mp3",
            background=BackgroundTask(_unlink_if_exists, temporary),
        )

    @app.put("/api/new/projects/{project_id}/items/{item_id}/voice")
    def configure_new_project_item_voice(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        voice_asset_id = str(payload.get("voice_asset_id") or "").strip()
        try:
            library = client.list_workbench_voices(token)
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if voice_asset_id not in selectable_voice_ids(library):
            raise HTTPException(status_code=422, detail="音色未激活或不属于当前账号")
        try:
            return project_store.configure_item_voice(
                user["user_id"],
                project_id,
                item_id,
                voice_asset_id=voice_asset_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/api/new/projects/{project_id}/voice")
    def configure_new_project_voice(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        voice_asset_id = str(payload.get("voice_asset_id") or "").strip()
        item_ids = payload.get("item_ids")
        if item_ids is not None and not isinstance(item_ids, list):
            raise HTTPException(status_code=422, detail="脚本行范围必须是数组")
        try:
            library = client.list_workbench_voices(token)
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if voice_asset_id not in selectable_voice_ids(library):
            raise HTTPException(status_code=422, detail="音色未激活或不属于当前账号")
        try:
            project = project_store.configure_project_voice(
                user["user_id"],
                project_id,
                voice_asset_id=voice_asset_id,
                item_ids=item_ids,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "ok": True,
            "project": project,
            "preferences": project_store.get_voice_preferences(user["user_id"]),
        }

    @app.post("/api/new/projects/{project_id}/audio/generate")
    def generate_new_project_audio(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        if payload.get("cost_confirmed") is not True:
            raise HTTPException(status_code=409, detail="请确认声音生成会产生 MiniMax 费用")
        item_ids = payload.get("item_ids")
        if item_ids is not None and (
            not isinstance(item_ids, list)
            or not item_ids
            or not all(isinstance(item_id, str) and item_id.strip() for item_id in item_ids)
            or len(set(item_ids)) != len(item_ids)
        ):
            raise HTTPException(status_code=422, detail="脚本行 ID 必须是非空且不重复的字符串列表")
        coordinator = project_audio_coordinator(client)
        try:
            return coordinator.start(
                user["user_id"],
                project_id,
                token,
                default_voice_asset_id=str(payload.get("default_voice_asset_id") or ""),
                voice_assignments=(
                    payload.get("voice_assignments")
                    if isinstance(payload.get("voice_assignments"), dict)
                    else {}
                ),
                settings=(
                    payload.get("voice_settings")
                    if isinstance(payload.get("voice_settings"), dict)
                    else {}
                ),
                resolution=str(payload.get("resolution") or "1024"),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                item_ids=list(item_ids) if item_ids is not None else None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/new/projects/{project_id}/audio/status")
    def sync_new_project_audio(project_id: str, request: Request) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            return project_audio_coordinator(client).sync(
                user["user_id"], project_id, token
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/new/projects/{project_id}/items/{item_id}/audio/retry")
    def retry_new_project_audio(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        if payload.get("cost_confirmed") is not True:
            raise HTTPException(status_code=409, detail="请确认重新生成声音可能再次产生费用")
        try:
            return project_audio_coordinator(client).retry(
                user["user_id"],
                project_id,
                item_id,
                token,
                idempotency_key=str(payload.get("idempotency_key") or uuid.uuid4().hex),
                settings=(
                    payload.get("voice_settings")
                    if isinstance(payload.get("voice_settings"), dict)
                    else project_store.get_voice_preferences(user["user_id"])[
                        "voice_settings"
                    ]
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或声音任务不存在") from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/new/projects/{project_id}/items/{item_id}/audio")
    def download_new_project_audio(
        project_id: str, item_id: str, request: Request
    ) -> FileResponse:
        user = current_project_user(request)
        try:
            project = project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        item = next((value for value in project["items"] if value["item_id"] == item_id), None)
        audio = item.get("outputs", {}).get("minimax_audio") if item else None
        if not isinstance(audio, dict) or not audio.get("managed_path"):
            raise HTTPException(status_code=404, detail="生成音频尚未准备完成")
        path = Path(str(audio["managed_path"])).resolve()
        try:
            path.relative_to(settings.storage_root.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="生成音频文件不存在") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="生成音频文件不存在")
        return FileResponse(
            path,
            media_type="audio/mpeg",
            filename=audio_export_filename(item, audio),
        )

    @app.get("/api/new/projects/{project_id}/audios/download")
    def download_new_project_current_audios(
        project_id: str, request: Request
    ) -> FileResponse:
        """Download every shared MiniMax source audio in one temporary ZIP."""

        user = current_project_user(request)
        try:
            project = project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        items = list(project.get("items") or [])
        if not items:
            raise HTTPException(status_code=409, detail="当前项目没有可下载的声音")
        selected: list[tuple[Path, str]] = []
        for item in items:
            audio = (item.get("outputs") or {}).get("minimax_audio")
            if not isinstance(audio, dict) or not audio.get("managed_path"):
                continue
            path = Path(str(audio["managed_path"])).resolve()
            if not is_managed_project_file(path) or not path.is_file():
                raise HTTPException(
                    status_code=404,
                    detail=f"任务 {item.get('row_key')} 的声音文件不存在",
                )
            selected.append(
                (
                    path,
                    _safe_filename(audio_export_filename(item, audio)),
                )
            )
        if not selected:
            raise HTTPException(status_code=409, detail="当前项目没有已完成的声音可下载")
        archive_root = settings.storage_root / "project_downloads"
        archive_root.mkdir(parents=True, exist_ok=True)
        archive_path = archive_root / f"{uuid.uuid4().hex}.zip"
        used: dict[str, int] = {}
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as archive:
            for path, filename in selected:
                used[filename] = used.get(filename, 0) + 1
                count = used[filename]
                archive_name = filename
                if count > 1:
                    source = Path(filename)
                    archive_name = f"{source.stem}-{count:02d}{source.suffix}"
                archive.write(path, arcname=archive_name)
        project_name = _safe_filename(str(project.get("name") or "数字人声音"))
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{project_name}-声音.zip",
            background=BackgroundTask(_unlink_if_exists, archive_path),
        )

    @app.get("/api/new/runninghub-execution-accounts")
    def new_workbench_runninghub_execution_accounts(
        request: Request,
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            if user.get("is_admin") is True:
                return client.list_workbench_execution_accounts(token)
            return client.list_workbench_dual_pool_accounts(token)
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/api/new/projects/{project_id}/composition/generate")
    def generate_new_project_composition(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        if not settings.enable_legacy_new_workbench_video_engines:
            raise HTTPException(status_code=409, detail=NEW_WORKBENCH_H3_ONLY_DETAIL)
        client, token = digital_human_access(request)
        if payload.get("cost_confirmed") is not True:
            raise HTTPException(
                status_code=409,
                detail="请确认画面生成会产生 RunningHub 费用",
            )
        item_ids = payload.get("item_ids")
        if item_ids is not None and (
            not isinstance(item_ids, list)
            or not item_ids
            or not all(isinstance(item_id, str) and item_id.strip() for item_id in item_ids)
            or len(set(item_ids)) != len(item_ids)
        ):
            raise HTTPException(status_code=422, detail="脚本行 ID 必须是非空且不重复的字符串列表")
        selection_provided = "runninghub_execution_account_ids" in payload
        selected_account_ids = payload.get("runninghub_execution_account_ids")
        seed_selection_provided = "seedvr2_execution_account_ids" in payload
        selected_seedvr2_account_ids = payload.get("seedvr2_execution_account_ids")
        requested_execution_mode = payload.get("execution_mode")
        if requested_execution_mode is not None and requested_execution_mode not in {
            "same_account_v1",
            "dual_pool_v1",
        }:
            raise HTTPException(status_code=422, detail="RunningHub 执行模式不合法")
        if selection_provided and (
            not isinstance(selected_account_ids, list)
            or not selected_account_ids
            or any(
                type(account_id) is not int or account_id <= 0
                for account_id in selected_account_ids
            )
            or len(set(selected_account_ids)) != len(selected_account_ids)
        ):
            raise HTTPException(
                status_code=422,
                detail="RunningHub 执行账号 ID 必须是非空且不重复的正整数列表",
            )
        if user.get("is_admin") is True and not selection_provided:
            raise HTTPException(
                status_code=422,
                detail="管理员画面生成必须至少选择一个 RunningHub 执行账号",
            )
        if seed_selection_provided and (
            not isinstance(selected_seedvr2_account_ids, list)
            or not selected_seedvr2_account_ids
            or any(type(account_id) is not int or account_id <= 0 for account_id in selected_seedvr2_account_ids)
            or len(set(selected_seedvr2_account_ids)) != len(selected_seedvr2_account_ids)
        ):
            raise HTTPException(status_code=422, detail="SeedVR2 执行账号 ID 必须是非空且不重复的正整数列表")
        try:
            coordinator = project_composition_coordinator(client)
            project = coordinator.start(
                user["user_id"],
                project_id,
                token,
                idempotency_key=str(payload.get("idempotency_key") or ""),
                resolution=str(payload.get("resolution") or "1024"),
                runninghub_execution_account_ids=(
                    list(selected_account_ids) if selection_provided else None
                ),
                seedvr2_execution_account_ids=(
                    list(selected_seedvr2_account_ids) if seed_selection_provided else None
                ),
                execution_mode=(
                    str(requested_execution_mode)
                    if requested_execution_mode is not None
                    else None
                ),
                item_ids=list(item_ids) if item_ids is not None else None,
            )
            if composition_start_dispatcher is None:
                raise ValueError("4A 后台协调器尚未启用")
            composition_start_dispatcher.submit(
                user["user_id"], project_id, token
            )
            return project
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/new/projects/{project_id}/composition/status")
    def sync_new_project_composition(
        project_id: str, request: Request
    ) -> dict[str, Any]:
        user = current_project_user(request)
        client, token = digital_human_access(request)
        try:
            coordinator = project_composition_coordinator(client)
            return coordinator.sync(
                user["user_id"], project_id, token
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/new/projects/{project_id}/items/{item_id}/composition/retry"
    )
    def retry_new_project_composition(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        if not settings.enable_legacy_new_workbench_video_engines:
            raise HTTPException(status_code=409, detail=NEW_WORKBENCH_H3_ONLY_DETAIL)
        client, token = digital_human_access(request)
        if payload.get("cost_confirmed") is not True:
            raise HTTPException(
                status_code=409,
                detail="请确认失败的 RunningHub 阶段重试可能再次产生费用",
            )
        try:
            return project_composition_coordinator(client).retry(
                user["user_id"],
                project_id,
                item_id,
                token,
                idempotency_key=str(
                    payload.get("idempotency_key") or uuid.uuid4().hex
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或画面任务不存在") from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/new/projects/{project_id}/items/{item_id}/composition/seedvr2-backfill"
    )
    def backfill_new_project_seedvr2(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        if not settings.enable_legacy_new_workbench_video_engines:
            raise HTTPException(status_code=409, detail=NEW_WORKBENCH_H3_ONLY_DETAIL)
        client, token = digital_human_access(request)
        if payload.get("cost_confirmed") is not True:
            raise HTTPException(
                status_code=409,
                detail="请确认 SeedVR2 48G 高清补跑会产生 RunningHub 费用",
            )
        try:
            return project_composition_coordinator(client).backfill_seedvr2(
                user["user_id"],
                project_id,
                item_id,
                token,
                idempotency_key=str(
                    payload.get("idempotency_key") or uuid.uuid4().hex
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或画面任务不存在") from exc
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/new/projects/{project_id}/items/{item_id}/base-video")
    def download_new_project_base_video(
        project_id: str, item_id: str, request: Request
    ) -> FileResponse:
        user = current_project_user(request)
        try:
            project = project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        item = next(
            (value for value in project["items"] if value["item_id"] == item_id),
            None,
        )
        base_video = item.get("outputs", {}).get("base_video") if item else None
        if not isinstance(base_video, dict) or not base_video.get("managed_path"):
            raise HTTPException(status_code=404, detail="基础视频尚未准备完成")
        path = Path(str(base_video["managed_path"])).resolve()
        try:
            path.relative_to(settings.storage_root.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="基础视频文件不存在") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="基础视频文件不存在")
        try:
            preview_path = browser_preview_path(
                path,
                settings.storage_root
                / "browser_previews"
                / str(project_id)
                / str(item_id),
            )
        except BrowserPreviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return FileResponse(
            preview_path,
            media_type="video/mp4",
            headers={
                "Cache-Control": "private, max-age=86400",
                "Content-Disposition": "inline",
            },
        )

    @app.get("/api/new/projects/{project_id}/items/{item_id}/preview-video")
    def preview_new_project_video(
        project_id: str, item_id: str, request: Request
    ) -> FileResponse:
        """Serve the audible H3 master while keeping the editable base silent."""

        user = current_project_user(request)
        try:
            project = project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        item = next(
            (value for value in project["items"] if value["item_id"] == item_id),
            None,
        )
        base_video = item.get("outputs", {}).get("base_video") if item else None
        if not isinstance(base_video, dict):
            raise HTTPException(status_code=404, detail="预览视频尚未准备完成")

        preview_asset = base_video
        base_metadata = (
            base_video.get("metadata")
            if isinstance(base_video.get("metadata"), dict)
            else {}
        )
        segment_signature = str(
            base_metadata.get("h3_segment_signature") or ""
        ).strip()
        if base_video.get("source_type") == "h3" and segment_signature:
            masters = (item or {}).get("asset_history", {}).get("h3_master_av", [])
            preview_asset = next(
                (
                    value
                    for value in reversed(masters)
                    if isinstance(value, dict)
                    and value.get("source_type") == "h3"
                    and value.get("status") == "READY"
                    and str(
                        (value.get("metadata") or {}).get("h3_segment_signature")
                        or ""
                    ).strip()
                    == segment_signature
                ),
                base_video,
            )

        path = Path(str(preview_asset.get("managed_path") or "")).resolve()
        try:
            path.relative_to(settings.storage_root.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="预览视频文件不存在") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="预览视频文件不存在")
        try:
            preview_path = browser_preview_path(
                path,
                settings.storage_root
                / "browser_previews"
                / str(project_id)
                / str(item_id),
            )
        except BrowserPreviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return FileResponse(
            preview_path,
            media_type="video/mp4",
            headers={
                "Cache-Control": "private, max-age=86400",
                "Content-Disposition": "inline",
            },
        )

    @app.get("/api/new/postprocess/options")
    def get_new_postprocess_options(request: Request) -> dict[str, Any]:
        current_project_user(request)
        fonts, bgm_assets = project_postprocess_resources()
        default_profile = layout_profile(DEFAULT_LAYOUT_PROFILE)
        default_caption = default_profile["caption"]
        return {
            "schema": "jyd.project-postprocess-options.v1",
            "default_font_identity": LAYOUT_PROFILE_FONT_IDENTITY,
            "caption": {
                "max_width_ratio": default_caption["max_width_ratio"],
                "max_lines": 1,
                "bottom_offset_ratio": 0.5 + default_caption["transform_y"] / 2,
                "transform_y": default_caption["transform_y"],
                "font_size": default_caption["font_size"],
                "clip_scale": default_caption["clip_scale"],
                "stroke_color": "#000000",
                "stroke_width": 0.0,
                "shadow_alpha": default_caption["shadow_alpha"],
            },
            "default_layout_profile": DEFAULT_LAYOUT_PROFILE,
            "layout_profiles": public_layout_profiles(),
            "fonts": [
                {
                    "identity": item.get("identity"),
                    "name": item.get("name"),
                    "preview_url": (
                        f"/api/assets/fonts/{quote(str(item.get('identity') or ''), safe='')}/file"
                    ),
                }
                for item in fonts
            ],
            "bgm": [
                {
                    "identity": item.get("identity"),
                    "name": item.get("name") or item.get("title") or item.get("filename"),
                    "preview_url": (
                        f"/api/audio-library/file?identity={quote(str(item.get('identity') or ''))}"
                    ),
                }
                for item in bgm_assets
            ],
        }

    @app.post("/api/new/projects/{project_id}/postprocess/generate")
    def generate_new_project_postprocess(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        items = payload.get("items")
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise HTTPException(status_code=422, detail="字幕与背景音乐参数格式不正确")
        try:
            requested_item_ids = [
                str(item.get("item_id") or "").strip()
                for item in items
                if str(item.get("item_id") or "").strip()
            ]
            current_project = project_store.get_project(user["user_id"], project_id)
            project_template = dict(current_project.get("settings") or {}).get(
                "jianying_template"
            )
            trusted_template: dict[str, Any] | None = None
            if isinstance(project_template, dict) and project_template.get("template_id"):
                trusted_template = user_template_store.render_binding(
                    user["user_id"], str(project_template["template_id"])
                )
            items = [dict(item) for item in items]
            for item in items:
                item.pop("jianying_template", None)
                if trusted_template is not None:
                    item["jianying_template"] = trusted_template
            requested_set = set(requested_item_ids)
            needs_seam_analysis = any(
                str(item.get("item_id") or "") in requested_set
                and bool(project_segment_boundaries(item))
                for item in current_project.get("items", [])
            )
            if needs_seam_analysis:
                try:
                    client, token = digital_human_access(request)
                    project_visual_analysis_coordinator(client).supplement_seams(
                        user["user_id"],
                        project_id,
                        token,
                        item_ids=requested_item_ids,
                    )
                except Exception as exc:
                    # Seam B-roll is opportunistic. A missing/failed cloud match
                    # must leave the existing dissolve available for 4B.
                    render_logger.warning(
                        "[postprocess] 连接处空镜补分析失败，继续生成原转场: %s",
                        exc,
                    )
            return project_postprocess_coordinator().start(
                user["user_id"],
                project_id,
                idempotency_key=str(payload.get("idempotency_key") or ""),
                item_settings=items,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.patch(
        "/api/new/projects/{project_id}/items/{item_id}/postprocess-settings"
    )
    def configure_new_project_postprocess_settings(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        font_identity = str(payload.get("font_identity") or "").strip()
        bgm_identity = str(payload.get("bgm_identity") or "").strip()
        bgm_selection_mode = str(
            payload.get("bgm_selection_mode") or "manual"
        ).strip().lower()
        text_color = str(payload.get("text_color") or "#FFFFFF").strip().upper()
        try:
            layout_profile_id = (
                normalize_layout_profile(payload.get("layout_profile"))
                if "layout_profile" in payload
                else None
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        fonts, bgm_assets = project_postprocess_resources()
        available_fonts = {str(item.get("identity") or "") for item in fonts}
        available_bgm = {str(item.get("identity") or "") for item in bgm_assets}
        if font_identity not in available_fonts:
            raise HTTPException(status_code=422, detail="字幕字体不可用")
        if bgm_identity and bgm_identity not in available_bgm:
            raise HTTPException(status_code=422, detail="BGM 素材不可用")
        if bgm_selection_mode not in {"auto", "manual"}:
            raise HTTPException(status_code=422, detail="BGM 选择模式不合法")
        if bgm_selection_mode == "auto" and bgm_identity:
            raise HTTPException(status_code=422, detail="AI 自动匹配时不能预设 BGM")
        if re.fullmatch(r"#[0-9A-F]{6}", text_color) is None:
            raise HTTPException(status_code=422, detail="字幕颜色格式不正确")
        try:
            top_title = (
                normalize_top_title(payload.get("top_title"))
                if "top_title" in payload
                else None
            )
            cover_title = (
                normalize_cover_title(payload.get("cover_title"))
                if "cover_title" in payload
                else None
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            return project_store.configure_item_postprocess(
                user["user_id"],
                project_id,
                item_id,
                font_identity=font_identity,
                bgm_identity=bgm_identity,
                text_color=text_color,
                bgm_selection_mode=bgm_selection_mode,
                top_title=top_title,
                cover_title=cover_title,
                layout_profile=layout_profile_id,
                force_invalidate=payload.get("force_retry") is True,
                preserve_auto_bgm=payload.get("preserve_auto_bgm") is True,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/new/projects/{project_id}/postprocess/status")
    def sync_new_project_postprocess(
        project_id: str, request: Request
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_postprocess_coordinator().sync(
                user["user_id"], project_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/new/variant-options")
    def get_new_variant_options(request: Request) -> dict[str, Any]:
        current_project_user(request)
        return project_variant_coordinator().options()

    @app.patch("/api/new/projects/{project_id}/variant-settings")
    def configure_new_project_variant_settings(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        items = payload.get("items", [])
        try:
            return project_store.configure_variant_settings(
                user["user_id"], project_id,
                settings=payload.get("settings") if isinstance(payload.get("settings"), dict) else None,
                items=items if isinstance(items, list) else [],
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/new/projects/{project_id}/variants/generate")
    def generate_new_project_variants(
        project_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        items = payload.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise HTTPException(status_code=422, detail="变体任务参数格式不正确")
        try:
            return project_variant_coordinator().start(
                user["user_id"],
                project_id,
                idempotency_key=str(payload.get("idempotency_key") or ""),
                settings=payload.get("settings") if isinstance(payload.get("settings"), dict) else None,
                items=items,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/new/projects/{project_id}/variants/status")
    def sync_new_project_variants(project_id: str, request: Request) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_variant_coordinator().sync(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/new/projects/{project_id}/items/{item_id}/variants/supplement")
    def supplement_new_project_variants(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_variant_coordinator().supplement(
                user["user_id"], project_id, item_id,
                idempotency_key=str(payload.get("idempotency_key") or ""),
                count=int(payload.get("count") or 0),
                settings=payload.get("settings") if isinstance(payload.get("settings"), dict) else None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/new/projects/{project_id}/items/{item_id}/variants/retry")
    def retry_new_project_variants(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_variant_coordinator().retry(
                user["user_id"], project_id, item_id,
                idempotency_key=str(payload.get("idempotency_key") or ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _project_variant_asset(
        owner_user_id: str, project_id: str, item_id: str, asset_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        project = project_store.get_project(owner_user_id, project_id)
        item = next((value for value in project["items"] if value["item_id"] == item_id), None)
        asset = next(
            (
                value for value in (item or {}).get("outputs", {}).get("variants", [])
                if value.get("asset_id") == asset_id
            ),
            None,
        )
        if item is None or asset is None:
            raise KeyError("变体不存在")
        return item, asset

    @app.get("/api/new/projects/{project_id}/items/{item_id}/variants/{asset_id}")
    def download_new_project_variant(
        project_id: str, item_id: str, asset_id: str, request: Request
    ) -> FileResponse:
        user = current_project_user(request)
        try:
            item, asset = _project_variant_asset(user["user_id"], project_id, item_id, asset_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="变体不存在") from exc
        path = Path(str(asset.get("managed_path") or "")).resolve()
        if not path.is_file() or not is_managed_project_file(path):
            raise HTTPException(status_code=404, detail="变体文件不存在")
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=variant_export_filename(item, asset),
        )

    @app.delete("/api/new/projects/{project_id}/items/{item_id}/variants/{asset_id}")
    def delete_new_project_variant(
        project_id: str, item_id: str, asset_id: str, request: Request
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            managed_path = project_store.delete_variant_asset(
                user["user_id"], project_id, item_id, asset_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="变体不存在") from exc
        if managed_path:
            path = Path(managed_path).resolve()
            if is_managed_project_file(path) and path.is_file():
                path.unlink()
        return project_store.get_project(user["user_id"], project_id)

    @app.get("/api/new/gallery")
    def list_new_project_gallery(
        request: Request,
        project_id: str = "",
        status: str = "",
        keyword: str = "",
        date_key: str = "",
        batch_no: int | None = None,
    ) -> dict[str, Any]:
        user = current_project_user(request)
        return project_result_library.list_results(
            user["user_id"],
            project_id=project_id,
            status=status,
            keyword=keyword,
            date_key=date_key,
            batch_no=batch_no,
        )

    @app.post("/api/new/gallery/downloads")
    def download_new_gallery_selection(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> FileResponse:
        user = current_project_user(request)
        requested = payload.get("asset_ids")
        if not isinstance(requested, list):
            raise HTTPException(status_code=422, detail="请选择需要打包的成果视频")
        asset_ids = list(dict.fromkeys(str(value or "").strip() for value in requested))
        if not asset_ids or len(asset_ids) > 500:
            raise HTTPException(status_code=422, detail="单次可打包 1 到 500 个成果视频")
        library = project_result_library.list_results(user["user_id"])
        available = {
            video["asset_id"]: video
            for batch in library["batches"]
            for video in batch["videos"]
        }
        selected = []
        project_cache: dict[str, dict[str, Any]] = {}
        for asset_id in asset_ids:
            video = available.get(asset_id)
            if video is None:
                raise HTTPException(status_code=404, detail="成果视频不存在或无权访问")
            path = Path(str(video.get("managed_path") or "")).resolve()
            if not path.is_file() or not is_managed_project_file(path):
                raise HTTPException(status_code=404, detail=f"成果文件不存在: {video.get('filename')}")
            project_id = str(video.get("project_id") or "")
            project = project_cache.get(project_id)
            if project is None:
                project = project_store.get_project(user["user_id"], project_id)
                project_cache[project_id] = project
            item = next(
                (
                    entry
                    for entry in project.get("items", [])
                    if entry.get("item_id") == video.get("item_id")
                ),
                {},
            )
            selected.append((path, variant_export_filename(item, video)))
        archive_root = settings.storage_root / "gallery_downloads"
        archive_root.mkdir(parents=True, exist_ok=True)
        archive_path = archive_root / f"{uuid.uuid4().hex}.zip"
        used: dict[str, int] = {}
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as archive:
            for path, filename in selected:
                safe_name = _safe_filename(filename)
                used[safe_name] = used.get(safe_name, 0) + 1
                count = used[safe_name]
                if count > 1:
                    source = Path(safe_name)
                    safe_name = f"{source.stem}-{count:02d}{source.suffix}"
                archive.write(path, arcname=safe_name)
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"成果视频-{datetime.now().strftime('%m.%d')}.zip",
            background=BackgroundTask(_unlink_if_exists, archive_path),
        )

    @app.post("/api/new/gallery/deletions")
    def delete_new_gallery_selection(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, int]:
        user = current_project_user(request)
        requested = payload.get("asset_ids")
        if not isinstance(requested, list):
            raise HTTPException(status_code=422, detail="请选择需要删除的成果视频")
        asset_ids = [
            asset_id
            for asset_id in dict.fromkeys(
                str(value or "").strip() for value in requested
            )
            if asset_id
        ]
        if not asset_ids or len(asset_ids) > 500:
            raise HTTPException(status_code=422, detail="单次可删除 1 到 500 个成果视频")
        try:
            deleted = project_store.delete_variant_assets(user["user_id"], asset_ids)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="成果视频不存在或无权访问"
            ) from exc
        file_deleted_count = 0
        for record in deleted:
            managed_path = str(record.get("managed_path") or "").strip()
            if not managed_path:
                continue
            path = Path(managed_path).resolve()
            if is_managed_project_file(path) and path.is_file():
                path.unlink()
                file_deleted_count += 1
        return {
            "deleted_count": len(deleted),
            "file_deleted_count": file_deleted_count,
        }

    @app.post(
        "/api/new/projects/{project_id}/items/{item_id}/postprocess/export"
    )
    def export_new_project_browser_preview(
        project_id: str,
        item_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            return project_postprocess_coordinator().export_preview(
                user["user_id"],
                project_id,
                item_id,
                idempotency_key=str(payload.get("idempotency_key") or ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目或脚本行不存在") from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/new/projects/{project_id}/items/{item_id}/current-video")
    def download_new_project_current_video(
        project_id: str, item_id: str, request: Request
    ) -> FileResponse:
        user = current_project_user(request)
        try:
            project = project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        item = next(
            (value for value in project["items"] if value["item_id"] == item_id),
            None,
        )
        video = item.get("outputs", {}).get("composition_video") if item else None
        if not isinstance(video, dict) or not video.get("managed_path"):
            raise HTTPException(status_code=404, detail="字幕与 BGM 成片尚未准备完成")
        path = Path(str(video["managed_path"])).resolve()
        try:
            path.relative_to(settings.storage_root.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="画面合成视频不存在") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="画面合成视频不存在")
        media_type = {
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".avi": "video/x-msvideo",
            ".mkv": "video/x-matroska",
            ".webm": "video/webm",
        }.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(
            path,
            media_type=media_type,
            filename=composition_export_filename(item, video),
        )

    @app.get("/api/new/projects/{project_id}/videos/download")
    def download_new_project_current_videos(
        project_id: str, request: Request, item_ids: str = ""
    ) -> FileResponse:
        """Download selected, or every, pre-variant current video in one temporary ZIP."""

        user = current_project_user(request)
        try:
            project = project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        items = list(project.get("items") or [])
        if not items:
            raise HTTPException(status_code=409, detail="当前项目没有可下载的视频")
        requested_ids = {
            value.strip() for value in str(item_ids or "").split(",") if value.strip()
        }
        if requested_ids:
            existing_ids = {str(item.get("item_id") or "") for item in items}
            unknown_ids = requested_ids - existing_ids
            if unknown_ids:
                raise HTTPException(status_code=422, detail="下载范围包含不属于当前项目的任务")
            items = [
                item for item in items if str(item.get("item_id") or "") in requested_ids
            ]
        selected: list[tuple[Path, str]] = []
        for item in items:
            video = (item.get("outputs") or {}).get("composition_video")
            if not isinstance(video, dict) or not video.get("managed_path"):
                continue
            path = Path(str(video["managed_path"])).resolve()
            if not is_managed_project_file(path) or not path.is_file():
                raise HTTPException(
                    status_code=404,
                    detail=f"任务 {item.get('row_key')} 的未变体成片文件不存在",
                )
            selected.append(
                (
                    path,
                    _safe_filename(composition_export_filename(item, video)),
                )
            )
        if not selected:
            raise HTTPException(status_code=409, detail="当前项目没有已导出的未变体成片可下载")
        archive_root = settings.storage_root / "project_downloads"
        archive_root.mkdir(parents=True, exist_ok=True)
        archive_path = archive_root / f"{uuid.uuid4().hex}.zip"
        used: dict[str, int] = {}
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as archive:
            for path, filename in selected:
                used[filename] = used.get(filename, 0) + 1
                count = used[filename]
                archive_name = filename
                if count > 1:
                    source = Path(filename)
                    archive_name = f"{source.stem}-{count:02d}{source.suffix}"
                archive.write(path, arcname=archive_name)
        project_name = _safe_filename(str(project.get("name") or "数字人成片"))
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{project_name}-未变体视频.zip",
            background=BackgroundTask(_unlink_if_exists, archive_path),
        )

    @app.get(
        "/api/new/projects/{project_id}/items/{item_id}/original-materials"
    )
    def download_new_project_original_materials(
        project_id: str, item_id: str, request: Request
    ) -> FileResponse:
        user = current_project_user(request)
        try:
            project = project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        item = next(
            (entry for entry in project["items"] if entry["item_id"] == item_id),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="脚本行不存在")
        segments = list(item.get("outputs", {}).get("original_video_segments") or [])
        segments.sort(
            key=lambda entry: int(
                (entry.get("external_ref") or {}).get("video_index") or 0
            )
        )
        if not segments:
            raise HTTPException(status_code=404, detail="当前视频没有可下载的原始片段")
        resolved: list[tuple[dict[str, Any], Path]] = []
        for segment in segments:
            path = Path(str(segment.get("managed_path") or "")).resolve()
            try:
                path.relative_to(settings.storage_root.resolve())
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="原始片段文件不存在") from exc
            if not path.is_file():
                raise HTTPException(status_code=404, detail="原始片段文件不存在")
            resolved.append((segment, path))
        if len(resolved) == 1:
            segment, path = resolved[0]
            return FileResponse(
                path,
                media_type="video/mp4",
                filename=segment_export_filename(item, segment, index=1),
            )

        archive_root = settings.storage_root / "temporary_downloads"
        archive_root.mkdir(parents=True, exist_ok=True)
        archive_path = archive_root / f"{uuid.uuid4().hex}.zip"
        manifest: list[dict[str, Any]] = []
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as archive:
            for sequence, (segment, path) in enumerate(resolved, start=1):
                archive_name = segment_export_filename(item, segment, index=sequence)
                archive.write(path, archive_name)
                manifest.append(
                    {
                        "order": sequence,
                        "filename": archive_name,
                        "asset_id": segment.get("asset_id"),
                        "video_index": (segment.get("external_ref") or {}).get(
                            "video_index"
                        ),
                        "start_seconds": (segment.get("metadata") or {}).get(
                            "start_seconds"
                        ),
                        "end_seconds": (segment.get("metadata") or {}).get(
                            "end_seconds"
                        ),
                    }
                )
            archive.writestr(
                "片段顺序清单.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{project_item_export_stem(item)}-原始片段.zip",
            background=BackgroundTask(_unlink_if_exists, archive_path),
        )

    @app.post(
        "/api/new/projects/{project_id}/items/{item_id}/current-video"
    )
    async def upload_new_project_current_video(
        project_id: str,
        item_id: str,
        request: Request,
        filename: str = "",
    ) -> dict[str, Any]:
        user = current_project_user(request)
        try:
            project = project_store.get_project(user["user_id"], project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        item = next(
            (entry for entry in project["items"] if entry["item_id"] == item_id),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="脚本行不存在")
        if not item.get("allowed_actions", {}).get("upload_current_video"):
            raise HTTPException(status_code=409, detail="当前脚本行暂时不能替换视频")

        original_filename = filename or request.headers.get("x-filename") or "upload.mp4"
        safe_name = _safe_filename(original_filename)
        suffix = Path(safe_name).suffix.lower()
        allowed_suffixes = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
        if suffix not in allowed_suffixes:
            raise HTTPException(status_code=422, detail=f"不支持的视频格式: {suffix or '无扩展名'}")
        declared_size = request.headers.get("content-length", "").strip()
        if declared_size:
            try:
                if int(declared_size) > settings.max_video_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"上传文件超过限制 {_format_bytes(settings.max_video_upload_bytes)}",
                    )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Content-Length 不合法") from exc

        directory = (
            settings.storage_root
            / "projects"
            / str(user["user_id"])
            / project_id
            / item_id
            / "uploads"
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid.uuid4().hex}_{safe_name}"
        size = 0
        try:
            with path.open("wb") as output:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > settings.max_video_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"上传文件超过限制 {_format_bytes(settings.max_video_upload_bytes)}",
                        )
                    output.write(chunk)
            if size <= 0:
                raise HTTPException(status_code=400, detail="上传文件为空")
            project_store.add_asset(
                owner_user_id=user["user_id"],
                project_id=project_id,
                item_id=item_id,
                asset_type="composition_video",
                source_type="user_upload",
                status="READY",
                filename=safe_name,
                managed_path=str(path),
                metadata={"size": size, "content_type": request.headers.get("content-type")},
                make_current=True,
            )
        except BaseException:
            _unlink_if_exists(path)
            raise
        return project_store.get_project(user["user_id"], project_id)

    @app.get("/api/digital-human/tasks")
    def list_digital_human_tasks(request: Request, limit: int = 50) -> dict[str, Any]:
        client, token = digital_human_access(request)
        try:
            tasks = client.list_workbench_tasks(token, limit=limit)
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {
            "schema": "jyd.digital-human-inbox.v1",
            "source_url": client.base_url,
            "tasks": tasks,
        }

    @app.post("/api/digital-human/tasks/{item_id}/import")
    def import_digital_human_task(item_id: str, request: Request) -> dict[str, Any]:
        require_local_file_access(request)
        client, token = digital_human_access(request)
        try:
            task = client.get_workbench_task(token, item_id)
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if task.get("status") != "AUTO_READY" or task.get("mode") != "AUTO_POSTPROCESS":
            raise HTTPException(status_code=409, detail="这个任务需要人工处理，不能直接导入自动后期")
        videos = task.get("source", {}).get("videos", [])
        if not isinstance(videos, list) or len(videos) != 1:
            raise HTTPException(status_code=409, detail="自动后期任务必须只有一个完整视频")
        video = videos[0]
        if not isinstance(video, dict) or video.get("status") != "SUCCESS":
            raise HTTPException(status_code=409, detail="数字人视频尚未生成完成")

        stable = hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:24]
        media_id = f"digital_{stable}"
        filename = _safe_filename(f"{task.get('row_key') or item_id}.mp4")
        media_dir = settings.storage_root / "media" / "video"
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / f"{media_id}_{filename}"
        temporary = path.with_suffix(path.suffix + f".tmp.{uuid.uuid4().hex}")
        try:
            size = client.download_workbench_video(
                token,
                item_id,
                int(video.get("index", 1)),
                temporary,
                max_bytes=settings.max_video_upload_bytes,
            )
            temporary.replace(path)
        except AuthCenterError as exc:
            _unlink_if_exists(temporary)
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        record = {
            "media_id": media_id,
            "kind": "video",
            "filename": filename,
            "path": str(path),
            "size": size,
            "storage_mode": "local_reference",
            "source": "digital_human",
            "source_item_id": item_id,
            "created_at": _now(),
            "expires_at": _expiry_after(settings.media_retention_hours),
        }
        _write_json(_media_meta_path(settings, media_id), record)
        return {
            "ok": True,
            "task": task,
            "captions": task.get("captions"),
            "media": {
                **record,
                "preview_url": f"/api/local/media/{media_id}/preview",
            },
        }

    @app.get("/api/digital-human/tasks/{item_id}/videos/{video_index}")
    def download_digital_human_video(
        item_id: str, video_index: int, request: Request
    ) -> FileResponse:
        client, token = digital_human_access(request)
        download_root = settings.storage_root / "temporary_downloads"
        download_root.mkdir(parents=True, exist_ok=True)
        path = download_root / f"{uuid.uuid4().hex}.mp4"
        try:
            client.download_workbench_video(
                token,
                item_id,
                video_index,
                path,
                max_bytes=settings.max_video_upload_bytes,
            )
        except AuthCenterError as exc:
            _unlink_if_exists(path)
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=f"{_safe_download_stem(item_id)}-segment-{video_index}.mp4",
            background=BackgroundTask(_unlink_if_exists, path),
        )

    @app.post("/api/auth/logout")
    def site_logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        site_auth.clear_session_cookie(response)
        admin_auth.clear_session_cookie(response)
        return response

    @app.post("/api/auth/center/login")
    def auth_center_login(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not settings.auth_authority:
            raise HTTPException(status_code=404, detail="当前处理端不是统一账号中心")
        user = site_auth.authenticate(
            str(payload.get("username", "")), str(payload.get("password", ""))
        )
        if user is None:
            raise HTTPException(status_code=401, detail="账号或密码错误")
        return {"ok": True, "access_token": site_auth.issue_token(user), "user": user}

    @app.post("/api/auth/center/verify")
    def auth_center_verify(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not settings.auth_authority:
            raise HTTPException(status_code=404, detail="当前处理端不是统一账号中心")
        user = site_auth.verify_token(str(payload.get("access_token", "")))
        if user is None:
            raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
        return {"valid": True, "user": user}

    @app.post("/api/auth/center/handoff")
    def auth_center_create_handoff(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not settings.auth_authority:
            raise HTTPException(status_code=404, detail="当前处理端不是统一账号中心")
        access_token = str(payload.get("access_token", ""))
        if site_auth.verify_token(access_token) is None:
            raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
        return {
            "handoff_code": auth_handoffs.issue(access_token),
            "expires_in": auth_handoffs.lifetime_seconds,
        }

    @app.get("/api/auth/handoff")
    def auth_center_accept_handoff(code: str = "", next: str = "/app") -> Response:
        if settings.auth_authority:
            access_token = auth_handoffs.consume(code)
            user = site_auth.verify_token(access_token or "")
            if user is not None:
                access_token = site_auth.issue_token(user)
        else:
            try:
                result = auth_center.consume_handoff(code) if auth_center else {}
            except AuthCenterError:
                result = {}
            access_token = str(result.get("access_token", ""))
            user = result.get("user") if isinstance(result.get("user"), dict) else None
        if user is None or not access_token:
            return RedirectResponse("/login?next=/app", status_code=303)
        response = RedirectResponse(_safe_site_next(next), status_code=303)
        set_site_token_cookie(response, access_token)
        return response

    @app.post("/api/auth/local-handoff")
    def create_local_auth_handoff(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        manager_token = request.headers.get("x-workbench-manager-token", "")
        if not runtime_control.manager_authorized(manager_token):
            raise HTTPException(status_code=403, detail="无权创建本地登录接力")
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise HTTPException(status_code=422, detail="本地登录接力数据无效")
        return {
            "handoff_code": auth_handoffs.issue(access_token),
            "expires_in": auth_handoffs.lifetime_seconds,
        }

    @app.get("/api/auth/local-handoff")
    def accept_local_auth_handoff(code: str = "", next: str = "/app") -> Response:
        access_token = auth_handoffs.consume(code)
        if not access_token:
            raise HTTPException(status_code=401, detail="本地登录接力码无效或已过期")
        response = RedirectResponse(_safe_site_next(next), status_code=303)
        set_site_token_cookie(response, access_token)
        return response

    @app.get("/api/auth/handoff-to")
    def auth_handoff_to_target(request: Request, target: str = "", next: str = "/app") -> Response:
        if settings.auth_authority or auth_center is None:
            raise HTTPException(status_code=409, detail="当前处理机尚未接入云端统一账号中心")
        destinations = {
            "local": "http://127.0.0.1:8010",
            "shared": settings.shared_processor_url.rstrip("/"),
            "ltx": (
                settings.ltx_workbench_url
                or _default_ltx_workbench_url(settings.auth_server_url)
            ).rstrip("/"),
        }
        destination = destinations.get(target)
        if not destination:
            detail = "尚未设置其他工作台" if target == "shared" else "处理位置无效"
            raise HTTPException(status_code=400, detail=detail)
        handoff_path = "/api/auth/handoff"
        target_next = _safe_site_next(next)
        if target == "ltx":
            parsed_destination = urlparse(destination)
            if (
                parsed_destination.scheme not in {"http", "https"}
                or not parsed_destination.netloc
            ):
                raise HTTPException(status_code=500, detail="对口型工作台地址配置无效")
            destination = (
                f"{parsed_destination.scheme}://{parsed_destination.netloc}"
            )
            handoff_path = "/api/session/handoff"
            target_next = _safe_peer_next(next)
        current_origin = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
        if destination == current_origin:
            return RedirectResponse(_safe_site_next(next), status_code=303)
        access_token = request.cookies.get(site_auth.cookie_name, "")
        if target == "ltx" and runtime_control.manager_token:
            user = verify_site_token(access_token)
            if user is None:
                raise HTTPException(status_code=401, detail="当前登录已经失效，请重新登录")
            try:
                code = create_local_workbench_handoff(
                    destination,
                    runtime_control.manager_token,
                    {"access_token": access_token, "user": user},
                    path="/api/session/local-handoff",
                )
                redirect_url = (
                    f"{destination}/api/session/local-handoff"
                    f"?code={quote(code, safe='')}&next={quote(target_next, safe='/')}"
                )
                return RedirectResponse(redirect_url, status_code=303)
            except AuthCenterError:
                pass
        try:
            code = auth_center.create_handoff(access_token)
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        redirect_url = (
            f"{destination}{handoff_path}"
            f"?code={quote(code, safe='')}&next={quote(target_next, safe='/')}"
        )
        return RedirectResponse(redirect_url, status_code=303)

    @app.get("/api/auth/handoff-to-center")
    def auth_handoff_to_center(request: Request, next: str = "/app") -> Response:
        if settings.auth_authority:
            return RedirectResponse(_safe_site_next(next), status_code=303)
        access_token = request.cookies.get(site_auth.cookie_name, "")
        try:
            code = auth_center.create_handoff(access_token) if auth_center else ""
        except AuthCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        if not code:
            raise HTTPException(status_code=503, detail="统一账号中心没有返回登录接力码")
        destination = (
            f"{settings.auth_server_url}/api/auth/handoff"
            f"?code={quote(code, safe='')}&next={quote(_safe_site_next(next), safe='/')}"
        )
        return RedirectResponse(destination, status_code=303)

    @app.get("/admin/login")
    def admin_login_frontend(request: Request):
        if not settings.auth_authority:
            return RedirectResponse(f"{settings.auth_server_url}/admin", status_code=303)
        token = request.cookies.get(admin_auth.cookie_name, "")
        if admin_auth.verify_token(token):
            return RedirectResponse("/app/assets", status_code=303)
        index_path = FRONTEND_ROOT / "admin-login.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail=f"管理员登录页面不存在: {index_path}")
        return FileResponse(index_path)

    @app.get("/local-admin/login")
    def local_admin_login_frontend(request: Request):
        token = request.cookies.get(admin_auth.cookie_name, "")
        if admin_auth.verify_token(token):
            return RedirectResponse("/app/assets", status_code=303)
        index_path = FRONTEND_ROOT / "admin-login.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail=f"管理员登录页面不存在: {index_path}")
        return FileResponse(index_path)

    @app.post("/api/admin/login")
    def admin_login(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        if not admin_auth.authenticate(username, password):
            raise HTTPException(status_code=401, detail="管理员账号或密码错误")
        next_path = _safe_admin_next(str(payload.get("next", "")))
        response = JSONResponse({"ok": True, "next": next_path})
        admin_auth.set_session_cookie(response)
        return response

    @app.get("/api/admin/session")
    def admin_session(request: Request) -> dict[str, Any]:
        authenticated = admin_auth.verify_token(
            request.cookies.get(admin_auth.cookie_name, "")
        )
        return {
            "authenticated": authenticated,
            "username": admin_auth.username if authenticated else "",
        }

    @app.post("/api/admin/logout")
    def admin_logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        admin_auth.clear_session_cookie(response)
        return response

    @app.get("/api/admin/users")
    def list_site_users() -> dict[str, Any]:
        if not settings.auth_authority:
            return {
                "users": [],
                "total": 0,
                "enabled": 0,
                "managed_remotely": True,
                "auth_server_url": settings.auth_server_url,
            }
        users = site_auth.list_users()
        return {
            "users": users,
            "total": len(users),
            "enabled": sum(1 for item in users if item.get("enabled")),
        }

    @app.post("/api/admin/users")
    def create_site_user(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not settings.auth_authority:
            raise HTTPException(
                status_code=409,
                detail=f"账号统一由 {settings.auth_server_url}/admin 管理",
            )
        try:
            return site_auth.create_user(
                str(payload.get("username", "")),
                str(payload.get("password", "")),
                display_name=str(payload.get("display_name", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/admin/users/{user_id}")
    def update_site_user(user_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not settings.auth_authority:
            raise HTTPException(
                status_code=409,
                detail=f"账号统一由 {settings.auth_server_url}/admin 管理",
            )
        enabled = payload.get("enabled") if "enabled" in payload else None
        if enabled is not None and not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled 必须是布尔值")
        try:
            return site_auth.update_user(
                user_id,
                display_name=str(payload.get("display_name", ""))
                if "display_name" in payload
                else None,
                enabled=enabled,
                password=str(payload.get("password", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/admin/users/{user_id}")
    def delete_site_user(user_id: str) -> dict[str, Any]:
        if not settings.auth_authority:
            raise HTTPException(
                status_code=409,
                detail=f"账号统一由 {settings.auth_server_url}/admin 管理",
            )
        try:
            return {"deleted": True, "user": site_auth.delete_user(user_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/admin")
    def admin_root() -> RedirectResponse:
        if not settings.auth_authority:
            return RedirectResponse(f"{settings.auth_server_url}/admin", status_code=303)
        return RedirectResponse("/app/assets", status_code=303)

    @app.get("/app/advanced")
    def advanced_frontend() -> FileResponse:
        index_path = FRONTEND_ROOT / "advanced.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail=f"高级前端文件不存在: {index_path}")
        return FileResponse(index_path)

    @app.get("/app/assets")
    def asset_admin_frontend() -> FileResponse:
        index_path = FRONTEND_ROOT / "assets.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail=f"素材管理页面不存在: {index_path}")
        return FileResponse(index_path)

    @app.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        agents = render_queue.list_agents()
        online_agents = [item for item in agents if item.get("status") != "offline"]
        pending_jobs = render_queue.store.pending_count()
        active_jobs = render_queue.store.active_count()
        running_jobs = max(0, active_jobs - pending_jobs)
        return {
            "ok": True,
            "execution_mode": settings.execution_mode,
            "online_agents": len(online_agents),
            "busy_agents": sum(1 for item in online_agents if item.get("status") == "busy"),
            "pending_jobs": pending_jobs,
            "running_jobs": running_jobs,
            "active_jobs": active_jobs,
            "workspace_status": "busy" if active_jobs else "idle",
            "local_file_access": has_local_file_access(request),
            "auth_authority": settings.auth_authority,
            "auth_server_url": settings.auth_server_url,
            "shared_processor_url": settings.shared_processor_url,
        }

    @app.get("/api/local/config")
    def get_local_mode_config(request: Request) -> dict[str, Any]:
        require_local_file_access(request)
        personal_root = settings.personal_library_root or (PROJECT_ROOT / "data" / "personal_libraries")
        personal_root.mkdir(parents=True, exist_ok=True)
        return {
            "mode": "standalone",
            "personal_library_root": str(personal_root.resolve()),
        }

    @app.get("/api/agents")
    def list_render_agents() -> list[dict[str, Any]]:
        return render_queue.list_agents()

    @app.post("/api/agents/register")
    def register_render_agent(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        require_agent_token(request)
        agent_id = str(_required(payload, "agent_id"))
        try:
            return render_queue.register_agent(agent_id, payload)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/agents/{agent_id}/heartbeat")
    def heartbeat_render_agent(
        agent_id: str, request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        require_agent_token(request)
        try:
            return render_queue.heartbeat_agent(agent_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/agents/{agent_id}/claim")
    def claim_render_job(agent_id: str, request: Request) -> dict[str, Any]:
        require_agent_token(request)
        try:
            return {"job": render_queue.claim_agent_job(agent_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/agents/{agent_id}/jobs/{job_id}/heartbeat")
    def heartbeat_render_job(
        agent_id: str,
        job_id: str,
        request: Request,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        require_agent_token(request)
        try:
            return render_queue.heartbeat_agent_job(agent_id, job_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/agents/{agent_id}/jobs/{job_id}/complete")
    def complete_render_job(
        agent_id: str,
        job_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        require_agent_token(request)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise HTTPException(status_code=400, detail="result 必须是对象")
        try:
            return render_queue.finish_agent_job(agent_id, job_id, result=result)
        except KeyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/agents/{agent_id}/jobs/{job_id}/fail")
    def fail_render_job(
        agent_id: str,
        job_id: str,
        request: Request,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        require_agent_token(request)
        try:
            return render_queue.finish_agent_job(
                agent_id, job_id, error=str(payload.get("error") or "处理机报告任务失败")
            )
        except KeyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/storage")
    def get_storage_status() -> dict[str, Any]:
        return storage_lifecycle.status()

    @app.post("/api/storage/cleanup")
    def cleanup_storage(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return storage_lifecycle.cleanup(dry_run=_as_bool(payload.get("dry_run", False)))

    @app.get("/api/assets/text-styles")
    def list_text_styles() -> list[dict[str, Any]]:
        return asset_admin.decorate(
            "text_style",
            _combined_library_items(settings, TEXT_STYLE_LIBRARY_ROOT, "text_style_library", _list_json_library),
        )

    @app.get("/api/assets/fonts")
    def list_fonts() -> list[dict[str, Any]]:
        return asset_admin.decorate(
            "font",
            _combined_library_items(settings, FONT_LIBRARY_ROOT, "font_library", _list_font_library),
        )

    @app.get("/api/assets/fonts/{font_identity}/file")
    def get_font_file(font_identity: str) -> FileResponse:
        font = next(
            (
                item
                for item in (
                    _list_system_fonts()
                    + _combined_library_items(
                        settings, FONT_LIBRARY_ROOT, "font_library", _list_font_library
                    )
                )
                if item.get("identity") == font_identity
            ),
            None,
        )
        if font is None or not font.get("available"):
            raise HTTPException(status_code=404, detail=f"字体不存在或不可用: {font_identity}")
        path = Path(str(font["path"]))
        suffix = path.suffix.lower()
        media_type = {
            ".otf": "font/otf",
            ".ttf": "font/ttf",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
        }.get(suffix, "application/octet-stream")
        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.get("/api/assets/text-styles/{style_name}/font")
    def get_text_style_font(style_name: str) -> FileResponse:
        safe_name = Path(style_name).name
        style_path = next(
            (
                root / f"{safe_name}.json"
                for root in _library_roots(settings, TEXT_STYLE_LIBRARY_ROOT, "text_style_library")
                if (root / f"{safe_name}.json").is_file()
            ),
            Path(),
        )
        if not style_path.is_file():
            raise HTTPException(status_code=404, detail=f"文字样式不存在: {safe_name}")
        data = _read_json(style_path)
        preview = _text_style_preview(data)
        font_path = Path(str(preview.get("font_path", "")))
        if not font_path.exists() or not font_path.is_file():
            raise HTTPException(status_code=404, detail=f"文字样式字体文件不存在: {font_path}")
        suffix = font_path.suffix.lower()
        media_type = {
            ".otf": "font/otf",
            ".ttf": "font/ttf",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
        }.get(suffix, "application/octet-stream")
        return FileResponse(font_path, media_type=media_type, filename=font_path.name)

    @app.get("/api/assets/effects")
    def list_effects() -> list[dict[str, Any]]:
        return asset_admin.decorate(
            "effect",
            _combined_library_items(settings, EFFECT_LIBRARY_ROOT, "effect_library", _list_json_library),
        )

    @app.get("/api/assets/text-effects")
    def list_text_effects() -> list[dict[str, Any]]:
        return asset_admin.decorate(
            "text_effect",
            _combined_bundle_items(
                settings, TEXT_EFFECT_LIBRARY_ROOT, "text_effect_library",
                "text_effect_manifest.json", "effects", "jyd_probe.text_effect.v1",
            ),
        )

    @app.get("/api/assets/text-templates")
    def list_text_templates() -> list[dict[str, Any]]:
        return asset_admin.decorate(
            "text_template",
            _combined_bundle_items(
                settings, TEXT_TEMPLATE_LIBRARY_ROOT, "text_template_library",
                "text_template_manifest.json", "templates", "jyd_probe.text_template.v1",
            ),
        )

    @app.get("/api/assets/stickers")
    def list_stickers() -> list[dict[str, Any]]:
        return asset_admin.decorate(
            "sticker",
            _combined_bundle_items(
                settings, STICKER_LIBRARY_ROOT, "sticker_library",
                "sticker_manifest.json", "stickers", "jyd_probe.fullscreen_sticker.v1",
            ),
        )

    @app.get("/api/assets/stickers/{sticker_identity}/preview")
    def preview_sticker(sticker_identity: str) -> FileResponse:
        item = next(
            (
                value
                for value in _combined_bundle_items(
                    settings, STICKER_LIBRARY_ROOT, "sticker_library",
                    "sticker_manifest.json", "stickers", "jyd_probe.fullscreen_sticker.v1",
                )
                if value.get("identity") == sticker_identity
            ),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail=f"全屏贴纸不存在: {sticker_identity}")
        preview_file = str(item.get("preview_file", "")).strip()
        item_root = Path(str(item.get("_library_root", STICKER_LIBRARY_ROOT))).resolve()
        path = (item_root / preview_file).resolve() if preview_file else Path()
        if not path.is_file() or not _is_relative_to(path, item_root):
            raise HTTPException(status_code=404, detail="全屏贴纸没有可用预览图")
        return FileResponse(path)

    @app.get("/api/assets/corner-stickers")
    def list_corner_stickers() -> list[dict[str, Any]]:
        return asset_admin.decorate(
            "corner_sticker",
            _combined_bundle_items(
                settings, CORNER_STICKER_LIBRARY_ROOT, "corner_sticker_library",
                "sticker_manifest.json", "stickers", "jyd_probe.fullscreen_sticker.v1",
            ),
        )

    @app.get("/api/assets/corner-stickers/{sticker_identity}/preview")
    def preview_corner_sticker(sticker_identity: str) -> FileResponse:
        item = next(
            (
                value
                for value in _combined_bundle_items(
                    settings, CORNER_STICKER_LIBRARY_ROOT, "corner_sticker_library",
                    "sticker_manifest.json", "stickers", "jyd_probe.fullscreen_sticker.v1",
                )
                if value.get("identity") == sticker_identity
            ),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail=f"四角贴纸不存在: {sticker_identity}")
        preview_file = str(item.get("preview_file", "")).strip()
        item_root = Path(str(item.get("_library_root", CORNER_STICKER_LIBRARY_ROOT))).resolve()
        path = (item_root / preview_file).resolve() if preview_file else Path()
        if not path.is_file() or not _is_relative_to(path, item_root):
            raise HTTPException(status_code=404, detail="四角贴纸没有可用预览图")
        return FileResponse(path)

    @app.get("/api/audio-library")
    def get_audio_library() -> dict[str, Any]:
        try:
            return _decorate_audio_snapshot(render_queue.audio_catalog.snapshot(), asset_admin)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/admin/assets")
    def list_admin_assets(request: Request, include_deleted: bool = True) -> dict[str, Any]:
        require_local_file_access(request)
        audio_snapshot = render_queue.audio_catalog.snapshot()
        groups = _raw_admin_asset_groups(settings, render_queue.audio_catalog)
        items: list[dict[str, Any]] = []
        for kind, group in groups.items():
            items.extend(asset_admin.decorate(kind, group, include_deleted=include_deleted))
        counts: dict[str, int] = {}
        for item in items:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        return {
            "items": items,
            "counts": counts,
            "total": len(items),
            "audio_categories": audio_snapshot.get("categories", []),
        }

    @app.patch("/api/admin/assets/{asset_kind}/{asset_identity}")
    def update_admin_asset(
        request: Request,
        asset_kind: str,
        asset_identity: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        require_local_file_access(request)
        _require_admin_asset(settings, render_queue.audio_catalog, asset_kind, asset_identity)
        try:
            if asset_kind == "audio" and "audio_category_ids" in payload:
                category_ids = payload["audio_category_ids"]
                if not isinstance(category_ids, list):
                    raise ValueError("audio_category_ids 必须是数组")
                render_queue.audio_catalog.assign(
                    asset_identity,
                    [str(value) for value in category_ids],
                )
            asset_admin.update(
                asset_kind,
                asset_identity,
                name=str(payload["name"]) if "name" in payload else None,
                category=str(payload["category"]) if "category" in payload else None,
                enabled=_as_bool(payload["enabled"]) if "enabled" in payload else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _decorated_admin_asset(
            settings, render_queue.audio_catalog, asset_admin, asset_kind, asset_identity
        )

    @app.delete("/api/admin/assets/{asset_kind}/{asset_identity}")
    def trash_admin_asset(
        request: Request, asset_kind: str, asset_identity: str
    ) -> dict[str, Any]:
        require_local_file_access(request)
        _require_admin_asset(settings, render_queue.audio_catalog, asset_kind, asset_identity)
        try:
            asset_admin.move_to_trash(asset_kind, asset_identity)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _decorated_admin_asset(
            settings, render_queue.audio_catalog, asset_admin, asset_kind, asset_identity
        )

    @app.post("/api/admin/assets/{asset_kind}/{asset_identity}/restore")
    def restore_admin_asset(
        request: Request, asset_kind: str, asset_identity: str
    ) -> dict[str, Any]:
        require_local_file_access(request)
        _require_admin_asset(settings, render_queue.audio_catalog, asset_kind, asset_identity)
        try:
            asset_admin.restore(asset_kind, asset_identity)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _decorated_admin_asset(
            settings, render_queue.audio_catalog, asset_admin, asset_kind, asset_identity
        )

    @app.post("/api/audio-library/categories")
    def create_audio_category(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return render_queue.audio_catalog.create_category(
                str(_required(payload, "name")),
                str(payload.get("id", "")),
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/audio-library/assign")
    def assign_audio_category(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            category_ids = payload.get("category_ids", [])
            if not isinstance(category_ids, list):
                raise ValueError("category_ids 必须是数组")
            return render_queue.audio_catalog.assign(
                str(_required(payload, "identity")),
                [str(item) for item in category_ids],
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/audio-library/file")
    def get_audio_library_file(identity: str) -> FileResponse:
        try:
            path = render_queue.audio_catalog.file_path(identity)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        media_type = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".aac": "audio/aac",
            ".flac": "audio/flac",
        }.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.post("/api/captions/preview")
    def preview_captions(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            raw_cues = payload.get("cues")
            srt_text = str(payload.get("srt_text") or payload.get("srt") or "").strip()
            maximum_end_us = int(payload.get("maximum_end_us", 0) or 0) or None
            if raw_cues:
                if not isinstance(raw_cues, list):
                    raise ValueError("cues 必须是数组")
                cues = validate_caption_cues(
                    caption_cues_from_payload(raw_cues),
                    maximum_end_us=maximum_end_us,
                )
            elif srt_text:
                cues = validate_caption_cues(
                    parse_srt_cues(srt_text),
                    maximum_end_us=maximum_end_us,
                )
            else:
                text = str(_required(payload, "text"))
                start_us = int(payload.get("start_us", 0) or 0)
                duration_us = int(_required(payload, "duration_us"))
                cues = build_caption_cues(
                    text,
                    start_us=start_us,
                    duration_us=duration_us,
                    max_chars=int(payload.get("max_chars", 16) or 16),
                    min_duration_us=int(payload.get("min_duration_us", 650_000) or 650_000),
                )
            return {
                "cue_count": len(cues),
                "cues": [cue.as_dict() for cue in cues],
                "srt": cues_to_srt(cues),
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/drafts")
    def list_drafts(root: str = "") -> dict[str, Any]:
        scan_root = Path(root).expanduser().resolve() if root else settings.default_draft_root
        if not scan_root.exists():
            raise HTTPException(status_code=404, detail=f"草稿根目录不存在: {scan_root}")
        if not scan_root.is_dir():
            raise HTTPException(status_code=400, detail=f"草稿根目录不是文件夹: {scan_root}")
        return {
            "root": str(scan_root),
            "drafts": _list_draft_dirs(scan_root),
        }

    @app.post("/api/media/{media_kind}")
    async def upload_media(media_kind: str, request: Request, filename: str = "") -> dict[str, Any]:
        if media_kind not in {"video", "audio"}:
            raise HTTPException(status_code=400, detail="media_kind 只能是 video 或 audio")

        original_filename = filename or request.headers.get("x-filename") or f"upload_{media_kind}"
        suffix = Path(original_filename).suffix.lower()
        if media_kind == "video" and suffix not in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
            raise HTTPException(status_code=400, detail=f"不支持的视频格式: {suffix}")
        if media_kind == "audio" and suffix not in {".mp3", ".wav", ".m4a", ".aac", ".flac"}:
            raise HTTPException(status_code=400, detail=f"不支持的音频格式: {suffix}")

        media_id = uuid.uuid4().hex
        safe_name = _safe_filename(original_filename)
        media_dir = settings.storage_root / "media" / media_kind
        media_dir.mkdir(parents=True, exist_ok=True)
        path = media_dir / f"{media_id}_{safe_name}"

        max_bytes = (
            settings.max_video_upload_bytes
            if media_kind == "video"
            else settings.max_audio_upload_bytes
        )
        declared_size = request.headers.get("content-length", "").strip()
        if declared_size:
            try:
                if int(declared_size) > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"上传文件超过限制 {_format_bytes(max_bytes)}",
                    )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Content-Length 不合法") from exc

        size = 0
        try:
            with path.open("wb") as f:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"上传文件超过限制 {_format_bytes(max_bytes)}",
                        )
                    f.write(chunk)
        except BaseException:
            _unlink_if_exists(path)
            raise

        if size <= 0:
            try:
                path.unlink()
            except OSError:
                pass
            raise HTTPException(status_code=400, detail="上传文件为空")

        record = {
            "media_id": media_id,
            "kind": media_kind,
            "filename": original_filename,
            "path": str(path),
            "size": size,
            "created_at": _now(),
            "expires_at": _expiry_after(settings.media_retention_hours),
        }
        _write_json(_media_meta_path(settings, media_id), record)
        return record

    @app.post("/api/local/media-reference")
    def register_local_media(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        require_local_file_access(request)
        if settings.execution_mode != "embedded":
            raise HTTPException(status_code=400, detail="本机文件只能交给本机内置处理机执行")
        media_kind = str(payload.get("kind", "video")).strip().lower()
        allowed_suffixes = {
            "video": {".mp4", ".mov", ".avi", ".mkv", ".webm"},
            "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac"},
        }
        if media_kind not in allowed_suffixes:
            raise HTTPException(status_code=400, detail="kind 只能是 video 或 audio")
        raw_path = str(payload.get("path", "")).strip()
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=404, detail=f"本机文件不存在: {raw_path}") from exc
        if not path.is_file():
            raise HTTPException(status_code=400, detail=f"本机路径不是文件: {path}")
        if path.suffix.lower() not in allowed_suffixes[media_kind]:
            raise HTTPException(status_code=400, detail=f"不支持的{media_kind}格式: {path.suffix}")
        media_id = f"local_{uuid.uuid4().hex}"
        record = {
            "media_id": media_id,
            "kind": media_kind,
            "filename": path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "storage_mode": "local_reference",
            "created_at": _now(),
            "expires_at": _expiry_after(settings.media_retention_hours),
        }
        _write_json(_media_meta_path(settings, media_id), record)
        return {**record, "preview_url": f"/api/local/media/{media_id}/preview"}

    @app.post("/api/local/select-output-folder")
    def select_local_output_folder(request: Request) -> dict[str, Any]:
        require_local_file_access(request)
        if settings.execution_mode != "embedded":
            raise HTTPException(status_code=400, detail="只有本机内置处理机可以选择导出文件夹")
        try:
            from .local_collector import LocalCollectorService

            selected = LocalCollectorService._ask_directory()
            if not selected:
                return {"cancelled": True}
            path = Path(selected).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise NotADirectoryError(f"选择的导出目录不存在: {path}")
            return {"cancelled": False, "path": str(path), "name": path.name or str(path)}
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/local/media/{media_id}/preview")
    def preview_local_media(media_id: str, request: Request) -> FileResponse:
        require_local_file_access(request)
        record = _load_media_record(settings, media_id)
        if record.get("storage_mode") != "local_reference" or record.get("kind") != "video":
            raise HTTPException(status_code=404, detail="本机视频引用不存在")
        return FileResponse(Path(str(record["path"])), media_type="video/mp4")

    @get_route(app, "/api/media/{media_id}")
    def get_media(media_id: str) -> dict[str, Any]:
        return _load_media_record(settings, media_id)

    @app.post("/api/draft-imports")
    async def import_draft_package(
        request: Request,
        template_name: str = "",
        lifecycle: str = "",
    ) -> dict[str, Any]:
        lifecycle = lifecycle.strip()
        if lifecycle not in {"", "excel_batch_once"}:
            raise HTTPException(status_code=400, detail="不支持的母版生命周期")
        incoming_id = uuid.uuid4().hex
        incoming_root = settings.storage_root / "draft_imports" / "incoming"
        incoming_root.mkdir(parents=True, exist_ok=True)
        package_path = incoming_root / f"{incoming_id}.zip"
        digest = hashlib.sha256()
        size = 0
        declared_size = request.headers.get("content-length", "").strip()
        if declared_size:
            try:
                if int(declared_size) > settings.max_draft_import_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"草稿迁移包超过限制 {_format_bytes(settings.max_draft_import_bytes)}",
                    )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Content-Length 不合法") from exc
        try:
            with package_path.open("wb") as stream:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > settings.max_draft_import_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"草稿迁移包超过限制 {_format_bytes(settings.max_draft_import_bytes)}",
                        )
                    digest.update(chunk)
                    stream.write(chunk)
            if size <= 0:
                raise ValueError("上传的迁移包为空")
            expected_checksum = request.headers.get("x-package-sha256", "").strip().lower()
            actual_checksum = digest.hexdigest()
            if expected_checksum and expected_checksum != actual_checksum:
                raise ValueError("迁移包校验失败，请重新上传")
            result = import_transfer_package(
                package_path,
                imports_root=settings.storage_root / "draft_imports" / "records",
                template_library_root=settings.template_library_root,
                template_name=template_name,
                font_library_root=FONT_LIBRARY_ROOT,
                expires_at=_expiry_after(
                    min(settings.template_retention_hours, 24)
                    if lifecycle == "excel_batch_once"
                    else settings.template_retention_hours
                ),
                lifecycle=lifecycle,
            )
            result["upload_size_bytes"] = size
            result["package_checksum_sha256"] = actual_checksum
            result["website_url"] = "/app"
            return result
        except HTTPException:
            _unlink_if_exists(package_path)
            raise
        except Exception as exc:
            try:
                package_path.unlink()
            except OSError:
                pass
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/personal-assets/import")
    async def import_personal_assets(request: Request) -> dict[str, Any]:
        incoming_root = settings.storage_root / "personal_asset_imports" / "incoming"
        incoming_root.mkdir(parents=True, exist_ok=True)
        package_path = incoming_root / f"{uuid.uuid4().hex}.zip"
        digest = hashlib.sha256()
        size = 0
        try:
            with package_path.open("wb") as stream:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > settings.max_draft_import_bytes:
                        raise HTTPException(status_code=413, detail="个人素材包超过上传大小限制")
                    digest.update(chunk)
                    stream.write(chunk)
            expected_checksum = request.headers.get("x-package-sha256", "").strip().lower()
            actual_checksum = digest.hexdigest()
            if expected_checksum and not hmac.compare_digest(expected_checksum, actual_checksum):
                raise HTTPException(status_code=400, detail="个人素材包校验失败")
            personal_root = settings.personal_library_root or (PROJECT_ROOT / "data" / "personal_libraries")
            result = _import_personal_asset_package(package_path, personal_root)
            return {
                "ok": True,
                "upload_size_bytes": size,
                "checksum_sha256": actual_checksum,
                **result,
            }
        except HTTPException:
            raise
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            package_path.unlink(missing_ok=True)

    @app.get("/api/local-assets")
    @app.get("/api/personal-assets", include_in_schema=False)
    def list_local_assets(request: Request, include_deleted: bool = True) -> dict[str, Any]:
        require_local_file_access(request)
        groups = _raw_local_asset_groups(settings, render_queue.audio_catalog)
        items: list[dict[str, Any]] = []
        for kind, group in groups.items():
            items.extend(asset_admin.decorate(kind, group, include_deleted=include_deleted))
        counts: dict[str, int] = {}
        for item in items:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        return {"items": items, "counts": counts, "total": len(items)}

    @app.patch("/api/local-assets/{asset_kind}/{asset_identity}")
    @app.patch("/api/personal-assets/{asset_kind}/{asset_identity}", include_in_schema=False)
    def update_local_asset(
        request: Request,
        asset_kind: str,
        asset_identity: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        require_local_file_access(request)
        _require_local_asset(settings, render_queue.audio_catalog, asset_kind, asset_identity)
        try:
            asset_admin.update(
                asset_kind,
                asset_identity,
                name=str(payload["name"]) if "name" in payload else None,
                category=str(payload["category"]) if "category" in payload else None,
                enabled=_as_bool(payload["enabled"]) if "enabled" in payload else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _decorated_local_asset(
            settings, render_queue.audio_catalog, asset_admin, asset_kind, asset_identity
        )

    @app.delete("/api/local-assets/{asset_kind}/{asset_identity}")
    @app.delete("/api/personal-assets/{asset_kind}/{asset_identity}", include_in_schema=False)
    def trash_local_asset(request: Request, asset_kind: str, asset_identity: str) -> dict[str, Any]:
        require_local_file_access(request)
        _require_local_asset(settings, render_queue.audio_catalog, asset_kind, asset_identity)
        try:
            asset_admin.move_to_trash(asset_kind, asset_identity)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _decorated_local_asset(
            settings, render_queue.audio_catalog, asset_admin, asset_kind, asset_identity
        )

    @app.post("/api/local-assets/{asset_kind}/{asset_identity}/restore")
    @app.post(
        "/api/personal-assets/{asset_kind}/{asset_identity}/restore", include_in_schema=False
    )
    def restore_local_asset(request: Request, asset_kind: str, asset_identity: str) -> dict[str, Any]:
        require_local_file_access(request)
        _require_local_asset(settings, render_queue.audio_catalog, asset_kind, asset_identity)
        try:
            asset_admin.restore(asset_kind, asset_identity)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _decorated_local_asset(
            settings, render_queue.audio_catalog, asset_admin, asset_kind, asset_identity
        )

    @app.get("/api/local-assets/{asset_kind}/{asset_identity}/preview")
    @app.get(
        "/api/personal-assets/{asset_kind}/{asset_identity}/preview", include_in_schema=False
    )
    def preview_local_asset(
        request: Request, asset_kind: str, asset_identity: str
    ) -> FileResponse:
        require_local_file_access(request)
        item = _require_local_asset(
            settings, render_queue.audio_catalog, asset_kind, asset_identity
        )
        preview_path = _local_asset_preview_path(item)
        if preview_path is None:
            raise HTTPException(status_code=404, detail="当前素材没有可用预览")
        return FileResponse(preview_path)

    @app.post("/api/templates/import")
    def import_template(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            library = TemplateLibrary(settings.template_library_root)
            record = library.import_template(
                _required(payload, "source_draft_dir"),
                template_id=str(payload.get("template_id", "")),
                name=str(payload.get("name", "")),
                replace=_as_bool(payload.get("replace", False)),
                auto_decrypt=not _as_bool(payload.get("no_auto_decrypt", False)),
                force_decrypt=_as_bool(payload.get("force_decrypt", False)),
                decrypt_work_root=payload.get("decrypt_work_root") or None,
                jy_draftc_exe=payload.get("jy_draftc_exe") or None,
                jy_install_dir=payload.get("jy_install_dir") or None,
                jy_draftc_debug=_as_bool(payload.get("jy_draftc_debug", False)),
            )
            return record.as_dict()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/templates")
    def list_templates() -> list[dict[str, Any]]:
        templates: list[dict[str, Any]] = []
        now = datetime.now()
        for record in TemplateLibrary(settings.template_library_root).list():
            expires_at = _parse_timestamp(record.expires_at)
            if expires_at is not None and expires_at <= now:
                continue
            item = record.as_dict()
            item["identity"] = record.template_id
            templates.append(item)
        return asset_admin.decorate("template", templates)

    @app.get("/api/templates/{template_id}/preview-video")
    def get_template_preview_video(template_id: str) -> FileResponse:
        try:
            record = TemplateLibrary(settings.template_library_root).get(template_id)
            data = _read_json(record.draft_dir / "draft_content.json")
            materials = data.get("materials", {})
            videos = materials.get("videos", []) if isinstance(materials, dict) else []
            for item in videos if isinstance(videos, list) else []:
                if not isinstance(item, dict):
                    continue
                path = Path(str(item.get("path", "")))
                if path.is_file():
                    media_type = {
                        ".mp4": "video/mp4",
                        ".mov": "video/quicktime",
                        ".webm": "video/webm",
                    }.get(path.suffix.lower(), "application/octet-stream")
                    return FileResponse(path, media_type=media_type)
            raise FileNotFoundError("母版没有可用于预览的本地视频")
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/templates/{template_id}")
    def get_template(template_id: str) -> dict[str, Any]:
        try:
            return TemplateLibrary(settings.template_library_root).get(template_id).as_dict()
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/render")
    def render(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return render_queue.submit(payload)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/render/batch")
    def render_batch(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            jobs, variants = _expand_batch_payload(payload)
            return render_queue.submit_batch(jobs, variants)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/excel-batch/template")
    def download_excel_batch_template() -> FileResponse:
        path = FRONTEND_ROOT / "batch-task-template.xlsx"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Excel 批量任务模板尚未生成")
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="剪映批量任务模板.xlsx",
        )

    @app.post("/api/excel-batch/preview")
    def preview_excel_batch(content: bytes = Body(...)) -> dict[str, Any]:
        try:
            return parse_excel_batch_workbook(content)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/render/excel-batch")
    def render_excel_batch(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            jobs, variants = _expand_excel_batch_payload(payload)
            temporary_ids = payload.get("temporary_template_ids", [])
            if not isinstance(temporary_ids, list):
                raise ValueError("temporary_template_ids 必须是数组")
            return render_queue.submit_batch(
                jobs,
                variants,
                temporary_template_ids=[str(item) for item in temporary_ids],
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: str) -> dict[str, Any]:
        return render_queue.get_batch_status(batch_id)

    @app.get("/api/recent-batches")
    def list_recent_batches(limit: int = 20) -> list[dict[str, Any]]:
        return render_queue.list_recent_batches(limit)

    @app.delete("/api/admin/batches/{batch_id}")
    def delete_batch_record(batch_id: str) -> dict[str, Any]:
        return render_queue.delete_batch_record(batch_id)

    @app.post("/api/batches/{batch_id}/cancel")
    def cancel_batch(batch_id: str) -> dict[str, Any]:
        return render_queue.cancel_batch(batch_id)

    @app.post("/api/batches/{batch_id}/retry-failed")
    def retry_failed_batch(batch_id: str) -> dict[str, Any]:
        return render_queue.retry_failed_batch(batch_id)

    @app.post("/api/batches/{batch_id}/downloads")
    def create_batch_download(batch_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return render_queue.create_batch_download(batch_id, _job_ids_from_payload(payload))

    @app.post("/api/batches/{batch_id}/delete-outputs")
    def delete_batch_outputs(batch_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return render_queue.delete_batch_outputs(batch_id, _job_ids_from_payload(payload))

    @app.get("/api/batch-downloads/{download_id}")
    def download_batch_archive(download_id: str) -> FileResponse:
        safe_id = re.sub(r"[^A-Za-z0-9]+", "", download_id)
        if safe_id != download_id:
            raise HTTPException(status_code=404, detail="批量下载文件不存在")
        path = settings.storage_root / "batch_downloads" / f"{safe_id}.zip"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="批量下载文件不存在或已经下载")
        return FileResponse(
            path,
            media_type="application/zip",
            filename=f"jianying-results-{safe_id[:8]}.zip",
            background=BackgroundTask(_unlink_if_exists, path),
        )

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        return render_queue.get_status(job_id)

        status_path = _job_dir(settings, job_id) / "status.json"
        if not status_path.exists():
            raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
        return _read_json(status_path)

    @app.get("/api/jobs/{job_id}/download")
    def download_job_output(job_id: str) -> FileResponse:
        status = get_job(job_id)
        if status.get("output_deleted"):
            raise HTTPException(status_code=410, detail="任务输出已删除")
        result = status.get("result", {})
        if not isinstance(result, dict):
            raise HTTPException(status_code=404, detail="任务没有输出结果")
        output_mp4 = result.get("output_mp4")
        if not output_mp4:
            raise HTTPException(status_code=404, detail="任务没有配置 MP4 输出")
        path = Path(output_mp4)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"MP4 文件不存在: {path}")
        display_name = _job_display_name(status) or path.stem
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=f"{_safe_download_stem(display_name)}.mp4",
        )

    @app.get("/api/jobs/{job_id}/preview")
    def preview_job_output(job_id: str) -> FileResponse:
        status = get_job(job_id)
        if status.get("output_deleted"):
            raise HTTPException(status_code=410, detail="任务输出已删除")
        result = status.get("result", {})
        if not isinstance(result, dict) or not result.get("exported"):
            raise HTTPException(status_code=404, detail="任务还没有可预览的 MP4")
        path = Path(str(result.get("output_mp4", "")))
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"MP4 文件不存在: {path}")
        return FileResponse(path, media_type="video/mp4")

    @app.post("/api/local/jobs/{job_id}/open")
    def open_local_job_output(
        job_id: str, request: Request, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        require_local_file_access(request)
        status = render_queue.get_status(job_id)
        result = status.get("result", {})
        path = Path(str(result.get("output_mp4", ""))) if isinstance(result, dict) else Path()
        if not path.is_file():
            raise HTTPException(status_code=404, detail="任务输出文件不存在")
        action = str(payload.get("action", "reveal")).strip().lower()
        try:
            if action == "play":
                if os.name != "nt":
                    raise OSError("打开视频功能当前仅支持 Windows")
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif action == "reveal":
                if os.name != "nt":
                    raise OSError("打开文件夹功能当前仅支持 Windows")
                subprocess.Popen(["explorer.exe", "/select,", str(path)])
            else:
                raise HTTPException(status_code=400, detail="action 只能是 play 或 reveal")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"无法打开本机文件: {exc}") from exc
        return {"ok": True, "action": action, "path": str(path)}

    return app


def get_route(app: FastAPI, path: str):
    """Keep decorators readable for endpoints whose name would conflict with helpers."""

    return app.get(path)


def _prepare_render_job_payload(
    settings: WebApiSettings,
    audio_catalog: AudioCatalog,
    payload: dict[str, Any],
    job_id: str,
) -> dict[str, Any]:
    job = deepcopy(payload)
    source = job.setdefault("source", {})
    if not isinstance(source, dict):
        raise HTTPException(status_code=400, detail="source 必须是对象")

    if source.get("media_id"):
        media_record = _load_media_record(settings, str(source["media_id"]))
        if media_record.get("storage_mode") == "local_reference":
            if not settings.allow_local_file_access or settings.execution_mode != "embedded":
                raise HTTPException(status_code=400, detail="本机文件引用不能交给公用处理机")
        source["media_path"] = media_record["path"]

    if source.get("type") == "video":
        source.setdefault("work_root", str(settings.storage_root / "generated_video_drafts"))

    if source.get("type") == "template" and source.get("template_id"):
        library = TemplateLibrary(settings.template_library_root)
        record = library.get(str(source["template_id"]))
        source.setdefault("library_root", str(settings.template_library_root))
        if _as_bool(source.get("preserve_original_video", False)):
            policies = record.import_info.get("policies", {})
            if not isinstance(policies, dict):
                policies = {}
            _reject_mother_composite_text_changes(job)
            job.setdefault(
                "remove_existing_audio",
                str(policies.get("audio", "keep")) in {"replace", "remove"},
            )
            job.setdefault(
                "remove_existing_effects",
                str(policies.get("video_effects", "keep")) in {"replace", "remove"},
            )
    existing_style = job.get("existing_text_style", {})
    if isinstance(existing_style, dict):
        style_path = str(existing_style.get("style_json_path", "")).strip()
        if style_path:
            _require_library_file_any(
                style_path, _library_roots(settings, TEXT_STYLE_LIBRARY_ROOT, "text_style_library"), "已有字幕样式"
            )

    existing_font = job.get("existing_text_font", {})
    if isinstance(existing_font, dict):
        font_path = str(existing_font.get("font_path", "")).strip()
        if font_path:
            _require_library_file_any(
                font_path, _font_library_roots(settings), "已有字幕字体"
            )

    captions = job.get("captions", {})
    if isinstance(captions, dict):
        caption_style_path = str(captions.get("style_json_path", "")).strip()
        if caption_style_path:
            _require_library_file_any(
                caption_style_path, _library_roots(settings, TEXT_STYLE_LIBRARY_ROOT, "text_style_library"), "字幕样式"
            )
        caption_font_path = str(captions.get("font_path", "")).strip()
        if caption_font_path:
            _require_library_file_any(
                caption_font_path, _font_library_roots(settings), "字幕字体"
            )

    for text in _list_items(job.get("texts")) + _list_items(job.get("text")):
        text_effect_path = str(text.get("text_effect_json_path", "")).strip()
        if text_effect_path:
            _require_library_file_any(
                text_effect_path, _library_roots(settings, TEXT_EFFECT_LIBRARY_ROOT, "text_effect_library"), "花字素材"
            )

    for text_template in _list_items(job.get("text_templates")) + _list_items(job.get("text_template")):
        template_path = str(
            text_template.get("template_json_path") or text_template.get("metadata_path") or ""
        ).strip()
        if template_path:
            _require_library_file_any(
                template_path, _library_roots(settings, TEXT_TEMPLATE_LIBRARY_ROOT, "text_template_library"), "复合文字模板"
            )

    for effect in _list_items(job.get("effects")) + _list_items(job.get("effect")):
        effect_path = str(effect.get("effect_json_path", "")).strip()
        if effect_path:
            _require_library_file_any(
                effect_path, _library_roots(settings, EFFECT_LIBRARY_ROOT, "effect_library"), "视频特效"
            )

    for sticker in _list_items(job.get("stickers")) + _list_items(job.get("sticker")):
        sticker_path = str(
            sticker.get("sticker_json_path") or sticker.get("metadata_path") or ""
        ).strip()
        if sticker_path:
            is_corner = bool(str(sticker.get("corner", "")).strip())
            _require_library_file_any(
                sticker_path,
                _library_roots(
                    settings,
                    CORNER_STICKER_LIBRARY_ROOT if is_corner else STICKER_LIBRARY_ROOT,
                    "corner_sticker_library" if is_corner else "sticker_library",
                ),
                "四角贴纸" if is_corner else "全屏贴纸",
            )

    for audio in _list_items(job.get("audios")):
        if audio.get("media_id"):
            audio["media_path"] = _load_media_record(settings, str(audio["media_id"]))["path"]
            continue

        selected_asset: dict[str, Any] | None = None
        if audio.get("library_identity"):
            selected_asset = audio_catalog.get_asset(str(audio["library_identity"]))
            if not selected_asset.get("available"):
                raise HTTPException(status_code=400, detail=f"音乐文件不可用: {audio['library_identity']}")
        elif audio.get("library_category_id"):
            selected_asset = audio_catalog.select_next(str(audio["library_category_id"]))

        if selected_asset is not None:
            audio["media_path"] = selected_asset["absolute_path"]
            audio["selected_library_audio"] = {
                "identity": selected_asset["identity"],
                "name": selected_asset.get("name", ""),
                "category_id": audio.get("library_category_id", ""),
                "selection_mode": selected_asset.get("selection_mode", "specific"),
                "sequence_index": selected_asset.get("sequence_index"),
            }

    for video in _list_items(job.get("videos")) + _list_items(job.get("video")):
        if video.get("media_id"):
            video["media_path"] = _load_media_record(settings, str(video["media_id"]))["path"]

    output = job.setdefault("output", {})
    if not isinstance(output, dict):
        raise HTTPException(status_code=400, detail="output 必须是对象")

    output.setdefault("draft_root", str(settings.default_draft_root))
    output.setdefault("draft_name", f"jyd_{job_id[:12]}")
    skip_export = _as_bool(output.get("skip_export", job.get("skip_export", False)))
    output["skip_export"] = skip_export
    if not skip_export:
        output_dir_text = str(output.get("output_dir", "")).strip()
        if output_dir_text:
            if not settings.allow_local_file_access or settings.execution_mode != "embedded":
                raise HTTPException(status_code=400, detail="指定本机导出目录只支持本机处理模式")
            output_dir = Path(output_dir_text).expanduser().resolve()
            if not output_dir.is_dir():
                raise HTTPException(status_code=400, detail=f"导出目录不存在: {output_dir}")
            filename = _safe_download_stem(
                str(output.get("draft_name") or output.get("output_name") or job_id)
            )
            requested_path = str(output.get("mp4_path", "")).strip()
            mp4_path = (
                Path(requested_path).expanduser().resolve()
                if requested_path
                else (output_dir / f"{filename}.mp4").resolve()
            )
            if not _is_relative_to(mp4_path, output_dir):
                raise HTTPException(status_code=400, detail="MP4 输出必须位于所选导出目录中")
            output["output_dir"] = str(output_dir)
            output["mp4_path"] = str(mp4_path)
            output["external_output"] = True
        else:
            output.setdefault("mp4_path", str(settings.storage_root / "outputs" / f"{job_id}.mp4"))

    return job


def _expand_batch_payload(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if isinstance(payload.get("dimensions"), list):
        return _expand_dimension_batch_payload(payload)
    return _expand_legacy_batch_payload(payload)


def _expand_excel_batch_payload(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("Excel 批量请求必须包含 rows 数组")
    if len(raw_rows) > 200:
        raise ValueError("Excel 批量请求最多支持 200 行")

    try:
        max_jobs = int(payload.get("max_jobs", 500) or 500)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_jobs 必须是正整数") from exc
    if max_jobs <= 0 or max_jobs > 500:
        raise ValueError("Excel 批次最多生成 500 个视频")

    row_groups: list[list[tuple[dict[str, Any], dict[str, Any]]]] = []
    total = 0
    for fallback_index, raw_row in enumerate(raw_rows, start=1):
        if not isinstance(raw_row, dict):
            raise ValueError(f"Excel 任务 #{fallback_index} 必须是对象")
        if raw_row.get("enabled") is False:
            continue
        row_number = int(raw_row.get("row_number", fallback_index) or fallback_index)
        task_name = str(raw_row.get("task_name", "")).strip() or f"第 {row_number} 行"
        job = raw_row.get("job")
        dimensions = raw_row.get("dimensions")
        selection = raw_row.get("selection")
        if not isinstance(job, dict) or not isinstance(dimensions, list):
            raise ValueError(f"{task_name} 缺少 job 或 dimensions 配置")

        row_jobs, row_variants = _expand_dimension_batch_payload(
            {
                "job": job,
                "dimensions": dimensions,
                "selection": selection,
                "max_jobs": max_jobs,
            }
        )
        group: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for output_index, (expanded_job, variant) in enumerate(
            zip(row_jobs, row_variants), start=1
        ):
            enriched = deepcopy(variant)
            original_name = str(enriched.get("display_name", "")).strip()
            enriched.update(
                {
                    "task_name": task_name,
                    "excel_row_number": row_number,
                    "row_output_index": output_index,
                    "row_output_total": len(row_jobs),
                    "display_name": f"{task_name}-{original_name}" if original_name else task_name,
                    "summary": f"{task_name} · {enriched.get('summary', '固定任务')}",
                }
            )
            group.append((expanded_job, enriched))
        total += len(group)
        if total > max_jobs:
            raise ValueError(
                f"Excel 各行合计会生成 {total} 个视频，超过当前上限 {max_jobs}"
            )
        row_groups.append(group)

    if not row_groups:
        raise ValueError("Excel 中没有启用的任务")

    jobs: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    longest = max(len(group) for group in row_groups)
    for output_index in range(longest):
        for group in row_groups:
            if output_index >= len(group):
                continue
            job, variant = group[output_index]
            jobs.append(job)
            variants.append(variant)
    return jobs, variants


def _expand_dimension_batch_payload(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_job = payload.get("job")
    if not isinstance(base_job, dict):
        raise ValueError("批量请求必须包含 job 对象")

    raw_dimensions = payload.get("dimensions")
    if not isinstance(raw_dimensions, list):
        raise ValueError("dimensions 必须是数组")

    fixed_candidates: list[tuple[str, str, dict[str, Any]]] = []
    product_dimensions: list[tuple[str, str, list[dict[str, Any]]]] = []
    seen_keys: set[str] = set()
    for index, raw_dimension in enumerate(raw_dimensions, start=1):
        if not isinstance(raw_dimension, dict):
            raise ValueError(f"组合维度 #{index} 必须是对象")
        key = str(raw_dimension.get("key", "")).strip()
        if not key or not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            raise ValueError(f"组合维度 #{index} 的 key 无效")
        if key in seen_keys:
            raise ValueError(f"组合维度 key 重复: {key}")
        seen_keys.add(key)

        label = str(raw_dimension.get("label", "")).strip() or key
        mode = str(raw_dimension.get("mode", "disabled")).strip().lower()
        if mode not in {"disabled", "fixed", "product"}:
            raise ValueError(f"组合维度 {label} 的 mode 无效: {mode}")
        if mode == "disabled":
            continue

        raw_candidates = raw_dimension.get("candidates")
        if not isinstance(raw_candidates, list):
            raise ValueError(f"组合维度 {label} 的 candidates 必须是数组")
        candidates = _deduplicate_batch_candidates([
            _normalize_batch_candidate(item, key, label, candidate_index)
            for candidate_index, item in enumerate(raw_candidates, start=1)
        ])
        if mode == "fixed":
            if len(candidates) != 1:
                raise ValueError(f"固定维度 {label} 必须且只能选择 1 个候选项")
            fixed_candidates.append((key, label, candidates[0]))
        elif not candidates:
            raise ValueError(f"参与组合的维度 {label} 至少选择 1 个候选项")
        else:
            product_dimensions.append((key, label, candidates))

    active_dimension_keys = [key for key, _, _ in fixed_candidates] + [
        key for key, _, _ in product_dimensions
    ]
    core_change_keys = [
        key for key in active_dimension_keys if key in CORE_SOURCE_CHANGE_DIMENSION_KEYS
    ]
    if len(core_change_keys) < MINIMUM_CORE_SOURCE_CHANGES:
        raise ValueError(
            "每个生成结果必须相对原视频至少改变两个核心元素：背景音乐、视频特效、全屏贴纸或画面变化；"
            "字体、花字和复合文字模板不计入核心变化数"
        )

    max_jobs = int(payload.get("max_jobs", 500) or 500)
    if max_jobs <= 0 or max_jobs > 1000:
        max_jobs = 500
    candidate_counts = [len(candidates) for _, _, candidates in product_dimensions]
    selection = payload.get("selection")
    selection_mode = "all"
    requested_total: int | None = None
    if selection is not None:
        if not isinstance(selection, dict):
            raise ValueError("selection 必须是对象")
        selection_mode = str(selection.get("mode", "random")).strip().lower()
        if selection_mode not in {"all", "balanced", "random"}:
            raise ValueError(f"selection.mode 无效: {selection_mode}")
        if selection_mode in {"balanced", "random"}:
            try:
                requested_total = int(selection.get("limit", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("selection.limit 必须是正整数") from exc
            if requested_total <= 0:
                raise ValueError("selection.limit 必须是正整数")
            if requested_total > max_jobs:
                raise ValueError(f"本次生成数量不能超过上限 {max_jobs}")

    if selection_mode == "balanced":
        combination_indices, raw_total = _balanced_combination_indices(
            candidate_counts, requested_total or max_jobs
        )
    elif selection_mode == "random":
        combination_indices, raw_total = _random_combination_indices(
            candidate_counts, requested_total or max_jobs
        )
    else:
        combination_indices, raw_total = _all_combination_indices(candidate_counts)
    total = len(combination_indices)
    if total > max_jobs:
        raise ValueError(f"本次完整组合会生成 {total} 个任务，超过上限 {max_jobs}")

    jobs: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    batch_stub = uuid.uuid4().hex[:8]
    display_name_counts: dict[str, int] = {}
    varying_keys = [
        key for key, _, candidates in product_dimensions if len(candidates) > 1
    ]
    filter_metadata = {
        "rule": "source_minimum_two_core_dimensions",
        "minimum_source_changes": MINIMUM_CORE_SOURCE_CHANGES,
        "core_dimensions": core_change_keys,
        "selection_mode": selection_mode,
        "requested_total": requested_total,
        "raw_total": raw_total,
        "filtered_total": total,
        "removed_total": raw_total - total,
        "varying_dimensions": varying_keys,
    }
    for job_index, combination_index in enumerate(combination_indices, start=1):
        combination = tuple(
            candidates[candidate_index]
            for (_, _, candidates), candidate_index in zip(product_dimensions, combination_index)
        )
        job = deepcopy(base_job)
        selections: list[tuple[str, str, str, dict[str, Any]]] = []
        for key, label, candidate in fixed_candidates:
            _apply_batch_candidate(job, candidate)
            selections.append((key, label, "fixed", candidate))
        for (key, label, _), candidate in zip(product_dimensions, combination):
            _apply_batch_candidate(job, candidate)
            selections.append((key, label, "product", candidate))

        output = job.setdefault("output", {})
        if not isinstance(output, dict):
            raise ValueError("job.output 必须是对象")
        display_name_base = _compact_variant_name(selections)
        display_name_counts[display_name_base] = display_name_counts.get(display_name_base, 0) + 1
        duplicate_index = display_name_counts[display_name_base]
        display_name = (
            display_name_base
            if duplicate_index == 1
            else f"{display_name_base}-{duplicate_index:02d}"
        )
        output["draft_name"] = f"{display_name}_{batch_stub}_{job_index:04d}"

        dimensions = {
            key: {
                "id": str(candidate.get("id", "")),
                "label": str(candidate.get("label", "")),
                "mode": mode,
            }
            for key, _, mode, candidate in selections
        }
        summary = " + ".join(
            f"{label}: {candidate.get('label', candidate.get('id', ''))}"
            for _, label, _, candidate in selections
        ) or "固定任务"
        jobs.append(job)
        variants.append(
            {
                "display_name": display_name,
                "dimensions": dimensions,
                "changed_elements": list(dimensions),
                "change_count": len(core_change_keys),
                "core_changed_elements": core_change_keys,
                "core_change_count": len(core_change_keys),
                "combination_filter": filter_metadata,
                "summary": summary,
            }
        )
    return jobs, variants


def _deduplicate_batch_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for candidate in candidates:
        identity = str(candidate.get("id", "")).strip()
        if identity in seen_ids:
            continue
        seen_ids.add(identity)
        unique.append(candidate)
    return unique


MINIMUM_CORE_SOURCE_CHANGES = 2
CORE_SOURCE_CHANGE_DIMENSION_KEYS = frozenset(
    {"bgm", "effect", "video_effect", "sticker", "mirror", "layout", "corner_sticker"}
)


def _all_combination_indices(
    candidate_counts: list[int],
) -> tuple[list[tuple[int, ...]], int]:
    """Return the complete deterministic Cartesian product of candidate indices."""
    if any(count <= 0 for count in candidate_counts):
        return [], 0
    if not candidate_counts:
        return [()], 1

    raw_total = 1
    for count in candidate_counts:
        raw_total *= count

    return list(product(*(range(count) for count in candidate_counts))), raw_total


def _balanced_combination_indices(
    candidate_counts: list[int],
    limit: int,
) -> tuple[list[tuple[int, ...]], int]:
    """Select a deterministic subset with balanced per-axis and pair coverage."""
    if any(count <= 0 for count in candidate_counts) or limit <= 0:
        return [], 0
    if not candidate_counts:
        return [()], 1

    raw_total = 1
    for count in candidate_counts:
        raw_total *= count
    selected_total = min(limit, raw_total)
    if selected_total == raw_total:
        return _all_combination_indices(candidate_counts)

    step_sample_size = min(raw_total, 512)
    candidate_steps: set[int] = set()
    for position in range(step_sample_size):
        step = 1 + position * (raw_total - 2) // max(1, step_sample_size - 1)
        while step < raw_total and gcd(step, raw_total) != 1:
            step += 1
        if step < raw_total:
            candidate_steps.add(step)

    best_score: tuple[int, int, int, int] | None = None
    best_rows: list[tuple[int, ...]] = []
    axis_pairs = list(combinations(range(len(candidate_counts)), 2))
    for step in sorted(candidate_steps):
        rows = [
            _decode_combination_index((index * step) % raw_total, candidate_counts)
            for index in range(selected_total)
        ]
        usage = [[0] * count for count in candidate_counts]
        for row in rows:
            for axis, candidate_index in enumerate(row):
                usage[axis][candidate_index] += 1
        missing_candidates = sum(value == 0 for axis_usage in usage for value in axis_usage)
        marginal_imbalance = sum(
            (value * len(axis_usage) - selected_total) ** 2
            for axis_usage in usage
            for value in axis_usage
        )
        repeated_pairs = sum(
            selected_total - len({(row[left], row[right]) for row in rows})
            for left, right in axis_pairs
        )
        score = (missing_candidates, marginal_imbalance, repeated_pairs, step)
        if best_score is None or score < best_score:
            best_score = score
            best_rows = rows
    return best_rows, raw_total


def _random_combination_indices(
    candidate_counts: list[int],
    limit: int,
    *,
    sampler: Any | None = None,
) -> tuple[list[tuple[int, ...]], int]:
    """Randomly sample unique Cartesian-product rows without materializing the product."""
    if any(count <= 0 for count in candidate_counts) or limit <= 0:
        return [], 0
    if not candidate_counts:
        return [()], 1

    raw_total = 1
    for count in candidate_counts:
        raw_total *= count
    selected_total = min(limit, raw_total)
    if selected_total == raw_total:
        return _all_combination_indices(candidate_counts)

    random_source = sampler or secrets.SystemRandom()
    flat_indices = random_source.sample(range(raw_total), selected_total)
    return [
        _decode_combination_index(flat_index, candidate_counts)
        for flat_index in flat_indices
    ], raw_total


def _decode_combination_index(
    flat_index: int,
    candidate_counts: list[int],
) -> tuple[int, ...]:
    values: list[int] = []
    remaining = flat_index
    for count in reversed(candidate_counts):
        remaining, value = divmod(remaining, count)
        values.append(value)
    return tuple(reversed(values))


def _batch_combination_filter(variants: list[dict[str, Any]]) -> dict[str, Any]:
    if not variants:
        return {}
    metadata = variants[0].get("combination_filter", {})
    return deepcopy(metadata) if isinstance(metadata, dict) else {}


def _compact_variant_name(
    selections: list[tuple[str, str, str, dict[str, Any]]],
) -> str:
    parts = [
        _short_name(candidate.get("short_name", candidate.get("label", candidate.get("id", ""))))
        for _, _, _, candidate in selections
    ]
    parts = [part for part in parts if part]
    return "+".join(parts) or "原片"


def _short_name(value: Any) -> str:
    text = Path(str(value).strip()).stem
    text = re.sub(r"^[\s._+\-—–,，。:：;；()（）【】\[\]{}]+", "", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", text)
    return (text or "素材")[:2]


def _normalize_batch_candidate(
    value: Any,
    dimension_key: str,
    dimension_label: str,
    index: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"组合维度 {dimension_label} 的候选项 #{index} 必须是对象")
    patch = value.get("patch", {})
    append = value.get("append", {})
    if not isinstance(patch, dict) or not isinstance(append, dict):
        raise ValueError(f"组合维度 {dimension_label} 的候选项 #{index} patch/append 必须是对象")
    if not patch and not append:
        raise ValueError(f"组合维度 {dimension_label} 的候选项 #{index} 没有任务修改内容")
    candidate = deepcopy(value)
    candidate["id"] = str(candidate.get("id", "")).strip() or f"{dimension_key}_{index}"
    candidate["label"] = str(candidate.get("label", "")).strip() or candidate["id"]
    candidate["patch"] = patch
    candidate["append"] = append
    return candidate


def _apply_batch_candidate(job: dict[str, Any], candidate: dict[str, Any]) -> None:
    _merge_batch_patch(job, candidate.get("patch", {}))
    append = candidate.get("append", {})
    for key, value in append.items():
        if not isinstance(value, list):
            raise ValueError(f"候选项 {candidate.get('label')} 的 append.{key} 必须是数组")
        target = job.setdefault(str(key), [])
        if not isinstance(target, list):
            raise ValueError(f"任务字段 {key} 不是数组，不能追加组合内容")
        target.extend(deepcopy(value))


def _merge_batch_patch(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_batch_patch(current, value)
        else:
            target[key] = deepcopy(value)


def _expand_legacy_batch_payload(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_job = payload.get("job")
    if not isinstance(base_job, dict):
        raise ValueError("批量请求必须包含 job 对象")

    music = payload.get("music", {})
    effects = payload.get("effects", {})
    if not isinstance(music, dict) or not isinstance(effects, dict):
        raise ValueError("music 和 effects 必须是对象")

    identities = _unique_strings(music.get("identities"))
    effect_paths = _unique_strings(effects.get("paths"))
    if not identities:
        raise ValueError("批量排列组合至少选择一首音乐")
    if not effect_paths:
        raise ValueError("批量排列组合至少选择一个特效")

    combination_indices, raw_total = _all_combination_indices(
        [len(identities), len(effect_paths)]
    )
    total = len(combination_indices)
    max_jobs = int(payload.get("max_jobs", 500) or 500)
    if max_jobs <= 0 or max_jobs > 1000:
        max_jobs = 500
    if total > max_jobs:
        raise ValueError(
            f"本次完整组合会生成 {total} 个任务，超过上限 {max_jobs}"
        )

    audio_defaults = music.get("config", {})
    effect_defaults = effects.get("config", {})
    if not isinstance(audio_defaults, dict) or not isinstance(effect_defaults, dict):
        raise ValueError("音乐和特效的 config 必须是对象")

    jobs: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    batch_stub = uuid.uuid4().hex[:8]
    filter_metadata = {
        "rule": "source_minimum_two_core_dimensions",
        "minimum_source_changes": MINIMUM_CORE_SOURCE_CHANGES,
        "core_dimensions": ["bgm", "effect"],
        "raw_total": raw_total,
        "filtered_total": total,
        "removed_total": 0,
        "varying_dimensions": [
            key for key, count in (("bgm", len(identities)), ("effect", len(effect_paths))) if count > 1
        ],
    }
    for index, (music_index, effect_index) in enumerate(combination_indices, start=1):
        music_identity = identities[music_index]
        effect_path = Path(effect_paths[effect_index]).expanduser().resolve()
        if not effect_path.is_file() or not _is_relative_to(effect_path, EFFECT_LIBRARY_ROOT.resolve()):
            raise ValueError(f"特效不在特效库中或文件不存在: {effect_path}")

        job = deepcopy(base_job)
        audio = deepcopy(audio_defaults)
        audio.update(
            {
                "type": "add",
                "library_identity": music_identity,
                "selection_mode": "specific",
            }
        )
        effect = deepcopy(effect_defaults)
        effect["effect_json_path"] = str(effect_path)
        job["audios"] = [audio]
        job["effects"] = [effect]
        output = job.setdefault("output", {})
        if not isinstance(output, dict):
            raise ValueError("job.output 必须是对象")
        output["draft_name"] = f"batch_{batch_stub}_{index:04d}"
        jobs.append(job)
        variants.append(
            {
                "music_identity": music_identity,
                "effect_path": str(effect_path),
                "effect_name": effect_path.stem,
                "changed_elements": ["bgm", "effect"],
                "change_count": 2,
                "combination_filter": filter_metadata,
            }
        )
    return jobs, variants


def _unique_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _recover_legacy_export_success(
    settings: WebApiSettings,
    job_id: str,
    status: dict[str, Any],
    status_path: Path,
) -> dict[str, Any]:
    error = str(status.get("error", ""))
    if (
        status.get("status") != "failed"
        or "RenderJobResult" not in error
        or "output_mp4_path" not in error
        or status.get("output_deleted")
    ):
        return status

    job_path = _job_dir(settings, job_id) / "job.json"
    if not job_path.is_file():
        return status
    job = _read_json(job_path)
    output = job.get("output", {})
    if not isinstance(output, dict):
        return status

    output_mp4_text = str(output.get("mp4_path") or output.get("output_mp4") or "").strip()
    if not output_mp4_text:
        return status
    output_mp4 = Path(output_mp4_text).expanduser().resolve()
    managed_output_root = (settings.storage_root / "outputs").resolve()
    if (
        not _is_relative_to(output_mp4, managed_output_root)
        or not output_mp4.is_file()
        or output_mp4.stat().st_size <= 0
    ):
        return status

    draft_name = str(output.get("draft_name") or output.get("output_name") or "").strip()
    draft_root = Path(str(output.get("draft_root") or settings.default_draft_root)).expanduser().resolve()
    source = job.get("source", {})
    source_kind = str(source.get("type", "")) if isinstance(source, dict) else ""
    recovered = deepcopy(status)
    recovered.update(
        {
            "status": "completed",
            "expires_at": _expiry_after(settings.completed_output_retention_hours),
            "result": {
                "source_kind": source_kind,
                "output_draft_dir": str(draft_root / draft_name) if draft_name else "",
                "output_draft_name": draft_name,
                "output_mp4": str(output_mp4),
                "exported": True,
            },
            "recovered_at": _now(),
            "recovery_reason": "legacy_output_mp4_path_status_bug",
        }
    )
    recovered.pop("error", None)
    _write_json(status_path, recovered)
    return recovered


def _batch_status_fields(status: dict[str, Any]) -> dict[str, Any]:
    return {
        key: status[key]
        for key in ("batch_id", "batch_index", "batch_total", "variant")
        if key in status
    }


def _expiry_after(hours: int, *, now: datetime | None = None) -> str:
    base = now or datetime.now()
    return (base + timedelta(hours=max(1, hours))).isoformat(timespec="seconds")


def _env_positive_int(name: str, default: int) -> int:
    value = str(os.environ.get(name, default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是正整数: {value}") from exc
    if parsed <= 0:
        raise RuntimeError(f"环境变量 {name} 必须是正整数: {value}")
    return parsed


def _load_or_create_text_secret(path: Path) -> str:
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except (OSError, UnicodeDecodeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def _media_ids_in_data(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "media_id" and child:
                result.add(str(child))
            else:
                result.update(_media_ids_in_data(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_media_ids_in_data(child))
    return result


def _extend_job_media_expiration(settings: WebApiSettings, job: dict[str, Any]) -> None:
    expires_at = _expiry_after(settings.media_retention_hours)
    for media_id in _media_ids_in_data(job):
        record_path = _media_meta_path(settings, media_id)
        if not record_path.exists():
            continue
        try:
            record = _read_json(record_path)
            current = _parse_timestamp(record.get("expires_at"))
            replacement = _parse_timestamp(expires_at)
            if current is None or (replacement is not None and replacement > current):
                record["expires_at"] = expires_at
                _write_json(record_path, record)
        except Exception:
            continue


def _active_job_media_ids(settings: WebApiSettings) -> set[str]:
    result: set[str] = set()
    jobs_root = settings.storage_root / "jobs"
    if not jobs_root.exists():
        return result
    for status_path in jobs_root.glob("*/status.json"):
        try:
            status = _read_json(status_path)
            if status.get("status") not in {"pending", "running"}:
                continue
            job_path = status_path.parent / "job.json"
            if job_path.exists():
                result.update(_media_ids_in_data(_read_json(job_path)))
        except Exception:
            continue
    return result


def _active_job_template_ids(settings: WebApiSettings) -> set[str]:
    result: set[str] = set()
    jobs_root = settings.storage_root / "jobs"
    if not jobs_root.exists():
        return result
    for status_path in jobs_root.glob("*/status.json"):
        try:
            status = _read_json(status_path)
            if status.get("status") not in {"pending", "running"}:
                continue
            job_path = status_path.parent / "job.json"
            if not job_path.exists():
                continue
            job = _read_json(job_path)
            source = job.get("source")
            template_id = source.get("template_id") if isinstance(source, dict) else ""
            if template_id:
                result.add(str(template_id))
        except Exception:
            continue
    return result


def _active_job_reference_text(settings: WebApiSettings) -> str:
    jobs_root = settings.storage_root / "jobs"
    if not jobs_root.exists():
        return ""
    payloads: list[str] = []
    for status_path in jobs_root.glob("*/status.json"):
        try:
            status = _read_json(status_path)
            if status.get("status") not in {"pending", "running"}:
                continue
            job_path = status_path.parent / "job.json"
            if job_path.is_file():
                payloads.append(json.dumps(_read_json(job_path), ensure_ascii=False))
        except Exception:
            continue
    return "\n".join(payloads)


def _purge_asset_storage(
    settings: WebApiSettings,
    audio_catalog: CombinedAudioCatalog,
    item: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, int]:
    kind = str(item.get("kind", ""))
    targets: list[tuple[Path, Path, bool]] = []

    if kind == "template":
        root = settings.template_library_root.resolve()
        raw_path = str(item.get("root_dir", "")).strip()
        if raw_path:
            targets.append((Path(raw_path).expanduser().resolve(), root, True))
    elif kind == "audio":
        raw_path = str(item.get("absolute_path", "")).strip()
        if raw_path:
            path = Path(raw_path).expanduser().resolve()
            for catalog in audio_catalog.catalogs:
                root = catalog.root.resolve()
                if _is_relative_to(path, root):
                    targets.append((path, root, False))
                    metadata_file = str(item.get("metadata_file", "")).strip()
                    if metadata_file:
                        targets.append(((root / metadata_file).resolve(), root, False))
                    break
    else:
        raw_root = str(item.get("_library_root", "")).strip()
        raw_path = str(item.get("path", "")).strip()
        if raw_root and raw_path:
            root = Path(raw_root).expanduser().resolve()
            path = Path(raw_path).expanduser().resolve()
            bundle_kinds = {"sticker", "corner_sticker", "text_effect", "text_template"}
            targets.append((path.parent if kind in bundle_kinds else path, root, kind in bundle_kinds))

    result = {"deleted_files": 0, "deleted_directories": 0, "deleted_bytes": 0}
    seen: set[Path] = set()
    for target, allowed_root, delete_directory in targets:
        if target in seen:
            continue
        seen.add(target)
        if target == allowed_root or not _is_relative_to(target, allowed_root) or not target.exists():
            continue
        if delete_directory:
            if not target.is_dir():
                raise RuntimeError(f"素材目录格式不正确: {target}")
            result["deleted_bytes"] += _directory_size(target)
            result["deleted_directories"] += 1
            if not dry_run:
                shutil.rmtree(target)
        else:
            if not target.is_file():
                raise RuntimeError(f"素材文件格式不正确: {target}")
            result["deleted_bytes"] += target.stat().st_size
            result["deleted_files"] += 1
            if not dry_run:
                target.unlink()
    return result


def _validate_batch_once_template_ids(
    settings: WebApiSettings,
    payloads: list[dict[str, Any]],
    template_ids: list[str],
) -> list[str]:
    requested = _unique_strings(template_ids)
    if not requested:
        return []
    referenced = {
        str(source.get("template_id", "")).strip()
        for payload in payloads
        for source in [payload.get("source")]
        if isinstance(source, dict) and str(source.get("template_id", "")).strip()
    }
    library = TemplateLibrary(settings.template_library_root)
    validated: list[str] = []
    for template_id in requested:
        record = library.get(template_id)
        if record.template_id not in referenced:
            raise ValueError(f"临时母版未被当前 Excel 批次使用: {record.name}")
        if record.import_info.get("source") != "local_collector" or record.import_info.get(
            "lifecycle"
        ) != "excel_batch_once":
            raise ValueError(f"母版不是 Excel 批量临时上传: {record.name}")
        validated.append(record.template_id)
    return validated


def _claim_batch_once_templates(
    settings: WebApiSettings,
    template_ids: list[str],
    batch_id: str,
) -> None:
    library = TemplateLibrary(settings.template_library_root)
    for template_id in template_ids:
        record = library.get(template_id)
        meta = _read_json(record.meta_path)
        import_info = meta.get("import_info")
        if not isinstance(import_info, dict):
            import_info = {}
        import_info["lifecycle"] = "excel_batch_once"
        import_info["batch_id"] = batch_id
        import_info["claimed_at"] = _now()
        meta["import_info"] = import_info
        _write_json(record.meta_path, meta)


def _cleanup_finished_batch_once_templates(
    settings: WebApiSettings,
    batch_id: str,
    *,
    dry_run: bool = False,
    keep_failed: bool = True,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "batch_id": batch_id,
        "deleted_templates": 0,
        "deleted_files": 0,
        "deleted_directories": 0,
        "deleted_bytes": 0,
    }
    batch_path = _batch_dir(settings, batch_id) / "batch.json"
    if not batch_path.is_file():
        result["status"] = "batch_missing"
        return result
    batch = _read_json(batch_path)
    template_ids = _unique_strings(batch.get("temporary_template_ids", []))
    if not template_ids or batch.get("temporary_templates_cleaned_at"):
        result["status"] = "nothing_to_delete"
        return result

    statuses: list[str] = []
    for item in batch.get("jobs", []):
        if not isinstance(item, dict) or not item.get("job_id"):
            continue
        status_path = _job_dir(settings, str(item["job_id"])) / "status.json"
        if not status_path.is_file():
            result["status"] = "waiting_for_jobs"
            return result
        statuses.append(str(_read_json(status_path).get("status", "")))
    if not statuses or any(status in {"", "pending", "running"} for status in statuses):
        result["status"] = "waiting_for_jobs"
        return result
    if keep_failed and any(status == "failed" for status in statuses):
        result["status"] = "kept_for_failed_retry"
        return result
    terminal_statuses = {"completed", "cancelled", "failed"} if not keep_failed else {"completed", "cancelled"}
    if any(status not in terminal_statuses for status in statuses):
        result["status"] = "waiting_for_jobs"
        return result

    current = now or datetime.now()
    retained_until = _parse_timestamp(batch.get("temporary_templates_retained_until"))
    if not force and retained_until is None:
        retained_until = current + timedelta(hours=24)
        result["status"] = "retained_for_24_hours"
        result["retained_until"] = retained_until.isoformat(timespec="seconds")
        if not dry_run:
            library = TemplateLibrary(settings.template_library_root)
            for template_id in template_ids:
                try:
                    record = library.get(template_id)
                except FileNotFoundError:
                    continue
                meta = _read_json(record.meta_path)
                existing_expiry = _parse_timestamp(meta.get("expires_at"))
                if existing_expiry is None or existing_expiry < retained_until:
                    meta["expires_at"] = retained_until.isoformat(timespec="seconds")
                    _write_json(record.meta_path, meta)
            batch["temporary_templates_retained_at"] = current.isoformat(timespec="seconds")
            batch["temporary_templates_retained_until"] = retained_until.isoformat(timespec="seconds")
            _write_json(batch_path, batch)
        return result
    if not force and retained_until is not None and retained_until > current:
        result["status"] = "retained_for_24_hours"
        result["retained_until"] = retained_until.isoformat(timespec="seconds")
        return result

    library = TemplateLibrary(settings.template_library_root)
    library_root = settings.template_library_root.resolve()
    records_root = (settings.storage_root / "draft_imports" / "records").resolve()
    incoming_root = (settings.storage_root / "draft_imports" / "incoming").resolve()
    deleted_ids: list[str] = []
    for template_id in template_ids:
        try:
            record = library.get(template_id)
        except FileNotFoundError:
            deleted_ids.append(template_id)
            continue
        if record.import_info.get("lifecycle") != "excel_batch_once":
            continue
        if str(record.import_info.get("batch_id", "")) != batch_id:
            continue

        paths: list[tuple[Path, Path]] = [(record.root_dir.resolve(), library_root)]
        import_id = str(record.import_info.get("import_id", "")).strip()
        if import_id and import_id.isalnum():
            paths.append(((records_root / import_id).resolve(), records_root))
        incoming_path = Path(str(record.import_info.get("incoming_package_path", ""))).resolve()
        if incoming_path != incoming_root and _is_relative_to(incoming_path, incoming_root):
            paths.append((incoming_path, incoming_root))

        for path, allowed_root in paths:
            if path == allowed_root or not _is_relative_to(path, allowed_root) or not path.exists():
                continue
            result["deleted_bytes"] += _directory_size(path) if path.is_dir() else path.stat().st_size
            if path.is_dir():
                result["deleted_directories"] += 1
                if not dry_run:
                    shutil.rmtree(path)
            else:
                result["deleted_files"] += 1
                if not dry_run:
                    path.unlink()
        result["deleted_templates"] += 1
        deleted_ids.append(record.template_id)

    result["status"] = "deleted" if deleted_ids else "nothing_to_delete"
    result["deleted_template_ids"] = deleted_ids
    if not dry_run:
        batch["temporary_templates_cleaned_at"] = _now()
        batch["temporary_templates_deleted"] = deleted_ids
        _write_json(batch_path, batch)
    return result


def _delete_managed_job_artifacts(
    settings: WebApiSettings,
    result: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
    include_drafts: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    deleted_files = 0
    deleted_directories = 0
    deleted_bytes = 0
    output_root = (settings.storage_root / "outputs").resolve()
    output_text = str(result.get("output_mp4", "")).strip()
    if output_text:
        output_path = Path(output_text).resolve()
        if output_path.is_file() and _is_relative_to(output_path, output_root):
            deleted_bytes += output_path.stat().st_size
            deleted_files += 1
            if not dry_run:
                output_path.unlink()

    draft_deletion = (
        _delete_managed_draft_artifacts(
            settings,
            result,
            job=job,
            dry_run=dry_run,
        )
        if include_drafts
        else {"deleted_files": 0, "deleted_directories": 0, "deleted_bytes": 0}
    )
    return {
        "deleted_files": deleted_files + draft_deletion["deleted_files"],
        "deleted_directories": deleted_directories + draft_deletion["deleted_directories"],
        "deleted_bytes": deleted_bytes + draft_deletion["deleted_bytes"],
    }


def _delete_managed_draft_artifacts(
    settings: WebApiSettings,
    result: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    deleted_directories = 0
    deleted_bytes = 0
    generated_root = (settings.storage_root / "generated_video_drafts").resolve()
    draft_root = settings.default_draft_root.resolve()

    directory_candidates: list[tuple[Path, Path]] = []
    output_draft_text = str(result.get("output_draft_dir", "")).strip()
    if output_draft_text:
        directory_candidates.append((Path(output_draft_text).resolve(), draft_root))
    elif isinstance(job, dict):
        output = job.get("output", {})
        if isinstance(output, dict):
            output_root_text = str(output.get("draft_root", "")).strip()
            output_name = str(output.get("draft_name") or output.get("output_name") or "").strip()
            if output_root_text and output_name:
                directory_candidates.append(
                    ((Path(output_root_text).expanduser() / output_name).resolve(), draft_root)
                )
    for key in ("source_draft_dir", "working_template_dir"):
        path_text = str(result.get(key, "")).strip()
        if path_text:
            directory_candidates.append((Path(path_text).resolve(), generated_root))

    seen: set[Path] = set()
    for path, allowed_root in directory_candidates:
        if path in seen or path == allowed_root or not path.is_dir():
            continue
        seen.add(path)
        if not _is_relative_to(path, allowed_root):
            continue
        deleted_bytes += _directory_size(path)
        deleted_directories += 1
        if not dry_run:
            shutil.rmtree(path)
    return {
        "deleted_files": 0,
        "deleted_directories": deleted_directories,
        "deleted_bytes": deleted_bytes,
    }


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _append_cleanup_error(report: dict[str, Any], path: Path, exc: Exception) -> None:
    errors = report.setdefault("errors", [])
    if len(errors) < 20:
        errors.append({"path": str(path), "error": str(exc)})


def _merge_deletion_report(report: dict[str, Any], deletion: dict[str, int]) -> None:
    report["deleted_files"] += deletion.get("deleted_files", 0)
    report["deleted_directories"] += deletion.get("deleted_directories", 0)
    report["deleted_bytes"] += deletion.get("deleted_bytes", 0)


def _estimate_batch_timing(statuses: list[dict[str, Any]]) -> dict[str, int | None]:
    durations: list[float] = []
    for status in statuses:
        if status.get("status") not in {"completed", "failed"}:
            continue
        started = _parse_timestamp(status.get("started_at"))
        finished = _parse_timestamp(status.get("finished_at"))
        if started is not None and finished is not None and finished >= started:
            durations.append((finished - started).total_seconds())

    if not durations:
        return {"average_job_seconds": None, "estimated_remaining_seconds": None}

    average = sum(durations) / len(durations)
    remaining = average * sum(1 for status in statuses if status.get("status") == "pending")
    now = datetime.now()
    for status in statuses:
        if status.get("status") != "running":
            continue
        started = _parse_timestamp(status.get("started_at"))
        elapsed = (now - started).total_seconds() if started is not None else 0
        remaining += max(0, average - elapsed)
    return {
        "average_job_seconds": max(0, round(average)),
        "estimated_remaining_seconds": max(0, round(remaining)),
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _job_ids_from_payload(payload: dict[str, Any]) -> list[str]:
    value = payload.get("job_ids")
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="job_ids 必须是数组")
    return _unique_strings(value)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _list_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _reject_mother_composite_text_changes(job: dict[str, Any]) -> None:
    additions = _list_items(job.get("text_templates")) + _list_items(job.get("text_template"))
    if additions:
        raise HTTPException(
            status_code=400,
            detail="剪辑母版中的复合文字模板由人工设计，生成时只能原样保留，不能自动新增或替换",
        )


def _job_display_name(status: dict[str, Any]) -> str:
    variant = status.get("variant", {})
    if not isinstance(variant, dict):
        return ""
    return str(variant.get("display_name", "")).strip()


def _safe_download_stem(value: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" .")
    return safe[:120] or "video"


def _load_media_record(settings: WebApiSettings, media_id: str) -> dict[str, Any]:
    meta_path = _media_meta_path(settings, media_id)
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail=f"素材不存在: {media_id}")
    record = _read_json(meta_path)
    path = Path(str(record.get("path", "")))
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"素材文件不存在: {path}")
    return record


def _media_meta_path(settings: WebApiSettings, media_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", media_id).strip("._")
    return settings.storage_root / "media" / "records" / f"{safe_id}.json"


def _job_dir(settings: WebApiSettings, job_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_id).strip("._")
    return settings.storage_root / "jobs" / safe_id


def _batch_dir(settings: WebApiSettings, batch_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", batch_id).strip("._")
    return settings.storage_root / "batches" / safe_id


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    safe = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "_", name).strip(" ._")
    return safe or "upload.bin"


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def _required(data: dict[str, Any], key: str) -> Any:
    value = data.get(key)
    if value in (None, ""):
        raise ValueError(f"缺少必填字段: {key}")
    return value


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON 顶层不是对象: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _list_draft_dirs(root: Path) -> list[dict[str, Any]]:
    if (root / "draft_content.json").exists():
        candidates = [root]
    else:
        candidates = [
            path
            for path in root.iterdir()
            if path.is_dir() and (path / "draft_content.json").exists()
        ]

    records = [_draft_dir_record(path) for path in candidates]
    records.sort(key=lambda item: str(item.get("modified_at", "")), reverse=True)
    return records


def _draft_dir_record(path: Path) -> dict[str, Any]:
    draft_content = path / "draft_content.json"
    plain = is_plain_json_file(draft_content)
    stat = draft_content.stat()
    record: dict[str, Any] = {
        "name": path.name,
        "path": str(path.resolve()),
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "draft_content_size": stat.st_size,
        "plain_json": plain,
        "needs_decrypt": not plain,
        "status": "plain" if plain else "encrypted",
        "summary": {},
    }

    if plain:
        try:
            data = _read_json(draft_content)
            tracks = data.get("tracks", [])
            record["duration_us"] = int(data.get("duration", 0) or 0)
            record["track_count"] = len(tracks) if isinstance(tracks, list) else 0
            record["summary"] = summarize_draft_data(data)
        except Exception as exc:
            record["status"] = "plain_read_error"
            record["error"] = str(exc)

    return record


def _require_library_file(path_value: str, library_root: Path, label: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    root = library_root.resolve()
    if not path.is_file() or not _is_relative_to(path, root):
        raise HTTPException(status_code=400, detail=f"{label}不在素材库中或文件不存在: {path}")
    return path


def _require_library_file_any(path_value: str, library_roots: list[Path], label: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if path.is_file() and any(_is_relative_to(path, root.resolve()) for root in library_roots):
        return path
    raise HTTPException(status_code=400, detail=f"{label}不在公共或个人素材库中: {path}")


def _raw_admin_asset_groups(
    settings: WebApiSettings,
    audio_catalog: AudioCatalog,
) -> dict[str, list[dict[str, Any]]]:
    audio = deepcopy(audio_catalog.snapshot().get("assets", []))
    for item in audio:
        if isinstance(item, dict) and item.get("identity"):
            item["preview_type"] = "audio"
            item["preview_url"] = f"/api/audio-library/file?identity={quote(str(item['identity']))}"

    fonts = _combined_library_items(
        settings, FONT_LIBRARY_ROOT, "font_library", _list_font_library
    )
    for item in fonts:
        if item.get("identity") and item.get("available"):
            item["preview_type"] = "font"
            item["preview_url"] = f"/api/assets/fonts/{quote(str(item['identity']), safe='')}/file"

    stickers = _combined_bundle_items(
        settings, STICKER_LIBRARY_ROOT, "sticker_library",
        "sticker_manifest.json", "stickers", "jyd_probe.fullscreen_sticker.v1",
    )
    for item in stickers:
        if item.get("identity") and item.get("preview_file"):
            item["preview_type"] = "image"
            item["preview_url"] = (
                f"/api/assets/stickers/{quote(str(item['identity']), safe='')}/preview"
            )

    corner_stickers = _combined_bundle_items(
        settings, CORNER_STICKER_LIBRARY_ROOT, "corner_sticker_library",
        "sticker_manifest.json", "stickers", "jyd_probe.fullscreen_sticker.v1",
    )
    for item in corner_stickers:
        if item.get("identity") and item.get("preview_file"):
            item["preview_type"] = "image"
            item["preview_url"] = (
                f"/api/assets/corner-stickers/{quote(str(item['identity']), safe='')}/preview"
            )

    text_styles = _combined_library_items(
        settings, TEXT_STYLE_LIBRARY_ROOT, "text_style_library", _list_json_library
    )
    for item in text_styles:
        if not item.get("error"):
            item["preview_type"] = "font"
            item["preview_url"] = (
                f"/api/assets/text-styles/{quote(str(item['original_file_stem']), safe='')}/font"
            )

    templates: list[dict[str, Any]] = []
    for record in TemplateLibrary(settings.template_library_root).list():
        item = record.as_dict()
        item["identity"] = record.template_id
        item["preview_type"] = "video"
        item["preview_url"] = f"/api/templates/{quote(record.template_id, safe='')}/preview-video"
        templates.append(item)

    return {
        "audio": [item for item in audio if isinstance(item, dict)],
        "font": fonts,
        "effect": _combined_library_items(
            settings, EFFECT_LIBRARY_ROOT, "effect_library", _list_json_library
        ),
        "sticker": stickers,
        "corner_sticker": corner_stickers,
        "text_effect": _combined_bundle_items(
            settings,
            TEXT_EFFECT_LIBRARY_ROOT,
            "text_effect_library",
            "text_effect_manifest.json",
            "effects",
            "jyd_probe.text_effect.v1",
        ),
        "text_style": text_styles,
        "text_template": _combined_bundle_items(
            settings,
            TEXT_TEMPLATE_LIBRARY_ROOT,
            "text_template_library",
            "text_template_manifest.json",
            "templates",
            "jyd_probe.text_template.v1",
        ),
        "template": templates,
    }


def _raw_local_asset_groups(
    settings: WebApiSettings,
    audio_catalog: AudioCatalog,
) -> dict[str, list[dict[str, Any]]]:
    groups = _raw_admin_asset_groups(settings, audio_catalog)
    local_groups = {
        kind: [
            {**item, "kind": kind}
            for item in items
        ]
        for kind, items in groups.items()
    }
    for kind, items in local_groups.items():
        for item in items:
            if _local_asset_preview_path(item) is None:
                item.pop("preview_url", None)
                item.pop("preview_type", None)
                continue
            item["preview_url"] = (
                f"/api/local-assets/{quote(kind, safe='')}/"
                f"{quote(str(item.get('identity', '')), safe='')}/preview"
            )
            item["preview_type"] = (
                "audio" if kind == "audio" else "font" if kind == "font" else "image"
            )
    return local_groups


def _require_local_asset(
    settings: WebApiSettings,
    audio_catalog: AudioCatalog,
    kind: str,
    identity: str,
) -> dict[str, Any]:
    groups = _raw_local_asset_groups(settings, audio_catalog)
    if kind not in groups:
        raise HTTPException(status_code=400, detail=f"不支持的本机素材类型: {kind!r}")
    item = next((value for value in groups[kind] if value.get("identity") == identity), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"本机素材不存在: {kind}/{identity}")
    return item


def _decorated_local_asset(
    settings: WebApiSettings,
    audio_catalog: AudioCatalog,
    asset_admin: AssetAdminCatalog,
    kind: str,
    identity: str,
) -> dict[str, Any]:
    source = _require_local_asset(settings, audio_catalog, kind, identity)
    decorated = asset_admin.decorate(kind, [source], include_deleted=True)
    if not decorated:
        raise HTTPException(status_code=404, detail=f"本机素材不存在: {kind}/{identity}")
    return decorated[0]


def _local_asset_preview_path(item: dict[str, Any]) -> Path | None:
    kind = str(item.get("kind", ""))
    if kind == "audio":
        path = Path(str(item.get("absolute_path", ""))).expanduser().resolve()
        return path if path.is_file() else None
    if kind == "font":
        path = Path(str(item.get("path", ""))).expanduser().resolve()
        return path if path.is_file() else None
    preview_file = str(item.get("preview_file", "")).strip()
    library_root = Path(str(item.get("_library_root", ""))).expanduser().resolve()
    if not preview_file or not library_root.is_dir():
        return None
    path = (library_root / preview_file).resolve()
    if not path.is_file() or not _is_relative_to(path, library_root):
        return None
    return path


def _require_admin_asset(
    settings: WebApiSettings,
    audio_catalog: AudioCatalog,
    kind: str,
    identity: str,
) -> dict[str, Any]:
    groups = _raw_admin_asset_groups(settings, audio_catalog)
    if kind not in groups:
        raise HTTPException(status_code=400, detail=f"不支持的素材类型: {kind!r}")
    item = next((value for value in groups[kind] if value.get("identity") == identity), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"素材不存在: {kind}/{identity}")
    return item


def _decorated_admin_asset(
    settings: WebApiSettings,
    audio_catalog: AudioCatalog,
    asset_admin: AssetAdminCatalog,
    kind: str,
    identity: str,
) -> dict[str, Any]:
    source = _require_admin_asset(settings, audio_catalog, kind, identity)
    decorated = asset_admin.decorate(kind, [source], include_deleted=True)
    if not decorated:
        raise HTTPException(status_code=404, detail=f"素材不存在: {kind}/{identity}")
    return decorated[0]


def _decorate_audio_snapshot(
    snapshot: dict[str, Any],
    asset_admin: AssetAdminCatalog,
) -> dict[str, Any]:
    result = deepcopy(snapshot)
    source_assets = result.get("assets", [])
    assets = asset_admin.decorate(
        "audio",
        [item for item in source_assets if isinstance(item, dict)],
    )
    result["assets"] = assets
    result["asset_count"] = len(assets)
    categories = result.get("categories", [])
    for category in categories if isinstance(categories, list) else []:
        if not isinstance(category, dict):
            continue
        category_id = str(category.get("id", ""))
        category["asset_count"] = sum(
            1
            for item in assets
            if item.get("enabled", True)
            and category_id in (item.get("category_ids", []) if isinstance(item.get("category_ids"), list) else [])
        )
    return result


def _import_personal_asset_package(package_path: Path, personal_root: Path) -> dict[str, Any]:
    allowed_directories = {
        "audio_library",
        "effect_library",
        "font_library",
        "sticker_library",
        "corner_sticker_library",
        "text_effect_library",
        "text_template_library",
    }
    destination = personal_root.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    staging = package_path.parent / f"extract_{uuid.uuid4().hex}"
    imported_files = 0
    imported_bytes = 0
    imported_libraries: set[str] = set()
    try:
        staging.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(package_path) as archive:
            infos = archive.infolist()
            if len(infos) > 50_000:
                raise ValueError("个人素材包文件数量过多")
            for info in infos:
                relative = PurePosixPath(info.filename)
                if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                    raise ValueError(f"个人素材包包含不安全路径: {info.filename}")
                if relative.name == "personal_assets_manifest.json" and len(relative.parts) == 1:
                    continue
                library_name = relative.parts[0]
                if library_name not in allowed_directories:
                    raise ValueError(f"个人素材包包含不支持的目录: {library_name}")
                imported_libraries.add(library_name)
                if info.is_dir():
                    continue
                imported_files += 1
                imported_bytes += int(info.file_size)
                target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        if not imported_libraries:
            raise ValueError("个人素材包中没有可导入的素材库")
        for library_name in sorted(imported_libraries):
            source = staging / library_name
            if source.is_dir():
                shutil.copytree(source, destination / library_name, dirs_exist_ok=True)
        return {
            "personal_library_root": str(destination),
            "imported_libraries": sorted(imported_libraries),
            "imported_files": imported_files,
            "imported_bytes": imported_bytes,
        }
    finally:
        if staging.is_dir():
            shutil.rmtree(staging)


def _library_roots(
    settings: WebApiSettings, public_root: Path, personal_name: str
) -> list[Path]:
    roots = [public_root.resolve()]
    if settings.personal_library_root is not None:
        personal = (settings.personal_library_root / personal_name).resolve()
        personal.mkdir(parents=True, exist_ok=True)
        if personal not in roots:
            roots.append(personal)
    return roots


def _font_library_roots(settings: WebApiSettings) -> list[Path]:
    roots = _library_roots(settings, FONT_LIBRARY_ROOT, "font_library")
    system = SYSTEM_FONT_ROOT.resolve()
    if system.is_dir() and system not in roots:
        roots.append(system)
    return roots


def _combined_library_items(
    settings: WebApiSettings,
    public_root: Path,
    personal_name: str,
    loader,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, root in enumerate(_library_roots(settings, public_root, personal_name)):
        for raw in loader(root):
            item = {**raw, "library_scope": "public" if index == 0 else "personal", "_library_root": str(root)}
            identity = str(item.get("identity") or item.get("path") or "")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            result.append(item)
    return result


def _combined_bundle_items(
    settings: WebApiSettings,
    public_root: Path,
    personal_name: str,
    manifest_name: str,
    manifest_key: str,
    asset_schema: str,
) -> list[dict[str, Any]]:
    return _combined_library_items(
        settings,
        public_root,
        personal_name,
        lambda root: _list_bundle_library(root, manifest_name, manifest_key, asset_schema),
    )


def _list_bundle_library(
    root: Path,
    manifest_name: str,
    manifest_key: str,
    asset_schema: str,
) -> list[dict[str, Any]]:
    manifest_path = root / "manifest" / manifest_name
    if not manifest_path.exists():
        return []
    try:
        manifest = _read_json(manifest_path)
    except Exception:
        return []
    entries = manifest.get(manifest_key, [])
    if not isinstance(entries, list):
        return []

    result: list[dict[str, Any]] = []
    root_resolved = root.resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        metadata_file = str(entry.get("metadata_file", "")).strip()
        metadata_path = (root / metadata_file).resolve() if metadata_file else Path()
        item = deepcopy(entry)
        item["path"] = str(metadata_path) if metadata_file else ""
        try:
            if not metadata_file or not metadata_path.is_file() or not _is_relative_to(metadata_path, root_resolved):
                raise FileNotFoundError(f"素材 metadata 不存在: {metadata_path}")
            data = _read_json(metadata_path)
            if data.get("schema") != asset_schema:
                raise RuntimeError(f"素材 schema 不匹配: {data.get('schema')!r}")
            item["schema"] = data.get("schema")
            item["name"] = data.get("name") or item.get("name") or metadata_path.parent.name
            slots = data.get("text_slots", [])
            if isinstance(slots, list):
                item["text_slots"] = [
                    {
                        "slot_index": int(slot.get("slot_index", index)),
                        "text": str(slot.get("text", "")),
                    }
                    for index, slot in enumerate(slots)
                    if isinstance(slot, dict)
                ]
        except Exception as exc:
            item["error"] = str(exc)
        result.append(item)
    return result


def _list_system_fonts() -> list[dict[str, Any]]:
    names = {
        "simhei.ttf": "Windows 黑体",
        "simsunb.ttf": "Windows 宋体",
        "arial.ttf": "Arial",
    }
    result: list[dict[str, Any]] = []
    for filename, display_name in names.items():
        path = (SYSTEM_FONT_ROOT / filename).resolve()
        if path.is_file():
            result.append(
                {
                    "identity": f"system:{filename.casefold()}",
                    "name": display_name,
                    "resource_id": f"system-{Path(filename).stem.casefold()}",
                    "path": str(path),
                    "available": True,
                    "enabled": True,
                }
            )
    return result


def _list_font_library(root: Path) -> list[dict[str, Any]]:
    manifest_path = root / "manifest" / "font_manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        manifest = _read_json(manifest_path)
    except Exception:
        return []
    if manifest.get("schema") != "jyd_probe.font_library_manifest.v1":
        return []
    entries = manifest.get("fonts", [])
    if not isinstance(entries, list):
        return []

    root_resolved = root.resolve()
    result: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        relative_file = str(entry.get("file", "")).strip()
        path = (root / relative_file).resolve() if relative_file else Path()
        resource_id = str(entry.get("resource_id") or entry.get("effect_id") or "").strip()
        available = bool(
            relative_file
            and resource_id
            and path.is_file()
            and _is_relative_to(path, root_resolved)
        )
        result.append(
            {
                "identity": str(entry.get("identity", "")),
                "name": str(entry.get("name", "")) or path.stem,
                "resource_id": resource_id,
                "effect_id": str(entry.get("effect_id", "")),
                "path": str(path) if relative_file and _is_relative_to(path, root_resolved) else "",
                "available": available,
                "size_bytes": int(entry.get("size_bytes", 0) or 0),
            }
        )
    layout_font_path = root / "files" / "FZCuJinLJW_7086699209738424840.ttf"
    if layout_font_path.is_file() and not any(
        item.get("identity") == LAYOUT_PROFILE_FONT_IDENTITY for item in result
    ):
        result.append(
            {
                "identity": LAYOUT_PROFILE_FONT_IDENTITY,
                "name": "FZCuJinLJW",
                "resource_id": "7086699209738424840",
                "effect_id": "",
                "path": str(layout_font_path.resolve()),
                "available": True,
                "size_bytes": layout_font_path.stat().st_size,
            }
        )
    return result


def _list_json_library(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        item: dict[str, Any] = {
            "name": path.stem,
            "original_file_stem": path.stem,
            "path": str(path.resolve()),
        }
        try:
            data = _read_json(path)
        except Exception as exc:
            item["error"] = str(exc)
            result.append(item)
            continue

        item["schema"] = data.get("schema", "")
        if data.get("schema") == "jyd_probe.text_style.v1":
            item["preview"] = _text_style_preview(data)
        source = data.get("source")
        if isinstance(source, dict):
            item["label"] = source.get("label", "")
        if data.get("effect_label"):
            item["label"] = data.get("effect_label", "")
        material = data.get("material")
        if isinstance(material, dict):
            item["effect_name"] = material.get("name", "")
            item["effect_id"] = material.get("effect_id", "")
            item["resource_id"] = material.get("resource_id", "")
        stable_id = str(item.get("resource_id") or item.get("effect_id") or "").strip()
        item["identity"] = f"resource_id:{stable_id}" if stable_id else f"file:{path.name}"
        result.append(item)
    return result


def _text_style_preview(data: dict[str, Any]) -> dict[str, Any]:
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    styles = content.get("styles") if isinstance(content, dict) else []
    style = styles[0] if isinstance(styles, list) and styles and isinstance(styles[0], dict) else {}
    material = data.get("material_fields") if isinstance(data.get("material_fields"), dict) else {}
    segment = data.get("segment_fields") if isinstance(data.get("segment_fields"), dict) else {}
    clip = segment.get("clip") if isinstance(segment.get("clip"), dict) else {}
    transform = clip.get("transform") if isinstance(clip.get("transform"), dict) else {}
    fill = style.get("fill") if isinstance(style.get("fill"), dict) else {}
    fill_content = fill.get("content") if isinstance(fill.get("content"), dict) else {}
    solid = fill_content.get("solid") if isinstance(fill_content.get("solid"), dict) else {}
    color = solid.get("color") if isinstance(solid.get("color"), list) else [1, 1, 1]
    if len(color) < 3:
        color = [1, 1, 1]

    def color_channel(value: Any) -> int:
        try:
            return max(0, min(255, round(float(value) * 255)))
        except (TypeError, ValueError):
            return 255

    return {
        "size": float(style.get("size", 8) or 8),
        "color": "#" + "".join(f"{color_channel(value):02X}" for value in color[:3]),
        "bold": bool(style.get("bold", False)),
        "italic": bool(style.get("italic", False)),
        "underline": bool(style.get("underline", False)),
        "font_path": str(style.get("font", {}).get("path", "")) if isinstance(style.get("font"), dict) else "",
        "line_max_width": float(material.get("line_max_width", 0.82) or 0.82),
        "transform_x": float(transform.get("x", 0) or 0),
        "transform_y": float(transform.get("y", -0.8) if transform.get("y") is not None else -0.8),
    }
