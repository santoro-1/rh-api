from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLOUD_BASE_URL = "https://video.lanyingjk01.com"
DEFAULT_PROCESSOR_ROOT = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe")


@dataclass(frozen=True)
class Settings:
    cloud_base_url: str
    runtime_dir: Path
    upload_dir: Path
    result_dir: Path
    database_path: Path
    jyd_root: Path
    jyd_database_path: Path
    jyd_python: str


def _server_url_from(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    result = str(value.get("digital_human_server_url") or "").strip()
    return result or None


def load_settings() -> Settings:
    runtime = Path(
        os.environ.get("H3_WORKBENCH_RUNTIME_DIR") or PROJECT_ROOT / "runtime"
    ).resolve()
    cloud_url = (
        os.environ.get("H3_CLOUD_BASE_URL")
        or _server_url_from(runtime / "processor_config.json")
        or _server_url_from(DEFAULT_PROCESSOR_ROOT / "data" / "processor_config.json")
        or DEFAULT_CLOUD_BASE_URL
    )
    return Settings(
        cloud_base_url=cloud_url.rstrip("/"),
        runtime_dir=runtime,
        upload_dir=runtime / "uploads",
        result_dir=runtime / "results",
        database_path=runtime / "h3_workbench.sqlite3",
        jyd_root=Path(
            os.environ.get("H3_JYD_ROOT") or DEFAULT_PROCESSOR_ROOT
        ).resolve(),
        jyd_database_path=Path(
            os.environ.get("H3_JYD_DATABASE_PATH")
            or DEFAULT_PROCESSOR_ROOT / "data" / "web_storage" / "control.db"
        ).resolve(),
        jyd_python=os.environ.get("H3_JYD_PYTHON") or sys.executable,
    )
