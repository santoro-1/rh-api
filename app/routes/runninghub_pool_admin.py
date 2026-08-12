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
from app.services.runninghub_pool import (
    DuplicateRunningHubCredentialError,
    RunningHubPoolValidationError,
    create_execution_account,
    execution_account_for_admin_page,
    execution_accounts_for_admin_page,
    update_execution_account,
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


@router.get("")
def runninghub_pool_page(
    request: Request,
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    administrators = db.scalars(
        select(User)
        .outerjoin(RunningHubDualPoolGrant)
        .where(
            User.is_active.is_(True),
            (User.is_admin.is_(True))
            | (
                RunningHubDualPoolGrant.is_enabled.is_(True)
                & RunningHubDualPoolGrant.allow_non_admin.is_(True)
            ),
        )
        .order_by(User.id)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin_runninghub_pool.html",
        {
            "accounts": execution_accounts_for_admin_page(db),
            "administrators": administrators,
            "current_user": current_user,
            "default_base_url": get_settings().runninghub_base_url,
        },
    )


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
    admin_user_ids: list[int] | None = Form(None),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    try:
        account = create_execution_account(
            db,
            label=label,
            api_key=api_key,
            base_url=base_url,
            digital_human_ai_app_id=digital_human_ai_app_id,
            max_concurrent_tasks=max_concurrent_tasks,
            is_enabled=is_enabled,
            admin_user_ids=admin_user_ids or [],
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
        admin_user_ids=sorted(admin_user_ids or []),
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
    admin_user_ids: list[int] | None = Form(None),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    account = execution_account_for_admin_page(db, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="RunningHub 执行账号不存在")
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
            admin_user_ids=admin_user_ids or [],
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
        admin_user_ids=sorted(admin_user_ids or []),
        changed_fields=sorted(changed_fields),
    )
    return RedirectResponse("/admin/runninghub-pool?updated=1", status_code=303)


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
    return templates.TemplateResponse(
        request,
        "admin_seedvr2_pool.html",
        {
            "accounts": seedvr2_execution_accounts_for_admin_page(db),
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
