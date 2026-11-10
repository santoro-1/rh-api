from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class CloudError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class CloudClient:
    def __init__(self, base_url: str, timeout_seconds: float = 180.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _response(self, response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError:
            value = None
        if response.status_code >= 400:
            detail = value.get("detail") if isinstance(value, dict) else None
            raise CloudError(
                str(detail or f"云端请求失败（HTTP {response.status_code}）"),
                status_code=response.status_code,
            )
        if not isinstance(value, dict):
            raise CloudError("云端返回了无法识别的数据")
        return value

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(f"{self.base_url}{path}", json=payload)
        except httpx.RequestError as exc:
            raise CloudError("无法连接云端数字人网站，请检查网络后重试") from exc
        return self._response(response)

    def login(self, username: str, password: str) -> dict[str, Any]:
        return self._post(
            "/api/auth/center/login",
            {"username": username, "password": password},
        )

    def upload_asset(
        self,
        token: str,
        kind: str,
        path: Path,
        original_name: str,
        content_type: str,
    ) -> dict[str, Any]:
        try:
            with path.open("rb") as stream, httpx.Client(
                timeout=self.timeout_seconds
            ) as client:
                response = client.post(
                    f"{self.base_url}/api/workbench/batch-assets",
                    data={"access_token": token, "kind": kind},
                    files={"file": (original_name, stream, content_type)},
                )
        except httpx.RequestError as exc:
            raise CloudError("上传素材时无法连接云端数字人网站") from exc
        return self._response(response)

    def accounts(self, token: str) -> dict[str, Any]:
        return self._post(
            "/api/workbench/h3-execution-accounts",
            {"access_token": token},
        )

    def audio_sources(self, token: str) -> dict[str, Any]:
        return self._post(
            "/api/workbench/h3-audio-sources",
            {"access_token": token},
        )

    def approve_audio_source(
        self,
        token: str,
        *,
        audio_batch_id: str,
        audio_item_id: str,
        audio_generation_version: int,
    ) -> dict[str, Any]:
        return self._post(
            "/api/workbench/h3-audio-sources/approve",
            {
                "access_token": token,
                "audio_batch_id": audio_batch_id,
                "audio_item_id": audio_item_id,
                "audio_generation_version": int(audio_generation_version),
            },
        )

    def prepare(self, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(
            "/api/workbench/h3-batches/prepare",
            {"access_token": token, **payload},
        )

    def confirm(self, token: str, batch_id: str) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-batches/{batch_id}/confirm",
            {"access_token": token, "cost_confirmed": True},
        )

    def batch(self, token: str, batch_id: str) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-batches/{batch_id}",
            {"access_token": token},
        )

    def prepare_regeneration(
        self,
        token: str,
        segment_id: str,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-segments/{segment_id}/regeneration/prepare",
            {"access_token": token},
        )

    def confirm_regeneration(
        self,
        token: str,
        segment_id: str,
        *,
        request_key: str,
        quote_token: str,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-segments/{segment_id}/regeneration/confirm",
            {
                "access_token": token,
                "request_key": request_key,
                "quote_token": quote_token,
                "cost_confirmed": True,
            },
        )

    def prepare_retry(self, token: str, segment_id: str) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-segments/{segment_id}/retry/prepare",
            {"access_token": token},
        )

    def confirm_retry(
        self,
        token: str,
        segment_id: str,
        *,
        request_key: str,
        quote_token: str,
        cost_confirmed: bool,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-segments/{segment_id}/retry/confirm",
            {
                "access_token": token,
                "request_key": request_key,
                "quote_token": quote_token,
                "cost_confirmed": cost_confirmed,
            },
        )

    def cancel_segment(
        self,
        token: str,
        segment_id: str,
        *,
        request_key: str,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-segments/{segment_id}/cancel",
            {"access_token": token, "request_key": request_key},
        )

    def download(self, token: str, relative_url: str, destination: Path) -> Path:
        if not relative_url.startswith("/") or relative_url.startswith("//"):
            raise CloudError("云端下载地址不合法")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with httpx.stream(
                "GET",
                f"{self.base_url}{relative_url}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout_seconds,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    self._response(response)
                with temporary.open("wb") as stream:
                    for chunk in response.iter_bytes():
                        stream.write(chunk)
            temporary.replace(destination)
        except httpx.RequestError as exc:
            raise CloudError("下载 H3 结果时无法连接云端数字人网站") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return destination
