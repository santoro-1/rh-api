from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests


class RunningHubError(RuntimeError):
    QUEUE_LIMIT_CODES = {"421", "1520", "TASK_QUEUE_MAXED"}
    TASK_NOT_FOUND_CODES = {
        "423",
        "1004",
        "TASK_NOT_FOUND",
        "TASK_NOT_FOUNED",
    }

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code

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
    ) -> None:
        code, message = cls._error_details(result)
        raise RunningHubError(
            f"{action}失败：{message or default_message}",
            error_code=code,
        )

    def get_account_current_task_count(self) -> int:
        """Return all current tasks for this API key, including website tasks."""

        try:
            response = self.session.post(
                f"{self.base_url}/uc/openapi/accountStatus",
                headers={**self.headers, "Content-Type": "application/json"},
                json={"apikey": self._api_key},
                timeout=(15, 60),
            )
        except requests.RequestException as exc:
            raise RunningHubError("读取 RunningHub 账号状态时网络请求失败") from exc
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
        try:
            with file_path.open("rb") as file_handle:
                response = self.session.post(
                    f"{self.base_url}/openapi/v2/media/upload/binary",
                    headers=self.headers,
                    files={"file": (file_path.name, file_handle)},
                    timeout=(15, 600),
                )
        except requests.RequestException as exc:
            raise RunningHubError("上传素材到 RunningHub 时网络请求失败") from exc
        result = self._parse_json(response, "上传素材")
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
                )
            raise RunningHubError(
                "上传素材成功响应中缺少 data.fileName/data.filename"
            )
        return str(filename)

    def submit_task(self, payload: dict[str, Any]) -> str:
        resource_path = (
            "workflow" if self.submission_type == "workflow" else "ai-app"
        )
        try:
            response = self.session.post(
                f"{self.base_url}/openapi/v2/run/{resource_path}/{self.ai_app_id}",
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
                timeout=(15, 120),
            )
        except requests.RequestException as exc:
            raise RunningHubError("提交 RunningHub 任务时网络请求失败") from exc
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
        try:
            response = self.session.post(
                f"{self.base_url}/openapi/v2/query",
                headers={**self.headers, "Content-Type": "application/json"},
                json={"taskId": task_id},
                timeout=(15, 120),
            )
        except requests.RequestException as exc:
            raise RunningHubError("查询 RunningHub 任务时网络请求失败") from exc
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

    def download_result(self, url: str, destination: Path) -> None:
        if not url.startswith(("https://", "http://")):
            raise RunningHubError("RunningHub 返回了无效的结果地址")
        temporary_path = destination.with_suffix(destination.suffix + ".part")
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
            raise RunningHubError("下载生成结果时网络请求失败") from exc
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
