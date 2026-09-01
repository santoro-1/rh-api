"""Single-request central-Agent permits, never cloud generation credentials.

Execution follows the current rollout/grant policy. Reporting can prove the
original key after a grant is stopped; the central queue must still match the
already-assigned account/key/job. A report permit never admits a new task.
"""

from __future__ import annotations

import json
import re
import secrets

from . import service
from .errors import DeviceAuthError
from .tokens import PRODUCT

PERMIT_TYPE = "workbench-agent-request+jwt"
PERMIT_AUDIENCE = PRODUCT + ":agent-request"
PERMIT_SCHEMA = "publicvideo.agent-request.v1"
PERMIT_SECONDS = 90
LOCAL_SCOPES = frozenset({"local:draft", "local:render"})


def issue_agent_permit(
    db, ring, *, user_id, bound, thumbprint, nonce, context_hash, intent, now
):
    if (
        type(user_id) is not int
        or user_id < 1
        or type(now) is not int
        or now < 1
        or intent not in {"execute", "report"}
        or not isinstance(nonce, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce)
        or not isinstance(context_hash, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", context_hash)
        or (
            thumbprint is not None
            and (
                not isinstance(thumbprint, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", thumbprint)
            )
        )
    ):
        raise DeviceAuthError("INVALID_AGENT_REQUEST", "处理机授权请求无效", 422)
    control = service.lock_control(db)
    service._active_user(db, user_id)
    if control.mode not in {"OFF", "OBSERVE", "ENFORCE"}:
        raise DeviceAuthError("DEVICE_CONTROL_INVALID", "设备授权控制状态异常", 503)
    if bound is not None and (
        bound["user_id"] != user_id or bound["cnf"]["jkt"] != thumbprint
    ):
        raise DeviceAuthError(
            "DEVICE_ACCOUNT_MISMATCH", "处理机账号与设备证明不一致", 403
        )

    scopes = set(LOCAL_SCOPES) if intent == "execute" else set()
    device = grant = None
    if thumbprint is not None:
        device, grant = service.find_registration(
            db, user_id=user_id, thumbprint=thumbprint
        )
    if intent == "execute" and control.mode == "ENFORCE":
        if bound is None or thumbprint is None:
            raise DeviceAuthError(
                "DEVICE_BOUND_TOKEN_REQUIRED", "请先校验执行机的设备授权", 401
            )
        device, grant, _ = service.validate_bound_claims(db, claims=bound, now=now)
        scopes = LOCAL_SCOPES.intersection(json.loads(grant.scopes_json)).intersection(
            bound["scopes"]
        )
        if not scopes:
            raise DeviceAuthError("DEVICE_SCOPE_DENIED", "此设备没有本地执行权限", 403)
    if intent == "report" and control.mode == "ENFORCE" and thumbprint is None:
        raise DeviceAuthError(
            "DEVICE_KEY_PROOF_REQUIRED", "回报原任务需要原执行机的持钥证明", 401
        )

    expires = min(
        now + PERMIT_SECONDS,
        bound["exp"] if bound is not None else now + PERMIT_SECONDS,
    )
    if (
        intent == "execute"
        and control.mode == "ENFORCE"
        and grant.expires_at is not None
    ):
        expires = min(expires, grant.expires_at)
    if expires <= now:
        raise DeviceAuthError(
            "AUTH_REFRESH_REQUIRED", "设备凭据已过期，请重新校验", 401
        )
    claims = {
        "schema": PERMIT_SCHEMA,
        "iss": ring.config.issuer,
        "aud": PERMIT_AUDIENCE,
        "product": PRODUCT,
        "environment": ring.config.environment,
        "sub": str(user_id),
        "user_id": user_id,
        "intent": intent,
        "nonce": nonce,
        "context_hash": context_hash,
        "mode": control.mode,
        "control_revision": control.revision,
        "scopes": sorted(scopes),
        "cnf": {"jkt": thumbprint} if thumbprint is not None else None,
        "device_id": device.id if device else None,
        "grant_id": grant.id if grant else None,
        "iat": now,
        "nbf": now,
        "exp": expires,
        "jti": secrets.token_urlsafe(24),
    }
    return {"agent_permit": ring.sign(claims, typ=PERMIT_TYPE)}
