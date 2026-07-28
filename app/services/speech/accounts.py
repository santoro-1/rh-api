from __future__ import annotations

import hashlib
import uuid

from sqlalchemy.orm import Session

from app.models import MiniMaxConfig, User
from app.services.security import encrypt_secret


def credential_fingerprint(api_key: str) -> str:
    """Identify an API account without storing or displaying its secret."""

    return hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()


def validate_minimax_config(
    base_url: str,
    requests_per_minute: int,
) -> None:
    if not base_url.startswith(("https://", "http://")):
        raise ValueError("MiniMax Base URL 必须以 http:// 或 https:// 开头")
    if not 1 <= requests_per_minute <= 60:
        raise ValueError("MiniMax 每分钟请求数必须在 1 到 60 之间")


def save_minimax_config(
    db: Session,
    user: User,
    *,
    api_key: str,
    base_url: str,
    requests_per_minute: int,
    account_label: str = "MiniMax 账号",
    start_new_account_binding: bool = False,
) -> MiniMaxConfig:
    """Save a credential while preserving a stable provider-account binding."""

    clean_url = base_url.strip().rstrip("/")
    validate_minimax_config(
        clean_url,
        requests_per_minute,
    )
    config = user.minimax_config
    if config is None:
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=None,
            account_binding_id=str(uuid.uuid4()),
            account_label=account_label.strip() or "MiniMax 账号",
            base_url=clean_url,
            requests_per_minute=requests_per_minute,
        )
    else:
        config.base_url = clean_url
        config.requests_per_minute = requests_per_minute
        config.account_label = account_label.strip() or "MiniMax 账号"
        if start_new_account_binding:
            if not api_key.strip():
                raise ValueError("切换到新的 MiniMax 账号时必须填写新的 API Key")
            config.account_binding_id = str(uuid.uuid4())
    if api_key.strip():
        config.api_key_encrypted = encrypt_secret(api_key.strip())
        config.credential_fingerprint = credential_fingerprint(api_key)
    db.add(config)
    return config
