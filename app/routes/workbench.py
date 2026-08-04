from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import GenerationBatch, GenerationBatchItem, TaskStatus, User
from app.routes.dependencies import check_rate_limit
from app.services.batch_manifests import DIGITAL_HUMAN_WORKFLOW
from app.services.batch_status import batch_query
from app.services.postproduction import postproduction_manifest
from app.services.security import verify_password
from app.services.storage import safe_relative_path
from app.services.workbench_auth import (
    HANDOFF_LIFETIME_SECONDS,
    decode_workbench_token,
    issue_workbench_token,
    public_workbench_user,
    token_matches_user,
    workbench_handoffs,
)


router = APIRouter(tags=["workbench"])


def _token_user(token: str, db: Session) -> User:
    settings = get_settings()
    payload = decode_workbench_token(token, settings)
    if payload is None:
        raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
    user = db.get(User, int(payload["user_id"]))
    if user is None or not token_matches_user(payload, user):
        raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
    return user


def _bearer_user(request: Request, db: Session) -> User:
    authorization = request.headers.get("authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    return _token_user(token, db)


def _item_for_user(item_id: str, user: User, db: Session) -> GenerationBatchItem:
    batch = db.scalar(
        batch_query()
        .join(GenerationBatchItem, GenerationBatchItem.batch_id == GenerationBatch.id)
        .where(
            GenerationBatchItem.id == item_id,
            GenerationBatch.user_id == user.id,
            GenerationBatch.workflow_type == DIGITAL_HUMAN_WORKFLOW,
        )
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="数字人任务不存在")
    return next(item for item in batch.items if item.id == item_id)


def _workbench_manifest(item: GenerationBatchItem) -> dict[str, Any]:
    manifest = postproduction_manifest(item, get_settings())
    for video in manifest["source"]["videos"]:
        video["download_url"] = (
            f"/api/workbench/tasks/{item.id}/videos/{video['index']}"
        )
        video.pop("preview_url", None)
    manifest["batch_name"] = item.batch.name
    manifest["created_at"] = item.batch.created_at.isoformat()
    manifest["updated_at"] = item.updated_at.isoformat()
    return manifest


@router.post("/api/auth/center/login")
def workbench_login(
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    check_rate_limit(request, "workbench-login", get_settings().login_rate_limit_per_minute)
    username = str(payload.get("username", "")).strip()
    user = db.scalar(select(User).where(User.username == username))
    if (
        user is None
        or not user.is_active
        or not verify_password(str(payload.get("password", "")), user.password_hash)
    ):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return {
        "ok": True,
        "access_token": issue_workbench_token(user, get_settings()),
        "user": public_workbench_user(user),
    }


@router.post("/api/auth/center/verify")
def workbench_verify(payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    user = _token_user(str(payload.get("access_token", "")), db)
    return {"valid": True, "user": public_workbench_user(user)}


@router.post("/api/auth/center/handoff")
def workbench_create_handoff(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
):
    token = str(payload.get("access_token", ""))
    _token_user(token, db)
    return {
        "handoff_code": workbench_handoffs.issue(token),
        "expires_in": HANDOFF_LIFETIME_SECONDS,
    }


@router.post("/api/auth/center/handoff/consume")
def workbench_consume_handoff(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
):
    token = workbench_handoffs.consume(str(payload.get("handoff_code", "")))
    if not token:
        raise HTTPException(status_code=401, detail="登录接力码无效或已过期")
    user = _token_user(token, db)
    refreshed = issue_workbench_token(user, get_settings())
    return {"access_token": refreshed, "user": public_workbench_user(user)}


@router.post("/api/workbench/tasks")
def workbench_tasks(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
):
    user = _token_user(str(payload.get("access_token", "")), db)
    limit = min(max(int(payload.get("limit", 50) or 50), 1), 100)
    batches = db.scalars(
        batch_query()
        .where(
            GenerationBatch.user_id == user.id,
            GenerationBatch.workflow_type == DIGITAL_HUMAN_WORKFLOW,
        )
        .order_by(GenerationBatch.created_at.desc())
        .limit(limit)
    ).unique().all()
    tasks = [_workbench_manifest(item) for batch in batches for item in batch.items]
    return {"schema": "runninghub.workbench-inbox.v1", "tasks": tasks[:limit]}


@router.post("/api/workbench/tasks/{item_id}")
def workbench_task(
    item_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token", "")), db)
    return _workbench_manifest(_item_for_user(item_id, user, db))


@router.get("/api/workbench/tasks/{item_id}/videos/{video_index}")
def workbench_video(
    item_id: str,
    video_index: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _bearer_user(request, db)
    item = _item_for_user(item_id, user, db)
    tasks = [
        segment.generation_task
        for segment in sorted(item.segments, key=lambda value: value.segment_index)
        if segment.generation_task is not None
    ] if item.segments else ([item.generation_task] if item.generation_task else [])
    if video_index < 1 or video_index > len(tasks):
        raise HTTPException(status_code=404, detail="视频片段不存在")
    task = tasks[video_index - 1]
    if task.status != TaskStatus.SUCCESS.value or not task.result_path:
        raise HTTPException(status_code=409, detail="视频片段尚未生成完成")
    try:
        path = safe_relative_path(task.result_path, get_settings().data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="视频文件不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    filename = Path(task.audio_original_name or f"segment-{video_index}.mp4").stem + ".mp4"
    return FileResponse(path, media_type="video/mp4", filename=filename)
