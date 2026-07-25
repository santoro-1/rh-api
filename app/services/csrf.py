from __future__ import annotations

import secrets

from fastapi import Form, HTTPException, Request, status


SESSION_KEY = "_csrf_token"


def get_csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return token


def require_csrf(
    request: Request,
    csrf_token: str | None = Form(None),
) -> None:
    expected = request.session.get(SESSION_KEY)
    supplied = request.headers.get("X-CSRF-Token") or csrf_token
    if (
        not isinstance(expected, str)
        or not isinstance(supplied, str)
        or not secrets.compare_digest(expected, supplied)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="页面安全令牌无效，请刷新页面后重试",
        )
