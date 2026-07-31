from __future__ import annotations

import hmac
import json
import logging
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.database import get_db
from app.models import LongAudioProject, LongAudioProjectStatus
from app.services.logging_config import log_event
from app.services.long_audio import (
    LongAudioError,
    apply_alignment_plans,
    materialize_long_audio_project,
    sync_linked_batch_item,
)
from app.services.media_segmentation import SegmentPlan
from app.services.storage import safe_relative_path


router = APIRouter(prefix="/api/media-worker/v1", tags=["media-worker"])
logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _authorize_worker(
    authorization: str | None = Header(default=None),
) -> Settings:
    settings = get_settings()
    expected = settings.media_worker_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="服务器尚未配置 MEDIA_WORKER_TOKEN",
        )
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="媒体节点令牌无效")
    if settings.media_processing_mode != "remote":
        raise HTTPException(
            status_code=409,
            detail="服务器当前未启用远程媒体节点模式",
        )
    return settings


def _clear_lease(project: LongAudioProject) -> None:
    project.remote_lease_id = None
    project.remote_lease_expires_at = None


def _recover_expired_leases(db: Session, now: datetime) -> None:
    common = (
        LongAudioProject.remote_lease_id.is_not(None),
        LongAudioProject.remote_lease_expires_at.is_not(None),
        LongAudioProject.remote_lease_expires_at < now,
    )
    db.execute(
        update(LongAudioProject)
        .where(
            LongAudioProject.status
            == LongAudioProjectStatus.ANALYZING.value,
            *common,
        )
        .values(
            status=LongAudioProjectStatus.PENDING_ANALYSIS.value,
            remote_lease_id=None,
            remote_worker_id=None,
            remote_lease_expires_at=None,
            remote_last_heartbeat_at=None,
        )
    )
    db.execute(
        update(LongAudioProject)
        .where(
            LongAudioProject.status
            == LongAudioProjectStatus.CUTTING.value,
            *common,
        )
        .values(
            status=LongAudioProjectStatus.PENDING_CUT.value,
            remote_lease_id=None,
            remote_worker_id=None,
            remote_lease_expires_at=None,
            remote_last_heartbeat_at=None,
        )
    )


def _claim_next(
    db: Session,
    settings: Settings,
    *,
    worker_id: str,
    capabilities: set[str],
) -> tuple[LongAudioProject, str] | None:
    now = _now()
    _recover_expired_leases(db, now)
    pending: list[str] = []
    if "analysis" in capabilities:
        pending.append(LongAudioProjectStatus.PENDING_ANALYSIS.value)
    if "cut" in capabilities:
        pending.append(LongAudioProjectStatus.PENDING_CUT.value)
    if not pending:
        db.commit()
        return None

    rows = db.execute(
        select(LongAudioProject.id, LongAudioProject.status)
        .where(LongAudioProject.status.in_(pending))
        .order_by(LongAudioProject.created_at, LongAudioProject.id)
        .limit(10)
    ).all()
    for project_id, pending_status in rows:
        action = (
            "analysis"
            if pending_status
            == LongAudioProjectStatus.PENDING_ANALYSIS.value
            else "cut"
        )
        claimed_status = (
            LongAudioProjectStatus.ANALYZING.value
            if action == "analysis"
            else LongAudioProjectStatus.CUTTING.value
        )
        lease_id = str(uuid.uuid4())
        result = db.execute(
            update(LongAudioProject)
            .where(
                LongAudioProject.id == project_id,
                LongAudioProject.status == pending_status,
            )
            .values(
                status=claimed_status,
                error_code=None,
                error_message=None,
                remote_lease_id=lease_id,
                remote_worker_id=worker_id,
                remote_lease_expires_at=now
                + timedelta(seconds=settings.media_worker_lease_seconds),
                remote_last_heartbeat_at=now,
            )
        )
        db.commit()
        if result.rowcount != 1:
            continue
        project = db.scalar(
            select(LongAudioProject)
            .options(
                selectinload(LongAudioProject.user),
                selectinload(LongAudioProject.batch),
            )
            .where(LongAudioProject.id == project_id)
        )
        if project is not None:
            return project, action
    return None


def _plan_payload(project: LongAudioProject) -> list[dict[str, Any]]:
    try:
        value = json.loads(project.plan_json or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=409, detail="服务器分段方案损坏") from exc
    if not isinstance(value, list):
        raise HTTPException(status_code=409, detail="服务器分段方案损坏")
    return value


def _job_payload(project: LongAudioProject, action: str) -> dict[str, Any]:
    lease_id = project.remote_lease_id
    return {
        "jobId": project.id,
        "action": action,
        "leaseId": lease_id,
        "leaseExpiresAt": (
            project.remote_lease_expires_at.isoformat()
            if project.remote_lease_expires_at
            else None
        ),
        "name": project.name,
        "workflowType": project.workflow_type,
        "reviewRequired": project.review_required,
        "scriptText": project.script_text,
        "durationSeconds": project.duration_seconds,
        "alignmentProvider": project.alignment_provider,
        "segments": _plan_payload(project) if action == "cut" else [],
        "source": {
            "audioUrl": (
                f"/api/media-worker/v1/jobs/{project.id}/source/audio"
                f"?leaseId={lease_id}"
            ),
            "videoUrl": (
                f"/api/media-worker/v1/jobs/{project.id}/source/video"
                f"?leaseId={lease_id}"
            ),
            "audioName": project.audio_original_name,
            "videoName": project.video_original_name,
        },
    }


def _leased_project(
    db: Session,
    project_id: str,
    lease_id: str,
    *,
    expected_status: str | None = None,
) -> LongAudioProject:
    project = db.scalar(
        select(LongAudioProject)
        .options(
            selectinload(LongAudioProject.user),
            selectinload(LongAudioProject.batch),
        )
        .where(LongAudioProject.id == project_id)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="媒体任务不存在")
    if not lease_id or not hmac.compare_digest(
        lease_id, project.remote_lease_id or ""
    ):
        raise HTTPException(status_code=409, detail="媒体任务租约已失效")
    if expected_status and project.status != expected_status:
        raise HTTPException(status_code=409, detail="媒体任务状态已经变化")
    return project


def _metrics_json(value: Any) -> str | None:
    if value is None:
        return None
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise HTTPException(status_code=400, detail="节点指标数据过大")
    return encoded


@router.post("/jobs/claim")
async def claim_job(
    request: Request,
    settings: Settings = Depends(_authorize_worker),
    db: Session = Depends(get_db),
):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="领取请求必须为 JSON") from exc
    worker_id = str(body.get("workerId") or "").strip()
    if not worker_id or len(worker_id) > 100:
        raise HTTPException(status_code=400, detail="workerId 长度必须为 1-100")
    raw_capabilities = body.get("capabilities") or ["analysis", "cut"]
    if not isinstance(raw_capabilities, list):
        raise HTTPException(status_code=400, detail="capabilities 格式错误")
    capabilities = {
        str(item).strip().lower()
        for item in raw_capabilities
        if str(item).strip().lower() in {"analysis", "cut"}
    }
    claimed = _claim_next(
        db,
        settings,
        worker_id=worker_id,
        capabilities=capabilities,
    )
    if claimed is None:
        return Response(status_code=204)
    project, action = claimed
    log_event(
        logger,
        "media.remote_claimed",
        "远程媒体节点已领取任务",
        project_id=project.id,
        worker_id=worker_id,
        action=action,
    )
    return _job_payload(project, action)


@router.get("/jobs/{project_id}/source/{kind}")
def download_source(
    project_id: str,
    kind: str,
    leaseId: str,
    settings: Settings = Depends(_authorize_worker),
    db: Session = Depends(get_db),
):
    project = _leased_project(db, project_id, leaseId)
    if kind == "audio":
        relative_path = project.audio_path
        name = project.audio_original_name
    elif kind == "video":
        relative_path = project.video_path
        name = project.video_original_name
    else:
        raise HTTPException(status_code=404, detail="素材类型不存在")
    path = safe_relative_path(relative_path, settings.data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="任务原始素材已清理")
    return FileResponse(path, filename=name)


@router.post("/jobs/{project_id}/heartbeat")
async def heartbeat(
    project_id: str,
    request: Request,
    settings: Settings = Depends(_authorize_worker),
    db: Session = Depends(get_db),
):
    body = await request.json()
    lease_id = str(body.get("leaseId") or "")
    project = _leased_project(db, project_id, lease_id)
    now = _now()
    project.remote_last_heartbeat_at = now
    project.remote_lease_expires_at = now + timedelta(
        seconds=settings.media_worker_lease_seconds
    )
    metrics = _metrics_json(body.get("metrics"))
    if metrics is not None:
        project.remote_metrics_json = metrics
    db.commit()
    return {
        "ok": True,
        "leaseExpiresAt": project.remote_lease_expires_at.isoformat(),
    }


def _remote_plans(raw_segments: Any) -> list[SegmentPlan]:
    if not isinstance(raw_segments, list):
        raise HTTPException(status_code=400, detail="segments 格式错误")
    plans: list[SegmentPlan] = []
    for position, raw in enumerate(raw_segments, start=1):
        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=400,
                detail=f"第 {position} 段格式错误",
            )
        try:
            plans.append(
                SegmentPlan(
                    index=int(raw.get("index", position)),
                    start_seconds=float(raw["startSeconds"]),
                    end_seconds=float(raw["endSeconds"]),
                    script_text=str(raw["scriptText"]).strip(),
                    alignment_method=str(
                        raw.get("alignmentMethod") or "asr_timestamp"
                    ),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"第 {position} 段格式错误",
            ) from exc
    return plans


@router.post("/jobs/{project_id}/analysis")
async def complete_analysis(
    project_id: str,
    request: Request,
    settings: Settings = Depends(_authorize_worker),
    db: Session = Depends(get_db),
):
    body = await request.json()
    lease_id = str(body.get("leaseId") or "")
    project = _leased_project(
        db,
        project_id,
        lease_id,
        expected_status=LongAudioProjectStatus.ANALYZING.value,
    )
    try:
        apply_alignment_plans(
            project,
            settings,
            _remote_plans(body.get("segments")),
            provider=str(body.get("provider") or "funasr_http")[:50],
        )
        metrics = _metrics_json(body.get("metrics"))
        if metrics is not None:
            project.remote_metrics_json = metrics
        worker_id = project.remote_worker_id
        _clear_lease(project)
        db.commit()
    except LongAudioError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        logger,
        "media.remote_analysis_completed",
        "远程媒体节点已完成分段分析",
        project_id=project.id,
        worker_id=worker_id,
    )
    return {"ok": True, "status": project.status}


def _extract_segment_archive(
    archive: UploadFile,
    target: Path,
    *,
    segment_count: int,
    maximum_bytes: int,
    include_video: bool = True,
) -> None:
    archive.file.seek(0, 2)
    compressed_size = archive.file.tell()
    archive.file.seek(0)
    if compressed_size <= 0:
        raise HTTPException(status_code=400, detail="切割结果压缩包为空")
    if compressed_size > maximum_bytes:
        raise HTTPException(status_code=413, detail="切割结果压缩包超过上限")

    kinds = [("audio", "mp3")]
    if include_video:
        kinds.append(("video", "mp4"))
    expected = {
        f"{kind}/segment-{index:03d}.{extension}"
        for index in range(1, segment_count + 1)
        for kind, extension in kinds
    }
    try:
        with zipfile.ZipFile(archive.file) as bundle:
            files = [info for info in bundle.infolist() if not info.is_dir()]
            names = [info.filename.replace("\\", "/") for info in files]
            if len(names) != len(set(names)) or set(names) != expected:
                raise HTTPException(
                    status_code=400,
                    detail="切割结果文件清单与分段方案不一致",
                )
            total_size = sum(info.file_size for info in files)
            if total_size > maximum_bytes:
                raise HTTPException(
                    status_code=413,
                    detail="切割结果解压后超过上限",
                )
            for info, normalized in zip(files, names):
                if info.flag_bits & 0x1:
                    raise HTTPException(
                        status_code=400,
                        detail="切割结果不能使用加密 ZIP",
                    )
                destination = target / Path(normalized)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="切割结果不是有效 ZIP") from exc


@router.post("/jobs/{project_id}/cut")
def complete_cut(
    project_id: str,
    leaseId: str = Form(...),
    metrics: str = Form(""),
    archive: UploadFile = File(...),
    settings: Settings = Depends(_authorize_worker),
    db: Session = Depends(get_db),
):
    project = _leased_project(
        db,
        project_id,
        leaseId,
        expected_status=LongAudioProjectStatus.CUTTING.value,
    )
    plans = _plan_payload(project)
    worker_id = project.remote_worker_id
    parsed_metrics: Any = None
    if metrics:
        try:
            parsed_metrics = json.loads(metrics)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="metrics 不是有效 JSON",
            ) from exc
        _metrics_json(parsed_metrics)
    project_directory = safe_relative_path(
        project.audio_path, settings.data_dir
    ).parent
    project_directory.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix="remote-cut-",
            dir=project_directory,
        ) as temporary:
            extracted = Path(temporary)
            _extract_segment_archive(
                archive,
                extracted,
                segment_count=len(plans),
                maximum_bytes=settings.media_worker_archive_limit_mb
                * 1024
                * 1024,
                include_video=project.workflow_type == "ltx_lip_sync",
            )
            batch = materialize_long_audio_project(
                db,
                project,
                settings,
                precut_directory=extracted,
            )
        if parsed_metrics is not None:
            project.remote_metrics_json = _metrics_json(parsed_metrics)
        _clear_lease(project)
        db.commit()
    except (LongAudioError, OSError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        logger,
        "media.remote_cut_completed",
        "远程媒体节点已完成切割并创建视频子任务",
        project_id=project.id,
        batch_id=batch.id,
        worker_id=worker_id,
    )
    return {"ok": True, "status": project.status, "batchId": batch.id}


@router.post("/jobs/{project_id}/failed")
async def fail_job(
    project_id: str,
    request: Request,
    settings: Settings = Depends(_authorize_worker),
    db: Session = Depends(get_db),
):
    del settings
    body = await request.json()
    lease_id = str(body.get("leaseId") or "")
    project = _leased_project(db, project_id, lease_id)
    action = (
        "ANALYSIS"
        if project.status == LongAudioProjectStatus.ANALYZING.value
        else "CUT"
    )
    worker_id = project.remote_worker_id
    project.status = LongAudioProjectStatus.FAILED.value
    project.error_code = f"{action}_FAILED"
    project.error_message = str(
        body.get("error") or "远程媒体节点处理失败"
    )[:4000]
    sync_linked_batch_item(project)
    metrics = _metrics_json(body.get("metrics"))
    if metrics is not None:
        project.remote_metrics_json = metrics
    _clear_lease(project)
    db.commit()
    log_event(
        logger,
        "media.remote_failed",
        "远程媒体节点处理失败",
        level=logging.WARNING,
        project_id=project.id,
        worker_id=worker_id,
        action=action.lower(),
        error=project.error_message,
    )
    return {"ok": True, "status": project.status}
