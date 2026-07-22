from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_local_dotenv() -> None:
    """Read a small local .env file without adding a runtime dependency."""
    dotenv_path = PROJECT_ROOT / ".env"
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_dotenv()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_secret_key: str
    app_encryption_key: str
    database_url: str
    data_dir: Path
    runninghub_base_url: str
    default_runninghub_ai_app_id: str
    default_runninghub_instance_type: str
    default_use_personal_queue: bool
    poll_interval_seconds: int
    runninghub_task_timeout_seconds: int
    max_image_size_mb: int
    max_audio_size_mb: int
    upload_retention_days: int
    output_retention_days: int
    cookie_secure: bool
    login_rate_limit_per_minute: int
    task_create_rate_limit_per_minute: int

    @classmethod
    def from_environment(cls) -> "Settings":
        app_env = os.getenv("APP_ENV", "development").strip().lower()
        secret = os.getenv("APP_SECRET_KEY", "development-only-change-me")
        configured_encryption_key = os.getenv("APP_ENCRYPTION_KEY", "").strip()
        if configured_encryption_key:
            encryption_key = configured_encryption_key
        elif app_env == "development":
            # Local convenience only. Production must explicitly provide a stable key.
            encryption_key = base64.urlsafe_b64encode(
                hashlib.sha256(secret.encode("utf-8")).digest()
            ).decode("ascii")
        else:
            raise ValueError("生产环境必须设置 APP_ENCRYPTION_KEY")

        instance_type = os.getenv("DEFAULT_RUNNINGHUB_INSTANCE_TYPE", "plus").strip()
        if instance_type not in {"default", "plus"}:
            raise ValueError("DEFAULT_RUNNINGHUB_INSTANCE_TYPE 只能为 default 或 plus")

        return cls(
            app_env=app_env,
            app_secret_key=secret,
            app_encryption_key=encryption_key,
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/app.db"),
            data_dir=_resolve_path(os.getenv("DATA_DIR", "./data")),
            runninghub_base_url=os.getenv(
                "RUNNINGHUB_BASE_URL", "https://www.runninghub.cn"
            ).rstrip("/"),
            default_runninghub_ai_app_id=os.getenv(
                "DEFAULT_RUNNINGHUB_AI_APP_ID", "2062251097452007426"
            ),
            default_runninghub_instance_type=instance_type,
            default_use_personal_queue=_as_bool(
                os.getenv("DEFAULT_USE_PERSONAL_QUEUE"), False
            ),
            poll_interval_seconds=_as_int("POLL_INTERVAL_SECONDS", 5),
            runninghub_task_timeout_seconds=_as_int(
                "RUNNINGHUB_TASK_TIMEOUT_SECONDS", 3600
            ),
            max_image_size_mb=_as_int("MAX_IMAGE_SIZE_MB", 20),
            max_audio_size_mb=_as_int("MAX_AUDIO_SIZE_MB", 100),
            upload_retention_days=_as_int("UPLOAD_RETENTION_DAYS", 3),
            output_retention_days=_as_int("OUTPUT_RETENTION_DAYS", 7),
            cookie_secure=_as_bool(os.getenv("COOKIE_SECURE"), False),
            login_rate_limit_per_minute=_as_int("LOGIN_RATE_LIMIT_PER_MINUTE", 10),
            task_create_rate_limit_per_minute=_as_int(
                "TASK_CREATE_RATE_LIMIT_PER_MINUTE", 10
            ),
        )

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"


@lru_cache
def get_settings() -> Settings:
    return Settings.from_environment()
