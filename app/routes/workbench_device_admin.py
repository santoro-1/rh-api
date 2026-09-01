from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routes.dependencies import get_page_admin
from app.services.csrf import require_csrf
from app.services.device_auth import service
from app.services.device_auth.models import (
    WorkbenchDevice,
    WorkbenchDeviceAuditEvent,
    WorkbenchDeviceGrant,
    WorkbenchDevicePolicy,
)
from app.web import templates

router = APIRouter(prefix="/admin/workbench-devices", tags=["workbench-device-admin"])


def _redirect():
    return RedirectResponse("/admin/workbench-devices", status_code=303)


@router.get("")
def device_page(
    request: Request,
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    records = db.execute(
        select(WorkbenchDeviceGrant, WorkbenchDevice, User)
        .join(WorkbenchDevice, WorkbenchDevice.id == WorkbenchDeviceGrant.device_id)
        .join(User, User.id == WorkbenchDeviceGrant.user_id)
        .order_by(WorkbenchDeviceGrant.created_at.desc())
        .limit(500)
    ).all()
    audits = db.scalars(
        select(WorkbenchDeviceAuditEvent)
        .order_by(WorkbenchDeviceAuditEvent.created_at.desc())
        .limit(50)
    ).all()
    users = db.scalars(
        select(User).where(User.is_active.is_(True)).order_by(User.username)
    ).all()
    policies = {p.user_id: p for p in db.scalars(select(WorkbenchDevicePolicy)).all()}
    return templates.TemplateResponse(
        request,
        "admin_workbench_devices.html",
        {
            "current_user": current_user,
            "records": records,
            "audits": audits,
            "users": users,
            "policies": policies,
            "mode": service.current_mode(db),
            "as_datetime": lambda epoch: datetime.fromtimestamp(epoch, timezone.utc),
        },
    )


@router.post("/policies/{user_id}")
def save_policy(
    user_id: int,
    max_devices: int = Form(1),
    allow_software: bool = Form(False),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    service.update_policy(
        db,
        actor_id=current_user.id,
        user_id=user_id,
        max_devices=max_devices,
        allow_software=allow_software,
        now=int(time.time()),
    )
    db.commit()
    return _redirect()


@router.post("/replace")
def replace(
    old_grant_id: str = Form(...),
    new_grant_id: str = Form(...),
    old_revision: int = Form(...),
    new_revision: int = Form(...),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    service.replace_grant(
        db,
        actor_id=current_user.id,
        old_grant_id=old_grant_id,
        new_grant_id=new_grant_id,
        old_revision=old_revision,
        new_revision=new_revision,
        now=int(time.time()),
    )
    db.commit()
    return _redirect()


@router.post("/devices/{device_id}/status")
def global_status(
    device_id: str,
    status: str = Form(...),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    service.change_device_status(
        db,
        actor_id=current_user.id,
        device_id=device_id,
        status=status,
        now=int(time.time()),
    )
    db.commit()
    return _redirect()


@router.post("/{grant_id}/{action}")
def grant_action(
    grant_id: str,
    action: str,
    revision: int = Form(...),
    label: str = Form(""),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_page_admin),
    db: Session = Depends(get_db),
):
    service.change_grant(
        db,
        actor_id=current_user.id,
        grant_id=grant_id,
        expected_revision=revision,
        action=action,
        label=label,
        now=int(time.time()),
    )
    db.commit()
    return _redirect()
