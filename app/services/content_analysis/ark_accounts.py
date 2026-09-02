from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.models import ArkConfig, User
from app.services.security import decrypt_secret, encrypt_secret


ARK_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


def _clean_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("豆包 Ark Base URL 必须是有效的 http:// 或 https:// 地址")
    if len(value) > 500:
        raise ValueError("豆包 Ark Base URL 不能超过 500 个字符")
    return value


def validate_ark_config(
    *,
    enabled: bool,
    base_url: str,
    model: str,
    timeout_seconds: int,
    max_retries: int,
    has_api_key: bool,
) -> tuple[str, str]:
    clean_url = _clean_base_url(base_url)
    clean_model = model.strip()
    if len(clean_model) > 200:
        raise ValueError("豆包 Ark 模型名称不能超过 200 个字符")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise ValueError("豆包 Ark 请求超时必须在 1 到 600 秒之间")
    if type(max_retries) is not int or not 0 <= max_retries <= 5:
        raise ValueError("豆包 Ark 额外重试次数必须在 0 到 5 之间")
    if enabled and not has_api_key:
        raise ValueError("启用豆包内容分析前必须填写 Ark API Key")
    if enabled and not clean_model:
        raise ValueError("启用豆包内容分析前必须填写 Ark 模型")
    return clean_url, clean_model


def save_ark_config(
    db: Session,
    user: User,
    *,
    enabled: bool,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: int,
    max_retries: int,
) -> ArkConfig:
    """Save one user's Ark settings without ever returning the plaintext key."""

    clean_key = api_key.strip()
    if len(clean_key) > 4096:
        raise ValueError("豆包 Ark API Key 不能超过 4096 个字符")
    config = user.ark_config
    existing_encrypted_key = config.api_key_encrypted if config is not None else None
    clean_url, clean_model = validate_ark_config(
        enabled=enabled,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        has_api_key=bool(clean_key or existing_encrypted_key),
    )
    if enabled and not clean_key:
        # Enabling must prove the saved value can still be decrypted with the
        # current application encryption key; merely having ciphertext is not enough.
        decrypt_secret(existing_encrypted_key, label="豆包 Ark API Key")
    if config is None:
        config = ArkConfig(user=user)
    config.enabled = enabled
    config.base_url = clean_url
    config.model = clean_model
    config.timeout_seconds = timeout_seconds
    config.max_retries = max_retries
    if clean_key:
        config.api_key_encrypted = encrypt_secret(clean_key)
    db.add(config)
    return config
