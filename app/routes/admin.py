from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.models import RunningHubConfig, User
from app.routes.dependencies import get_page_admin
from app.services.security import encrypt_secret, hash_password, mask_secret
from app.services.workflow_configs import save_workflow_config
from app.web import templates


router = APIRouter(prefix="/admin", tags=["admin"])


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


@router.get("/users")
def users_page(
    request: Request,
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    users = db.scalars(
        select(User).options(selectinload(User.runninghub_config)).order_by(User.id)
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
    instance_type: str = Form("plus"),
    default_prompt: str = Form(""),
    max_concurrent_tasks: int = Form(1),
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
        .options(selectinload(User.runninghub_config))
        .where(User.id == user_id)
    )
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return templates.TemplateResponse(
        request,
        "admin_user_form.html",
        {
            "editing": True,
            "user": user,
            "config": user.runninghub_config,
            "error": None,
            "current_user": current_user,
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
    instance_type: str = Form("plus"),
    default_prompt: str = Form(""),
    max_concurrent_tasks: int = Form(1),
    _: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
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
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/admin/users", status_code=303)
