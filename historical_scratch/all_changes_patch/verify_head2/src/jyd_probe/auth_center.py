from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .logging_config import log_event


WORKBENCH_ANALYSIS_TIMEOUT_SECONDS = 900.0
analysis_logger = logging.getLogger(__name__)


def _safe_connection_cause(error: BaseException) -> dict[str, object]:
    cause = error.__cause__
    if cause is None:
        return {}
    nested = cause.reason if isinstance(cause, URLError) else cause
    return {
        "transport_exception": type(cause).__name__,
        "transport_cause": type(nested).__name__,
        "transport_errno": getattr(nested, "errno", None),
        "transport_summary": str(nested).strip()[:300] or None,
    }


class AuthCenterError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        error_code: str | None = None,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        self.status_code = int(status_code)
        self.error_code = error_code or self._default_error_code(self.status_code)
        self.retryable = (
            self.status_code >= 500
            if retryable is None
            else bool(retryable)
        )

    @staticmethod
    def _default_error_code(status_code: int) -> str:
        if status_code == 401:
            return "DIGITAL_HUMAN_AUTH_EXPIRED"
        if status_code == 403:
            return "DIGITAL_HUMAN_FORBIDDEN"
        if status_code == 429:
            return "DIGITAL_HUMAN_RATE_LIMITED"
        if status_code >= 500:
            return "DIGITAL_HUMAN_SERVER_UNAVAILABLE"
        return "DIGITAL_HUMAN_REQUEST_REJECTED"


class AuthCenterConnectionError(AuthCenterError):
    def __init__(self, message: str):
        super().__init__(
            message,
            status_code=503,
            error_code="DIGITAL_HUMAN_CONNECTION_FAILED",
            retryable=True,
        )


def create_local_workbench_handoff(
    base_url: str,
    manager_token: str,
    login_payload: dict[str, Any],
    *,
    path: str,
    timeout_seconds: float = 4.0,
) -> str:
    """Create a one-time ticket on the other bundled local service."""

    normalized = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise AuthCenterError("本地工作台地址无效", status_code=500)
    token = str(manager_token or "").strip()
    access_token = str(login_payload.get("access_token") or "").strip()
    user = login_payload.get("user")
    if not token or not access_token or not isinstance(user, dict):
        raise AuthCenterError("本地登录接力数据无效", status_code=401)
    request = Request(
        f"{normalized}{path}",
        data=json.dumps(
            {"access_token": access_token, "user": user}, ensure_ascii=False
        ).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "X-Workbench-Manager-Token": token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(1.0, float(timeout_seconds))) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise AuthCenterError(
            f"本地工作台登录接力失败（HTTP {exc.code}）",
            status_code=exc.code,
        ) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise AuthCenterError("本地工作台登录接力失败") from exc
    code = str(payload.get("handoff_code") or "") if isinstance(payload, dict) else ""
    if not code:
        raise AuthCenterError("本地工作台没有返回登录接力码")
    return code


class AuthHandoffStore:
    """Short-lived, one-time tickets for moving a browser session between hosts."""

    def __init__(self, *, lifetime_seconds: int = 60, max_pending: int = 2048):
        self.lifetime_seconds = max(15, int(lifetime_seconds))
        self.max_pending = max(16, int(max_pending))
        self._lock = threading.Lock()
        self._tickets: dict[str, tuple[str, float]] = {}

    def issue(self, access_token: str) -> str:
        token = access_token.strip()
        if not token:
            raise ValueError("登录令牌不能为空")
        now = time.time()
        with self._lock:
            self._purge(now)
            if len(self._tickets) >= self.max_pending:
                oldest = min(self._tickets, key=lambda code: self._tickets[code][1])
                self._tickets.pop(oldest, None)
            code = secrets.token_urlsafe(32)
            self._tickets[code] = (token, now + self.lifetime_seconds)
        return code

    def consume(self, code: str) -> str | None:
        now = time.time()
        with self._lock:
            self._purge(now)
            record = self._tickets.pop(code.strip(), None)
        if record is None or record[1] <= now:
            return None
        return record[0]

    def _purge(self, now: float) -> None:
        expired = [code for code, (_, expires_at) in self._tickets.items() if expires_at <= now]
        for code in expired:
            self._tickets.pop(code, None)


class AuthCenterClient:
    """HTTP client used by standalone processors to share one account center."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 4.0):
        normalized = base_url.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("数字人网站必须是有效的 http:// 或 https:// 地址")
        if parsed.query or parsed.fragment:
            raise ValueError("数字人网站地址不能包含查询参数或锚点")
        self.base_url = normalized
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def login(self, username: str, password: str) -> dict[str, Any]:
        data = self._post(
            "/api/auth/center/login",
            {"username": username, "password": password},
        )
        token = str(data.get("access_token", "")).strip()
        user = data.get("user")
        if not token or not isinstance(user, dict):
            raise AuthCenterError("数字人网站返回了无效的登录结果")
        return {"access_token": token, "user": user}

    def verify(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        started_at = time.monotonic()
        try:
            data = self._post("/api/auth/center/verify", {"access_token": token})
        except AuthCenterError as exc:
            parsed = urlsplit(self.base_url)
            try:
                target_port = parsed.port
            except ValueError:
                target_port = None
            log_event(
                analysis_logger,
                "auth_center.session_verify_failed",
                "数字人网站登录状态校验失败",
                level=logging.WARNING if exc.status_code == 401 else logging.ERROR,
                component="workbench",
                endpoint="/api/auth/center/verify",
                target_scheme=parsed.scheme,
                target_host=parsed.hostname,
                target_port=target_port,
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
                error_code=exc.error_code,
                http_status=exc.status_code,
                retryable=exc.retryable,
                error_summary=str(exc).strip()[:500],
                **_safe_connection_cause(exc),
            )
            if exc.status_code == 401:
                return None
            raise
        user = data.get("user")
        return user if data.get("valid") is True and isinstance(user, dict) else None

    def create_handoff(self, token: str) -> str:
        if not token:
            raise AuthCenterError("当前登录已经失效，请重新登录", status_code=401)
        data = self._post("/api/auth/center/handoff", {"access_token": token})
        code = str(data.get("handoff_code", "")).strip()
        if not code:
            raise AuthCenterError("数字人网站没有返回登录接力码")
        return code

    def consume_handoff(self, code: str) -> dict[str, Any]:
        if not code:
            raise AuthCenterError("登录接力码不能为空", status_code=401)
        data = self._post(
            "/api/auth/center/handoff/consume",
            {"handoff_code": code},
        )
        token = str(data.get("access_token", "")).strip()
        user = data.get("user")
        if not token or not isinstance(user, dict):
            raise AuthCenterError("数字人网站返回了无效的登录接力结果")
        return {"access_token": token, "user": user}

    def list_workbench_tasks(self, token: str, *, limit: int = 50) -> list[dict[str, Any]]:
        data = self._post(
            "/api/workbench/tasks",
            {"access_token": token, "limit": max(1, min(int(limit), 100))},
        )
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            raise AuthCenterError("数字人网站返回了无效的任务列表")
        return [item for item in tasks if isinstance(item, dict)]

    def get_workbench_task(self, token: str, item_id: str) -> dict[str, Any]:
        data = self._post(
            f"/api/workbench/tasks/{item_id}",
            {"access_token": token},
        )
        if data.get("item_id") != item_id:
            raise AuthCenterError("数字人网站返回了错误的任务")
        return data

    def analyze_workbench_content(
        self,
        token: str,
        original_script: str,
        *,
        force_refresh: bool = False,
        visual_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trace_id = secrets.token_hex(8)
        script_sha256 = hashlib.sha256(original_script.encode("utf-8")).hexdigest()
        parsed = urlsplit(self.base_url)
        try:
            target_port = parsed.port
        except ValueError:
            target_port = None
        started_at = time.monotonic()
        diagnostic_context = {
            "trace_id": trace_id,
            "target_scheme": parsed.scheme,
            "target_host": parsed.hostname,
            "target_port": target_port,
            "endpoint": "/api/workbench/content-analysis",
            "timeout_seconds": WORKBENCH_ANALYSIS_TIMEOUT_SECONDS,
            "script_sha256": script_sha256,
            "script_length": len(original_script),
            "force_refresh": bool(force_refresh),
            "has_visual_context": visual_context is not None,
        }
        log_event(
            analysis_logger,
            "content_analysis.remote_request_started",
            "开始请求数字人网站统一内容分析",
            component="workbench",
            **diagnostic_context,
        )
        payload: dict[str, Any] = {
            "access_token": token,
            "original_script": original_script,
            "force_refresh": force_refresh,
        }
        if visual_context is not None:
            payload["visual_context"] = visual_context
        try:
            result = self._post(
                "/api/workbench/content-analysis",
                payload,
                timeout_seconds=WORKBENCH_ANALYSIS_TIMEOUT_SECONDS,
            )
        except AuthCenterError as exc:
            log_event(
                analysis_logger,
                "content_analysis.remote_request_failed",
                "数字人网站统一内容分析请求失败",
                level=logging.ERROR,
                component="workbench",
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
                error_code=exc.error_code,
                http_status=exc.status_code,
                retryable=exc.retryable,
                error_summary=str(exc).strip()[:500],
                **diagnostic_context,
                **_safe_connection_cause(exc),
            )
            raise
        log_event(
            analysis_logger,
            "content_analysis.remote_response_received",
            "已收到数字人网站统一内容分析响应",
            component="workbench",
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
            response_status=result.get("overall_status"),
            provider_request_id=result.get("provider_request_id"),
            provider_attempts=result.get("provider_attempts"),
            cache_hit=result.get("cache_hit") is True,
            **diagnostic_context,
        )
        return result

    def analyze_workbench_visuals(
        self,
        token: str,
        payload: dict[str, Any],
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        return self._post(
            "/api/workbench/visual-analysis",
            {
                "access_token": token,
                **payload,
                "force_refresh": force_refresh,
            },
            timeout_seconds=WORKBENCH_ANALYSIS_TIMEOUT_SECONDS,
        )

    def start_workbench_composition(
        self,
        token: str,
        batch_id: str,
        item_id: str,
        *,
        idempotency_key: str,
        image_asset_id: str,
        image_sha256: str,
        resolution: str = "1024",
        correlation_id: str = "",
        runninghub_execution_account_ids: list[int] | None = None,
        seedvr2_execution_account_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "access_token": token,
            "cost_confirmed": True,
            "idempotency_key": idempotency_key,
            "image_asset_id": image_asset_id,
            "image_sha256": image_sha256,
            "resolution": str(resolution or "1024"),
            "correlation_id": correlation_id,
        }
        if runninghub_execution_account_ids is not None:
            payload["runninghub_execution_account_ids"] = list(
                runninghub_execution_account_ids
            )
        if seedvr2_execution_account_ids is not None:
            payload["seedvr2_execution_account_ids"] = list(
                seedvr2_execution_account_ids
            )
        return self._post(
            f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition",
            payload,
        )

    def backfill_workbench_video_enhancement(
        self,
        token: str,
        item_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/tasks/{item_id}/enhancement/backfill",
            {
                "access_token": token,
                "cost_confirmed": True,
                "idempotency_key": str(idempotency_key or "").strip(),
            },
        )

    def list_workbench_execution_accounts(self, token: str) -> dict[str, Any]:
        return self._post(
            "/api/workbench/runninghub-execution-accounts",
            {"access_token": token},
        )

    def list_workbench_dual_pool_accounts(self, token: str) -> dict[str, Any]:
        return self._post(
            "/api/workbench/runninghub-dual-pool-accounts",
            {"access_token": token},
        )

    def retry_workbench_composition(
        self, token: str, item_id: str, *, resolution: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "access_token": token,
            "cost_confirmed": True,
        }
        if str(resolution or "").strip():
            payload["resolution"] = str(resolution).strip()
        return self._post(
            f"/api/workbench/tasks/{item_id}/composition/retry",
            payload,
        )

    def list_workbench_voices(self, token: str) -> dict[str, Any]:
        return self._post("/api/workbench/voices", {"access_token": token})

    def create_official_voice_preview(
        self,
        token: str,
        voice_asset_id: str,
        *,
        preview_text: str,
        cost_confirmed: bool,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/voices/{voice_asset_id}/preview",
            {
                "access_token": token,
                "preview_text": preview_text,
                "cost_confirmed": cost_confirmed,
            },
        )

    def create_voice_creation(
        self,
        token: str,
        *,
        fields: dict[str, Any],
        source_a_name: str,
        source_a: bytes,
        source_a_content_type: str,
        source_b_name: str | None = None,
        source_b: bytes | None = None,
        source_b_content_type: str | None = None,
    ) -> dict[str, Any]:
        files = [
            (
                "source_a",
                source_a_name,
                source_a,
                source_a_content_type or "application/octet-stream",
            )
        ]
        if source_b is not None and source_b_name:
            files.append(
                (
                    "source_b",
                    source_b_name,
                    source_b,
                    source_b_content_type or "application/octet-stream",
                )
            )
        return self._multipart_post(
            "/api/workbench/voice-creations",
            {"access_token": token, **fields},
            files,
        )

    def save_voice_creation(self, token: str, task_id: str) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/voice-creations/{task_id}/save",
            {"access_token": token},
        )

    def import_workbench_voice(
        self,
        token: str,
        *,
        voice_id: str,
        name: str,
        already_activated: bool,
    ) -> dict[str, Any]:
        return self._post(
            "/api/workbench/voices/import",
            {
                "access_token": token,
                "voice_id": voice_id,
                "name": name,
                "already_activated": already_activated,
            },
        )

    def activate_workbench_voice(
        self, token: str, voice_asset_id: str, *, cost_confirmed: bool
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/voices/{voice_asset_id}/activate",
            {"access_token": token, "cost_confirmed": cost_confirmed},
        )

    def delete_workbench_voice(
        self, token: str, voice_asset_id: str
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/voices/{voice_asset_id}/delete",
            {"access_token": token},
        )

    def upload_workbench_batch_asset(
        self, token: str, path: Path, *, kind: str, filename: str
    ) -> dict[str, Any]:
        return self._multipart_post(
            "/api/workbench/batch-assets",
            {"access_token": token, "kind": kind},
            [
                (
                    "file",
                    filename,
                    path.read_bytes(),
                    mimetypes.guess_type(filename)[0] or "application/octet-stream",
                )
            ],
        )

    def list_h3_execution_accounts(self, token: str) -> dict[str, Any]:
        return self._post(
            "/api/workbench/h3-execution-accounts",
            {"access_token": token},
        )

    def approve_h3_audio_source(
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

    def prepare_h3_batch(self, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(
            "/api/workbench/h3-batches/prepare",
            {"access_token": token, **payload},
            timeout_seconds=360.0,
        )

    def confirm_h3_batch(self, token: str, batch_id: str) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-batches/{batch_id}/confirm",
            {"access_token": token, "cost_confirmed": True},
            timeout_seconds=120.0,
        )

    def get_h3_batch(self, token: str, batch_id: str) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-batches/{batch_id}",
            {"access_token": token},
        )

    def prepare_h3_segment_regeneration(
        self, token: str, segment_id: str
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-segments/{segment_id}/regeneration/prepare",
            {"access_token": token},
        )

    def confirm_h3_segment_regeneration(
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

    def prepare_h3_segment_retry(
        self, token: str, segment_id: str
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-segments/{segment_id}/retry/prepare",
            {"access_token": token},
        )

    def confirm_h3_segment_retry(
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
                "cost_confirmed": bool(cost_confirmed),
            },
        )

    def cancel_h3_segment(
        self, token: str, segment_id: str, *, request_key: str
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/h3-segments/{segment_id}/cancel",
            {"access_token": token, "request_key": request_key},
        )

    def create_workbench_audio_batch(
        self, token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._post(
            "/api/workbench/audio-batches",
            {"access_token": token, **payload},
        )

    def get_workbench_audio_batch(self, token: str, batch_id: str) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/audio-batches/{batch_id}",
            {"access_token": token},
        )

    def retry_workbench_audio(
        self,
        token: str,
        batch_id: str,
        item_id: str,
        *,
        speed: float,
    ) -> dict[str, Any]:
        return self._post(
            f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/retry",
            {
                "access_token": token,
                "cost_confirmed": True,
                "speed": speed,
            },
        )

    def download_voice_preview(
        self,
        token: str,
        voice_asset_id: str,
        target: Path,
        *,
        max_bytes: int,
    ) -> int:
        return self._download(
            f"/api/workbench/voices/{voice_asset_id}/preview",
            token,
            target,
            max_bytes=max_bytes,
            timeout_seconds=120.0,
            failure_message="下载声音试听失败",
        )

    def download_voice_creation_preview(
        self,
        token: str,
        task_id: str,
        target: Path,
        *,
        max_bytes: int,
    ) -> int:
        return self._download(
            f"/api/workbench/voice-creations/{task_id}/preview",
            token,
            target,
            max_bytes=max_bytes,
            timeout_seconds=120.0,
            failure_message="下载克隆声音试听失败",
        )

    def download_workbench_audio(
        self,
        token: str,
        batch_id: str,
        item_id: str,
        target: Path,
        *,
        max_bytes: int,
    ) -> int:
        return self._download(
            f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/audio",
            token,
            target,
            max_bytes=max_bytes,
            timeout_seconds=300.0,
            failure_message="下载生成音频失败",
        )

    def download_workbench_video(
        self,
        token: str,
        item_id: str,
        video_index: int,
        target: Path,
        *,
        max_bytes: int,
    ) -> int:
        return self._download(
            f"/api/workbench/tasks/{item_id}/videos/{int(video_index)}",
            token,
            target,
            max_bytes=max_bytes,
            timeout_seconds=120.0,
            failure_message="下载数字人视频失败",
        )

    def download_workbench_base_video(
        self,
        token: str,
        item_id: str,
        target: Path,
        *,
        max_bytes: int,
    ) -> int:
        return self._download(
            f"/api/workbench/tasks/{item_id}/base-video",
            token,
            target,
            max_bytes=max_bytes,
            timeout_seconds=300.0,
            failure_message="下载基础视频失败",
        )

    def download_h3_segment_video(
        self,
        token: str,
        segment_id: str,
        target: Path,
        *,
        max_bytes: int,
    ) -> int:
        clean_segment_id = str(segment_id or "").strip()
        if not clean_segment_id:
            raise ValueError("H3 分段编号不能为空")
        return self._download(
            f"/api/workbench/h3-segments/{clean_segment_id}/video",
            token,
            target,
            max_bytes=max_bytes,
            timeout_seconds=300.0,
            failure_message="下载 H3 标准化分段失败",
        )

    def _download(
        self,
        path: str,
        token: str,
        target: Path,
        *,
        max_bytes: int,
        timeout_seconds: float,
        failure_message: str,
    ) -> int:
        request = Request(
            f"{self.base_url}{path}",
            method="GET",
            headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with urlopen(
                request, timeout=max(self.timeout_seconds, timeout_seconds)
            ) as response:
                with target.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise AuthCenterError("远程文件超过工作台允许的文件大小", status_code=413)
                        output.write(chunk)
        except HTTPError as exc:
            raw = exc.read()
            target.unlink(missing_ok=True)
            raise AuthCenterError(
                self._detail(raw) or f"数字人网站拒绝下载（HTTP {exc.code}）",
                status_code=int(exc.code),
            ) from exc
        except (URLError, OSError, TimeoutError) as exc:
            target.unlink(missing_ok=True)
            raise AuthCenterConnectionError(
                f"{failure_message}，请检查数字人网站是否在线"
            ) from exc
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        if size <= 0:
            target.unlink(missing_ok=True)
            raise AuthCenterError("数字人网站返回了空文件")
        return size

    def _multipart_post(
        self,
        path: str,
        fields: dict[str, Any],
        files: list[tuple[str, str, bytes, str]],
    ) -> dict[str, Any]:
        boundary = f"----jyd-{secrets.token_hex(16)}"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    (
                        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    ).encode("utf-8"),
                    str(value).lower().encode("utf-8")
                    if isinstance(value, bool)
                    else str(value).encode("utf-8"),
                    b"\r\n",
                ]
            )
        for field_name, filename, content, content_type in files:
            safe_filename = filename.replace('"', "")
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    (
                        f'Content-Disposition: form-data; name="{field_name}"; '
                        f'filename="{safe_filename}"\r\n'
                    ).encode("utf-8"),
                    f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                    content,
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        request = Request(
            f"{self.base_url}{path}",
            data=b"".join(chunks),
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )
        return self._read_json_response(request, timeout_seconds=300.0)

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return self._read_json_response(
            request,
            timeout_seconds=(
                self.timeout_seconds
                if timeout_seconds is None
                else max(self.timeout_seconds, float(timeout_seconds))
            ),
        )

    def _read_json_response(
        self, request: Request, *, timeout_seconds: float
    ) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200))
        except HTTPError as exc:
            raw = exc.read()
            message = self._detail(raw) or f"数字人网站拒绝请求（HTTP {exc.code}）"
            raise AuthCenterError(message, status_code=int(exc.code)) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise AuthCenterConnectionError(
                f"无法连接数字人网站 {self.base_url}，请确认数字人网站已经启动"
            ) from exc
        if status < 200 or status >= 300:
            raise AuthCenterError(self._detail(raw) or f"数字人网站返回 HTTP {status}", status_code=status)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthCenterError(
                "数字人网站返回了无法识别的数据",
                status_code=502,
                error_code="DIGITAL_HUMAN_INVALID_RESPONSE",
                retryable=True,
            ) from exc
        if not isinstance(data, dict):
            raise AuthCenterError(
                "数字人网站返回格式错误",
                status_code=502,
                error_code="DIGITAL_HUMAN_INVALID_RESPONSE",
                retryable=True,
            )
        return data

    @staticmethod
    def _detail(raw: bytes) -> str:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if not isinstance(data, dict):
            return ""
        detail = str(data.get("detail", "")).strip()
        errors = data.get("errors")
        if isinstance(errors, list):
            messages = [
                str(item.get("message") or "").strip()
                for item in errors
                if isinstance(item, dict) and str(item.get("message") or "").strip()
            ]
            if messages:
                return f"{detail}：{'；'.join(messages)}" if detail else "；".join(messages)
        return detail
