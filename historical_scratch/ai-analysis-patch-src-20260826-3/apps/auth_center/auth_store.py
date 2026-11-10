from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any


USERNAME_PATTERN = re.compile(r"^[^\s/\\:;]{2,40}$")
PBKDF2_ITERATIONS = 310_000


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class UserStore:
    """Small atomic JSON account store for a single-process auth service."""

    def __init__(self, data_dir: Path, *, session_hours: int = 12) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.users_path = self.data_dir / "users.json"
        self.secret_path = self.data_dir / "user_session_secret.txt"
        self.session_seconds = max(1, int(session_hours)) * 3600
        self._lock = threading.RLock()
        self.session_secret = self._load_or_create_secret(self.secret_path)

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        normalized = username.strip().casefold()
        with self._lock:
            record = next(
                (
                    item
                    for item in self._load()["users"]
                    if str(item.get("username", "")).casefold() == normalized
                ),
                None,
            )
        if not record or not record.get("enabled", True):
            return None
        if not self._verify_password(password, record):
            return None
        return self._public(record)

    def issue_token(self, user: dict[str, Any]) -> str:
        payload = {
            "user_id": str(user["user_id"]),
            "username": str(user["username"]),
            "session_version": int(user.get("session_version", 1)),
            "expires_at": int(time.time()) + self.session_seconds,
            "nonce": secrets.token_hex(8),
        }
        encoded = _encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return f"{encoded}.{self._sign(encoded)}"

    def verify_token(self, token: str) -> dict[str, Any] | None:
        try:
            encoded, signature = token.split(".", 1)
        except ValueError:
            return None
        if not hmac.compare_digest(signature, self._sign(encoded)):
            return None
        try:
            payload = json.loads(_decode(encoded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or int(payload.get("expires_at", 0)) <= int(time.time()):
            return None
        with self._lock:
            record = next(
                (
                    item
                    for item in self._load()["users"]
                    if item.get("user_id") == payload.get("user_id")
                ),
                None,
            )
        if (
            not record
            or not record.get("enabled", True)
            or record.get("username") != payload.get("username")
            or int(record.get("session_version", 1)) != int(payload.get("session_version", 0))
        ):
            return None
        return self._public(record)

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public(item) for item in self._load()["users"]]

    def create_user(self, username: str, password: str, *, display_name: str = "") -> dict[str, Any]:
        clean_username = self._validate_username(username)
        clean_password = self._validate_password(password)
        with self._lock:
            data = self._load()
            if any(
                str(item.get("username", "")).casefold() == clean_username.casefold()
                for item in data["users"]
            ):
                raise ValueError(f"账号已存在：{clean_username}")
            record = self._new_record(clean_username, clean_password, display_name)
            data["users"].append(record)
            self._save(data)
            return self._public(record)

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
        password: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            record = self._find(data, user_id)
            if display_name is not None:
                record["display_name"] = display_name.strip()[:80]
            if enabled is not None and bool(record.get("enabled", True)) != bool(enabled):
                record["enabled"] = bool(enabled)
                record["session_version"] = int(record.get("session_version", 1)) + 1
            if password:
                record.update(self._password_fields(self._validate_password(password)))
                record["password_changed_at"] = _now()
                record["session_version"] = int(record.get("session_version", 1)) + 1
            record["updated_at"] = _now()
            self._save(data)
            return self._public(record)

    def delete_user(self, user_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            record = self._find(data, user_id)
            data["users"] = [item for item in data["users"] if item.get("user_id") != user_id]
            self._save(data)
            return self._public(record)

    @staticmethod
    def _find(data: dict[str, Any], user_id: str) -> dict[str, Any]:
        record = next((item for item in data["users"] if item.get("user_id") == user_id), None)
        if record is None:
            raise KeyError(f"用户不存在：{user_id}")
        return record

    def _new_record(self, username: str, password: str, display_name: str) -> dict[str, Any]:
        now = _now()
        return {
            "user_id": secrets.token_hex(12),
            "username": username,
            "display_name": display_name.strip()[:80],
            "enabled": True,
            "session_version": 1,
            "created_at": now,
            "updated_at": now,
            **self._password_fields(password),
        }

    @staticmethod
    def _password_fields(password: str) -> dict[str, Any]:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return {
            "password_algorithm": "pbkdf2_sha256",
            "password_iterations": PBKDF2_ITERATIONS,
            "password_salt": _encode(salt),
            "password_hash": _encode(digest),
        }

    @staticmethod
    def _verify_password(password: str, record: dict[str, Any]) -> bool:
        try:
            salt = _decode(str(record["password_salt"]))
            iterations = int(record.get("password_iterations", PBKDF2_ITERATIONS))
            expected = str(record["password_hash"])
        except (KeyError, ValueError, TypeError):
            return False
        actual = _encode(hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations))
        return hmac.compare_digest(actual, expected)

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "user_id",
                "username",
                "display_name",
                "enabled",
                "session_version",
                "created_at",
                "updated_at",
                "password_changed_at",
            )
            if key in record
        }

    @staticmethod
    def _validate_username(value: str) -> str:
        username = value.strip()
        if not USERNAME_PATTERN.fullmatch(username):
            raise ValueError("账号需为 2-40 个字符，不能包含空格、斜杠、冒号或分号")
        return username

    @staticmethod
    def _validate_password(value: str) -> str:
        if len(value) < 8:
            raise ValueError("密码至少需要 8 个字符")
        if len(value) > 128:
            raise ValueError("密码不能超过 128 个字符")
        return value

    def _load(self) -> dict[str, Any]:
        if not self.users_path.is_file():
            return {"schema": "jyd.cloud_accounts.v1", "users": []}
        try:
            data = json.loads(self.users_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("用户账号文件损坏") from exc
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            raise RuntimeError("用户账号文件格式错误")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        data["schema"] = "jyd.cloud_accounts.v1"
        data["updated_at"] = _now()
        temporary = self.users_path.with_suffix(f".tmp.{secrets.token_hex(6)}")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.users_path)

    def _sign(self, encoded: str) -> str:
        digest = hmac.new(
            self.session_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        return _encode(digest)

    @staticmethod
    def _load_or_create_secret(path: Path) -> str:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(48)
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
        return value


class OneTimeHandoffStore:
    def __init__(self, *, lifetime_seconds: int = 60, max_pending: int = 2048) -> None:
        self.lifetime_seconds = max(15, int(lifetime_seconds))
        self.max_pending = max(16, int(max_pending))
        self._lock = threading.Lock()
        self._tickets: dict[str, tuple[str, float]] = {}

    def issue(self, access_token: str) -> str:
        now = time.time()
        with self._lock:
            self._purge(now)
            if len(self._tickets) >= self.max_pending:
                oldest = min(self._tickets, key=lambda code: self._tickets[code][1])
                self._tickets.pop(oldest, None)
            code = secrets.token_urlsafe(32)
            self._tickets[code] = (access_token, now + self.lifetime_seconds)
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
