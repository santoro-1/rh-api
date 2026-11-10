from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings


DRAIN_MARKER_NAME = "deployment-drain.json"


def deployment_drain_path(settings: Settings | None = None) -> Path:
    current = settings or get_settings()
    return current.runtime_dir / DRAIN_MARKER_NAME


def read_deployment_drain(
    settings: Settings | None = None,
    *,
    now_epoch: float | None = None,
) -> dict[str, Any] | None:
    marker = deployment_drain_path(settings)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        expires_at = float(payload["expiresAtEpoch"])
    except (KeyError, TypeError, ValueError):
        return None
    if expires_at <= (time.time() if now_epoch is None else now_epoch):
        return None
    return payload


def is_deployment_draining(settings: Settings | None = None) -> bool:
    return read_deployment_drain(settings) is not None
