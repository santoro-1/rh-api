"""Unified workbench request admission, separate from account authentication.

Read/recovery calls still require a valid account and (when using a bound token)
a fresh proof. They do not require the device grant to remain enabled. Business
services must still perform resource ownership checks. New paid operations check
current server policy; body fields, headers and client versions cannot disable it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from types import MappingProxyType
from typing import Mapping, Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models import User
from . import service
from .authentication import authenticated_account, consume_request_proof
from .errors import DeviceAuthError
from .models import WorkbenchDeviceAuditEvent
from .protocol import canonical_json


@dataclass(frozen=True)
class WorkbenchIdentity:
    user_id: int
    thumbprint: str | None
    claims: Mapping[str, Any] | None = field(default=None, repr=False)


def workbench_user(
    request: Request,
    db: Session,
    *,
    body_token: str | None = None,
    new_work: bool = False,
) -> User:
    """Must run before business writes; proof consumption commits replay state."""
    user, token, bound = authenticated_account(request, db, body_token=body_token)
    thumbprint = None
    if bound is not None:
        proof = consume_request_proof(
            request, db, user=user, token=token, bound=bound, purpose="request"
        )
        thumbprint = proof.thumbprint
    elif request.headers.get("dpop"):
        # Do not accept an unbound bearer plus a decorative proof as activation.
        raise DeviceAuthError(
            "DEVICE_BOUND_TOKEN_REQUIRED", "请先换取设备绑定授权凭据", 401
        )
    frozen = None
    if bound is not None:
        frozen = dict(bound)
        frozen["scopes"] = tuple(frozen["scopes"])
        frozen["cnf"] = MappingProxyType(dict(frozen["cnf"]))
        frozen = MappingProxyType(frozen)
    identity = WorkbenchIdentity(user.id, thumbprint, frozen)
    request.state.workbench_device_identity = identity
    if new_work:
        require_new_work(db, user_id=user.id, identity=identity)
    return user


def request_identity(request: Request) -> WorkbenchIdentity:
    identity = getattr(request.state, "workbench_device_identity", None)
    if not isinstance(identity, WorkbenchIdentity):
        raise DeviceAuthError("DEVICE_CONTEXT_MISSING", "缺少设备验证上下文", 401)
    return identity


def require_new_work(
    db: Session,
    *,
    user_id: int,
    identity: WorkbenchIdentity | None,
    scope: str = "cloud:generate",
) -> None:
    """Recheck at the actual new-work boundary, not a client's retry_scope value.

    Does not commit, write business state or call a paid provider. The proof was
    already verified by workbench_user. Worker admission requires a separate
    persistent binding; an HTTP decision is NOT a permanent queue permit.
    """
    if scope not in service.SCOPES:
        raise ValueError("unknown workbench scope")
    service._active_user(db, user_id)
    if identity is not None and identity.user_id != user_id:
        raise DeviceAuthError(
            "DEVICE_ACCOUNT_MISMATCH", "设备准入与业务账号不一致", 403
        )
    mode = service.current_mode(db)
    if mode not in {"OFF", "OBSERVE", "ENFORCE"}:
        raise DeviceAuthError("DEVICE_CONTROL_INVALID", "设备授权控制状态异常", 503)
    if mode == "OFF":
        return
    denial = None
    try:
        if identity is None or identity.claims is None or identity.thumbprint is None:
            raise DeviceAuthError(
                "DEVICE_BOUND_TOKEN_REQUIRED",
                "请更新工作台并校验本机授权后再启动新任务",
                401,
            )
        if (
            identity.claims["user_id"] != user_id
            or identity.claims["cnf"]["jkt"] != identity.thumbprint
        ):
            raise DeviceAuthError(
                "DEVICE_ACCOUNT_MISMATCH", "设备证明与授权上下文不一致", 403
            )
        if int(time.time()) >= identity.claims["exp"]:
            raise DeviceAuthError(
                "AUTH_REFRESH_REQUIRED", "设备凭据已过期，请刷新授权后继续", 401
            )
        # Copy immutable collections into the service contract shape.
        claims = {
            **identity.claims,
            "scopes": list(identity.claims["scopes"]),
            "cnf": dict(identity.claims["cnf"]),
        }
        service.validate_bound_claims(
            db, claims=claims, scope=scope, now=int(time.time())
        )
    except DeviceAuthError as exc:
        if mode == "ENFORCE":
            raise
        denial = exc.code
    if mode == "OBSERVE" and denial:
        db.add(
            WorkbenchDeviceAuditEvent(
                actor_user_id=user_id,
                subject_user_id=user_id,
                device_id=(
                    identity.claims["device_id"]
                    if identity and identity.claims
                    else None
                ),
                grant_id=(
                    identity.claims["grant_id"]
                    if identity and identity.claims
                    else None
                ),
                action="device.observe_unbound_work",
                details_json=canonical_json({"reason": denial, "scope": scope}),
            )
        )
        # Caller commits alongside business work; never commit a task halfway.
