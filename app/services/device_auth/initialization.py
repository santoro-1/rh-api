"""Short-lived software-key setup consent, never a device grant or work token.

The context digest is requester-provided correlation, not hardware attestation.
Actual machine/Windows-user context must be rechecked by the local initializer.
"""

from __future__ import annotations

import re
import secrets

from . import service
from .errors import DeviceAuthError
from .protocol import sha256_b64
from .tokens import PRODUCT

PERMIT_TYPE = "workbench-software-initialization+jwt"
PERMIT_AUDIENCE = PRODUCT + ":software-initialization"
PERMIT_SCHEMA = "publicvideo.software-initialization.v1"
PERMIT_SECONDS = 120


def issue_software_initialization_permit(
    db, ring, *, user_id, account_token, nonce, context_hash, now
):
    if (
        type(user_id) is not int
        or user_id < 1
        or type(now) is not int
        or now < 1
        or not isinstance(account_token, str)
        or not account_token
        or not isinstance(nonce, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce)
        or not isinstance(context_hash, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", context_hash)
    ):
        raise DeviceAuthError(
            "INVALID_DEVICE_INITIALIZATION", "设备初始化请求无效", 422
        )
    # Same lock as admin policy changes. OFF/OBSERVE do not imply software consent.
    service.lock_control(db)
    service._active_user(db, user_id)
    policy = service.policy_for(db, user_id)
    if policy.allow_software is not True:
        raise DeviceAuthError(
            "DEVICE_SOFTWARE_NOT_ALLOWED",
            "请先由网站管理员明确允许此账号使用软件保护设备",
        )
    claims = {
        "schema": PERMIT_SCHEMA,
        "iss": ring.config.issuer,
        "aud": PERMIT_AUDIENCE,
        "product": PRODUCT,
        "environment": ring.config.environment,
        "sub": str(user_id),
        "user_id": user_id,
        "ath": sha256_b64(account_token),
        "nonce": nonce,
        "context_hash": context_hash,
        "action": "initialize-software-key",
        "software_allowed": True,
        "policy_revision": policy.revision,
        "iat": now,
        "nbf": now,
        "exp": now + PERMIT_SECONDS,
        "jti": secrets.token_urlsafe(24),
    }
    result = {"initialization_permit": ring.sign(claims, typ=PERMIT_TYPE)}
    service._audit(
        db,
        action="device.software_initialization_permitted",
        actor=user_id,
        subject=user_id,
        details={
            "policy_revision": policy.revision,
            "context_hash": context_hash,
            "expires_at": claims["exp"],
        },
    )
    # Caller commits atomically; no device, grant, task or seat allocation here.
    return result
