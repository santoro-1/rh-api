from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from auth_store import OneTimeHandoffStore, UserStore


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
DATA_DIR = Path(os.environ.get("JYD_AUTH_DATA_DIR", APP_ROOT / "data")).expanduser().resolve()
ADMIN_USERNAME = os.environ.get("JYD_AUTH_ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.environ.get("JYD_AUTH_ADMIN_PASSWORD", "")
COOKIE_SECURE = os.environ.get("JYD_AUTH_COOKIE_SECURE", "true").strip().lower() in {"1", "true", "yes", "on"}
SESSION_HOURS = max(1, int(os.environ.get("JYD_AUTH_SESSION_HOURS", "12")))
ALLOWED_HOSTS = [
    item.strip()
    for item in os.environ.get(
        "JYD_AUTH_ALLOWED_HOSTS", "auth.lanyingjk01.com,127.0.0.1,localhost,testserver"
    ).split(",")
    if item.strip()
]

if not ADMIN_PASSWORD:
    raise RuntimeError("必须设置 JYD_AUTH_ADMIN_PASSWORD")

DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_or_create_secret(path: Path) -> str:
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(48)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return value


ADMIN_SESSION_SECRET = _load_or_create_secret(DATA_DIR / "admin_session_secret.txt")
ADMIN_COOKIE = "jyd_cloud_admin"
ADMIN_SESSION_SECONDS = SESSION_HOURS * 3600
users = UserStore(DATA_DIR, session_hours=SESSION_HOURS)
handoffs = OneTimeHandoffStore(lifetime_seconds=60)


class LoginLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        now = time.time()
        with self._lock:
            recent = [stamp for stamp in self._failures.get(key, []) if now - stamp < 300]
            self._failures[key] = recent
            if len(recent) >= 8:
                raise HTTPException(status_code=429, detail="登录失败次数过多，请五分钟后再试")

    def fail(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.time())

    def success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


login_limiter = LoginLimiter()
app = FastAPI(title="JYD Auth Center", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def _admin_sign(encoded: str) -> str:
    digest = hmac.new(
        ADMIN_SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _issue_admin_token() -> str:
    payload = {
        "username": ADMIN_USERNAME,
        "expires_at": int(time.time()) + ADMIN_SESSION_SECONDS,
        "nonce": secrets.token_hex(8),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    return f"{encoded}.{_admin_sign(encoded)}"


def _verify_admin_token(token: str) -> bool:
    try:
        encoded, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, _admin_sign(encoded)):
            return False
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("username") == ADMIN_USERNAME
        and int(payload.get("expires_at", 0)) > int(time.time())
    )


def _require_admin(request: Request) -> None:
    if not _verify_admin_token(request.cookies.get(ADMIN_COOKIE, "")):
        raise HTTPException(status_code=401, detail="管理员登录已失效")


def _login_key(request: Request, username: str) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    client = forwarded or (request.client.host if request.client else "unknown")
    return f"{client}:{username.strip().casefold()}"


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=303)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "jyd-auth-center", "accounts": len(users.list_users())}


@app.get("/admin/login")
def admin_login_page(request: Request) -> Response:
    if _verify_admin_token(request.cookies.get(ADMIN_COOKIE, "")):
        return RedirectResponse("/admin", status_code=303)
    return FileResponse(STATIC_ROOT / "admin-login.html")


@app.get("/admin")
def admin_page(request: Request) -> Response:
    if not _verify_admin_token(request.cookies.get(ADMIN_COOKIE, "")):
        return RedirectResponse("/admin/login", status_code=303)
    return FileResponse(STATIC_ROOT / "admin.html")


@app.post("/api/admin/login")
def admin_login(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    key = _login_key(request, username)
    login_limiter.check(key)
    if not (
        hmac.compare_digest(username, ADMIN_USERNAME)
        and hmac.compare_digest(password, ADMIN_PASSWORD)
    ):
        login_limiter.fail(key)
        raise HTTPException(status_code=401, detail="管理员账号或密码错误")
    login_limiter.success(key)
    response = JSONResponse({"ok": True, "username": ADMIN_USERNAME})
    response.set_cookie(
        ADMIN_COOKIE,
        _issue_admin_token(),
        max_age=ADMIN_SESSION_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return response


@app.get("/api/admin/session")
def admin_session(request: Request) -> dict[str, Any]:
    valid = _verify_admin_token(request.cookies.get(ADMIN_COOKIE, ""))
    return {"authenticated": valid, "username": ADMIN_USERNAME if valid else ""}


@app.post("/api/admin/logout")
def admin_logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(
        ADMIN_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True, samesite="strict"
    )
    return response


@app.get("/api/admin/users")
def list_users(request: Request) -> dict[str, Any]:
    _require_admin(request)
    records = users.list_users()
    return {
        "users": records,
        "total": len(records),
        "enabled": sum(1 for item in records if item.get("enabled")),
    }


@app.post("/api/admin/users")
def create_user(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _require_admin(request)
    try:
        return users.create_user(
            str(payload.get("username", "")),
            str(payload.get("password", "")),
            display_name=str(payload.get("display_name", "")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/admin/users/{user_id}")
def update_user(user_id: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _require_admin(request)
    enabled = payload.get("enabled") if "enabled" in payload else None
    if enabled is not None and not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled 必须是布尔值")
    try:
        return users.update_user(
            user_id,
            display_name=str(payload.get("display_name", "")) if "display_name" in payload else None,
            enabled=enabled,
            password=str(payload.get("password", "")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: str, request: Request) -> dict[str, Any]:
    _require_admin(request)
    try:
        return {"deleted": True, "user": users.delete_user(user_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/auth/center/login")
def center_login(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    username = str(payload.get("username", ""))
    key = _login_key(request, username)
    login_limiter.check(key)
    user = users.authenticate(username, str(payload.get("password", "")))
    if user is None:
        login_limiter.fail(key)
        raise HTTPException(status_code=401, detail="账号或密码错误")
    login_limiter.success(key)
    return {"ok": True, "access_token": users.issue_token(user), "user": user}


@app.post("/api/auth/center/verify")
def center_verify(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    user = users.verify_token(str(payload.get("access_token", "")))
    if user is None:
        raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
    return {"valid": True, "user": user}


@app.post("/api/auth/center/handoff")
def center_handoff(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    access_token = str(payload.get("access_token", ""))
    if users.verify_token(access_token) is None:
        raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
    return {
        "handoff_code": handoffs.issue(access_token),
        "expires_in": handoffs.lifetime_seconds,
    }


@app.post("/api/auth/center/handoff/consume")
def center_consume_handoff(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    access_token = handoffs.consume(str(payload.get("handoff_code", "")))
    user = users.verify_token(access_token or "")
    if user is None:
        raise HTTPException(status_code=401, detail="登录接力码无效或已过期")
    return {"access_token": access_token, "user": user}
