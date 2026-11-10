from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .draft_import_analyzer import DEFAULT_HASH_LIMIT_BYTES
from .local_collector import LocalCollectorService, LocalCollectorSettings
from .runtime_paths import resource_path


FRONTEND_ROOT = resource_path("apps", "collector", "frontend")
LAN_WEBSITE_ORIGIN_REGEX = (
    r"^https?://(?:"
    r"localhost|127(?:\.\d{1,3}){3}|"
    r"10(?:\.\d{1,3}){3}|"
    r"192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.(?:local|lan))?"
    r")(?:\:\d{1,5})?$"
)


def create_local_collector_app(
    settings: LocalCollectorSettings | None = None,
) -> FastAPI:
    service = LocalCollectorService(settings)
    app = FastAPI(title="Jianying Local Collector", version="0.1.0")
    app.state.collector_service = service

    configured_server = urlsplit(service.settings.render_server_url)
    configured_origin = (
        f"{configured_server.scheme}://{configured_server.netloc}"
        if configured_server.scheme and configured_server.netloc
        else ""
    )
    allowed_origins = {
        "http://127.0.0.1:8010",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }
    if configured_origin:
        allowed_origins.add(configured_origin)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(allowed_origins),
        allow_origin_regex=LAN_WEBSITE_ORIGIN_REGEX,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_private_network=True,
    )

    if FRONTEND_ROOT.exists():
        app.mount(
            "/collector-static",
            StaticFiles(directory=str(FRONTEND_ROOT)),
            name="collector-static",
        )

    @app.get("/")
    def frontend() -> FileResponse:
        index_path = FRONTEND_ROOT / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=404, detail=f"采集器前端不存在: {index_path}")
        return FileResponse(index_path)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "mode": "local_collector", **service.get_config()}

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return service.get_config()

    @app.post("/api/config/draft-root")
    def set_draft_root(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return service.set_draft_root(str(payload.get("draft_root", "")))
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/config/server-url")
    def set_server_url(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return service.set_render_server_url(str(payload.get("render_server_url", "")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/config/access-token")
    def set_access_token(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.set_access_token(str(payload.get("access_token", "")))

    def require_trusted_website(request: Request) -> None:
        origin = request.headers.get("origin", "").rstrip("/")
        configured = service.settings.render_server_url.rstrip("/")
        local_origins = {
            "http://127.0.0.1:8010",
            "http://localhost:8000",
            "http://127.0.0.1:8001",
            "http://localhost:8001",
        }
        if origin and origin != configured and origin not in local_origins:
            raise HTTPException(status_code=403, detail="当前网页无权调用本机文件选择器")

    @app.post("/api/local/select-media")
    def select_media(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        require_trusted_website(request)
        try:
            return service.select_media_file(str(payload.get("media_kind", "video")))
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/config/personal-library-root")
    def set_personal_library_root(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        require_trusted_website(request)
        try:
            return service.set_personal_library_root(
                str(payload.get("personal_library_root", ""))
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/local/select-output-folder")
    def select_output_folder(request: Request) -> dict[str, Any]:
        require_trusted_website(request)
        try:
            return service.select_output_folder()
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/drafts")
    def list_drafts() -> list[dict[str, Any]]:
        try:
            return service.list_drafts()
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/drafts/analyze")
    def analyze_draft(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            hash_mode = str(payload.get("hash_mode", "small_files"))
            if hash_mode == "all":
                hash_limit = None
            elif hash_mode == "none":
                hash_limit = -1
            else:
                hash_limit = DEFAULT_HASH_LIMIT_BYTES
            return service.analyze_draft(
                str(payload.get("draft_dir", "")),
                hash_limit_bytes=hash_limit,
            )
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/drafts/extract-fonts")
    def extract_fonts(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return service.extract_fonts(
                str(payload.get("draft_dir", "")),
                replace=bool(payload.get("replace", False)),
            )
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/drafts/collect-personal-assets")
    def collect_personal_assets(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return service.collect_personal_assets(
                str(payload.get("draft_dir", "")),
                replace=bool(payload.get("replace", False)),
                kinds=[str(item) for item in payload.get("kinds", [])]
                if isinstance(payload.get("kinds"), list)
                else None,
                upload=bool(payload.get("upload", False)),
                server_url=str(payload.get("server_url", "")),
            )
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/upload-plans")
    def create_upload_plan(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        policies = payload.get("policies", {})
        if not isinstance(policies, dict):
            raise HTTPException(status_code=400, detail="policies 必须是对象")
        try:
            return service.create_upload_plan(
                str(payload.get("report_id", "")),
                {str(key): str(value) for key, value in policies.items()},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/upload-plans/{plan_id}")
    def get_upload_plan(plan_id: str) -> dict[str, Any]:
        try:
            return service.get_upload_plan(plan_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/upload-plans/{plan_id}/upload")
    def upload_plan(plan_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        try:
            return service.upload_plan(
                plan_id,
                template_name=str(payload.get("template_name", "")),
                template_lifecycle=str(payload.get("template_lifecycle", "")),
                server_url=str(payload.get("server_url", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, RuntimeError, ConnectionError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/reports/{report_id}")
    def get_report(report_id: str) -> dict[str, Any]:
        try:
            return service.get_report(report_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app
