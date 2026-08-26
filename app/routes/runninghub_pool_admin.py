from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import RunningHubDualPoolGrant, User
from app.routes.dependencies import get_page_admin
from app.services.csrf import require_csrf
from app.services.logging_config import log_event
from app.services.security import encrypt_secret
from app.services.h3.prompt import (
    H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION,
    H3_PROMPT_TEMPLATE_VERSION,
)
from app.services.workflow_configs import (
    get_system_workflow_config,
    save_system_workflow_config,
)
from app.workflows.registry import get_workflow, list_workflows
from app.services.runninghub_pool import (
    DuplicateRunningHubCredentialError,
    RunningHubPoolValidationError,
    create_execution_account,
    execution_account_for_admin_page,
    execution_accounts_for_admin_page,
    update_execution_account,
)
from app.services.runninghub_balance import balance_summary, refresh_pool_account_balance
from app.services.runninghub_dual_pool import (
    dual_pool_runtime_control,
    dual_pool_runtime_enabled,
    set_dual_pool_runtime_enabled,
)
from app.services.seedvr2_pool import (
    DuplicateSeedVR2CredentialError,
    SeedVR2PoolValidationError,
    create_seedvr2_execution_account,
    seedvr2_execution_account_for_admin_page,
    seedvr2_execution_accounts_for_admin_page,
    update_seedvr2_execution_account,
)
from app.web import templates


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/runninghub-pool", tags=["runninghub-pool-admin"])


@router.get("/workflows")
def workflow_config_page(
    request: Request,
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    workflows = list_workflows()
    configs = {
        workflow.key: get_system_workflow_config(db, workflow.key)
        for workflow in workflows
    }
    return templates.TemplateResponse(
        request,
        "admin_workflow_configs.html",
        {
            "workflows": workflows,
            "configs": configs,
            "current_user": current_user,
            "h3_prompt_template_version": H3_PROMPT_TEMPLATE_VERSION,
            "h3_loop_anchor_prompt_template_version": (
                H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION
            ),
        },
    )


@router.post("/workflows/{workflow_key}")
def update_workflow_config(
    workflow_key: str,
    ai_app_id: str = Form(""),
    instance_type: str = Form("plus"),
    default_prompt: str = Form(""),
    is_enabled: bool = Form(False),
    access_password: str = Form(""),
    clear_access_password: bool = Form(False),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    try:
        workflow = get_workflow(workflow_key)
        existing = get_system_workflow_config(db, workflow_key)
        settings = dict(existing.settings)
        if clear_access_password:
            settings.pop("access_password_encrypted", None)
        elif access_password.strip():
            if len(access_password) > 500:
                raise ValueError("工作流访问密码不能超过 500 个字符")
            settings["access_password_encrypted"] = encrypt_secret(
                access_password.strip()
            )
        save_system_workflow_config(
            db,
            workflow_key,
            ai_app_id=ai_app_id,
            instance_type=instance_type,
            default_prompt=(
                workflow.default_prompt
                if workflow_key == "minimax_h3_ref2va"
                else default_prompt
            ),
            is_enabled=is_enabled,
            settings=settings,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        logger,
        "runninghub_workflow.updated",
        "管理员更新系统 RunningHub 工作流配置",
        operator_user_id=current_user.id,
        workflow_key=workflow.key,
        has_access_password=bool(settings.get("access_password_encrypted")),
    )
    return RedirectResponse(
        f"/admin/runninghub-pool/workflows?updated={workflow_key}",
        status_code=303,
    )


@router.get("")
def runninghub_pool_page(
    request: Request,
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    runtime_control = dual_pool_runtime_control(db)
    accounts = execution_accounts_for_admin_page(db)
    digital_human_config = get_system_workflow_config(db, "digital_human")
    return templates.TemplateResponse(
        request,
        "admin_runninghub_pool.html",
        {
            "accounts": accounts,
            "balances": {
                account.id: balance_summary(db, account.credential_fingerprint)
                for account in accounts
            },
            "current_user": current_user,
            "default_base_url": get_settings().runninghub_base_url,
            "digital_human_workflow_id": digital_human_config.ai_app_id,
            "dual_pool_enabled": dual_pool_runtime_enabled(db),
            "dual_pool_control_saved": runtime_control is not None,
        },
    )


@router.post("/runtime-mode")
def update_runninghub_pool_runtime_mode(
    dual_pool_enabled: bool = Form(False),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    before = dual_pool_runtime_enabled(db)
    set_dual_pool_runtime_enabled(
        db, enabled=dual_pool_enabled, updated_by_user_id=current_user.id
    )
    db.commit()
    log_event(
        logger,
        "runninghub_pool.runtime_mode_updated",
        "管理员更新新版工作台 RunningHub 资源池运行模式",
        operator_user_id=current_user.id,
        before_dual_pool_enabled=before,
        after_dual_pool_enabled=dual_pool_enabled,
    )
    return RedirectResponse("/admin/runninghub-pool?mode_updated=1", status_code=303)


def _pool_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DuplicateRunningHubCredentialError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, IntegrityError):
        return HTTPException(
            status_code=409,
            detail="资源池账号或管理员成员关系发生冲突，请刷新页面后重试",
        )
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/accounts")
def create_runninghub_pool_account(
    label: str = Form(...),
    api_key: str = Form(...),
    base_url: str = Form(...),
    digital_human_ai_app_id: str = Form(...),
    max_concurrent_tasks: int = Form(5),
    is_enabled: bool = Form(False),
    user_ids: list[int] | None = Form(None),
    admin_user_ids: list[int] | None = Form(None),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    member_user_ids = user_ids if user_ids is not None else (admin_user_ids or [])
    try:
        account = create_execution_account(
            db,
            label=label,
            api_key=api_key,
            base_url=base_url,
            digital_human_ai_app_id=digital_human_ai_app_id,
            max_concurrent_tasks=max_concurrent_tasks,
            is_enabled=is_enabled,
            user_ids=member_user_ids,
        )
        db.commit()
        db.refresh(account)
    except (RunningHubPoolValidationError, IntegrityError) as exc:
        db.rollback()
        raise _pool_error(exc) from exc
    log_event(
        logger,
        "runninghub_pool.account_created",
        "管理员新增 RunningHub 执行账号",
        operator_user_id=current_user.id,
        execution_account_id=account.id,
        execution_account_label=account.label,
        max_concurrent_tasks=account.max_concurrent_tasks,
        is_enabled=account.is_enabled,
        user_ids=sorted(member_user_ids),
    )
    return RedirectResponse("/admin/runninghub-pool?created=1", status_code=303)


@router.post("/accounts/{account_id}")
def update_runninghub_pool_account(
    account_id: int,
    label: str = Form(...),
    api_key: str = Form(""),
    base_url: str = Form(...),
    digital_human_ai_app_id: str = Form(...),
    max_concurrent_tasks: int = Form(5),
    is_enabled: bool = Form(False),
    user_ids: list[int] | None = Form(None),
    admin_user_ids: list[int] | None = Form(None),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    account = execution_account_for_admin_page(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="RunningHub 执行账号不存在")
    if user_ids is not None:
        member_user_ids = user_ids
    elif admin_user_ids is not None:
        member_user_ids = admin_user_ids
    else:
        member_user_ids = [
            membership.admin_user_id for membership in account.pool_memberships
        ]
    try:
        changed_fields = update_execution_account(
            db,
            account,
            label=label,
            api_key=api_key,
            base_url=base_url,
            digital_human_ai_app_id=digital_human_ai_app_id,
            max_concurrent_tasks=max_concurrent_tasks,
            is_enabled=is_enabled,
            user_ids=member_user_ids,
        )
        db.commit()
    except (RunningHubPoolValidationError, IntegrityError) as exc:
        db.rollback()
        raise _pool_error(exc) from exc
    log_event(
        logger,
        "runninghub_pool.account_updated",
        "管理员更新 RunningHub 执行账号",
        operator_user_id=current_user.id,
        execution_account_id=account.id,
        execution_account_label=account.label,
        max_concurrent_tasks=account.max_concurrent_tasks,
        is_enabled=account.is_enabled,
        user_ids=sorted(member_user_ids),
        changed_fields=sorted(changed_fields),
    )
    return RedirectResponse("/admin/runninghub-pool?updated=1", status_code=303)


@router.post("/accounts/{account_id}/refresh-balance")
def refresh_runninghub_pool_account_balance(
    account_id: int,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    account = execution_account_for_admin_page(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="RunningHub 执行账号不存在")
    try:
        balance = refresh_pool_account_balance(db, account)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="RunningHub 凭据无法读取") from exc
    log_event(
        logger,
        "runninghub_pool.balance_refreshed",
        "管理员刷新 RunningHub 执行账号 RH 币",
        operator_user_id=current_user.id,
        execution_account_id=account.id,
        balance_status=balance.balance_status,
    )
    return RedirectResponse("/admin/runninghub-pool?balance_refreshed=1", status_code=303)


@router.get("/seedvr2")
def seedvr2_pool_page(
    request: Request,
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    users = db.scalars(
        select(User)
        .join(RunningHubDualPoolGrant)
        .where(
            User.is_active.is_(True),
            RunningHubDualPoolGrant.is_enabled.is_(True),
            (User.is_admin.is_(True))
            | (RunningHubDualPoolGrant.allow_non_admin.is_(True)),
        )
        .order_by(User.id)
    ).all()
    accounts = seedvr2_execution_accounts_for_admin_page(db)
    return templates.TemplateResponse(
        request,
        "admin_seedvr2_pool.html",
        {
            "accounts": accounts,
            "balances": {
                account.id: balance_summary(db, account.credential_fingerprint)
                for account in accounts
            },
            "users": users,
            "current_user": current_user,
            "default_base_url": get_settings().runninghub_base_url,
        },
    )


def _seedvr2_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DuplicateSeedVR2CredentialError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, IntegrityError):
        return HTTPException(
            status_code=409,
            detail="SeedVR2 账号或授权成员关系发生冲突，请刷新页面后重试",
        )
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/seedvr2/accounts")
def create_seedvr2_pool_account(
    label: str = Form(...),
    api_key: str = Form(...),
    base_url: str = Form(...),
    seedvr2_ai_app_id: str = Form(...),
    max_concurrent_tasks: int = Form(5),
    is_enabled: bool = Form(False),
    user_ids: list[int] | None = Form(None),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    try:
        account = create_seedvr2_execution_account(
            db,
            label=label,
            api_key=api_key,
            base_url=base_url,
            seedvr2_ai_app_id=seedvr2_ai_app_id,
            max_concurrent_tasks=max_concurrent_tasks,
            is_enabled=is_enabled,
            user_ids=user_ids or [],
        )
        db.commit()
    except (SeedVR2PoolValidationError, IntegrityError) as exc:
        db.rollback()
        raise _seedvr2_error(exc) from exc
    log_event(
        logger,
        "seedvr2_pool.account_created",
        "管理员新增 SeedVR2 执行账号",
        operator_user_id=current_user.id,
        execution_account_id=account.id,
        user_ids=sorted(user_ids or []),
    )
    return RedirectResponse("/admin/runninghub-pool/seedvr2?created=1", status_code=303)


@router.post("/seedvr2/accounts/{account_id}")
def update_seedvr2_pool_account(
    account_id: int,
    label: str = Form(...),
    api_key: str = Form(""),
    base_url: str = Form(...),
    seedvr2_ai_app_id: str = Form(...),
    max_concurrent_tasks: int = Form(5),
    is_enabled: bool = Form(False),
    user_ids: list[int] | None = Form(None),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    account = seedvr2_execution_account_for_admin_page(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="SeedVR2 执行账号不存在")
    try:
        changed = update_seedvr2_execution_account(
            db,
            account,
            label=label,
            api_key=api_key,
            base_url=base_url,
            seedvr2_ai_app_id=seedvr2_ai_app_id,
            max_concurrent_tasks=max_concurrent_tasks,
            is_enabled=is_enabled,
            user_ids=user_ids or [],
        )
        db.commit()
    except (SeedVR2PoolValidationError, IntegrityError) as exc:
        db.rollback()
        raise _seedvr2_error(exc) from exc
    log_event(
        logger,
        "seedvr2_pool.account_updated",
        "管理员更新 SeedVR2 执行账号",
        operator_user_id=current_user.id,
        execution_account_id=account.id,
        changed_fields=sorted(changed),
        user_ids=sorted(user_ids or []),
    )
    return RedirectResponse("/admin/runninghub-pool/seedvr2?updated=1", status_code=303)


@router.post("/seedvr2/accounts/{account_id}/refresh-balance")
def refresh_seedvr2_pool_account_balance(
    account_id: int,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    account = seedvr2_execution_account_for_admin_page(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="SeedVR2 执行账号不存在")
    try:
        balance = refresh_pool_account_balance(db, account)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="RunningHub 凭据无法读取") from exc
    log_event(
        logger,
        "seedvr2_pool.balance_refreshed",
        "管理员刷新 SeedVR2 执行账号 RH 币",
        operator_user_id=current_user.id,
        execution_account_id=account.id,
        balance_status=balance.balance_status,
    )
    return RedirectResponse(
        "/admin/runninghub-pool/seedvr2?balance_refreshed=1", status_code=303
    )
