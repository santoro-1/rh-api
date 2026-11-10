from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time

from starlette.responses import Response


class AdminAuth:
    def __init__(
        self,
        storage_root: Path,
        *,
        username: str = "admin",
        password: str = "",
        session_secret: str = "",
        session_hours: int = 12,
        secure_cookie: bool = False,
        cookie_name: str = "jyd_admin_session",
        password_filename: str = "admin_password.txt",
        secret_filename: str = "admin_session_secret.txt",
    ) -> None:
        self.storage_root = storage_root.resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.username = username.strip() or "admin"
        self.session_seconds = max(1, int(session_hours)) * 3600
        self.secure_cookie = bool(secure_cookie)
        self.cookie_name = cookie_name

        self.password_file = self.storage_root / password_filename
        self.secret_file = self.storage_root / secret_filename
        configured_password = password.strip()
        self.generated_password = not configured_password and not self.password_file.is_file()
        if configured_password:
            self.password = configured_password
            self._store_value(self.password_file, configured_password)
        else:
            generated = secrets.token_urlsafe(14)
            self.password = self._load_or_create(self.password_file, generated)
        self.session_secret = session_secret.strip() or self._load_or_create(
            self.secret_file, secrets.token_urlsafe(32)
        )

    def authenticate(self, username: str, password: str) -> bool:
        return hmac.compare_digest(username, self.username) and hmac.compare_digest(
            password, self.password
        )

    def issue_token(self) -> str:
        payload = {
            "username": self.username,
            "expires_at": int(time.time()) + self.session_seconds,
            "nonce": secrets.token_hex(8),
        }
        encoded = self._encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = self._sign(encoded)
        return f"{encoded}.{signature}"

    def verify_token(self, token: str) -> bool:
        try:
            encoded, signature = token.split(".", 1)
        except ValueError:
            return False
        if not hmac.compare_digest(signature, self._sign(encoded)):
            return False
        try:
            payload = json.loads(self._decode(encoded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("username") == self.username
            and int(payload.get("expires_at", 0)) > int(time.time())
        )

    def set_session_cookie(self, response: Response) -> None:
        response.set_cookie(
            self.cookie_name,
            self.issue_token(),
            max_age=self.session_seconds,
            httponly=True,
            secure=self.secure_cookie,
            samesite="lax",
            path="/",
        )

    def clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(
            self.cookie_name,
            path="/",
            secure=self.secure_cookie,
            httponly=True,
            samesite="lax",
        )

    def _sign(self, encoded: str) -> str:
        digest = hmac.new(
            self.session_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        return self._encode(digest)

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)

    @staticmethod
    def _load_or_create(path: Path, generated_value: str) -> str:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        AdminAuth._store_value(path, generated_value)
        return generated_value

    @staticmethod
    def _store_value(path: Path, value: str) -> None:
        if path.is_file():
            try:
                if path.read_text(encoding="utf-8").strip() == value:
                    return
            except (OSError, UnicodeDecodeError):
                pass
        path.write_text(value + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
