from __future__ import annotations

import json
import mimetypes
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import get_settings
from app.database import get_db
from app.models import (
    AudioTaskStatus,
    GenerationBatch,
    GenerationSegment,
    LongAudioProjectStatus,
    MiniMaxVoiceAsset,
    User,
    VoiceAssetStatus,
)
from app.routes.dependencies import (
    check_rate_limit,
    get_current_user,
    get_page_user,
)
from app.services.batch_assets import StagedAssetError, stage_asset
from app.services.audio_review import (
    AudioReviewError,
    approve_all_audio as approve_all_review_audio,
    approve_item_audio,
    regenerate_item_audio,
)
from app.services.batch_generation import (
    BatchValidationError,
    create_batch,
    validate_batch,
)
from app.services.batch_lifecycle import (
    BatchFileCleanupError,
    BatchLifecycleError,
    batch_is_deletable,
    cancel_pending_media_projects,
    delete_terminal_batch,
    retry_failed_batch,
    video_tasks,
)
from app.services.batch_manifests import (
    DIGITAL_HUMAN_WORKFLOW,
    LTX_LIP_SYNC_WORKFLOW,
    ManifestError,
    csv_template,
    parse_manifest,
)
from app.services.batch_status import (
    ACTIVE_VIDEO_STATUSES,
    STATUS_LABELS,
    batch_detail_status,
    batch_query,
    batch_view_revision,
    item_display_status,
    summarize_batch,
)
from app.services.csrf import require_csrf
from app.services.speech.system_voices import group_system_voice_assets
from app.services.storage import (
    UploadValidationError,
    remove_directory,
    safe_relative_path,
    save_upload,
    task_upload_dir,
    to_relative_data_path,
)
from app.services.task_management import RETRYABLE_TASK_STATUSES
from app.services.task_management import (
    TaskManagementError,
    prepare_successful_segment_regeneration,
)
from app.services.postproduction import (
    postproduction_manifest,
    postproduction_mode,
    postproduction_status,
)
from app.services.video_merge import (
    AWAITING_VIDEO_REVIEW,
    MERGE_FAILED,
    MERGED_VIDEO_READY,
    MERGED_PREVIEW_READY,
    VideoMergeError,
    approve_merged_video,
    invalidate_merged_video,
    retry_video_merge,
)
from app.services.task_cancellation import (
    TaskCancellationError,
    cancel_generation_task,
)
from app.services.runninghub import RunningHubError
from app.services.workflow_configs import get_user_workflow_config
from app.web import templates


router = APIRouter(tags=["batches"])


def _ensure_batch_access(batch: GenerationBatch, user: User) -> None:
    if batch.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=404, detail="批次不存在")


def _batch_item(batch: GenerationBatch, item_id: str):
    item = next((candidate for candidate in batch.items if candidate.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="批次任务不存在")
    return item


def _archive_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE).strip("._")
    return cleaned[:80] or fallback


def _batch_video_files(batch: GenerationBatch) -> list[tuple[Path, str]]:
    settings = get_settings()
    videos: list[tuple[Path, str]] = []
    for item in batch.items:
        candidates = (
            [(item.generation_task, None)]
            if item.generation_task
            else [
                (segment.generation_task, segment.segment_index)
                for segment in item.segments
                if segment.generation_task
            ]
        )
        for task, segment_index in candidates:
            if (
                task.status != "SUCCESS"
                or not task.result_path
            ):
                continue
            try:
                path = safe_relative_path(task.result_path, settings.data_dir)
            except ValueError:
                continue
            if not path.is_file():
                continue
            row_key = _archive_name(item.row_key, f"row-{item.row_number:03d}")
            segment_suffix = (
                f"-segment-{segment_index:03d}"
                if segment_index is not None
                else ""
            )
            videos.append(
                (
                    path,
                    (
                        f"{item.row_number:03d}-{row_key}"
                        f"{segment_suffix}-{task.id[:8]}{path.suffix or '.mp4'}"
                    ),
                )
            )
    return videos


@router.get("/generate/batch")
def batch_generate_page(
    request: Request,
    workflow: str = DIGITAL_HUMAN_WORKFLOW,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if workflow not in {DIGITAL_HUMAN_WORKFLOW, LTX_LIP_SYNC_WORKFLOW}:
        workflow = DIGITAL_HUMAN_WORKFLOW
    digital_config = get_user_workflow_config(current_user, DIGITAL_HUMAN_WORKFLOW)
    ltx_config = get_user_workflow_config(current_user, LTX_LIP_SYNC_WORKFLOW)
    active_voices = db.scalars(
        select(MiniMaxVoiceAsset)
        .where(
            MiniMaxVoiceAsset.user_id == current_user.id,
            MiniMaxVoiceAsset.is_saved.is_(True),
            MiniMaxVoiceAsset.status.in_(
                {
                    VoiceAssetStatus.READY.value,
                    VoiceAssetStatus.ACTIVE.value,
                }
            ),
            MiniMaxVoiceAsset.account_binding_id
            == (
                current_user.minimax_config.account_binding_id
                if current_user.minimax_config
                else ""
            ),
        )
        .order_by(MiniMaxVoiceAsset.name, MiniMaxVoiceAsset.created_at)
    ).all()
    custom_voices = [
        voice for voice in active_voices if voice.method != "system"
    ]
    system_voice_groups = group_system_voice_assets(
        voice for voice in active_voices if voice.method == "system"
    )
    return templates.TemplateResponse(
        request,
        "batch_generate.html",
        {
            "current_user": current_user,
            "initial_workflow": workflow,
            "digital_config": digital_config,
            "ltx_config": ltx_config,
            "max_batch_items": get_settings().max_batch_items,
            "minimax_configured": bool(
                current_user.minimax_config
                and current_user.minimax_config.api_key_encrypted
                and current_user.minimax_config.account_binding_id
            ),
            "minimax_voices": active_voices,
            "minimax_custom_voices": custom_voices,
            "minimax_system_voice_groups": system_voice_groups,
            "minimax_system_voice_count": sum(
                len(group["voices"]) for group in system_voice_groups
            ),
        },
    )


@router.post("/api/batch-manifests/parse")
def parse_batch_manifest(
    manifest: UploadFile = File(...),
    workflowType: str = Form(...),
    audioMode: str = Form("upload"),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
):
    del csrf_ok, current_user
    content = manifest.file.read(5 * 1024 * 1024 + 1)
    manifest.file.seek(0)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="任务清单不能超过 5 MB")
    try:
        parsed = parse_manifest(
            manifest.filename or "",
            content,
            workflowType,
            audioMode,
        )
    except ManifestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "workflowType": parsed.workflow_type,
        "sourceFormat": parsed.source_format,
        "rows": parsed.rows,
    }


@router.post("/api/batch-assets")
def upload_batch_asset(
    request: Request,
    file: UploadFile = File(...),
    kind: str = Form(...),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    settings = get_settings()
    check_rate_limit(
        request,
        f"batch-asset:{current_user.id}",
        max(settings.task_create_rate_limit_per_minute * 20, 100),
    )
    try:
        asset = stage_asset(db, current_user, file, kind, settings)
    except (UploadValidationError, StagedAssetError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "assetId": asset.id,
            "kind": asset.kind,
            "originalName": asset.original_name,
            "sizeBytes": asset.size_bytes,
            "expiresAt": asset.expires_at.isoformat(),
        },
        status_code=201,
    )


def _body_parts(
    body: dict[str, Any],
) -> tuple[
    str,
    str,
    list[dict[str, str]],
    list[str],
    dict[str, str],
    dict[str, Any],
    bool,
    bool,
]:
    audio_mode = str(body.get("audioMode") or "upload")
    workflow_type = str(body.get("workflowType") or "")
    raw_rows = body.get("rows")
    raw_asset_ids = body.get("assetIds")
    raw_batch_parameters = body.get("batchParameters") or {}
    raw_speech_options = body.get("speechOptions") or {}
    review_required = body.get("longAudioReviewRequired") is True
    video_review_required = body.get("videoReviewRequired") is True
    if not isinstance(raw_rows, list) or not all(
        isinstance(row, dict) for row in raw_rows
    ):
        raise HTTPException(status_code=400, detail="批量任务行格式不合法")
    if not isinstance(raw_asset_ids, list):
        raise HTTPException(status_code=400, detail="批量素材列表格式不合法")
    if not isinstance(raw_batch_parameters, dict):
        raise HTTPException(status_code=400, detail="批次统一参数格式不合法")
    if not isinstance(raw_speech_options, dict):
        raise HTTPException(status_code=400, detail="语音生成参数格式不合法")
    rows = [
        {str(key): str(value or "") for key, value in row.items()}
        for row in raw_rows
    ]
    asset_ids = [str(value) for value in raw_asset_ids]
    batch_parameters = {
        str(key): str(value or "")
        for key, value in raw_batch_parameters.items()
    }
    return (
        workflow_type,
        audio_mode,
        rows,
        asset_ids,
        batch_parameters,
        raw_speech_options,
        review_required,
        video_review_required,
    )


@router.post("/api/batches/validate")
async def validate_batch_request(
    request: Request,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求内容不是有效对象")
    (
        workflow_type,
        audio_mode,
        rows,
        asset_ids,
        batch_parameters,
        speech_options,
        review_required,
        video_review_required,
    ) = _body_parts(body)
    try:
        plan = validate_batch(
            db,
            current_user,
            get_settings(),
            workflow_type=workflow_type,
            rows=rows,
            asset_ids=asset_ids,
            batch_parameters=batch_parameters,
            audio_mode=audio_mode,
            speech_options=speech_options,
            review_required=review_required,
            video_review_required=video_review_required,
        )
    except BatchValidationError as exc:
        return JSONResponse(
            {"valid": False, "errors": exc.errors},
            status_code=400,
        )
    return {"valid": True, "rowCount": len(plan.rows), "errors": []}


@router.post("/api/batches")
async def create_batch_request(
    request: Request,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    settings = get_settings()
    check_rate_limit(
        request,
        f"batch-create:{current_user.id}",
        settings.task_create_rate_limit_per_minute,
    )
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求内容不是有效对象")
    (
        workflow_type,
        audio_mode,
        rows,
        asset_ids,
        batch_parameters,
        speech_options,
        review_required,
        video_review_required,
    ) = _body_parts(body)
    request_key = str(body.get("requestKey") or "").strip()
    existing = db.scalar(
        select(GenerationBatch).where(
            GenerationBatch.user_id == current_user.id,
            GenerationBatch.request_key == request_key,
        )
    )
    if existing is not None:
        return JSONResponse(
            {"batchId": existing.id, "created": False},
            status_code=200,
        )

    created_directories: list[Path] = []
    try:
        plan = validate_batch(
            db,
            current_user,
            settings,
            workflow_type=workflow_type,
            rows=rows,
            asset_ids=asset_ids,
            batch_parameters=batch_parameters,
            audio_mode=audio_mode,
            speech_options=speech_options,
            review_required=review_required,
            video_review_required=video_review_required,
        )
        batch, created_directories = create_batch(
            db,
            current_user,
            settings,
            name=str(body.get("name") or ""),
            request_key=request_key,
            plan=plan,
        )
        db.commit()
    except BatchValidationError as exc:
        db.rollback()
        for directory in created_directories:
            remove_directory(directory)
        return JSONResponse(
            {"detail": str(exc), "errors": exc.errors},
            status_code=400,
        )
    except (OSError, ValueError) as exc:
        db.rollback()
        for directory in created_directories:
            remove_directory(directory)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {"batchId": batch.id, "created": True, "taskCount": batch.total_items},
        status_code=201,
    )


@router.get("/api/batches/{batch_id}")
def batch_status(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    return {
        **summarize_batch(batch),
        "viewRevision": batch_view_revision(batch),
        "items": batch_detail_status(batch),
    }


@router.get("/api/batches/{batch_id}/items/{item_id}/postproduction")
def batch_item_postproduction_manifest(
    batch_id: str,
    item_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Expose the exact editor hand-off without starting a paid task."""

    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    return postproduction_manifest(_batch_item(batch, item_id), get_settings())


@router.get("/batches/{batch_id}/items/{item_id}/postproduction")
def batch_item_postproduction_page(
    batch_id: str,
    item_id: str,
    request: Request,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    """Human-readable editor hand-off page; the API route remains machine-readable."""

    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    item = _batch_item(batch, item_id)
    manifest = postproduction_manifest(item, get_settings())
    return templates.TemplateResponse(
        request,
        "postproduction_detail.html",
        {
            "current_user": current_user,
            "batch": batch,
            "item": item,
            "manifest": manifest,
            "current_status": item_display_status(item),
            "current_status_label": STATUS_LABELS.get(
                item_display_status(item), item_display_status(item)
            ),
        },
    )


@router.get("/api/batch-templates/{workflow_type}.csv")
def download_csv_template(
    workflow_type: str,
    current_user: User = Depends(get_current_user),
):
    del current_user
    try:
        content = csv_template(workflow_type)
    except ManifestError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{workflow_type}-batch-template.csv"'
            )
        },
    )


@router.get("/api/batch-templates/{workflow_type}.xlsx")
def download_xlsx_template(
    workflow_type: str,
    current_user: User = Depends(get_current_user),
):
    del current_user
    if workflow_type not in {
        DIGITAL_HUMAN_WORKFLOW,
        LTX_LIP_SYNC_WORKFLOW,
        "script",
    }:
        raise HTTPException(status_code=404, detail="模板不存在")
    path = (
        Path(__file__).resolve().parents[1]
        / "static"
        / "templates"
        / f"{workflow_type}-batch-template.xlsx"
    )
    if not path.is_file():
        raise HTTPException(status_code=404, detail="模板尚未生成")
    return FileResponse(
        path,
        filename=f"{workflow_type}-batch-template.xlsx",
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )


@router.get("/batches")
def batches_page(
    request: Request,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    statement = batch_query().order_by(GenerationBatch.created_at.desc())
    if not current_user.is_admin:
        statement = statement.where(GenerationBatch.user_id == current_user.id)
    batches = db.scalars(statement).unique().all()
    summaries = {batch.id: summarize_batch(batch) for batch in batches}
    deletable_batches = {
        batch.id: batch_is_deletable(batch) for batch in batches
    }
    return templates.TemplateResponse(
        request,
        "batches.html",
        {
            "current_user": current_user,
            "batches": batches,
            "summaries": summaries,
            "deletable_batches": deletable_batches,
        },
    )


@router.get("/batches/{batch_id}")
def batch_detail_page(
    batch_id: str,
    request: Request,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    manifests = {
        item.id: json.loads(item.manifest_json) for item in batch.items
    }
    return templates.TemplateResponse(
        request,
        "batch_detail.html",
        {
            "current_user": current_user,
            "batch": batch,
            "summary": summarize_batch(batch),
            "view_revision": batch_view_revision(batch),
            "manifests": manifests,
            "item_statuses": {
                item.id: item_display_status(item) for item in batch.items
            },
            "postproduction_modes": {
                item.id: postproduction_mode(item) for item in batch.items
            },
            "postproduction_statuses": {
                item.id: postproduction_status(item) for item in batch.items
            },
            "active_statuses": ACTIVE_VIDEO_STATUSES,
            "retryable_statuses": RETRYABLE_TASK_STATUSES,
            "status_labels": STATUS_LABELS,
            "auto_retry_limit": get_settings().runninghub_auto_retry_limit,
            "can_delete_batch": batch_is_deletable(batch),
            "can_cancel_batch": any(
                task.status in ACTIVE_VIDEO_STATUSES
                for task in video_tasks(batch)
            )
            or any(
                item.long_audio_project is not None
                and item.long_audio_project.status
                in {
                    LongAudioProjectStatus.PENDING_ANALYSIS.value,
                    LongAudioProjectStatus.REVIEW.value,
                    LongAudioProjectStatus.PENDING_CUT.value,
                }
                for item in batch.items
            ),
        },
    )


@router.get("/batches/{batch_id}/download")
def download_batch_videos(
    batch_id: str,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    videos = _batch_video_files(batch)
    if not videos:
        raise HTTPException(status_code=404, detail="当前批次还没有可下载的视频")

    archive_dir = get_settings().data_dir / "runtime" / "batch-downloads"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{batch.id}-{uuid.uuid4().hex}.zip"
    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            for path, archive_name in videos:
                archive.write(path, arcname=archive_name)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"batch-{batch.id}-videos.zip",
        content_disposition_type="attachment",
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@router.get("/batches/{batch_id}/segments/{segment_id}/audio")
def download_segment_audio(
    batch_id: str,
    segment_id: str,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    segment = next(
        (
            candidate
            for item in batch.items
            for candidate in item.segments
            if candidate.id == segment_id
        ),
        None,
    )
    if segment is None:
        raise HTTPException(status_code=404, detail="分段任务不存在")
    path = safe_relative_path(segment.audio_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="分段音频文件不存在")
    return FileResponse(
        path,
        filename=f"{segment.batch_item.row_key}-{segment.segment_index:03d}.mp3",
        media_type="audio/mpeg",
    )


def _merged_video_response(
    batch: GenerationBatch,
    item_id: str,
    *,
    download: bool,
) -> FileResponse:
    item = _batch_item(batch, item_id)
    if (
        item.merged_video_status
        not in {
            AWAITING_VIDEO_REVIEW,
            MERGED_VIDEO_READY,
            MERGED_PREVIEW_READY,
        }
        or not item.merged_video_path
    ):
        raise HTTPException(status_code=404, detail="完整视频尚不可用")
    try:
        path = safe_relative_path(item.merged_video_path, get_settings().data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="完整视频不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="完整视频不存在")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=(
            f"{_archive_name(item.row_key, item.id)}-complete.mp4"
            if download
            else None
        ),
        content_disposition_type="attachment" if download else "inline",
    )


@router.get("/batches/{batch_id}/items/{item_id}/merged-video")
def preview_merged_video(
    batch_id: str,
    item_id: str,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    return _merged_video_response(batch, item_id, download=False)


@router.get("/batches/{batch_id}/items/{item_id}/merged-video/download")
def download_merged_video(
    batch_id: str,
    item_id: str,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    return _merged_video_response(batch, item_id, download=True)


@router.post("/batches/{batch_id}/items/{item_id}/approve-video")
def approve_complete_video(
    batch_id: str,
    item_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    try:
        approve_merged_video(_batch_item(batch, item_id))
    except VideoMergeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.post("/batches/{batch_id}/items/{item_id}/retry-video-merge")
def retry_complete_video_merge(
    batch_id: str,
    item_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    try:
        retry_video_merge(_batch_item(batch, item_id), get_settings())
    except (OSError, VideoMergeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.post("/batches/{batch_id}/segments/{segment_id}/regenerate")
def regenerate_successful_segment(
    batch_id: str,
    segment_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    segment = next(
        (
            candidate
            for item in batch.items
            for candidate in item.segments
            if candidate.id == segment_id
        ),
        None,
    )
    if segment is None or segment.generation_task is None:
        raise HTTPException(status_code=404, detail="分段任务不存在")
    try:
        prepare_successful_segment_regeneration(
            segment.generation_task,
            get_settings(),
        )
        invalidate_merged_video(segment.batch_item, get_settings())
    except (OSError, TaskManagementError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.get("/batches/{batch_id}/items/{item_id}/audio")
def download_full_audio(
    batch_id: str,
    item_id: str,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    item = next(
        (candidate for candidate in batch.items if candidate.id == item_id),
        None,
    )
    if item is None or item.audio_task is None or not item.audio_task.output_path:
        raise HTTPException(status_code=404, detail="完整语音尚未生成")
    path = safe_relative_path(
        item.audio_task.output_path,
        get_settings().data_dir,
    )
    if not path.is_file():
        raise HTTPException(status_code=404, detail="完整语音文件不存在")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
    )


@router.get(
    "/batches/{batch_id}/items/{item_id}/audio-attempts/{attempt_id}"
)
def download_audio_attempt(
    batch_id: str,
    item_id: str,
    attempt_id: str,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    item = next(
        (candidate for candidate in batch.items if candidate.id == item_id),
        None,
    )
    attempt = (
        next(
            (
                candidate
                for candidate in item.audio_task.attempts
                if candidate.id == attempt_id
            ),
            None,
        )
        if item is not None and item.audio_task is not None
        else None
    )
    if attempt is None:
        raise HTTPException(status_code=404, detail="语音版本不存在")
    path = safe_relative_path(attempt.output_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="语音版本文件不存在")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@router.post("/batches/{batch_id}/items/{item_id}/approve-audio")
def approve_audio(
    batch_id: str,
    item_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    try:
        approve_item_audio(batch, item_id)
    except AudioReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.post("/batches/{batch_id}/approve-audio")
def approve_all_audio(
    batch_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    try:
        approved = approve_all_review_audio(batch)
    except AudioReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(
        f"/batches/{batch.id}?approved={approved}",
        status_code=303,
    )


@router.post("/batches/{batch_id}/items/{item_id}/regenerate-audio")
def regenerate_audio(
    batch_id: str,
    item_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    try:
        regenerate_item_audio(batch, item_id)
    except AudioReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.post("/batches/{batch_id}/items/{item_id}/replace-video")
def replace_full_flow_video(
    batch_id: str,
    item_id: str,
    source_video: UploadFile = File(...),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    item = next((candidate for candidate in batch.items if candidate.id == item_id), None)
    if (
        item is None
        or batch.workflow_type != LTX_LIP_SYNC_WORKFLOW
        or batch.audio_mode != "minimax"
        or item.audio_task is None
        or item.audio_task.status != AudioTaskStatus.FAILED.value
        or not item.audio_task.output_path
        or item.segments
    ):
        raise HTTPException(
            status_code=409,
            detail="当前任务不能在保留音频的情况下更换源视频",
        )
    settings = get_settings()
    directory = task_upload_dir(
        settings,
        current_user.id,
        item.audio_task.planned_generation_task_id,
    )
    try:
        path, original_name = save_upload(
            source_video,
            directory,
            "video",
            settings,
        )
    except (UploadValidationError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    item.audio_task.primary_path = to_relative_data_path(path, settings)
    item.audio_task.primary_original_name = original_name
    item.audio_task.status = AudioTaskStatus.PENDING.value
    item.audio_task.error_code = None
    item.audio_task.error_message = None
    item.audio_task.completed_at = None
    item.audio_status = "AUDIO_READY"
    item.status = "VIDEO_REPLACED"
    db.commit()
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.post("/batches/{batch_id}/retry")
def retry_batch(
    batch_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    result = retry_failed_batch(batch, get_settings())
    db.commit()
    return RedirectResponse(
        (
            f"/batches/{batch.id}?retried={result.retried}"
            f"&skipped={result.skipped}"
        ),
        status_code=303,
    )


@router.post("/batches/{batch_id}/cancel")
def cancel_batch(
    batch_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    cancelled = 0
    failed = 0
    cancelled += cancel_pending_media_projects(batch)
    db.commit()
    for task in video_tasks(batch):
        if task.status not in ACTIVE_VIDEO_STATUSES:
            continue
        try:
            cancel_generation_task(db, task)
            db.commit()
            cancelled += 1
        except (TaskCancellationError, RunningHubError):
            db.rollback()
            failed += 1
    for item in batch.items:
        child_tasks = [
            segment.generation_task
            for segment in item.segments
            if segment.generation_task is not None
        ]
        if child_tasks and all(task.status == "CANCELLED" for task in child_tasks):
            item.merged_video_status = "NOT_APPLICABLE"
            item.merged_video_error = None
    db.commit()
    return RedirectResponse(
        f"/batches/{batch.id}?cancelled={cancelled}&cancelFailed={failed}",
        status_code=303,
    )


@router.post("/batches/{batch_id}/delete")
def delete_batch(
    batch_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    _ensure_batch_access(batch, current_user)
    try:
        delete_terminal_batch(db, batch, get_settings())
    except BatchLifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BatchFileCleanupError as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc
    return RedirectResponse("/batches", status_code=303)
