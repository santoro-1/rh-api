from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Sequence

import requests


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


def _retry_after_seconds(response: requests.Response | None, retry_index: int) -> float:
    if response is not None:
        value = response.headers.get("retry-after")
        if value:
            try:
                return min(max(float(value), 0.0), 10.0)
            except ValueError:
                pass
    return min(0.5 * (2**retry_index), 4.0)


class ArkClient:
    """Small injectable client for Ark's OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int = 30,
        max_retries: int = 2,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
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
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {clean_key}",
                "Content-Type": "application/json",
            }
        )
        self._sleep = sleep

    def create_chat_completion(
        self,
        *,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any] | None = None,
        temperature: float = 0.0,
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

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": normalized_messages,
            "temperature": float(temperature),
        }
        if response_format is not None:
            payload["response_format"] = dict(response_format)

        endpoint = f"{self.base_url}/chat/completions"
        total_attempts = self.max_retries + 1
        for attempt_index in range(total_attempts):
            attempt = attempt_index + 1
            response: requests.Response | None = None
            try:
                response = self.session.post(
                    endpoint,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except requests.Timeout as exc:
                if attempt < total_attempts:
                    self._sleep(_retry_after_seconds(None, attempt_index))
                    continue
                raise ArkAPIError(
                    "ARK_TIMEOUT",
                    "豆包 Ark 请求超时",
                    retryable=True,
                    attempts=attempt,
                ) from exc
            except requests.ConnectionError as exc:
                if attempt < total_attempts:
                    self._sleep(_retry_after_seconds(None, attempt_index))
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
                self._sleep(_retry_after_seconds(response, attempt_index))
                continue
            if not 200 <= response.status_code < 300:
                raise ArkAPIError(
                    "ARK_HTTP_ERROR",
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
    from app.services.security import decrypt_secret

    if not config.enabled:
        raise ValueError("该用户未启用豆包内容分析")
    return ArkClient(
        decrypt_secret(config.api_key_encrypted, label="豆包 Ark API Key"),
        base_url=config.base_url,
        model=config.model,
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        session=session,
        sleep=sleep,
    )
