from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .draft_factory import probe_video_duration_us
from .project_store import ProjectStore


LTX_MODE = "ltx_lip_sync"


class LtxWorkbenchError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 503):
        super().__init__(message)
        self.status_code = int(status_code)


class LtxWorkbenchClient:
    """Loopback-only client for the bundled LTX base-video engine."""

    def __init__(
        self,
        base_url: str,
        manager_token: str,
        *,
        timeout_seconds: float = 1800.0,
    ):
        normalized = str(base_url or "").strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("LTX 引擎必须是本机回环地址")
        if parsed.query or parsed.fragment:
            raise ValueError("LTX 引擎地址不能包含查询参数或锚点")
        token = str(manager_token or "").strip()
        if not token:
            raise ValueError("LTX 引擎缺少工作台管理令牌")
        self.base_url = normalized
        self.manager_token = token
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def sync(self, token: str, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", self._project_path(project_id, "sync"), token, payload)

    def state(self, token: str, project_id: str, item_ids: list[str]) -> dict[str, Any]:
        return self._json(
            "POST", self._project_path(project_id, "state"), token, {"item_ids": item_ids}
        )

    def upload_source_video(
        self,
        token: str,
        project_id: str,
        item_id: str,
        path: Path,
        *,
        filename: str,
    ) -> dict[str, Any]:
        url_path = (
            self._project_path(project_id, "items")
            + f"/{quote(item_id, safe='')}/source-video?filename={quote(filename, safe='')}"
        )
        source_path = Path(path)
        with source_path.open("rb") as source:
            return self._request_json(
                Request(
                    f"{self.base_url}{url_path}",
                    data=source,
                    headers={
                        **self._headers(token),
                        "Content-Type": mimetypes.guess_type(filename)[0]
                        or "application/octet-stream",
                        "Content-Length": str(source_path.stat().st_size),
                    },
                    method="PUT",
                )
            )

    def start(
        self,
        token: str,
        project_id: str,
        item_ids: list[str],
        *,
        cost_confirmed: bool,
        force_new: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            self._project_path(project_id, "start"),
            token,
            {
                "item_ids": item_ids,
                "cost_confirmed": cost_confirmed,
                "force_new": force_new,
            },
        )

    def refresh(self, token: str, project_id: str, item_ids: list[str]) -> dict[str, Any]:
        return self._json(
            "POST", self._project_path(project_id, "refresh"), token, {"item_ids": item_ids}
        )

    def retry(self, token: str, project_id: str, item_id: str) -> dict[str, Any]:
        path = self._project_path(project_id, "items") + (
            f"/{quote(item_id, safe='')}/retry"
        )
        return self._json("POST", path, token, {"cost_confirmed": True})

    def download_base_video(
        self, token: str, project_id: str, item_id: str, destination: Path
    ) -> Path:
        path = self._project_path(project_id, "items") + (
            f"/{quote(item_id, safe='')}/base-video"
        )
        request = Request(
            f"{self.base_url}{path}", headers=self._headers(token), method="GET"
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
        except HTTPError as exc:
            self._raise_http(exc)
        except (URLError, TimeoutError, OSError) as exc:
            raise LtxWorkbenchError("无法连接本地视频生成服务，请重新启动工作台") from exc
        if not destination.is_file() or destination.stat().st_size <= 0:
            destination.unlink(missing_ok=True)
            raise LtxWorkbenchError("视频生成服务返回了空文件")
        return destination

    def _project_path(self, project_id: str, action: str) -> str:
        return (
            f"/api/integrations/jyd/projects/{quote(str(project_id), safe='')}"
            f"/{action}"
        )

    def _headers(self, token: str) -> dict[str, str]:
        access_token = str(token or "").strip()
        if not access_token:
            raise LtxWorkbenchError("数字人账号登录已失效", status_code=401)
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-Workbench-Manager-Token": self.manager_token,
        }

    def _json(
        self,
        method: str,
        path: str,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                **self._headers(token),
                "Content-Type": "application/json; charset=utf-8",
            },
            method=method,
        )
        return self._request_json(request)

    def _request_json(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            self._raise_http(exc)
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise LtxWorkbenchError("无法连接本地视频生成服务，请重新启动工作台") from exc
        if not isinstance(payload, dict):
            raise LtxWorkbenchError("视频生成服务返回格式无效")
        return payload

    @staticmethod
    def _raise_http(exc: HTTPError) -> None:
        message = f"视频生成服务请求失败（HTTP {exc.code}）"
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if detail:
                message = str(detail)
        except (OSError, UnicodeDecodeError, ValueError):
            pass
        raise LtxWorkbenchError(message, status_code=int(exc.code)) from exc


class ProjectLtxCoordinator:
    """Use LTX only for base video; JYD remains authoritative for everything else."""

    def __init__(
        self,
        store: ProjectStore,
        client: LtxWorkbenchClient,
        *,
        storage_root: Path,
    ):
        self.store = store
        self.client = client
        self.storage_root = Path(storage_root).resolve()

    @staticmethod
    def _assert_mode(project: dict[str, Any]) -> None:
        settings = project.get("settings") if isinstance(project.get("settings"), dict) else {}
        if settings.get("generation_mode") != LTX_MODE:
            raise ValueError("请先把画面生成方式切换为视频对口型")

    @staticmethod
    def _target_items(
        project: dict[str, Any], item_ids: list[str] | None
    ) -> list[dict[str, Any]]:
        items = [item for item in project.get("items", []) if isinstance(item, dict)]
        if item_ids is None:
            return items
        clean = list(dict.fromkeys(str(value or "").strip() for value in item_ids))
        if not clean or any(not value for value in clean):
            raise ValueError("至少需要选择一条脚本")
        by_id = {str(item.get("item_id") or ""): item for item in items}
        if any(item_id not in by_id for item_id in clean):
            raise ValueError("选择中包含不属于当前项目的脚本")
        return [by_id[item_id] for item_id in clean]

    @staticmethod
    def _latest_minimax_audio(item: dict[str, Any]) -> dict[str, Any] | None:
        current = (item.get("outputs") or {}).get("audio") or {}
        if current.get("source_type") == "minimax" and current.get("status") == "READY":
            return current
        history = (item.get("asset_history") or {}).get("audio") or []
        candidates = [
            value
            for value in history
            if isinstance(value, dict)
            and value.get("source_type") == "minimax"
            and value.get("status") == "READY"
        ]
        return candidates[-1] if candidates else None

    @staticmethod
    def _sync_payload(project: dict[str, Any]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for item in project.get("items", []):
            if not isinstance(item, dict):
                continue
            audio = ProjectLtxCoordinator._latest_minimax_audio(item) or {}
            external = audio.get("external_ref") if isinstance(audio.get("external_ref"), dict) else {}
            audio_payload = None
            if (
                audio.get("status") == "READY"
                and audio.get("source_type") == "minimax"
                and external.get("batch_id")
                and external.get("remote_item_id")
            ):
                audio_payload = {
                    "batch_id": str(external["batch_id"]),
                    "item_id": str(external["remote_item_id"]),
                    "generation_version": int(external.get("generation_version") or 1),
                }
            items.append(
                {
                    "item_id": str(item.get("item_id") or ""),
                    "row_key": str(item.get("row_key") or ""),
                    "script_text": str(item.get("script_text") or ""),
                    "audio": audio_payload,
                }
            )
        return {"name": str(project.get("name") or "视频对口型"), "items": items}

    def sync(
        self, owner_user_id: str, project_id: str, token: str
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        self._assert_mode(project)
        result = self.client.sync(token, project_id, self._sync_payload(project))
        state = result.get("state") if isinstance(result.get("state"), dict) else result
        return {"project": project, "ltx": state}

    def state(
        self, owner_user_id: str, project_id: str, token: str
    ) -> dict[str, Any]:
        synced = self.sync(owner_user_id, project_id, token)
        return synced

    def upload_source_video(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        token: str,
        path: Path,
        *,
        filename: str,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        self._assert_mode(project)
        self._target_items(project, [item_id])
        self.client.sync(token, project_id, self._sync_payload(project))
        state = self.client.upload_source_video(
            token, project_id, item_id, path, filename=filename
        )
        project = self.store.invalidate_item_composition(
            owner_user_id,
            project_id,
            item_id,
            reason="LTX_SOURCE_VIDEO_CHANGED",
        )
        return {"project": project, "ltx": state}

    def start(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        *,
        item_ids: list[str] | None,
        cost_confirmed: bool,
        force_new: bool = False,
    ) -> dict[str, Any]:
        if not cost_confirmed:
            raise ValueError("请确认每个分段的生成与清晰处理费用")
        project = self.store.get_project(owner_user_id, project_id)
        self._assert_mode(project)
        targets = self._target_items(project, item_ids)
        self.client.sync(token, project_id, self._sync_payload(project))
        state = self.client.start(
            token,
            project_id,
            [str(item["item_id"]) for item in targets],
            cost_confirmed=True,
            force_new=force_new,
        )
        return {"project": project, "ltx": state}

    def refresh(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        self._assert_mode(project)
        self.client.sync(token, project_id, self._sync_payload(project))
        item_ids = [str(item["item_id"]) for item in project.get("items", [])]
        state = self.client.refresh(token, project_id, item_ids)
        project = self._import_ready_videos(owner_user_id, project_id, token, project, state)
        return {"project": project, "ltx": state}

    def retry(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        token: str,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        self._assert_mode(project)
        self._target_items(project, [item_id])
        self.client.sync(token, project_id, self._sync_payload(project))
        state = self.client.retry(token, project_id, item_id)
        return {"project": project, "ltx": state}

    def _import_ready_videos(
        self,
        owner_user_id: str,
        project_id: str,
        token: str,
        project: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        by_id = {
            str(item.get("item_id") or ""): item
            for item in project.get("items", [])
            if isinstance(item, dict)
        }
        for engine_item in state.get("items", []):
            if not isinstance(engine_item, dict) or not engine_item.get("base_video_ready"):
                continue
            item_id = str(engine_item.get("item_id") or "")
            item = by_id.get(item_id)
            if item is None:
                continue
            signature_payload = {
                "remote_item_id": engine_item.get("remote_item_id"),
                "source_video": engine_item.get("source_video"),
                "audio_asset_id": ((item.get("outputs") or {}).get("audio") or {}).get("asset_id"),
            }
            signature = hashlib.sha256(
                json.dumps(signature_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            current = (item.get("outputs") or {}).get("base_video") or {}
            current_ref = current.get("external_ref") if isinstance(current.get("external_ref"), dict) else {}
            if current.get("source_type") == "ltx" and current_ref.get("engine_signature") == signature:
                continue
            item_dir = self.storage_root / "projects" / hashlib.sha256(
                f"{owner_user_id}\0{project_id}\0{item_id}".encode("utf-8")
            ).hexdigest()[:32] / "ltx"
            destination = item_dir / f"base-{signature[:16]}.mp4"
            self.client.download_base_video(token, project_id, item_id, destination)
            try:
                duration_us = probe_video_duration_us(destination)
            except (OSError, RuntimeError, ValueError):
                duration_us = 0
            asset = self.store.add_asset(
                owner_user_id=owner_user_id,
                project_id=project_id,
                item_id=item_id,
                asset_type="base_video",
                source_type="ltx",
                status="READY",
                filename=f"{item.get('row_key') or item_id}-口型高清.mp4",
                managed_path=str(destination),
                external_ref={
                    "engine_signature": signature,
                    "remote_batch_id": engine_item.get("remote_batch_id"),
                    "remote_item_id": engine_item.get("remote_item_id"),
                    "source_video_sha256": (engine_item.get("source_video") or {}).get("sha256"),
                },
                metadata={
                    "duration_us": duration_us,
                    "segment_count": len(engine_item.get("segments") or []),
                    "source_engine": LTX_MODE,
                    "enhanced_by": "seedvr2",
                },
                make_current=True,
            )
            fresh = self.store.get_project(owner_user_id, project_id)
            fresh_item = next(
                value for value in fresh.get("items", []) if value.get("item_id") == item_id
            )
            subtitles = dict(fresh_item.get("subtitles") or {})
            subtitles["bound_video_asset_id"] = asset["asset_id"]
            subtitles["render_cues"] = []
            subtitles["status"] = "READY" if subtitles.get("raw_cues") else "NOT_AVAILABLE"
            project = self.store.set_item_subtitles(
                owner_user_id, project_id, item_id, subtitles
            )
            by_id[item_id] = next(
                value for value in project.get("items", []) if value.get("item_id") == item_id
            )
        return project
