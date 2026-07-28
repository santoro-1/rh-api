from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.config import get_settings


_SECRET_PATTERNS = (
    re.compile(
        r"""(?ix)
        (authorization["']?\s*[:=]\s*["']?\s*bearer\s+)
        [^"',\s}]+
        """
    ),
    re.compile(
        r"""(?ix)
        (
          (?:api[_-]?key|access[_-]?password)
          (?:[_-]?encrypted)?
          ["']?\s*[:=]\s*["']?
        )
        [^"',\s}]+
        """
    ),
)
_HEARTBEAT_WRITE_LOCK = threading.Lock()
EVENT_PREFIX = "[EVENT"


def log_event(
    target_logger: logging.Logger,
    event_code: str,
    message: str,
    *,
    level: int = logging.INFO,
    **details: object,
) -> None:
    """Write one operator-facing state change with searchable structured data.

    event_code is a stable machine-readable identifier used by the admin log
    stream. message is the short Chinese explanation shown to administrators.
    details contains safe identifiers and state values, never credentials.
    """

    safe_details = {
        key: value
        for key, value in details.items()
        if value is not None
    }
    suffix = (
        " "
        + json.dumps(
            safe_details,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        if safe_details
        else ""
    )
    target_logger.log(
        level,
        f"{EVENT_PREFIX} %s] %s%s",
        event_code,
        message,
        suffix,
    )


class SecretRedactionFilter(logging.Filter):
    """Mask credential-shaped values before any handler writes them."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern in _SECRET_PATTERNS:
            message = pattern.sub(r"\1***", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(service_name: str, *, console: bool = True) -> Path:
    settings = get_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.logs_dir / f"{service_name}.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    marker = f"runninghub:{service_name}"
    if not any(getattr(handler, "_runninghub_marker", "") == marker for handler in root.handlers):
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
        file_handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            backupCount=max(settings.log_retention_days, 1),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(SecretRedactionFilter())
        file_handler._runninghub_marker = marker  # type: ignore[attr-defined]
        root.addHandler(file_handler)
        if console and not root.handlers[:-1]:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            console_handler.addFilter(SecretRedactionFilter())
            root.addHandler(console_handler)
    return log_path


def write_heartbeat(service_name: str, **details: object) -> None:
    settings = get_settings()
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    path = settings.runtime_dir / f"{service_name}.heartbeat.json"
    temporary = path.with_suffix(".tmp")
    payload = {
        "service": service_name,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    with _HEARTBEAT_WRITE_LOCK:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)


def start_heartbeat(service_name: str, interval_seconds: float = 5) -> None:
    """Keep service status fresh while an external API call is blocking."""

    def report() -> None:
        while True:
            try:
                write_heartbeat(service_name)
            except OSError:
                logging.getLogger(__name__).exception("服务心跳写入失败")
            time.sleep(max(interval_seconds, 1))

    threading.Thread(
        target=report,
        name=f"{service_name}-heartbeat",
        daemon=True,
    ).start()
