from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
import webbrowser


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.local_collector import (  # noqa: E402
    DEFAULT_RENDER_SERVER_URL,
    LocalCollectorService,
    LocalCollectorSettings,
)
from jyd_probe.local_collector_api import create_local_collector_app  # noqa: E402
from jyd_probe.logging_config import configure_file_logging  # noqa: E402
from jyd_probe.runtime_paths import collector_state_root  # noqa: E402


configure_file_logging(
    collector_state_root() / "logs",
    "collector.log",
    logger_name="jyd_probe.collector",
    propagate=False,
)
app = create_local_collector_app()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动剪映本地草稿采集器。")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--draft-root", default="")
    parser.add_argument("--server-url", default=DEFAULT_RENDER_SERVER_URL)
    parser.add_argument("--access-token", default="")
    parser.add_argument("--open-browser", action="store_true", help="排错时打开独立采集器页面")
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = LocalCollectorSettings.defaults()
    if args.draft_root or args.server_url or args.access_token:
        override_service = LocalCollectorService(settings)
        if args.draft_root:
            override_service.set_draft_root(Path(args.draft_root).expanduser().resolve())
        if args.server_url:
            override_service.set_render_server_url(args.server_url)
        if args.access_token:
            override_service.set_access_token(args.access_token)
        settings = override_service.settings
    collector_app = create_local_collector_app(settings)
    website_url = f"{settings.render_server_url.rstrip('/')}/app"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(website_url)).start()

    import uvicorn

    uvicorn.run(collector_app, host=args.host, port=args.port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
