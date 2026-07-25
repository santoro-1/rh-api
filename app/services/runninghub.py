from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests


class RunningHubError(RuntimeError):
    pass


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
        self.headers = {"Authorization": f"Bearer {api_key}"}

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
        if not isinstance(data, dict) or not data.get("fileName"):
            raise RunningHubError("上传素材成功响应中缺少 data.fileName")
        return str(data["fileName"])

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
            message = result.get("errorMessage") or "响应中缺少 taskId"
            raise RunningHubError(f"提交任务失败：{message}")
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
        return self._parse_json(response, "查询任务")

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
