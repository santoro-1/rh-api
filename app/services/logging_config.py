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
        (https?://)
        [^/\s:@]+:[^@\s/]+@
        """
    ),
    re.compile(
        r"""(?ix)
        (
          [?&](?:api[_-]?key|token|access[_-]?password)=
        )
        [^&\s]+
        """
    ),
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


class BoundedTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Rotate at midnight or at a size limit and bound archive count.

    Each service owns its own file, so the handler does not need cross-process
    locking. Size-triggered archives use a timestamp suffix while the normal
    midnight archive keeps the standard date suffix.
    """

    def __init__(
        self,
        filename: Path,
        *,
        retention_days: int,
        max_bytes: int,
        encoding: str = "utf-8",
    ) -> None:
        self.max_bytes = max(int(max_bytes), 1024)
        self._size_rollover = False
        super().__init__(
            filename,
            when="midnight",
            backupCount=max(int(retention_days), 1),
            encoding=encoding,
        )

    def shouldRollover(self, record: logging.LogRecord) -> int:
        if super().shouldRollover(record):
            self._size_rollover = False
            return 1
        if self.stream is None:
            self.stream = self._open()
        message = f"{self.format(record)}\n".encode(
            self.encoding or "utf-8",
            errors="replace",
        )
        self.stream.seek(0, 2)
        self._size_rollover = self.stream.tell() + len(message) >= self.max_bytes
        return int(self._size_rollover)

    def getFilesToDelete(self) -> list[str]:
        path = Path(self.baseFilename)
        archives = sorted(
            (
                candidate
                for candidate in path.parent.glob(f"{path.name}.*")
                if candidate.is_file()
            ),
            key=lambda candidate: candidate.stat().st_mtime,
        )
        excess = max(len(archives) - self.backupCount, 0)
        return [str(candidate) for candidate in archives[:excess]]

    def doRollover(self) -> None:
        if not self._size_rollover:
            super().doRollover()
            return
        if self.stream:
            self.stream.close()
            self.stream = None
        path = Path(self.baseFilename)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        sequence = 1
        archive = path.with_name(f"{path.name}.{stamp}.{sequence:03d}")
        while archive.exists():
            sequence += 1
            archive = path.with_name(f"{path.name}.{stamp}.{sequence:03d}")
        if path.exists():
            path.replace(archive)
        for old_path in self.getFilesToDelete():
            try:
                Path(old_path).unlink()
            except OSError:
                pass
        self._size_rollover = False
        if not self.delay:
            self.stream = self._open()


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
        for index, pattern in enumerate(_SECRET_PATTERNS):
            replacement = r"\1***:***@" if index == 0 else r"\1***"
            message = pattern.sub(replacement, message)
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
        file_handler = BoundedTimedRotatingFileHandler(
            log_path,
            retention_days=settings.log_retention_days,
            max_bytes=settings.log_max_bytes,
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
