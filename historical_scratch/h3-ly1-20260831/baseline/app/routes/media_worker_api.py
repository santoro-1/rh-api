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
from app.models import (
    H3RemoteAsrJob,
    H3RemoteAsrJobStatus,
    LongAudioProject,
    LongAudioProjectStatus,
    LtxPreparationJob,
    LtxPreparationStatus,
)
from app.services.h3.postprocess import (
    h3_head_trim_decision_payload,
    parse_h3_head_trim_decision,
)
from app.services.h3.remote_asr import (
    H3_ASR_ACTION_AUDIO_ALIGNMENT,
    H3_ASR_ACTION_HEAD_TRIM,
    normalize_h3_audio_alignment_result,
)
from app.services.deployment_drain import is_deployment_draining
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
        update(H3RemoteAsrJob)
        .where(
            H3RemoteAsrJob.status == H3RemoteAsrJobStatus.RUNNING.value,
            H3RemoteAsrJob.remote_lease_id.is_not(None),
            H3RemoteAsrJob.remote_lease_expires_at.is_not(None),
            H3RemoteAsrJob.remote_lease_expires_at < now,
        )
        .values(
            status=H3RemoteAsrJobStatus.PENDING.value,
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
        preparation_status = (
            LtxPreparationStatus.ASR_RUNNING.value
            if action == "analysis"
            else LtxPreparationStatus.MATERIALIZING.value
        )
        db.execute(
            update(LtxPreparationJob)
            .where(LtxPreparationJob.long_audio_project_id == project_id)
            .values(status=preparation_status, error_code=None, error_message=None)
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


def _claim_next_h3_asr(
    db: Session,
    settings: Settings,
    *,
    worker_id: str,
    capabilities: set[str],
) -> H3RemoteAsrJob | None:
    now = _now()
    _recover_expired_leases(db, now)
    rows = db.scalars(
        select(H3RemoteAsrJob.id)
        .where(
            H3RemoteAsrJob.status == H3RemoteAsrJobStatus.PENDING.value,
            H3RemoteAsrJob.action.in_(capabilities),
        )
        .order_by(H3RemoteAsrJob.created_at, H3RemoteAsrJob.id)
        .limit(10)
    ).all()
    for job_id in rows:
        lease_id = str(uuid.uuid4())
        result = db.execute(
            update(H3RemoteAsrJob)
            .where(
                H3RemoteAsrJob.id == job_id,
                H3RemoteAsrJob.status == H3RemoteAsrJobStatus.PENDING.value,
            )
            .values(
                status=H3RemoteAsrJobStatus.RUNNING.value,
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
        return db.get(H3RemoteAsrJob, job_id)
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


def _h3_asr_payload(job: H3RemoteAsrJob) -> dict[str, Any]:
    lease_id = job.remote_lease_id
    return {
        "jobId": job.id,
        "action": job.action,
        "leaseId": lease_id,
        "leaseExpiresAt": (
            job.remote_lease_expires_at.isoformat()
            if job.remote_lease_expires_at
            else None
        ),
        "workflowType": "minimax_h3_ref2va",
        "scriptText": job.script_text,
        "source": {
            (
                "audioUrl"
                if job.action == H3_ASR_ACTION_AUDIO_ALIGNMENT
                else "videoUrl"
            ): (
                f"/api/media-worker/v1/h3-asr-jobs/{job.id}/source"
                f"?leaseId={lease_id}"
            ),
            (
                "audioName"
                if job.action == H3_ASR_ACTION_AUDIO_ALIGNMENT
                else "videoName"
            ): job.source_name,
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


def _leased_h3_asr_job(
    db: Session,
    job_id: str,
    lease_id: str,
    *,
    expected_status: str | None = None,
) -> H3RemoteAsrJob:
    job = db.get(H3RemoteAsrJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="H3 ASR 任务不存在")
    if not lease_id or not hmac.compare_digest(
        lease_id, job.remote_lease_id or ""
    ):
        raise HTTPException(status_code=409, detail="H3 ASR 任务租约已失效")
    if expected_status and job.status != expected_status:
        raise HTTPException(status_code=409, detail="H3 ASR 任务状态已经变化")
    return job


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
        if str(item).strip().lower()
        in {
            "analysis",
            "cut",
            H3_ASR_ACTION_AUDIO_ALIGNMENT,
            H3_ASR_ACTION_HEAD_TRIM,
        }
    }
    if is_deployment_draining(settings):
        return Response(status_code=204)
    h3_capabilities = capabilities.intersection(
        {H3_ASR_ACTION_AUDIO_ALIGNMENT, H3_ASR_ACTION_HEAD_TRIM}
    )
    if h3_capabilities:
        h3_job = _claim_next_h3_asr(
            db,
            settings,
            worker_id=worker_id,
            capabilities=h3_capabilities,
        )
        if h3_job is not None:
            log_event(
                logger,
                "media.h3_asr_claimed",
                "远程媒体节点已领取 H3 ASR 任务",
                job_id=h3_job.id,
                generation_task_id=h3_job.generation_task_id,
                action=h3_job.action,
                worker_id=worker_id,
            )
            return _h3_asr_payload(h3_job)
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


@router.get("/h3-asr-jobs/{job_id}/source")
def download_h3_asr_source(
    job_id: str,
    leaseId: str,
    settings: Settings = Depends(_authorize_worker),
    db: Session = Depends(get_db),
):
    job = _leased_h3_asr_job(db, job_id, leaseId)
    path = safe_relative_path(job.source_path, settings.data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="H3 ASR 源素材已清理")
    return FileResponse(path, filename=job.source_name)


@router.post("/h3-asr-jobs/{job_id}/heartbeat")
async def heartbeat_h3_asr(
    job_id: str,
    request: Request,
    settings: Settings = Depends(_authorize_worker),
    db: Session = Depends(get_db),
):
    body = await request.json()
    job = _leased_h3_asr_job(
        db,
        job_id,
        str(body.get("leaseId") or ""),
        expected_status=H3RemoteAsrJobStatus.RUNNING.value,
    )
    now = _now()
    job.remote_last_heartbeat_at = now
    job.remote_lease_expires_at = now + timedelta(
        seconds=settings.media_worker_lease_seconds
    )
    metrics = _metrics_json(body.get("metrics"))
    if metrics is not None:
        job.remote_metrics_json = metrics
    db.commit()
    return {"ok": True, "leaseExpiresAt": job.remote_lease_expires_at.isoformat()}


@router.post("/h3-asr-jobs/{job_id}/complete")
async def complete_h3_asr(
    job_id: str,
    request: Request,
    settings: Settings = Depends(_authorize_worker),
    db: Session = Depends(get_db),
):
    del settings
    body = await request.json()
    job = _leased_h3_asr_job(
        db,
        job_id,
        str(body.get("leaseId") or ""),
        expected_status=H3RemoteAsrJobStatus.RUNNING.value,
    )
    try:
        if job.action == H3_ASR_ACTION_HEAD_TRIM:
            decision = parse_h3_head_trim_decision(body.get("decision"))
            result_payload = h3_head_trim_decision_payload(decision)
        elif job.action == H3_ASR_ACTION_AUDIO_ALIGNMENT:
            if not job.audio_batch_id or not job.audio_item_id:
                raise ValueError("H3 音频对齐任务缺少输入绑定")
            result_payload = normalize_h3_audio_alignment_result(
                body.get("alignment"),
                script_text=job.script_text,
                script_sha256=job.script_sha256,
                audio_sha256=job.source_sha256,
                audio_batch_id=job.audio_batch_id,
                audio_item_id=job.audio_item_id,
                audio_generation_version=int(job.audio_generation_version or 0),
            )
            decision = None
        else:
            raise ValueError("H3 ASR 任务类型不受支持")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job.result_json = json.dumps(
        result_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    job.status = H3RemoteAsrJobStatus.SUCCESS.value
    job.error_code = None
    job.error_message = None
    job.completed_at = _now()
    metrics = _metrics_json(body.get("metrics"))
    if metrics is not None:
        job.remote_metrics_json = metrics
    worker_id = job.remote_worker_id
    job.remote_lease_id = None
    job.remote_lease_expires_at = None
    db.commit()
    log_event(
        logger,
        "media.h3_asr_completed",
        "远程媒体节点已完成 H3 ASR 任务",
        job_id=job.id,
        generation_task_id=job.generation_task_id,
        action=job.action,
        worker_id=worker_id,
        trim_mode=decision.mode if decision is not None else None,
        trim_seconds=decision.trim_seconds if decision is not None else None,
    )
    return {"ok": True, "status": job.status}


@router.post("/h3-asr-jobs/{job_id}/failed")
async def fail_h3_asr(
    job_id: str,
    request: Request,
    settings: Settings = Depends(_authorize_worker),
    db: Session = Depends(get_db),
):
    del settings
    body = await request.json()
    job = _leased_h3_asr_job(
        db,
        job_id,
        str(body.get("leaseId") or ""),
        expected_status=H3RemoteAsrJobStatus.RUNNING.value,
    )
    job.status = H3RemoteAsrJobStatus.FAILED.value
    job.error_code = "REMOTE_ASR_FAILED"
    job.error_message = str(body.get("error") or "远程 ASR 处理失败")[:4000]
    job.completed_at = _now()
    metrics = _metrics_json(body.get("metrics"))
    if metrics is not None:
        job.remote_metrics_json = metrics
    worker_id = job.remote_worker_id
    job.remote_lease_id = None
    job.remote_lease_expires_at = None
    db.commit()
    log_event(
        logger,
        "media.h3_asr_failed",
        "远程媒体节点 H3 ASR 任务失败",
        level=logging.WARNING,
        job_id=job.id,
        generation_task_id=job.generation_task_id,
        action=job.action,
        worker_id=worker_id,
        error=job.error_message,
    )
    return {"ok": True, "status": job.status}


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


def _alignment_timeline(
    project: LongAudioProject,
    raw_alignment: Any,
    plans: list[SegmentPlan],
    provider: str,
) -> tuple[str, float]:
    if not isinstance(raw_alignment, dict):
        raise HTTPException(status_code=400, detail="alignment 格式错误")
    raw_tokens = raw_alignment.get("tokens")
    if not isinstance(raw_tokens, list) or not raw_tokens:
        raise HTTPException(status_code=400, detail="alignment 缺少原稿时间轴")
    try:
        match_ratio = float(raw_alignment["matchRatio"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="alignment 匹配率格式错误") from exc
    if not 0 <= match_ratio <= 1:
        raise HTTPException(status_code=400, detail="alignment 匹配率超出范围")
    tokens: list[dict[str, Any]] = []
    previous_script_end = -1
    previous_start = -0.1
    for position, raw in enumerate(raw_tokens, start=1):
        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=400, detail=f"alignment 第 {position} 个 token 格式错误"
            )
        try:
            text = str(raw["text"])
            script_start = int(raw["scriptStart"])
            script_end = int(raw["scriptEnd"])
            start_seconds = float(raw["startSeconds"])
            end_seconds = float(raw["endSeconds"])
            confidence = (
                float(raw["confidence"])
                if raw.get("confidence") is not None
                else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"alignment 第 {position} 个 token 格式错误"
            ) from exc
        if (
            not text
            or script_start < 0
            or script_end <= script_start
            or script_end > len(project.script_text)
            or script_start < previous_script_end
            or start_seconds < previous_start
            or end_seconds <= start_seconds
            or end_seconds > project.duration_seconds + 1.0
        ):
            raise HTTPException(
                status_code=400, detail=f"alignment 第 {position} 个 token 时间轴无效"
            )
        if project.script_text[script_start:script_end] != text:
            raise HTTPException(
                status_code=400, detail=f"alignment 第 {position} 个 token 未绑定原稿"
            )
        tokens.append(
            {
                "text": text,
                "script_start": script_start,
                "script_end": script_end,
                "start_ms": round(start_seconds * 1000),
                "end_ms": round(end_seconds * 1000),
                "confidence": confidence,
            }
        )
        previous_script_end = script_end
        previous_start = start_seconds
    segment_rows: list[dict[str, Any]] = []
    script_cursor = 0
    for plan in plans:
        script_start = project.script_text.find(plan.script_text, script_cursor)
        if script_start < 0:
            raise HTTPException(
                status_code=400,
                detail=f"第 {plan.index} 段原稿无法映射回完整原稿",
            )
        script_end = script_start + len(plan.script_text)
        segment_rows.append(
            {
                "index": plan.index,
                "start_ms": round(plan.start_seconds * 1000),
                "end_ms": round(plan.end_seconds * 1000),
                "script_start": script_start,
                "script_end": script_end,
                "script_text": plan.script_text,
            }
        )
        script_cursor = script_end
    preparation = project.ltx_preparation_job
    timeline = {
        "schema": "ltx.aligned-script.v1",
        "script_sha256": (
            preparation.script_sha256 if preparation is not None else None
        ),
        "audio_sha256": (
            preparation.source_audio_sha256 if preparation is not None else None
        ),
        "provider": provider,
        "match_ratio": match_ratio,
        "tokens": tokens,
        "segments": segment_rows,
    }
    encoded = json.dumps(timeline, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 4 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="alignment 原稿时间轴过大")
    return encoded, match_ratio


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
        plans = _remote_plans(body.get("segments"))
        provider = str(body.get("provider") or "funasr_http")[:50]
        apply_alignment_plans(
            project,
            settings,
            plans,
            provider=provider,
        )
        preparation = project.ltx_preparation_job
        if preparation is not None:
            timeline_json, match_ratio = _alignment_timeline(
                project,
                body.get("alignment"),
                plans,
                provider,
            )
            preparation.alignment_provider = provider
            preparation.alignment_score = match_ratio
            preparation.alignment_timeline_json = timeline_json
            preparation.segment_plan_json = project.plan_json
            preparation.status = LtxPreparationStatus.READY_TO_MATERIALIZE.value
            preparation.error_code = None
            preparation.error_message = None
            full_source_plan = (
                len(plans) == 1
                and plans[0].start_seconds <= 0.05
                and abs(plans[0].end_seconds - project.duration_seconds) <= 0.75
            )
            if full_source_plan:
                materialize_long_audio_project(
                    db,
                    project,
                    settings,
                    preserve_full_source=True,
                )
                preparation.status = LtxPreparationStatus.COMPLETED.value
                preparation.completed_at = _now()
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
        preparation = project.ltx_preparation_job
        if preparation is not None:
            preparation.status = LtxPreparationStatus.COMPLETED.value
            preparation.segment_plan_json = project.plan_json
            preparation.error_code = None
            preparation.error_message = None
            preparation.completed_at = _now()
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
    preparation = project.ltx_preparation_job
    if preparation is not None:
        preparation.status = LtxPreparationStatus.FAILED.value
        preparation.error_code = project.error_code
        preparation.error_message = project.error_message
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
