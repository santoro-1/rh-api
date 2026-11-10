from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
EVENT_PREFIX = "[EVENT"

_SECRET_PATTERNS = (
    re.compile(r"(?ix)(https?://)[^/\s:@]+:[^@\s/]+@"),
    re.compile(
        r"(?ix)([?&](?:api[_-]?key|token|access[_-]?password)=)[^&\s]+"
    ),
    re.compile(
        r"(?ix)(authorization[\"']?\s*[:=]\s*[\"']?\s*bearer\s+)[^\"',\s}]+"
    ),
    re.compile(
        r"(?ix)(cookie[\"']?\s*[:=]\s*[\"']?)[^\"'\r\n}]+"
    ),
    re.compile(
        r"(?ix)((?:api[_-]?key|access[_-]?password|"
        r"(?:access|refresh|session|agent)?[_-]?token)"
        r"(?:[_-]?encrypted)?[\"']?\s*[:=]\s*[\"']?)[^\"',&\s}]+"
    ),
)


def redact_text(value: str) -> str:
    """Mask common credentials before text is persisted or exported."""

    message = str(value)
    for index, pattern in enumerate(_SECRET_PATTERNS):
        replacement = r"\1***:***@" if index == 0 else r"\1***"
        message = pattern.sub(replacement, message)
    return message


class BoundedTimedRotatingFileHandler(TimedRotatingFileHandler):
    """按日期或文件大小轮转，并限制历史文件数量。"""

    def __init__(
        self,
        filename: Path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.max_bytes = max(int(max_bytes), 1024)
        self._size_rollover = False
        super().__init__(
            filename,
            when="midnight",
            backupCount=max(int(retention_days), 1),
            encoding="utf-8",
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


class SecretRedactionFilter(logging.Filter):
    """在写入磁盘前隐藏常见凭据。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def configure_file_logging(
    log_dir: str | Path,
    filename: str,
    *,
    logger_name: str | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    console: bool = False,
    propagate: bool = True,
) -> Path:
    """为一个进程或组件配置可重复调用的持久日志。"""

    directory = Path(log_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    target = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    target.setLevel(logging.INFO)
    if logger_name:
        target.propagate = propagate
    marker = f"jyd_probe:{path}"
    if not any(getattr(handler, "_jyd_probe_marker", "") == marker for handler in target.handlers):
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        file_handler = BoundedTimedRotatingFileHandler(
            path,
            retention_days=retention_days,
            max_bytes=max_bytes,
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(SecretRedactionFilter())
        file_handler._jyd_probe_marker = marker  # type: ignore[attr-defined]
        target.addHandler(file_handler)
        if console:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            console_handler.addFilter(SecretRedactionFilter())
            console_handler._jyd_probe_marker = f"{marker}:console"  # type: ignore[attr-defined]
            target.addHandler(console_handler)
    return path


def log_event(
    target_logger: logging.Logger,
    event_code: str,
    message: str,
    *,
    level: int = logging.INFO,
    **details: object,
) -> None:
    safe_details = {key: value for key, value in details.items() if value is not None}
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
    target_logger.log(level, f"{EVENT_PREFIX} %s] %s%s", event_code, message, suffix)
