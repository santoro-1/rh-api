from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any

from app.config import Settings
from app.models import User


TOKEN_LIFETIME_SECONDS = 12 * 60 * 60
HANDOFF_LIFETIME_SECONDS = 60


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _password_revision(user: User) -> str:
    return hashlib.sha256(user.password_hash.encode("utf-8")).hexdigest()[:20]


def _signature(settings: Settings, encoded: str) -> str:
    digest = hmac.new(
        settings.app_secret_key.encode("utf-8"),
        f"workbench-auth-v1:{encoded}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _encode(digest)


def issue_workbench_token(user: User, settings: Settings) -> str:
    now = int(time.time())
    payload = {
        "schema": "runninghub.workbench-auth.v1",
        "user_id": user.id,
        "username": user.username,
        "password_revision": _password_revision(user),
        "issued_at": now,
        "expires_at": now + TOKEN_LIFETIME_SECONDS,
        "nonce": secrets.token_hex(8),
    }
    encoded = _encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return f"{encoded}.{_signature(settings, encoded)}"


def decode_workbench_token(token: str, settings: Settings) -> dict[str, Any] | None:
    try:
        encoded, supplied_signature = token.strip().split(".", 1)
        if not hmac.compare_digest(_signature(settings, encoded), supplied_signature):
            return None
        payload = json.loads(_decode(encoded).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("schema") != "runninghub.workbench-auth.v1":
            return None
        if int(payload.get("expires_at", 0)) <= int(time.time()):
            return None
        int(payload["user_id"])
        return payload
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def token_matches_user(payload: dict[str, Any], user: User) -> bool:
    return bool(
        user.is_active
        and int(payload.get("user_id", 0)) == user.id
        and payload.get("username") == user.username
        and payload.get("password_revision") == _password_revision(user)
    )


def public_workbench_user(user: User) -> dict[str, Any]:
    return {
        "user_id": str(user.id),
        "username": user.username,
        "display_name": user.username,
        "enabled": user.is_active,
        "is_admin": user.is_admin,
    }


class WorkbenchHandoffStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, tuple[str, float]] = {}

    def issue(self, access_token: str) -> str:
        now = time.time()
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._purge(now)
            self._tickets[code] = (access_token, now + HANDOFF_LIFETIME_SECONDS)
        return code

    def consume(self, code: str) -> str | None:
        now = time.time()
        with self._lock:
            self._purge(now)
            record = self._tickets.pop(code.strip(), None)
        if record is None or record[1] <= now:
            return None
        return record[0]

    def _purge(self, now: float) -> None:
        for code in [key for key, (_, expiry) in self._tickets.items() if expiry <= now]:
            self._tickets.pop(code, None)


workbench_handoffs = WorkbenchHandoffStore()
