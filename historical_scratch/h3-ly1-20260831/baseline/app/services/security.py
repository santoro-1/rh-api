from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken
from passlib.context import CryptContext

from app.config import get_settings


password_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_context.verify(password, password_hash)


def _fernet() -> Fernet:
    return Fernet(get_settings().app_encryption_key.encode("ascii"))


def encrypt_secret(value: str) -> str:
    if not value.strip():
        raise ValueError("密钥不能为空")
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(
    value: str | None,
    *,
    label: str = "RunningHub API Key",
) -> str:
    if not value:
        raise ValueError(f"尚未配置{label}")
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise ValueError(f"保存的{label}无法解密") from exc


def mask_secret(value: str | None) -> str:
    if not value:
        return "未配置"
    return "已加密保存"


def secret_fingerprint(value: str) -> str:
    """Return a stable one-way identifier without exposing a credential."""

    clean_value = value.strip()
    if not clean_value:
        raise ValueError("密钥不能为空")
    return hashlib.sha256(clean_value.encode("utf-8")).hexdigest()
