from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.models import (
    AudioGenerationTask,
    AudioTaskStatus,
    GenerationTask,
    MiniMaxVoiceAsset,
    RunningHubConfig,
    TaskStatus,
    User,
    VoiceAssetStatus,
    VoiceCreationStatus,
    VoiceCreationTask,
)
from app.routes.dependencies import get_page_admin
from app.services.csrf import require_csrf
from app.services.content_analysis.ark_accounts import (
    ARK_DEFAULT_BASE_URL,
    save_ark_config,
)
from app.services.logging_config import log_event
from app.services.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    mask_secret,
    secret_fingerprint,
)
from app.services.speech.accounts import save_minimax_config
from app.services.speech.minimax import MiniMaxAPIError, MiniMaxClient
from app.services.speech.system_voices import (
    group_system_voice_assets,
    sync_system_voices,
)
from app.services.storage import remove_directory
from app.services.workflow_configs import (
    get_user_workflow_config,
    save_workflow_config,
)
from app.web import templates
from app.workflows import get_workflow


router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)
LTX_DEFAULT_PROMPT = get_workflow("ltx_lip_sync").default_prompt
ACTIVE_VIDEO_STATUSES = {
    TaskStatus.PENDING.value,
    TaskStatus.UPLOADING.value,
    TaskStatus.SUBMITTED.value,
    TaskStatus.RUNNING.value,
}
ACTIVE_AUDIO_STATUSES = {
    AudioTaskStatus.PENDING.value,
    AudioTaskStatus.CLONING.value,
    AudioTaskStatus.SYNTHESIZING.value,
    AudioTaskStatus.REMOTE_PENDING.value,
    AudioTaskStatus.AWAITING_REVIEW.value,
    AudioTaskStatus.ALIGNING.value,
    AudioTaskStatus.SEGMENTING.value,
    AudioTaskStatus.HANDOFF.value,
}
ACTIVE_VOICE_CREATION_STATUSES = {
    VoiceCreationStatus.PENDING.value,
    VoiceCreationStatus.CLONING.value,
    VoiceCreationStatus.SYNTHESIZING.value,
    VoiceCreationStatus.SAVE_PENDING.value,
    VoiceCreationStatus.SAVING.value,
}


def _voice_management_context(user: User) -> dict[str, object]:
    config = user.minimax_config
    if config is None:
        return {
            "system_voice_groups": [],
            "hidden_system_voice_groups": [],
            "custom_minimax_voices": [],
            "system_voice_count": 0,
            "hidden_system_voice_count": 0,
        }
    current_voices = [
        voice
        for voice in user.minimax_voices
        if voice.account_binding_id == config.account_binding_id
    ]
    active = [
        voice
        for voice in current_voices
        if voice.is_saved
        and voice.status
        in {
            VoiceAssetStatus.READY.value,
            VoiceAssetStatus.ACTIVE.value,
        }
    ]
    system_voices = [voice for voice in active if voice.method == "system"]
    hidden_system_voices = [
        voice
        for voice in current_voices
        if voice.method == "system"
        and voice.status == VoiceAssetStatus.HIDDEN.value
    ]
    return {
        "system_voice_groups": group_system_voice_assets(system_voices),
        "hidden_system_voice_groups": group_system_voice_assets(
            hidden_system_voices
        ),
        "custom_minimax_voices": [
            voice for voice in active if voice.method != "system"
        ],
        "system_voice_count": len(system_voices),
        "hidden_system_voice_count": len(hidden_system_voices),
    }


def _system_voice_for_admin(
    db: Session,
    user_id: int,
    voice_asset_id: str,
) -> tuple[User, MiniMaxVoiceAsset]:
    user = db.scalar(
        select(User)
        .options(selectinload(User.minimax_config))
        .where(User.id == user_id)
    )
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    config = user.minimax_config
    voice = db.scalar(
        select(MiniMaxVoiceAsset).where(
            MiniMaxVoiceAsset.id == voice_asset_id,
            MiniMaxVoiceAsset.user_id == user_id,
            MiniMaxVoiceAsset.method == "system",
        )
    )
    if (
        voice is None
        or config is None
        or voice.account_binding_id != config.account_binding_id
    ):
        raise HTTPException(status_code=404, detail="官方音色不存在")
    return user, voice


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
    ark_enabled: bool,
    ark_api_key: str,
    ark_base_url: str,
    ark_model: str,
    ark_timeout_seconds: int,
    ark_max_retries: int,
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
        clean_api_key = api_key.strip()
        config.api_key_encrypted = encrypt_secret(clean_api_key)
        config.credential_fingerprint = secret_fingerprint(clean_api_key)
    elif config.api_key_encrypted and not config.credential_fingerprint:
        config.credential_fingerprint = secret_fingerprint(
            decrypt_secret(config.api_key_encrypted)
        )
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
    save_ark_config(
        db,
        user,
        enabled=ark_enabled,
        api_key=ark_api_key,
        base_url=ark_base_url,
        model=ark_model,
        timeout_seconds=ark_timeout_seconds,
        max_retries=ark_max_retries,
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
            selectinload(User.ark_config),
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
            "ark_config": {
                "enabled": False,
                "api_key_encrypted": None,
                "base_url": ARK_DEFAULT_BASE_URL,
                "model": "",
                "timeout_seconds": 30,
                "max_retries": 2,
            },
            "system_voice_groups": [],
            "hidden_system_voice_groups": [],
            "custom_minimax_voices": [],
            "system_voice_count": 0,
            "hidden_system_voice_count": 0,
        },
    )


@router.post("/users")
def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(False),
    is_active: bool = Form(False),
    h3_access_enabled: bool = Form(False),
    api_key: str = Form(""),
    base_url: str = Form(...),
    ai_app_id: str = Form(...),
    instance_type: str = Form("plus"),
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
    ark_enabled: bool = Form(False),
    ark_api_key: str = Form(""),
    ark_base_url: str = Form(ARK_DEFAULT_BASE_URL),
    ark_model: str = Form(""),
    ark_timeout_seconds: int = Form(30),
    ark_max_retries: int = Form(2),
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
            h3_access_enabled=h3_access_enabled,
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
            ark_enabled,
            ark_api_key,
            ark_base_url,
            ark_model,
            ark_timeout_seconds,
            ark_max_retries,
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
            selectinload(User.ark_config),
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
            "ark_config": user.ark_config
            or {
                "enabled": False,
                "api_key_encrypted": None,
                "base_url": ARK_DEFAULT_BASE_URL,
                "model": "",
                "timeout_seconds": 30,
                "max_retries": 2,
            },
            **_voice_management_context(user),
        },
    )


@router.post("/users/{user_id}")
def update_user(
    user_id: int,
    username: str = Form(...),
    password: str = Form(""),
    is_admin: bool = Form(False),
    is_active: bool = Form(False),
    h3_access_enabled: bool = Form(False),
    api_key: str = Form(""),
    base_url: str = Form(...),
    ai_app_id: str = Form(...),
    instance_type: str = Form("plus"),
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
    ark_enabled: bool = Form(False),
    ark_api_key: str = Form(""),
    ark_base_url: str = Form(ARK_DEFAULT_BASE_URL),
    ark_model: str = Form(""),
    ark_timeout_seconds: int = Form(30),
    ark_max_retries: int = Form(2),
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
            selectinload(User.ark_config),
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
        user.h3_access_enabled = h3_access_enabled
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
            ark_enabled,
            ark_api_key,
            ark_base_url,
            ark_model,
            ark_timeout_seconds,
            ark_max_retries,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
def delete_user(
    user_id: int,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    """Delete one account and only its own database records/files."""

    if user_id == current_user.id:
        raise HTTPException(status_code=409, detail="不能删除当前登录的管理员账号")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    active_counts = {
        "视频": db.scalar(
            select(func.count(GenerationTask.id)).where(
                GenerationTask.user_id == user_id,
                GenerationTask.status.in_(ACTIVE_VIDEO_STATUSES),
            )
        )
        or 0,
        "语音": db.scalar(
            select(func.count(AudioGenerationTask.id)).where(
                AudioGenerationTask.user_id == user_id,
                AudioGenerationTask.status.in_(ACTIVE_AUDIO_STATUSES),
            )
        )
        or 0,
        "音色制作": db.scalar(
            select(func.count(VoiceCreationTask.id)).where(
                VoiceCreationTask.user_id == user_id,
                VoiceCreationTask.status.in_(ACTIVE_VOICE_CREATION_STATUSES),
            )
        )
        or 0,
    }
    active_summary = "、".join(
        f"{label} {count} 个"
        for label, count in active_counts.items()
        if count
    )
    if active_summary:
        raise HTTPException(
            status_code=409,
            detail=f"该账号仍有运行中任务（{active_summary}），不能删除",
        )

    settings = get_settings()
    user_directories = (
        settings.uploads_dir / str(user_id),
        settings.outputs_dir / str(user_id),
        settings.staged_assets_dir / str(user_id),
        settings.voice_sources_dir / str(user_id),
        settings.voice_creations_dir / str(user_id),
    )
    db.delete(user)
    db.commit()
    try:
        for directory in user_directories:
            remove_directory(directory)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="账号记录已删除，但该账号的部分本地文件清理失败",
        ) from exc
    return RedirectResponse("/admin/users?deleted=1", status_code=303)


@router.post("/users/{user_id}/system-voices/sync")
def sync_user_system_voices(
    user_id: int,
    csrf_ok: None = Depends(require_csrf),
    _: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    user = db.scalar(
        select(User)
        .options(selectinload(User.minimax_config))
        .where(User.id == user_id)
    )
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    config = user.minimax_config
    if not config or not config.api_key_encrypted:
        raise HTTPException(
            status_code=409,
            detail="请先保存该用户的 MiniMax API Key",
        )
    try:
        client = MiniMaxClient(
            decrypt_secret(config.api_key_encrypted, label="MiniMax API Key"),
            base_url=config.base_url,
            timeout=get_settings().minimax_request_timeout_seconds,
        )
        available = client.list_voices("system")
        selections: dict[str, str] = {}
        for item in available[:100]:
            voice_id = str(item.get("voice_id") or "").strip()
            if not voice_id:
                continue
            provider_name = str(
                item.get("voice_name")
                or item.get("name")
                or voice_id
            ).strip()
            selections[voice_id] = provider_name[:100]
        if not selections:
            raise ValueError("MiniMax 当前没有返回可用的官方音色")
        saved = sync_system_voices(
            db,
            user,
            client,
            selections,
            available_voices=available,
        )
        db.commit()
    except (MiniMaxAPIError, OSError, ValueError) as exc:
        db.rollback()
        log_event(
            logger,
            "voice.system_sync_failed",
            "同步 MiniMax 官方音色失败",
            level=logging.WARNING,
            user_id=user.id,
            username=user.username,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RedirectResponse(
        f"/admin/users/{user_id}?system_voice_count={len(saved)}",
        status_code=303,
    )


@router.post("/users/{user_id}/system-voices/{voice_asset_id}/delete")
def delete_user_system_voice(
    user_id: int,
    voice_asset_id: str,
    csrf_ok: None = Depends(require_csrf),
    _: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    """Hide one provider voice locally without touching MiniMax's catalogue."""

    user, voice = _system_voice_for_admin(db, user_id, voice_asset_id)
    active_references = db.scalar(
        select(func.count(AudioGenerationTask.id)).where(
            AudioGenerationTask.user_id == user_id,
            AudioGenerationTask.status.in_(ACTIVE_AUDIO_STATUSES),
            or_(
                AudioGenerationTask.voice_a_id == voice.id,
                AudioGenerationTask.voice_b_id == voice.id,
                AudioGenerationTask.voice_asset_id == voice.id,
            ),
        )
    ) or 0
    if active_references:
        raise HTTPException(
            status_code=409,
            detail="该官方音色仍被运行中的语音任务使用，暂时不能删除",
        )
    voice.status = VoiceAssetStatus.HIDDEN.value
    voice.is_saved = False
    db.commit()
    log_event(
        logger,
        "voice.system_deleted",
        "管理员从站内账号删除官方音色",
        user_id=user.id,
        username=user.username,
        voice_asset_id=voice.id,
        voice_id=voice.voice_id,
    )
    return RedirectResponse(
        f"/admin/users/{user_id}?system_voice_deleted=1",
        status_code=303,
    )


@router.post("/users/{user_id}/system-voices/{voice_asset_id}/restore")
def restore_user_system_voice(
    user_id: int,
    voice_asset_id: str,
    csrf_ok: None = Depends(require_csrf),
    _: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    """Restore one locally hidden provider voice."""

    user, voice = _system_voice_for_admin(db, user_id, voice_asset_id)
    voice.status = VoiceAssetStatus.ACTIVE.value
    voice.is_saved = True
    db.commit()
    log_event(
        logger,
        "voice.system_restored",
        "管理员恢复站内官方音色",
        user_id=user.id,
        username=user.username,
        voice_asset_id=voice.id,
        voice_id=voice.voice_id,
    )
    return RedirectResponse(
        f"/admin/users/{user_id}?system_voice_restored=1",
        status_code=303,
    )
