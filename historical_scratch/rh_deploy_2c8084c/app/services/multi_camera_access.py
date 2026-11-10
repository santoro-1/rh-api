from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MultiCameraUserAccess, User


MULTI_CAMERA_BOOTSTRAP_USERNAMES = ("admin", "Cx_ceshi")


class MultiCameraAccessError(PermissionError):
    """The signed-in user is outside the controlled feature rollout."""


def bootstrap_multi_camera_access(db: Session) -> int:
    """Map the two environment-local usernames to durable user-id grants.

    Username is only a deployment bootstrap key.  Duplicate, missing, or
    inactive accounts are not granted; all request-time checks use user_id.
    """

    created = 0
    for username in MULTI_CAMERA_BOOTSTRAP_USERNAMES:
        matches = db.scalars(select(User).where(User.username == username)).all()
        if len(matches) != 1 or not matches[0].is_active:
            continue
        user = matches[0]
        if db.get(MultiCameraUserAccess, user.id) is None:
            db.add(MultiCameraUserAccess(user_id=user.id, is_enabled=True))
            created += 1
    if created:
        db.commit()
    return created


def user_has_multi_camera_access(db: Session, user: User) -> bool:
    if not user.is_active:
        return False
    grant = db.get(MultiCameraUserAccess, user.id)
    return bool(grant and grant.is_enabled)


def ensure_multi_camera_access(db: Session, user: User) -> None:
    if db.get(MultiCameraUserAccess, user.id) is None:
        # Also supports a controlled account created after the web process
        # started (common in fresh local test databases).
        bootstrap_multi_camera_access(db)
    if not user_has_multi_camera_access(db, user):
        raise MultiCameraAccessError("当前账号未开放多机位测试功能")
