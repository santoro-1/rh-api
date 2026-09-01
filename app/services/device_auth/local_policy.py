"""Signed, short-lived rollout policy; never a device grant or a cloud token."""

from __future__ import annotations

import re
import secrets

from .errors import DeviceAuthError
from .protocol import sha256_b64
from .tokens import DeviceKeyRing, PRODUCT

POLICY_TYPE = "workbench-local-policy+jwt"
POLICY_AUDIENCE = PRODUCT + ":local-policy"
POLICY_SCHEMA = "publicvideo.local-policy.v1"
POLICY_LIFETIME_SECONDS = 300


def issue_local_policy(
    ring: DeviceKeyRing,
    *,
    user_id: int,
    account_token: str,
    nonce: str,
    mode: str,
    revision: int,
    now: int,
) -> dict:
    if (
        mode not in {"OFF", "OBSERVE", "ENFORCE"}
        or type(revision) is not int
        or revision < 1
    ):
        raise DeviceAuthError("DEVICE_CONTROL_INVALID", "设备授权控制状态异常", 503)
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce):
        raise DeviceAuthError(
            "INVALID_DEVICE_POLICY_NONCE", "本地策略请求标识无效", 422
        )
    claims = {
        "schema": POLICY_SCHEMA,
        "iss": ring.config.issuer,
        "aud": POLICY_AUDIENCE,
        "product": PRODUCT,
        "environment": ring.config.environment,
        "sub": str(user_id),
        "user_id": user_id,
        "ath": sha256_b64(account_token),
        "nonce": nonce,
        "mode": mode,
        "control_revision": revision,
        "iat": now,
        "nbf": now,
        "exp": now + POLICY_LIFETIME_SECONDS,
        "jti": secrets.token_urlsafe(24),
    }
    return {"policy_token": ring.sign(claims, typ=POLICY_TYPE)}
