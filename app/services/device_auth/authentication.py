"""Shared HTTP identity/proof adapter for activation and protected workbench APIs."""

from __future__ import annotations

import time
from urllib.parse import quote

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User
from app.services.workbench_auth import decode_workbench_token, token_matches_user
from . import service
from .errors import DeviceAuthError
from .protocol import DeviceProof, verify_proof
from .tokens import ACCESS_TYPE, get_device_auth_config, load_key_ring


def authenticated_account(
    request: Request,
    db: Session,
    *,
    require_bound: bool = False,
    body_token: str | None = None,
) -> tuple[User, str, dict | None]:
    """Bootstrap identity only; this function alone never authorizes new business."""
    authorization = request.headers.get("authorization", "")
    if body_token is not None:
        if not isinstance(body_token, str):
            raise DeviceAuthError("LOGIN_REQUIRED", "登录凭据格式无效", 401)
        if authorization:
            _, _, header_token = authorization.partition(" ")
            if body_token and body_token != header_token:
                raise DeviceAuthError(
                    "AMBIGUOUS_ACCOUNT_TOKEN", "请求包含不一致的账号凭据", 401
                )
        elif body_token:
            authorization = "Bearer " + body_token
    scheme, _, token = authorization.partition(" ")
    if not token or len(token) > 8192 or scheme.lower() not in {"bearer", "dpop"}:
        raise DeviceAuthError("LOGIN_REQUIRED", "请先登录工作台账号", 401)
    bound = None
    if scheme.lower() == "dpop":
        bound = load_key_ring(get_device_auth_config()).verify(
            token, typ=ACCESS_TYPE, now=int(time.time())
        )
        payload = bound
    else:
        if require_bound:
            raise DeviceAuthError(
                "DEVICE_BOUND_TOKEN_REQUIRED", "请先校验此设备的授权", 401
            )
        payload = decode_workbench_token(token, get_settings())
    if not payload:
        raise DeviceAuthError("LOGIN_REQUIRED", "登录已失效，请重新登录", 401)
    user = db.get(User, payload["user_id"], populate_existing=True)
    if user is None or not token_matches_user(payload, user):
        raise DeviceAuthError("LOGIN_REQUIRED", "账号已停用或登录失效", 401)
    return user, token, bound


def consume_request_proof(
    request: Request,
    db: Session,
    *,
    user: User,
    token: str,
    bound: dict | None,
    purpose: str,
) -> DeviceProof:
    config = get_device_auth_config()
    encoded_path = request.scope.get("raw_path")
    try:
        raw_path = (
            encoded_path.decode("ascii")
            if encoded_path
            else quote(request.url.path, safe="/%:@")
        )
    except (UnicodeError, AttributeError) as exc:
        raise DeviceAuthError("INVALID_DEVICE_PROOF", "设备请求地址无效", 401) from exc
    proof = verify_proof(
        request.headers.get("dpop", ""),
        method=request.method,
        uri=config.origin + raw_path,
        access_token=token,
        now=int(time.time()),
        expected_thumbprint=bound["cnf"]["jkt"] if bound else None,
    )
    service.consume_proof(
        db, user_id=user.id, proof=proof, purpose=purpose, now=int(time.time())
    )
    # Keep replay rejection durable even if the later business operation fails.
    # Business callers must authenticate BEFORE staging their own database writes.
    db.commit()
    return proof
