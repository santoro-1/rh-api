from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet


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


def _as_csv(value: str | None, default: str) -> tuple[str, ...]:
    items = [item.strip() for item in (value or default).split(",") if item.strip()]
    return tuple(dict.fromkeys(items))


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
    runninghub_auto_retry_limit: int
    runninghub_auto_retry_base_delay_seconds: int
    max_image_size_mb: int
    max_audio_size_mb: int
    max_video_size_mb: int
    upload_retention_days: int
    output_retention_days: int
    cookie_secure: bool
    allowed_hosts: tuple[str, ...]
    login_rate_limit_per_minute: int
    task_create_rate_limit_per_minute: int
    max_batch_items: int
    max_batch_total_upload_mb: int
    staged_asset_retention_hours: int
    minimax_default_base_url: str
    minimax_request_timeout_seconds: int
    temporary_voice_retention_hours: int
    long_audio_alignment_provider: str
    asr_base_url: str
    asr_shared_token: str
    asr_request_timeout_seconds: int
    media_processing_mode: str
    media_worker_token: str
    media_worker_lease_seconds: int
    media_worker_archive_limit_mb: int
    log_retention_days: int
    log_max_bytes: int

    @classmethod
    def from_environment(cls) -> "Settings":
        app_env = os.getenv("APP_ENV", "development").strip().lower()
        if app_env not in {"development", "test", "production"}:
            raise ValueError("APP_ENV 只能为 development、test 或 production")

        secret = os.getenv("APP_SECRET_KEY", "development-only-change-me")
        cookie_secure = _as_bool(os.getenv("COOKIE_SECURE"), False)
        allowed_hosts = _as_csv(
            os.getenv("ALLOWED_HOSTS"),
            "localhost,127.0.0.1,testserver",
        )
        if app_env == "production":
            if (
                len(secret) < 32
                or secret == "development-only-change-me"
                or secret.lower().startswith(("change-", "replace-"))
            ):
                raise ValueError("生产环境必须设置至少 32 个字符的随机 APP_SECRET_KEY")
            if not cookie_secure:
                raise ValueError("生产环境必须设置 COOKIE_SECURE=true")
            if not allowed_hosts or "*" in allowed_hosts:
                raise ValueError("生产环境必须在 ALLOWED_HOSTS 中填写明确域名")

        configured_encryption_key = os.getenv("APP_ENCRYPTION_KEY", "").strip()
        if configured_encryption_key:
            encryption_key = configured_encryption_key
            try:
                Fernet(encryption_key.encode("ascii"))
            except (ValueError, TypeError) as exc:
                raise ValueError("APP_ENCRYPTION_KEY 必须是有效的 Fernet Key") from exc
        elif app_env in {"development", "test"}:
            # Local convenience only. Production must explicitly provide a stable key.
            encryption_key = base64.urlsafe_b64encode(
                hashlib.sha256(secret.encode("utf-8")).digest()
            ).decode("ascii")
        else:
            raise ValueError("生产环境必须设置 APP_ENCRYPTION_KEY")

        instance_type = os.getenv("DEFAULT_RUNNINGHUB_INSTANCE_TYPE", "default").strip()
        if instance_type not in {"default", "plus"}:
            raise ValueError("DEFAULT_RUNNINGHUB_INSTANCE_TYPE 只能为 default 或 plus")
        media_processing_mode = os.getenv(
            "MEDIA_PROCESSING_MODE", "local"
        ).strip().lower()
        if media_processing_mode not in {"local", "remote"}:
            raise ValueError("MEDIA_PROCESSING_MODE 只能为 local 或 remote")
        media_worker_token = os.getenv("MEDIA_WORKER_TOKEN", "").strip()
        if (
            app_env == "production"
            and media_processing_mode == "remote"
            and (
                len(media_worker_token) < 32
                or media_worker_token.lower().startswith(
                    ("change-", "replace-")
                )
            )
        ):
            raise ValueError(
                "远程媒体节点模式必须设置至少 32 个字符的 MEDIA_WORKER_TOKEN"
            )
        media_worker_lease_seconds = _as_int(
            "MEDIA_WORKER_LEASE_SECONDS", 1800
        )
        if media_worker_lease_seconds < 120:
            raise ValueError("MEDIA_WORKER_LEASE_SECONDS 不能小于 120")
        media_worker_archive_limit_mb = _as_int(
            "MEDIA_WORKER_ARCHIVE_LIMIT_MB", 500
        )
        if not 1 <= media_worker_archive_limit_mb <= 500:
            raise ValueError(
                "MEDIA_WORKER_ARCHIVE_LIMIT_MB 必须为 1-500"
            )
        runninghub_auto_retry_limit = _as_int(
            "RUNNINGHUB_AUTO_RETRY_LIMIT", 3
        )
        if not 0 <= runninghub_auto_retry_limit <= 10:
            raise ValueError("RUNNINGHUB_AUTO_RETRY_LIMIT 必须为 0-10")
        runninghub_auto_retry_base_delay_seconds = _as_int(
            "RUNNINGHUB_AUTO_RETRY_BASE_DELAY_SECONDS", 60
        )
        if not 1 <= runninghub_auto_retry_base_delay_seconds <= 3600:
            raise ValueError(
                "RUNNINGHUB_AUTO_RETRY_BASE_DELAY_SECONDS 必须为 1-3600"
            )

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
            runninghub_auto_retry_limit=runninghub_auto_retry_limit,
            runninghub_auto_retry_base_delay_seconds=(
                runninghub_auto_retry_base_delay_seconds
            ),
            max_image_size_mb=_as_int("MAX_IMAGE_SIZE_MB", 20),
            max_audio_size_mb=_as_int("MAX_AUDIO_SIZE_MB", 100),
            max_video_size_mb=_as_int("MAX_VIDEO_SIZE_MB", 500),
            upload_retention_days=_as_int("UPLOAD_RETENTION_DAYS", 2),
            output_retention_days=_as_int("OUTPUT_RETENTION_DAYS", 7),
            cookie_secure=cookie_secure,
            allowed_hosts=allowed_hosts,
            login_rate_limit_per_minute=_as_int("LOGIN_RATE_LIMIT_PER_MINUTE", 10),
            task_create_rate_limit_per_minute=_as_int(
                "TASK_CREATE_RATE_LIMIT_PER_MINUTE", 10
            ),
            max_batch_items=_as_int("MAX_BATCH_ITEMS", 50),
            max_batch_total_upload_mb=_as_int(
                "MAX_BATCH_TOTAL_UPLOAD_MB", 5120
            ),
            staged_asset_retention_hours=_as_int(
                "STAGED_ASSET_RETENTION_HOURS", 24
            ),
            minimax_default_base_url=os.getenv(
                "MINIMAX_DEFAULT_BASE_URL", "https://api.minimaxi.com"
            ).rstrip("/"),
            minimax_request_timeout_seconds=_as_int(
                "MINIMAX_REQUEST_TIMEOUT_SECONDS", 120
            ),
            temporary_voice_retention_hours=_as_int(
                "TEMPORARY_VOICE_RETENTION_HOURS", 48
            ),
            long_audio_alignment_provider=os.getenv(
                "LONG_AUDIO_ALIGNMENT_PROVIDER", "funasr_http"
            ).strip(),
            asr_base_url=os.getenv(
                "ASR_BASE_URL", "http://127.0.0.1:18084"
            ).rstrip("/"),
            asr_shared_token=os.getenv("ASR_SHARED_TOKEN", "").strip(),
            asr_request_timeout_seconds=_as_int(
                "ASR_REQUEST_TIMEOUT_SECONDS", 1800
            ),
            media_processing_mode=media_processing_mode,
            media_worker_token=media_worker_token,
            media_worker_lease_seconds=media_worker_lease_seconds,
            media_worker_archive_limit_mb=media_worker_archive_limit_mb,
            log_retention_days=_as_int("LOG_RETENTION_DAYS", 7),
            log_max_bytes=_as_int("LOG_MAX_BYTES", 10 * 1024 * 1024),
        )

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def staged_assets_dir(self) -> Path:
        return self.data_dir / "staged-assets"

    @property
    def voice_sources_dir(self) -> Path:
        return self.data_dir / "voice-sources"

    @property
    def voice_creations_dir(self) -> Path:
        return self.data_dir / "voice-creations"

    @property
    def long_audio_dir(self) -> Path:
        return self.data_dir / "long-audio"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def runtime_dir(self) -> Path:
        return self.data_dir / "runtime"


@lru_cache
def get_settings() -> Settings:
    return Settings.from_environment()
