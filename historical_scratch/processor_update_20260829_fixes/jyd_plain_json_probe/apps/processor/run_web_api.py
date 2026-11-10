from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import create_app  # noqa: E402
from jyd_probe.logging_config import configure_file_logging  # noqa: E402


configure_file_logging(PROJECT_ROOT / "data" / "logs", "workbench.log")
configure_file_logging(
    PROJECT_ROOT / "data" / "logs",
    "render.log",
    logger_name="jyd_probe.render",
    propagate=False,
)
app = create_app()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动剪映渲染 FastAPI 后端。")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        "apps.processor.run_web_api:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=str(PROJECT_ROOT),
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
