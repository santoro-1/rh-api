import json
import time

from sqlalchemy import select

from app.database import SessionLocal
from app.models import User
from app.services.device_auth import service
from app.services.device_auth.models import WorkbenchDeviceGrant


with SessionLocal() as db:
    admin = db.scalar(select(User).where(User.username == "staging-admin"))
    user = db.scalar(select(User).where(User.username == "device-test"))
    if admin is None or not admin.is_admin or user is None:
        raise SystemExit("staging test identities are missing")
    grants = list(
        db.scalars(
            select(WorkbenchDeviceGrant).where(
                WorkbenchDeviceGrant.user_id == user.id,
                WorkbenchDeviceGrant.label == "A-local",
            )
        )
    )
    if len(grants) != 1:
        raise SystemExit(f"expected one A-local grant, found {len(grants)}")
    grant = grants[0]
    if grant.status != "PENDING":
        raise SystemExit(f"A-local must be PENDING before approval, got {grant.status}")
    previous_revision = grant.revision
    approved = service.change_grant(
        db,
        actor_id=admin.id,
        grant_id=grant.id,
        expected_revision=grant.revision,
        action="approve",
        label="",
        now=int(time.time()),
    )
    db.commit()
    print(
        json.dumps(
            {
                "ok": True,
                "label": approved.label,
                "status": approved.status,
                "grant_id": approved.id,
                "device_id": approved.device_id,
                "previous_revision": previous_revision,
                "revision": approved.revision,
            },
            sort_keys=True,
        )
    )
