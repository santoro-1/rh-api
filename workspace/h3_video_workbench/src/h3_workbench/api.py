from __future__ import annotations

import hashlib
import json
import mimetypes
import secrets
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, Cookie, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from .cloud_client import CloudClient, CloudError
from .config import PROJECT_ROOT, Settings, load_settings
from .project_store import ProjectStore, StoreError
from .jyd_bridge import JYDBridge, JYDBridgeError
from .postprocess import (
    H3LocalPostprocessError,
    build_jyd_handoff,
    prepare_h3_jyd_assets,
    write_jyd_handoff,
)


STATIC_DIR = PROJECT_ROOT / "apps" / "frontend"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}


class SessionRegistry:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def create(self, payload: dict[str, Any]) -> str:
        token = str(payload.get("access_token") or "")
        if not token:
            raise CloudError("云端登录响应缺少访问令牌")
        session_id = secrets.token_urlsafe(32)
        self.values[session_id] = {
            "access_token": token,
            "user": payload.get("user") or {},
        }
        return session_id


def create_app(
    settings: Settings | None = None,
    cloud_client: CloudClient | None = None,
    jyd_bridge: JYDBridge | None = None,
) -> FastAPI:
    active = settings or load_settings()
    active.upload_dir.mkdir(parents=True, exist_ok=True)
    active.result_dir.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(active.database_path)
    store.initialize()
    cloud = cloud_client or CloudClient(active.cloud_base_url)
    jyd = jyd_bridge or JYDBridge(active)
    sessions = SessionRegistry()
    app = FastAPI(title="MiniMax H3 多参考工作台", version="0.1.0")
    app.state.store = store
    app.state.cloud = cloud
    app.state.sessions = sessions
    app.state.jyd = jyd

    def auth(cookie: str | None) -> dict[str, Any]:
        value = sessions.values.get(cookie or "")
        if value is None:
            raise HTTPException(status_code=401, detail="请先登录数字人网站账号")
        return value

    def owner(value: dict[str, Any]) -> str:
        user = value.get("user") or {}
        result = user.get("id") or user.get("user_id") or user.get("username")
        if result is None:
            raise HTTPException(status_code=401, detail="云端账号缺少稳定身份标识")
        return str(result)

    def max_user_reference_images(value: dict[str, Any]) -> int:
        capability = value.get("adapter_capability") or {}
        try:
            result = int(capability.get("max_user_reference_images", 5))
        except (TypeError, ValueError):
            result = 5
        return min(max(result, 0), 20)

    def quote_invalidation(
        project_value: dict[str, Any],
        *,
        defaults: object | None = None,
        rows: object | None = None,
        selected_account_ids: object | None = None,
        identity_images: object | None = None,
        force_change: bool = False,
    ) -> dict[str, object]:
        changed = force_change or any(
            candidate is not None and project_value[field] != candidate
            for field, candidate in (
                ("defaults", defaults),
                ("rows", rows),
                ("selected_account_ids", selected_account_ids),
                ("identity_images", identity_images),
            )
        )
        if not changed or not project_value["remote_batch_id"]:
            return {}
        status = str((project_value.get("snapshot") or {}).get("status") or "")
        if status != "AWAITING_COST_CONFIRMATION":
            raise HTTPException(
                status_code=409,
                detail="H3 批次已提交，费用相关输入已锁定；请新建项目后再修改",
            )
        return {"remote_batch_id": None, "snapshot": None}

    @app.exception_handler(StoreError)
    async def store_error(request, exc: StoreError):
        del request
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(CloudError)
    async def cloud_error(request, exc: CloudError):
        del request
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.post("/api/session/login")
    def login(payload: dict[str, Any] = Body(...)):
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not username or not password:
            raise HTTPException(status_code=422, detail="请输入账号和密码")
        login_payload = cloud.login(username, password)
        session_id = sessions.create(login_payload)
        response = JSONResponse({"user": login_payload.get("user") or {}})
        response.set_cookie(
            "h3_session",
            session_id,
            httponly=True,
            samesite="strict",
            max_age=12 * 60 * 60,
        )
        return response

    @app.post("/api/session/logout")
    def logout(h3_session: str | None = Cookie(None)):
        sessions.values.pop(h3_session or "", None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("h3_session")
        return response

    @app.get("/api/session")
    def current_session(h3_session: str | None = Cookie(None)):
        value = auth(h3_session)
        return {"user": value["user"], "cloud_base_url": active.cloud_base_url}

    @app.get("/api/accounts")
    def accounts(h3_session: str | None = Cookie(None)):
        value = auth(h3_session)
        payload = cloud.accounts(value["access_token"])
        value["adapter_capability"] = payload.get("adapter_capability") or {}
        return payload

    @app.get("/api/audio-sources")
    def audio_sources(h3_session: str | None = Cookie(None)):
        value = auth(h3_session)
        return cloud.audio_sources(value["access_token"])

    @app.get("/api/audio-sources/{batch_id}/{item_id}/audio")
    def audio_source_preview(
        batch_id: str,
        item_id: str,
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        payload = cloud.audio_sources(value["access_token"])
        source = next(
            (
                candidate
                for candidate in payload.get("sources") or []
                if str(candidate.get("audio_batch_id")) == batch_id
                and str(candidate.get("audio_item_id")) == item_id
            ),
            None,
        )
        if source is None:
            raise HTTPException(status_code=404, detail="MiniMax 音频不可试听或已被占用")
        relative_url = str(source.get("audio_download_url") or "").strip()
        if not relative_url:
            raise HTTPException(status_code=409, detail="MiniMax 音频试听地址尚未准备完成")
        target = active.upload_dir / "audio-preview" / f"{uuid.uuid4().hex}.mp3"
        cloud.download(value["access_token"], relative_url, target)
        return FileResponse(
            target,
            media_type="audio/mpeg",
            filename="minimax-preview.mp3",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    @app.post("/api/projects/{project_id}/review-audio")
    def review_project_audio(
        project_id: str,
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        reviewed = []
        for row in project_value.get("rows") or []:
            if not row.get("audio_batch_id") or not row.get("audio_item_id"):
                raise HTTPException(
                    status_code=409,
                    detail=f"{row.get('row_id') or '脚本行'} 尚未选择 MiniMax 完整音频",
                )
            reviewed.append(
                cloud.approve_audio_source(
                    value["access_token"],
                    audio_batch_id=str(row["audio_batch_id"]),
                    audio_item_id=str(row["audio_item_id"]),
                    audio_generation_version=int(
                        row.get("audio_generation_version") or 1
                    ),
                )
            )
        return {"reviewed": reviewed}

    @app.get("/api/projects")
    def projects(h3_session: str | None = Cookie(None)):
        value = auth(h3_session)
        return {"projects": store.list(owner(value))}

    @app.post("/api/projects")
    def create_project(
        payload: dict[str, Any] = Body(...),
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        return store.create(owner(value), str(payload.get("name") or ""))

    @app.get("/api/projects/{project_id}")
    def project(project_id: str, h3_session: str | None = Cookie(None)):
        value = auth(h3_session)
        return store.get(owner(value), project_id)

    @app.put("/api/projects/{project_id}")
    def save_project(
        project_id: str,
        payload: dict[str, Any] = Body(...),
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        rows = payload.get("rows")
        defaults = payload.get("defaults")
        selected = payload.get("selected_account_ids")
        if not isinstance(rows, list) or not isinstance(defaults, dict):
            raise HTTPException(status_code=422, detail="项目参数格式错误")
        if not isinstance(selected, list) or any(type(item) is not int for item in selected):
            raise HTTPException(status_code=422, detail="账号选择格式错误")
        project_value = store.get(owner(value), project_id)
        invalidation = quote_invalidation(
            project_value,
            defaults=defaults,
            rows=rows,
            selected_account_ids=selected,
        )
        return store.update(
            owner(value),
            project_id,
            name=str(payload.get("name") or "未命名 H3 项目").strip(),
            defaults=defaults,
            rows=rows,
            selected_account_ids=selected,
            **invalidation,
        )

    def save_upload(file: UploadFile, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as stream:
            shutil.copyfileobj(file.file, stream, length=1024 * 1024)
        if target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="上传文件为空")

    @app.post("/api/projects/{project_id}/identity-images")
    def upload_identity_image(
        project_id: str,
        file: UploadFile = File(...),
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        images = list(project_value["identity_images"])
        maximum = max_user_reference_images(value)
        if len(images) >= maximum:
            raise HTTPException(
                status_code=409,
                detail=f"当前 H3 适配器的人物参考图最多 {maximum} 张",
            )
        original = Path(file.filename or "").name
        suffix = Path(original).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise HTTPException(status_code=400, detail="人物图格式不受支持")
        invalidation = quote_invalidation(project_value, force_change=True)
        path = active.upload_dir / project_id / "identity" / f"{uuid.uuid4().hex}{suffix}"
        try:
            save_upload(file, path)
            uploaded = cloud.upload_asset(
                value["access_token"],
                "image",
                path,
                original,
                file.content_type or mimetypes.guess_type(original)[0] or "image/png",
            )
        finally:
            path.unlink(missing_ok=True)
        images.append({"asset_id": str(uploaded["asset_id"]), "original_name": original})
        return store.update(
            owner(value),
            project_id,
            identity_images=images,
            **invalidation,
        )

    @app.delete("/api/projects/{project_id}/identity-images/{asset_id}")
    def remove_identity_image(
        project_id: str,
        asset_id: str,
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        images = [
            image for image in project_value["identity_images"]
            if str(image.get("asset_id")) != asset_id
        ]
        invalidation = quote_invalidation(
            project_value,
            identity_images=images,
        )
        return store.update(
            owner(value),
            project_id,
            identity_images=images,
            **invalidation,
        )

    @app.post("/api/projects/{project_id}/rows/{row_id}/reference-video")
    def upload_reference_video(
        project_id: str,
        row_id: str,
        file: UploadFile = File(...),
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        rows = list(project_value["rows"])
        row = next((item for item in rows if str(item.get("row_id")) == row_id), None)
        if row is None:
            raise HTTPException(status_code=404, detail="脚本行不存在，请先保存项目")
        original = Path(file.filename or "").name
        suffix = Path(original).suffix.lower()
        if suffix not in VIDEO_SUFFIXES:
            raise HTTPException(status_code=400, detail="参考视频格式不受支持")
        invalidation = quote_invalidation(project_value, force_change=True)
        row_upload_key = hashlib.sha256(row_id.encode("utf-8")).hexdigest()[:16]
        path = (
            active.upload_dir
            / project_id
            / row_upload_key
            / f"{uuid.uuid4().hex}{suffix}"
        )
        try:
            save_upload(file, path)
            uploaded = cloud.upload_asset(
                value["access_token"],
                "video",
                path,
                original,
                file.content_type or mimetypes.guess_type(original)[0] or "video/mp4",
            )
        finally:
            path.unlink(missing_ok=True)
        row["video_asset_id"] = str(uploaded["asset_id"])
        row["video_original_name"] = original
        return store.update(owner(value), project_id, rows=rows, **invalidation)

    @app.post("/api/projects/{project_id}/prepare")
    def prepare(
        project_id: str,
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        if not project_value["rows"]:
            raise HTTPException(status_code=409, detail="请至少添加一行脚本")
        payload = {
            "name": project_value["name"],
            "correlation_id": project_id,
            "reference_image_asset_ids": [
                image["asset_id"] for image in project_value["identity_images"]
            ],
            "selected_account_ids": project_value["selected_account_ids"],
            "defaults": project_value["defaults"],
            "rows": project_value["rows"],
        }
        contract_sha256 = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        payload["request_key"] = f"h3-{project_id[:12]}-{contract_sha256[:20]}"
        snapshot = cloud.prepare(value["access_token"], payload)
        return store.update(
            owner(value),
            project_id,
            remote_batch_id=str(snapshot["batch_id"]),
            snapshot=snapshot,
        )

    @app.post("/api/projects/{project_id}/confirm")
    def confirm(
        project_id: str,
        payload: dict[str, Any] = Body(...),
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        if payload.get("cost_confirmed") is not True:
            raise HTTPException(status_code=409, detail="请明确确认 H3 分段调用费用")
        if not project_value["remote_batch_id"]:
            raise HTTPException(status_code=409, detail="请先计算费用")
        snapshot = cloud.confirm(
            value["access_token"], project_value["remote_batch_id"]
        )
        return store.update(owner(value), project_id, snapshot=snapshot)

    @app.post("/api/projects/{project_id}/refresh")
    def refresh(project_id: str, h3_session: str | None = Cookie(None)):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        if not project_value["remote_batch_id"]:
            return project_value
        snapshot = cloud.batch(value["access_token"], project_value["remote_batch_id"])
        return store.update(owner(value), project_id, snapshot=snapshot)

    def snapshot_segment(project_value: dict[str, Any], segment_id: str) -> dict[str, Any]:
        for item in (project_value.get("snapshot") or {}).get("items") or []:
            for segment in item.get("segments") or []:
                if str(segment.get("segment_id") or "") == segment_id:
                    return segment
        raise HTTPException(status_code=404, detail="H3 云端分段不存在")

    @app.get("/api/projects/{project_id}/segments/{segment_id}/video")
    def download_segment_for_review(
        project_id: str,
        segment_id: str,
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        segment = snapshot_segment(project_value, segment_id)
        relative_url = str(segment.get("normalized_video_download_url") or "").strip()
        if segment.get("status") != "SUCCESS" or not relative_url:
            raise HTTPException(status_code=409, detail="H3 分段成片尚未准备完成")
        try:
            index = int(segment.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        target = active.upload_dir / "video-review" / f"{uuid.uuid4().hex}.mp4"
        cloud.download(value["access_token"], relative_url, target)
        return FileResponse(
            target,
            media_type="video/mp4",
            filename=f"h3-segment-{index + 1:03d}.mp4",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    def validated_handoff_segments(item: dict[str, Any]) -> list[dict[str, Any]]:
        raw_segments = item.get("segments") or []
        if not isinstance(raw_segments, list) or not raw_segments:
            raise HTTPException(status_code=409, detail="H3 分段尚未全部标准化完成")
        normalized: list[tuple[int, dict[str, Any]]] = []
        segment_ids: set[str] = set()
        for raw in raw_segments:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=409, detail="H3 云端分段结构损坏")
            try:
                index = int(raw["index"])
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=409, detail="H3 云端分段序号损坏") from exc
            segment_id = str(raw.get("segment_id") or "").strip()
            if index < 0 or not segment_id or segment_id in segment_ids:
                raise HTTPException(status_code=409, detail="H3 云端分段编号重复或无效")
            if raw.get("status") != "SUCCESS" or not str(
                raw.get("normalized_video_download_url") or ""
            ).strip():
                raise HTTPException(status_code=409, detail="H3 分段尚未全部标准化完成")
            segment_ids.add(segment_id)
            normalized.append((index, raw))
        normalized.sort(key=lambda pair: pair[0])
        if [index for index, _ in normalized] != list(range(len(normalized))):
            raise HTTPException(status_code=409, detail="H3 云端分段序号不连续")
        return [segment for _, segment in normalized]

    @app.post("/api/projects/{project_id}/segments/{segment_id}/regeneration/prepare")
    def prepare_regeneration(
        project_id: str,
        segment_id: str,
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        snapshot_segment(project_value, segment_id)
        return cloud.prepare_regeneration(value["access_token"], segment_id)

    @app.post("/api/projects/{project_id}/segments/{segment_id}/regeneration/confirm")
    def confirm_regeneration(
        project_id: str,
        segment_id: str,
        payload: dict[str, Any] = Body(...),
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        snapshot_segment(project_value, segment_id)
        if payload.get("cost_confirmed") is not True:
            raise HTTPException(status_code=409, detail="请明确确认 H3 级联重生成费用")
        request_key = str(payload.get("request_key") or "").strip()
        quote_token = str(payload.get("quote_token") or "").strip()
        if not request_key or not quote_token:
            raise HTTPException(status_code=422, detail="H3 重生成确认参数不完整")
        snapshot = cloud.confirm_regeneration(
            value["access_token"],
            segment_id,
            request_key=request_key,
            quote_token=quote_token,
        )
        return store.update(owner(value), project_id, snapshot=snapshot)

    @app.post("/api/projects/{project_id}/segments/{segment_id}/retry/prepare")
    def prepare_segment_retry(
        project_id: str,
        segment_id: str,
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        snapshot_segment(project_value, segment_id)
        return cloud.prepare_retry(value["access_token"], segment_id)

    @app.post("/api/projects/{project_id}/segments/{segment_id}/retry/confirm")
    def confirm_segment_retry(
        project_id: str,
        segment_id: str,
        payload: dict[str, Any] = Body(...),
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        snapshot_segment(project_value, segment_id)
        request_key = str(payload.get("request_key") or "").strip()
        quote_token = str(payload.get("quote_token") or "").strip()
        cost_confirmed = payload.get("cost_confirmed")
        if not request_key or not quote_token or not isinstance(cost_confirmed, bool):
            raise HTTPException(status_code=422, detail="H3 重试确认参数不完整")
        snapshot = cloud.confirm_retry(
            value["access_token"],
            segment_id,
            request_key=request_key,
            quote_token=quote_token,
            cost_confirmed=cost_confirmed,
        )
        return store.update(owner(value), project_id, snapshot=snapshot)

    @app.post("/api/projects/{project_id}/segments/{segment_id}/cancel")
    def cancel_segment(
        project_id: str,
        segment_id: str,
        payload: dict[str, Any] = Body(...),
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        snapshot_segment(project_value, segment_id)
        request_key = str(payload.get("request_key") or "").strip()
        if not request_key:
            raise HTTPException(status_code=422, detail="H3 取消请求缺少幂等键")
        snapshot = cloud.cancel_segment(
            value["access_token"],
            segment_id,
            request_key=request_key,
        )
        return store.update(owner(value), project_id, snapshot=snapshot)

    @app.post("/api/projects/{project_id}/rows/{row_id}/jyd-handoff")
    def create_jyd_handoff(
        project_id: str,
        row_id: str,
        h3_session: str | None = Cookie(None),
    ):
        value = auth(h3_session)
        project_value = store.get(owner(value), project_id)
        snapshot = project_value.get("snapshot") or {}
        item = next(
            (
                candidate for candidate in snapshot.get("items") or []
                if str(candidate.get("row_id")) == row_id
            ),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="H3 云端脚本行不存在")
        local_row = next(
            (
                candidate for candidate in project_value.get("rows") or []
                if str(candidate.get("row_id")) == row_id
            ),
            None,
        )
        if local_row is None:
            raise HTTPException(status_code=404, detail="H3 本地脚本行不存在")
        segments = validated_handoff_segments(item)
        if not str(local_row.get("script_text") or "").strip():
            raise HTTPException(status_code=409, detail="H3 本地冻结脚本为空")
        row_hash = hashlib.sha256(row_id.encode("utf-8")).hexdigest()[:16]
        target_dir = active.result_dir / project_id / row_hash
        segment_paths = []
        segment_texts = []
        for segment in segments:
            segment_text = str(segment.get("script_text") or "").strip()
            if not segment_text:
                raise HTTPException(status_code=409, detail="H3 云端分段缺少冻结台词")
            target = target_dir / "segments" / f"segment-{int(segment['index']) + 1:03d}.mp4"
            cloud.download(
                value["access_token"],
                str(segment["normalized_video_download_url"]),
                target,
            )
            segment_paths.append(target)
            segment_texts.append(segment_text)
        input_cues_url = str(
            item.get("input_raw_cues_download_url")
            or item.get("raw_cues_download_url")
            or ""
        ).strip()
        input_cues_path = None
        if input_cues_url:
            input_cues_path = cloud.download(
                value["access_token"],
                input_cues_url,
                target_dir / "minimax-input-raw-cues.json",
            )
        try:
            assets = prepare_h3_jyd_assets(
                segment_paths=segment_paths,
                segment_texts=segment_texts,
                script_text=str(local_row.get("script_text") or ""),
                target_dir=target_dir,
            )
            handoff = build_jyd_handoff(
                project_id=f"{project_id}:{row_id}",
                silent_base_video=str(assets.silent_base_video_path),
                authoritative_h3_audio=str(assets.authoritative_audio_path),
                h3_segment_cues_asset=str(assets.subtitle_cues_path),
                h3_master_av=str(assets.master_av_path),
                segment_ids=[str(segment["segment_id"]) for segment in segments],
                row_key=row_id,
                script_text=str(local_row.get("script_text") or ""),
                input_raw_cues_asset=(str(input_cues_path) if input_cues_path else ""),
            )
            manifest = write_jyd_handoff(target_dir / "jyd-handoff.json", handoff)
            user = value.get("user") or {}
            jyd_import = jyd.import_handoff(
                manifest_path=manifest,
                owner_user_id=owner(value),
                owner_username=str(user.get("username") or ""),
                project_name=f"{project_value['name']} · {row_id}",
            )
        except (H3LocalPostprocessError, JYDBridgeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "schema": "h3.local-jyd-handoff.v2",
            "manifest_path": str(manifest),
            "h3_master_av_path": str(assets.master_av_path),
            "base_video_path": str(assets.silent_base_video_path),
            "full_audio_path": str(assets.authoritative_audio_path),
            "raw_cues_path": str(assets.subtitle_cues_path),
            "handoff": handoff,
            "jyd_import": jyd_import,
        }

    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
