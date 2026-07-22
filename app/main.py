from __future__ import annotations

from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.routes import admin, auth, tasks


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        settings.outputs_dir.mkdir(parents=True, exist_ok=True)
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
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
        name="static",
    )
    app.state.rate_limits = defaultdict(deque)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(tasks.router)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        from fastapi.exception_handlers import http_exception_handler

        return await http_exception_handler(request, exc)

    return app


app = create_app()
