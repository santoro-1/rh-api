from __future__ import annotations


class DeviceAuthError(Exception):
    """Safe, structured errors: never include tokens, public input or key material."""

    def __init__(self, code: str, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
