from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests


_URL_CREDENTIALS_RE = re.compile(
    r"(?i)(https?://)([^/\s:@]+):([^@\s/]+)@"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|token|access[_-]?password)=)[^&\s]+"
)
RUNNINGHUB_UPLOAD_GUIDANCE_BYTES = 30 * 1024 * 1024


def _safe_network_message(exc: BaseException) -> str:
    """Keep actionable transport details without leaking URL credentials."""

    message = str(exc).replace("\r", " ").replace("\n", " ")
    message = _URL_CREDENTIALS_RE.sub(r"\1***:***@", message)
    message = _QUERY_SECRET_RE.sub(r"\1***", message)
    return message[:1000]


def _endpoint_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        return (
            f"{hostname}:{parsed.port}"
            if hostname and parsed.port
            else hostname
        )
    except ValueError:
        return ""


def runninghub_upload_diagnostics(size_bytes: int) -> dict[str, object]:
    """Describe upload size for logs without enforcing a product limit."""

    size_mb = round(max(int(size_bytes), 0) / 1024 / 1024, 2)
    details: dict[str, object] = {
        "asset_size_bytes": max(int(size_bytes), 0),
        "asset_size_mb": size_mb,
    }
    if size_bytes > RUNNINGHUB_UPLOAD_GUIDANCE_BYTES:
        details["upload_size_warning"] = (
            f"素材体积 {size_mb}MB，超过 RunningHub 官方建议的 30MB，"
            "可能导致上传失败"
        )
    return details


class RunningHubError(RuntimeError):
    QUEUE_LIMIT_CODES = {"421", "1520", "TASK_QUEUE_MAXED"}
    TASK_NOT_FOUND_CODES = {
        "423",
        "1004",
        "TASK_NOT_FOUND",
        "TASK_NOT_FOUNED",
    }

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        retry_safe: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retry_safe = retry_safe
        self.diagnostics = dict(diagnostics or {})

    @classmethod
    def from_network_error(
        cls,
        message: str,
        *,
        operation: str,
        endpoint: str,
        exc: requests.RequestException,
        started_at: float,
        retry_safe: bool = False,
        **details: object,
    ) -> "RunningHubError":
        cause = exc.__cause__ or exc.__context__
        response = getattr(exc, "response", None)
        diagnostics = {
            "runninghub_operation": operation,
            "endpoint_host": _endpoint_host(endpoint),
            "network_error_type": type(exc).__name__,
            "network_error": _safe_network_message(exc),
            "network_cause_type": (
                type(cause).__name__ if cause is not None else None
            ),
            "http_status": (
                getattr(response, "status_code", None)
                if response is not None
                else None
            ),
            "elapsed_ms": max(
                round((time.perf_counter() - started_at) * 1000),
                0,
            ),
            **details,
        }
        return cls(
            message,
            retry_safe=retry_safe,
            diagnostics={
                key: value
                for key, value in diagnostics.items()
                if value is not None and value != ""
            },
        )

    def log_details(self) -> dict[str, Any]:
        """Return only pre-sanitized fields intended for operator logs."""

        return dict(self.diagnostics)

    @property
    def is_capacity_limited(self) -> bool:
        normalized_code = str(self.error_code or "").strip().upper()
        normalized_message = str(self).lower()
        return (
            normalized_code in self.QUEUE_LIMIT_CODES
            or "queue limit reached" in normalized_message
            or "concurrency limit reached" in normalized_message
            or "api 并发数已达" in normalized_message
            or "并发达到上限" in normalized_message
        )

    @property
    def is_task_not_found(self) -> bool:
        normalized_code = str(self.error_code or "").strip().upper()
        normalized_message = str(self).lower()
        return (
            normalized_code in self.TASK_NOT_FOUND_CODES
            or "task not found" in normalized_message
            or "任务不存在或已过期" in normalized_message
        )


class RunningHubClient:
    """Small server-side client. It never returns or logs the API key."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        ai_app_id: str,
        session: requests.Session | None = None,
        submission_type: str = "ai-app",
    ) -> None:
        if not api_key.strip():
            raise ValueError("RunningHub API Key 不能为空")
        if not ai_app_id.strip():
            raise ValueError("RunningHub AI App ID 不能为空")
        self.base_url = base_url.rstrip("/")
        self.ai_app_id = ai_app_id.strip()
        if submission_type not in {"ai-app", "workflow"}:
            raise ValueError("RunningHub 提交类型不合法")
        self.submission_type = submission_type
        self.session = session or requests.Session()
        self._api_key = api_key.strip()
        self.headers = {"Authorization": f"Bearer {self._api_key}"}

    def _parse_json(self, response: requests.Response, action: str) -> dict[str, Any]:
        if response.status_code != 200:
            raise RunningHubError(f"{action}失败（HTTP {response.status_code}）")
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise RunningHubError(f"{action}返回了无效 JSON") from exc
        if not isinstance(data, dict):
            raise RunningHubError(f"{action}返回格式不正确")
        return data

    @staticmethod
    def _error_details(result: dict[str, Any]) -> tuple[str | None, str | None]:
        raw_code = result.get("errorCode")
        if raw_code in {None, ""}:
            raw_code = result.get("code")
        code = str(raw_code) if raw_code not in {None, ""} else None
        message = (
            result.get("errorMessage")
            or result.get("msg")
            or result.get("message")
        )
        return code, str(message) if message else None

    @classmethod
    def _raise_business_error(
        cls,
        result: dict[str, Any],
        action: str,
        *,
        default_message: str,
        retry_safe: bool = False,
    ) -> None:
        code, message = cls._error_details(result)
        raise RunningHubError(
            f"{action}失败：{message or default_message}",
            error_code=code,
            retry_safe=retry_safe,
        )

    def get_account_current_task_count(self) -> int:
        """Return all current tasks for this API key, including website tasks."""

        endpoint = f"{self.base_url}/uc/openapi/accountStatus"
        started_at = time.perf_counter()
        try:
            response = self.session.post(
                endpoint,
                headers={**self.headers, "Content-Type": "application/json"},
                json={"apikey": self._api_key},
                timeout=(15, 60),
            )
        except requests.RequestException as exc:
            raise RunningHubError.from_network_error(
                "读取 RunningHub 账号状态时网络请求失败",
                operation="account_status",
                endpoint=endpoint,
                exc=exc,
                started_at=started_at,
            ) from exc
        result = self._parse_json(response, "读取 RunningHub 账号状态")
        code, _ = self._error_details(result)
        if code not in {None, "0", "200"}:
            self._raise_business_error(
                result,
                "读取 RunningHub 账号状态",
                default_message="未知业务错误",
            )
        data = result.get("data")
        raw_count = data.get("currentTaskCounts") if isinstance(data, dict) else None
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise RunningHubError(
                "读取 RunningHub 账号状态失败：响应中缺少有效的 "
                "data.currentTaskCounts"
            ) from exc
        if count < 0:
            raise RunningHubError(
                "读取 RunningHub 账号状态失败：data.currentTaskCounts 不能为负数"
            )
        return count

    def upload_file(self, file_path: Path) -> str:
        if not file_path.is_file():
            raise RunningHubError("待上传文件不存在")
        endpoint = f"{self.base_url}/openapi/v2/media/upload/binary"
        asset_size_bytes = file_path.stat().st_size
        started_at = time.perf_counter()
        try:
            with file_path.open("rb") as file_handle:
                response = self.session.post(
                    endpoint,
                    headers=self.headers,
                    files={"file": (file_path.name, file_handle)},
                    timeout=(15, 600),
                )
        except requests.RequestException as exc:
            raise RunningHubError.from_network_error(
                "上传素材到 RunningHub 时网络请求失败",
                operation="asset_upload",
                endpoint=endpoint,
                exc=exc,
                started_at=started_at,
                retry_safe=True,
                **runninghub_upload_diagnostics(asset_size_bytes),
            ) from exc
        try:
            result = self._parse_json(response, "上传素材")
        except RunningHubError as exc:
            exc.retry_safe = True
            raise
        data = result.get("data")
        filename = (
            data.get("fileName") or data.get("filename")
            if isinstance(data, dict)
            else None
        )
        if not filename:
            code, _ = self._error_details(result)
            if code not in {None, "0", "200"}:
                self._raise_business_error(
                    result,
                    "上传素材",
                    default_message="未知业务错误",
                    retry_safe=True,
                )
            raise RunningHubError(
                "上传素材成功响应中缺少 data.fileName/data.filename",
                retry_safe=True,
            )
        return str(filename)

    def submit_task(self, payload: dict[str, Any]) -> str:
        resource_path = (
            "workflow" if self.submission_type == "workflow" else "ai-app"
        )
        endpoint = (
            f"{self.base_url}/openapi/v2/run/{resource_path}/{self.ai_app_id}"
        )
        started_at = time.perf_counter()
        try:
            response = self.session.post(
                endpoint,
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
                timeout=(15, 120),
            )
        except requests.RequestException as exc:
            raise RunningHubError.from_network_error(
                "提交 RunningHub 任务时网络请求失败",
                operation="task_submit",
                endpoint=endpoint,
                exc=exc,
                started_at=started_at,
            ) from exc
        result = self._parse_json(response, "提交任务")
        task_id = result.get("taskId")
        if not task_id:
            self._raise_business_error(
                result,
                "提交任务",
                default_message="响应中缺少 taskId",
            )
        return str(task_id)

    def query_task(self, task_id: str) -> dict[str, Any]:
        endpoint = f"{self.base_url}/openapi/v2/query"
        started_at = time.perf_counter()
        try:
            response = self.session.post(
                endpoint,
                headers={**self.headers, "Content-Type": "application/json"},
                json={"taskId": task_id},
                timeout=(15, 120),
            )
        except requests.RequestException as exc:
            raise RunningHubError.from_network_error(
                "查询 RunningHub 任务时网络请求失败",
                operation="task_query",
                endpoint=endpoint,
                exc=exc,
                started_at=started_at,
            ) from exc
        result = self._parse_json(response, "查询任务")
        if not result.get("status"):
            code, message = self._error_details(result)
            if code or message:
                self._raise_business_error(
                    result,
                    "查询任务",
                    default_message="响应中缺少任务状态",
                )
        return result

    def cancel_task(self, task_id: str) -> None:
        """Cancel a submitted ComfyUI task and require business success."""

        if not str(task_id).strip():
            raise RunningHubError("取消任务失败：任务 ID 不能为空")
        endpoint = f"{self.base_url}/task/openapi/cancel"
        started_at = time.perf_counter()
        try:
            response = self.session.post(
                endpoint,
                headers={**self.headers, "Content-Type": "application/json"},
                json={"apiKey": self._api_key, "taskId": str(task_id)},
                timeout=(15, 120),
            )
        except requests.RequestException as exc:
            raise RunningHubError.from_network_error(
                "取消 RunningHub 任务时网络请求失败",
                operation="task_cancel",
                endpoint=endpoint,
                exc=exc,
                started_at=started_at,
            ) from exc
        result = self._parse_json(response, "取消任务")
        code, _ = self._error_details(result)
        if code not in {None, "0", "200"}:
            self._raise_business_error(
                result,
                "取消任务",
                default_message="RunningHub 未确认取消成功",
            )

    def download_result(self, url: str, destination: Path) -> None:
        if not url.startswith(("https://", "http://")):
            raise RunningHubError("RunningHub 返回了无效的结果地址")
        temporary_path = destination.with_suffix(destination.suffix + ".part")
        started_at = time.perf_counter()
        try:
            with self.session.get(url, stream=True, timeout=(15, 600)) as response:
                if response.status_code != 200:
                    raise RunningHubError(f"下载结果失败（HTTP {response.status_code}）")
                with temporary_path.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
            if not temporary_path.exists() or temporary_path.stat().st_size == 0:
                raise RunningHubError("下载结果为空")
            temporary_path.replace(destination)
        except requests.RequestException as exc:
            temporary_path.unlink(missing_ok=True)
            raise RunningHubError.from_network_error(
                "下载生成结果时网络请求失败",
                operation="result_download",
                endpoint=url,
                exc=exc,
                started_at=started_at,
            ) from exc
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
