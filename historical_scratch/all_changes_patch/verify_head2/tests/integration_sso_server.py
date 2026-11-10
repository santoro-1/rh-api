from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import uvicorn  # noqa: E402

from jyd_probe.web_api import WebApiSettings, create_app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--center-url", required=True)
    parser.add_argument("--authority", action="store_true")
    parser.add_argument("--root", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    settings = WebApiSettings(
        storage_root=root / "storage",
        template_library_root=root / "templates",
        default_draft_root=root / "drafts",
        audio_library_root=root / "audio",
        admin_password="admin123",
        admin_session_secret="admin-secret",
        site_username="operator",
        site_password="operator123",
        site_session_secret="site-secret",
        auth_authority=args.authority,
        auth_server_url=args.center_url,
        execution_mode="agent",
    )
    for directory in (
        settings.storage_root,
        settings.template_library_root,
        settings.default_draft_root,
        settings.audio_library_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
