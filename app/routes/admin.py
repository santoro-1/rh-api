from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.models import MiniMaxVoiceAsset, RunningHubConfig, User, VoiceAssetStatus
from app.routes.dependencies import get_page_admin
from app.services.csrf import require_csrf
from app.services.security import encrypt_secret, hash_password, mask_secret
from app.services.speech.accounts import save_minimax_config
from app.services.workflow_configs import (
    get_user_workflow_config,
    save_workflow_config,
)
from app.web import templates
from app.workflows import get_workflow


router = APIRouter(prefix="/admin", tags=["admin"])
LTX_DEFAULT_PROMPT = get_workflow("ltx_lip_sync").default_prompt


def _validate_config(base_url: str, ai_app_id: str, instance_type: str, max_tasks: int) -> None:
    if not base_url.startswith(("https://", "http://")):
        raise ValueError("RunningHub Base URL 必须以 http:// 或 https:// 开头")
    if not ai_app_id.strip():
        raise ValueError("AI App ID 不能为空")
    if instance_type not in {"default", "plus"}:
        raise ValueError("实例类型只能为 default 或 plus")
    if not 1 <= max_tasks <= 20:
        raise ValueError("最大并发任务数必须在 1 到 20 之间")


def _save_config(
    db: Session,
    user: User,
    api_key: str,
    base_url: str,
    ai_app_id: str,
    instance_type: str,
    default_prompt: str,
    max_concurrent_tasks: int,
    ltx_workflow_id: str,
    ltx_instance_type: str,
    ltx_default_prompt: str,
    ltx_enabled: bool,
    ltx_access_password: str,
    ltx_clear_access_password: bool,
    minimax_api_key: str,
    minimax_base_url: str,
    minimax_requests_per_minute: int,
    minimax_account_label: str,
    minimax_new_account: bool,
) -> None:
    _validate_config(base_url, ai_app_id, instance_type, max_concurrent_tasks)
    config = user.runninghub_config
    if config is None:
        config = RunningHubConfig(
            user=user,
            api_key_encrypted=None,
            base_url=base_url.rstrip("/"),
            ai_app_id=ai_app_id.strip(),
            instance_type=instance_type,
            default_prompt=default_prompt.strip() or "人物自然地说话，表情自然，动作自然，镜头保持稳定。",
            max_concurrent_tasks=max_concurrent_tasks,
        )
    else:
        config.base_url = base_url.rstrip("/")
        config.ai_app_id = ai_app_id.strip()
        config.instance_type = instance_type
        config.default_prompt = (
            default_prompt.strip() or "人物自然地说话，表情自然，动作自然，镜头保持稳定。"
        )
        config.max_concurrent_tasks = max_concurrent_tasks
    if api_key.strip():
        config.api_key_encrypted = encrypt_secret(api_key.strip())
    db.add(config)
    workflow_config = save_workflow_config(
        user,
        "digital_human",
        ai_app_id=config.ai_app_id,
        instance_type=config.instance_type,
        default_prompt=config.default_prompt,
    )
    db.add(workflow_config)
    ltx_workflow_id = ltx_workflow_id.strip()
    if ltx_enabled and not ltx_workflow_id:
        raise ValueError("启用视频对口型工作流时，Workflow ID 不能为空")
    if len(ltx_access_password) > 500:
        raise ValueError("视频对口型工作流访问密码不能超过 500 个字符")
    existing_ltx_config = get_user_workflow_config(user, "ltx_lip_sync")
    ltx_settings = dict(existing_ltx_config.settings)
    if ltx_clear_access_password:
        ltx_settings.pop("access_password_encrypted", None)
    elif ltx_access_password:
        ltx_settings["access_password_encrypted"] = encrypt_secret(
            ltx_access_password
        )
    ltx_config = save_workflow_config(
        user,
        "ltx_lip_sync",
        ai_app_id=ltx_workflow_id or "2080551073030434817",
        instance_type=ltx_instance_type,
        default_prompt=(
            ltx_default_prompt.strip()
            or LTX_DEFAULT_PROMPT
        ),
        is_enabled=ltx_enabled,
        settings=ltx_settings,
    )
    db.add(ltx_config)
    save_minimax_config(
        db,
        user,
        api_key=minimax_api_key,
        base_url=minimax_base_url,
        requests_per_minute=minimax_requests_per_minute,
        account_label=minimax_account_label,
        start_new_account_binding=minimax_new_account,
    )


@router.get("/users")
def users_page(
    request: Request,
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    users = db.scalars(
        select(User)
        .options(
            selectinload(User.runninghub_config),
            selectinload(User.workflow_configs),
            selectinload(User.minimax_config),
        )
        .order_by(User.id)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {"users": users, "mask_secret": mask_secret, "current_user": current_user},
    )


@router.get("/users/new")
def new_user_page(
    request: Request, current_user: User = Depends(get_page_admin)
):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "admin_user_form.html",
        {
            "editing": False,
            "user": None,
            "config": {
                "base_url": settings.runninghub_base_url,
                "ai_app_id": settings.default_runninghub_ai_app_id,
                "instance_type": settings.default_runninghub_instance_type,
                "default_prompt": "人物自然地说话，表情自然，动作自然，镜头保持稳定。",
                "max_concurrent_tasks": 1,
            },
            "error": None,
            "current_user": current_user,
            "ltx_config": {
                "ai_app_id": "2080551073030434817",
                "instance_type": "plus",
                "default_prompt": LTX_DEFAULT_PROMPT,
                "is_enabled": False,
            },
            "ltx_has_access_password": False,
            "minimax_config": {
                "base_url": settings.minimax_default_base_url,
                "requests_per_minute": 20,
                "account_label": "MiniMax 账号",
            },
            "active_minimax_voices": [],
        },
    )


@router.post("/users")
def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(False),
    is_active: bool = Form(False),
    api_key: str = Form(""),
    base_url: str = Form(...),
    ai_app_id: str = Form(...),
    instance_type: str = Form("default"),
    default_prompt: str = Form(""),
    max_concurrent_tasks: int = Form(1),
    ltx_workflow_id: str = Form("2080551073030434817"),
    ltx_instance_type: str = Form("plus"),
    ltx_default_prompt: str = Form(""),
    ltx_enabled: bool = Form(False),
    ltx_access_password: str = Form(""),
    ltx_clear_access_password: bool = Form(False),
    minimax_api_key: str = Form(""),
    minimax_base_url: str = Form("https://api.minimaxi.com"),
    minimax_requests_per_minute: int = Form(20),
    minimax_account_label: str = Form("MiniMax 账号"),
    minimax_new_account: bool = Form(False),
    csrf_ok: None = Depends(require_csrf),
    _: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    username = username.strip()
    if not username or len(username) > 80:
        raise HTTPException(status_code=400, detail="用户名长度必须在 1 到 80 之间")
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(status_code=400, detail="用户名已存在")
    try:
        user = User(
            username=username,
            password_hash=hash_password(password),
            is_admin=is_admin,
            is_active=is_active,
        )
        db.add(user)
        _save_config(
            db,
            user,
            api_key,
            base_url,
            ai_app_id,
            instance_type,
            default_prompt,
            max_concurrent_tasks,
            ltx_workflow_id,
            ltx_instance_type,
            ltx_default_prompt,
            ltx_enabled,
            ltx_access_password,
            ltx_clear_access_password,
            minimax_api_key,
            minimax_base_url,
            minimax_requests_per_minute,
            minimax_account_label,
            minimax_new_account,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/users/{user_id}")
def edit_user_page(
    user_id: int,
    request: Request,
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    user = db.scalar(
        select(User)
        .options(
            selectinload(User.runninghub_config),
            selectinload(User.workflow_configs),
            selectinload(User.minimax_config),
            selectinload(User.minimax_voices),
        )
        .where(User.id == user_id)
    )
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    ltx_config = get_user_workflow_config(user, "ltx_lip_sync")
    return templates.TemplateResponse(
        request,
        "admin_user_form.html",
        {
            "editing": True,
            "user": user,
            "config": user.runninghub_config,
            "error": None,
            "current_user": current_user,
            "ltx_config": ltx_config,
            "ltx_has_access_password": bool(
                ltx_config.settings.get("access_password_encrypted")
            ),
            "minimax_config": user.minimax_config
            or {
                "base_url": get_settings().minimax_default_base_url,
                "requests_per_minute": 20,
                "account_label": "MiniMax 账号",
            },
            "active_minimax_voices": [
                voice
                for voice in user.minimax_voices
                if voice.is_saved
                and voice.status
                in {
                    VoiceAssetStatus.READY.value,
                    VoiceAssetStatus.ACTIVE.value,
                }
                and user.minimax_config is not None
                and voice.account_binding_id
                == user.minimax_config.account_binding_id
            ],
        },
    )


@router.post("/users/{user_id}")
def update_user(
    user_id: int,
    username: str = Form(...),
    password: str = Form(""),
    is_admin: bool = Form(False),
    is_active: bool = Form(False),
    api_key: str = Form(""),
    base_url: str = Form(...),
    ai_app_id: str = Form(...),
    instance_type: str = Form("default"),
    default_prompt: str = Form(""),
    max_concurrent_tasks: int = Form(1),
    ltx_workflow_id: str = Form("2080551073030434817"),
    ltx_instance_type: str = Form("plus"),
    ltx_default_prompt: str = Form(""),
    ltx_enabled: bool = Form(False),
    ltx_access_password: str = Form(""),
    ltx_clear_access_password: bool = Form(False),
    minimax_api_key: str = Form(""),
    minimax_base_url: str = Form("https://api.minimaxi.com"),
    minimax_requests_per_minute: int = Form(20),
    minimax_account_label: str = Form("MiniMax 账号"),
    minimax_new_account: bool = Form(False),
    csrf_ok: None = Depends(require_csrf),
    _: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    user = db.scalar(
        select(User)
        .options(
            selectinload(User.runninghub_config),
            selectinload(User.workflow_configs),
            selectinload(User.minimax_config),
            selectinload(User.minimax_voices),
        )
        .where(User.id == user_id)
    )
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    username = username.strip()
    existing = db.scalar(select(User).where(User.username == username, User.id != user_id))
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")
    try:
        user.username = username
        user.is_admin = is_admin
        user.is_active = is_active
        if password:
            user.password_hash = hash_password(password)
        _save_config(
            db,
            user,
            api_key,
            base_url,
            ai_app_id,
            instance_type,
            default_prompt,
            max_concurrent_tasks,
            ltx_workflow_id,
            ltx_instance_type,
            ltx_default_prompt,
            ltx_enabled,
            ltx_access_password,
            ltx_clear_access_password,
            minimax_api_key,
            minimax_base_url,
            minimax_requests_per_minute,
            minimax_account_label,
            minimax_new_account,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/admin/users", status_code=303)
