from __future__ import annotations

import logging
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.database import engine
from app.routes import (
    admin,
    auth,
    batches,
    long_audio,
    media_worker_api,
    operations,
    runninghub_pool_admin,
    tasks,
    voices,
    workbench,
    workbench_ltx,
)
from app.services.deployment_drain import is_deployment_draining
from app.services.logging_config import configure_logging, log_event


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.app_env != "test":
            configure_logging("web")
            log_event(logger, "web.started", "Web 服务已启动")
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        settings.outputs_dir.mkdir(parents=True, exist_ok=True)
        settings.staged_assets_dir.mkdir(parents=True, exist_ok=True)
        settings.voice_sources_dir.mkdir(parents=True, exist_ok=True)
        settings.voice_creations_dir.mkdir(parents=True, exist_ok=True)
        settings.long_audio_dir.mkdir(parents=True, exist_ok=True)
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(
        title="数字人视频生成中转站",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key,
        https_only=settings.cookie_secure,
        same_site="lax",
        max_age=60 * 60 * 24 * 7,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(settings.allowed_hosts),
    )
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
        name="static",
    )
    app.state.rate_limits = defaultdict(deque)

    @app.middleware("http")
    async def deployment_drain_middleware(request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        allowed_write = path in {"/login", "/logout"} or path.startswith(
            "/api/media-worker/"
        )
        if (
            method not in {"GET", "HEAD", "OPTIONS"}
            and not allowed_write
            and is_deployment_draining(settings)
        ):
            message = (
                "系统正在发布新版本，已暂停新建或修改任务；"
                "现有任务、浏览、预览和下载不受影响，请稍后重试。"
            )
            headers = {"Retry-After": "15", "X-Deployment-Draining": "1"}
            if path.startswith("/api/"):
                return JSONResponse(
                    {"detail": message, "code": "DEPLOYMENT_DRAINING"},
                    status_code=503,
                    headers=headers,
                )
            return PlainTextResponse(message, status_code=503, headers=headers)
        return await call_next(request)

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(runninghub_pool_admin.router)
    app.include_router(tasks.router)
    app.include_router(batches.router)
    app.include_router(voices.router)
    app.include_router(long_audio.router)
    app.include_router(media_worker_api.router)
    app.include_router(operations.router)
    app.include_router(workbench.router)
    app.include_router(workbench_ltx.router)

    @app.get("/healthz", include_in_schema=False)
    def healthcheck():
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return {"status": "ok"}

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        from fastapi.exception_handlers import http_exception_handler

        return await http_exception_handler(request, exc)

    return app


app = create_app()
