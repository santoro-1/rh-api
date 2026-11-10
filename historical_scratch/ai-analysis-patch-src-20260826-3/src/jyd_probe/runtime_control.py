from __future__ import annotations

import hmac
import os
import threading
import time
import uuid


class RuntimeControl:
    """Tracks open browser pages and exposes a manager-requested shutdown."""

    def __init__(
        self,
        manager_token: str | None = None,
        *,
        lease_timeout_seconds: float = 30.0,
    ):
        self.manager_token = (
            str(manager_token or os.environ.get("PUBLIC_WORKBENCH_MANAGER_TOKEN") or "")
            .strip()
        )
        self.shutdown_requested = threading.Event()
        self._lock = threading.Lock()
        self.lease_timeout_seconds = max(5.0, float(lease_timeout_seconds))
        self._pages: dict[str, float] = {}
        self._seen_page = False

    def open_page(self) -> str:
        lease_id = uuid.uuid4().hex
        with self._lock:
            self._pages[lease_id] = time.monotonic()
            self._seen_page = True
        return lease_id

    def touch_page(self, lease_id: str) -> bool:
        with self._lock:
            if lease_id not in self._pages:
                return False
            self._pages[lease_id] = time.monotonic()
            return True

    def close_page(self, lease_id: str) -> None:
        with self._lock:
            self._pages.pop(lease_id, None)

    def status(self) -> dict[str, object]:
        with self._lock:
            expired_before = time.monotonic() - self.lease_timeout_seconds
            self._pages = {
                lease_id: touched_at
                for lease_id, touched_at in self._pages.items()
                if touched_at >= expired_before
            }
            return {
                "active_pages": len(self._pages),
                "seen_page": self._seen_page,
                "shutdown_requested": self.shutdown_requested.is_set(),
            }

    def request_shutdown(self, token: str) -> bool:
        if not self.manager_authorized(token):
            return False
        self.shutdown_requested.set()
        return True

    def manager_authorized(self, token: str) -> bool:
        return bool(self.manager_token) and hmac.compare_digest(
            self.manager_token, str(token or "")
        )
