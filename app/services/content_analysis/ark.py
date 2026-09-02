from __future__ import annotations

from email.utils import parsedate_to_datetime
import hashlib
import random
import threading
import time
from typing import Any, Callable, Mapping, Sequence

import requests
from requests.adapters import HTTPAdapter


class ArkAPIError(RuntimeError):
    """Safe Ark transport error that never embeds request or response bodies."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        request_id: str | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.request_id = request_id
        self.attempts = attempts

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "request_id": self.request_id,
            "attempts": self.attempts,
        }


def _request_id(response: requests.Response) -> str | None:
    for name in ("x-request-id", "x-tt-logid", "trace-id"):
        value = response.headers.get(name)
        if value:
            return str(value)[:200]
    return None


def _retry_after_seconds(
    response: requests.Response | None,
    retry_index: int,
    *,
    random_uniform: Callable[[float, float], float] = random.uniform,
) -> float:
    if response is not None:
        value = response.headers.get("retry-after")
        if value:
            try:
                return min(max(float(value), 0.0), 60.0)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(value).timestamp()
                    return min(max(retry_at - time.time(), 0.0), 60.0)
                except (TypeError, ValueError, OverflowError):
                    pass
    ceiling = min(0.5 * (2**retry_index), 8.0)
    return max(0.0, random_uniform(0.0, ceiling))


_ARK_THREAD_SESSIONS = threading.local()
_ARK_SESSION_LOCK = threading.Lock()
_ARK_ALL_SESSIONS: set[requests.Session] = set()


def _pooled_session(slot: str, fingerprint: str) -> requests.Session:
    sessions = getattr(_ARK_THREAD_SESSIONS, "sessions", None)
    if sessions is None:
        sessions = {}
        _ARK_THREAD_SESSIONS.sessions = sessions
    existing = sessions.get(slot)
    if existing is not None and existing[0] == fingerprint:
        return existing[1]
    if existing is not None:
        old = existing[1]
        old.close()
        with _ARK_SESSION_LOCK:
            _ARK_ALL_SESSIONS.discard(old)
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        pool_block=True,
        max_retries=0,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    sessions[slot] = (fingerprint, session)
    with _ARK_SESSION_LOCK:
        _ARK_ALL_SESSIONS.add(session)
    return session


def close_ark_sessions() -> None:
    with _ARK_SESSION_LOCK:
        sessions = list(_ARK_ALL_SESSIONS)
        _ARK_ALL_SESSIONS.clear()
    for session in sessions:
        session.close()
    _ARK_THREAD_SESSIONS.sessions = {}


class ArkClient:
    """Small injectable client for Ark's OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int = 30,
        connect_timeout_seconds: int = 10,
        max_retries: int = 2,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        from app.services.content_analysis.ark_accounts import validate_ark_config

        clean_key = api_key.strip()
        if not clean_key:
            raise ValueError("豆包 Ark API Key 不能为空")
        clean_url, clean_model = validate_ark_config(
            enabled=True,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            has_api_key=True,
        )
        self.base_url = clean_url
        self.model = clean_model
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = max(1, int(connect_timeout_seconds))
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.api_key = clean_key
        self._sleep = sleep
        self._random_uniform = random_uniform

    def create_chat_completion(
        self,
        *,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        max_attempts: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        if not messages:
            raise ValueError("Ark messages 不能为空")
        normalized_messages: list[dict[str, str]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role not in {"system", "user", "assistant"}:
                raise ValueError("Ark message role 不合法")
            if not isinstance(content, str) or not content:
                raise ValueError("Ark message content 必须是非空字符串")
            normalized_messages.append({"role": role, "content": content})
        if not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
            raise ValueError("Ark temperature 必须在 0 到 2 之间")
        if max_tokens is not None and (
            isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0
        ):
            raise ValueError("Ark max_tokens 必须为正整数")
        if max_attempts is not None and (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts <= 0
        ):
            raise ValueError("Ark max_attempts 必须为正整数")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": normalized_messages,
            "temperature": float(temperature),
        }
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        endpoint = f"{self.base_url}/chat/completions"
        total_attempts = (
            self.max_retries + 1 if max_attempts is None else max_attempts
        )
        for attempt_index in range(total_attempts):
            attempt = attempt_index + 1
            response: requests.Response | None = None
            remaining = (
                float(deadline_monotonic) - time.monotonic()
                if deadline_monotonic is not None
                else float(self.timeout_seconds)
            )
            if remaining <= 0:
                raise ArkAPIError(
                    "ARK_TOTAL_DEADLINE_EXCEEDED",
                    "豆包请求总预算已耗尽",
                    retryable=False,
                    attempts=max(1, attempt_index),
                )
            try:
                response = self.session.post(
                    endpoint,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=(
                        min(float(self.connect_timeout_seconds), remaining),
                        min(float(self.timeout_seconds), remaining),
                    ),
                )
            except requests.Timeout as exc:
                if attempt < total_attempts:
                    delay = _retry_after_seconds(
                        None,
                        attempt_index,
                        random_uniform=self._random_uniform,
                    )
                    if deadline_monotonic is not None and (
                        time.monotonic() + delay >= deadline_monotonic
                    ):
                        raise ArkAPIError(
                            "ARK_TOTAL_DEADLINE_EXCEEDED",
                            "豆包请求总预算已耗尽",
                            retryable=False,
                            attempts=attempt,
                        ) from exc
                    self._sleep(delay)
                    continue
                raise ArkAPIError(
                    "ARK_TIMEOUT",
                    "豆包 Ark 请求超时",
                    retryable=True,
                    attempts=attempt,
                ) from exc
            except requests.ConnectionError as exc:
                if attempt < total_attempts:
                    delay = _retry_after_seconds(
                        None,
                        attempt_index,
                        random_uniform=self._random_uniform,
                    )
                    if deadline_monotonic is not None and (
                        time.monotonic() + delay >= deadline_monotonic
                    ):
                        raise ArkAPIError(
                            "ARK_TOTAL_DEADLINE_EXCEEDED",
                            "豆包请求总预算已耗尽",
                            retryable=False,
                            attempts=attempt,
                        ) from exc
                    self._sleep(delay)
                    continue
                raise ArkAPIError(
                    "ARK_CONNECTION_ERROR",
                    "无法连接豆包 Ark 服务",
                    retryable=True,
                    attempts=attempt,
                ) from exc
            except requests.RequestException as exc:
                raise ArkAPIError(
                    "ARK_REQUEST_ERROR",
                    "豆包 Ark 请求失败",
                    retryable=False,
                    attempts=attempt,
                ) from exc

            request_id = _request_id(response)
            retryable_status = response.status_code == 429 or response.status_code >= 500
            if retryable_status and attempt < total_attempts:
                delay = _retry_after_seconds(
                    response,
                    attempt_index,
                    random_uniform=self._random_uniform,
                )
                if deadline_monotonic is not None and (
                    time.monotonic() + delay >= deadline_monotonic
                ):
                    raise ArkAPIError(
                        "ARK_TOTAL_DEADLINE_EXCEEDED",
                        "豆包请求总预算已耗尽",
                        status_code=response.status_code,
                        retryable=False,
                        request_id=request_id,
                        attempts=attempt,
                    )
                self._sleep(delay)
                continue
            if not 200 <= response.status_code < 300:
                raise ArkAPIError(
                    "ARK_UPSTREAM_TRANSIENT" if retryable_status else "ARK_HTTP_ERROR",
                    f"豆包 Ark 返回 HTTP {response.status_code}",
                    status_code=response.status_code,
                    retryable=retryable_status,
                    request_id=request_id,
                    attempts=attempt,
                )
            try:
                result = response.json()
            except ValueError as exc:
                raise ArkAPIError(
                    "ARK_INVALID_JSON",
                    "豆包 Ark 返回了无效 JSON",
                    status_code=response.status_code,
                    retryable=False,
                    request_id=request_id,
                    attempts=attempt,
                ) from exc
            if not isinstance(result, dict):
                raise ArkAPIError(
                    "ARK_INVALID_RESPONSE",
                    "豆包 Ark 返回结构不正确",
                    status_code=response.status_code,
                    retryable=False,
                    request_id=request_id,
                    attempts=attempt,
                )
            return result
        raise AssertionError("Ark retry loop exited unexpectedly")


def ark_client_from_config(
    config: Any,
    *,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ArkClient:
    from app.config import get_settings
    from app.services.security import decrypt_secret

    if not config.enabled:
        raise ValueError("该用户未启用豆包内容分析")
    api_key = decrypt_secret(config.api_key_encrypted, label="豆包 Ark API Key")
    settings = get_settings()
    fingerprint = hashlib.sha256(
        "\0".join(
            (
                str(config.base_url or "").strip().lower(),
                str(config.model or "").strip(),
                api_key,
            )
        ).encode("utf-8")
    ).hexdigest()
    resolved_session = session or _pooled_session(
        f"ark-config:{getattr(config, 'id', config.user_id)}", fingerprint
    )
    return ArkClient(
        api_key,
        base_url=config.base_url,
        model=config.model,
        timeout_seconds=config.timeout_seconds,
        connect_timeout_seconds=settings.ark_connect_timeout_seconds,
        max_retries=config.max_retries,
        session=resolved_session,
        sleep=sleep,
    )
