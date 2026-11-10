from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from .config import Settings


class JYDBridgeError(RuntimeError):
    pass


class JYDBridge:
    def __init__(self, settings: Settings):
        self.settings = settings

    def import_handoff(
        self,
        *,
        manifest_path: Path,
        owner_user_id: str,
        owner_username: str,
        project_name: str,
    ) -> dict[str, object]:
        script = self.settings.jyd_root / "tools" / "jobs" / "import_h3_handoff.py"
        if not script.is_file():
            raise JYDBridgeError("JYD H3 导入脚本不存在")
        environment = os.environ.copy()
        jyd_src = str(self.settings.jyd_root / "src")
        environment["PYTHONPATH"] = (
            jyd_src
            if not environment.get("PYTHONPATH")
            else jyd_src + os.pathsep + environment["PYTHONPATH"]
        )
        command = [
            self.settings.jyd_python,
            str(script),
            "--manifest",
            str(manifest_path),
            "--owner-user-id",
            owner_user_id,
            "--owner-username",
            owner_username,
            "--project-name",
            project_name,
            "--database",
            str(self.settings.jyd_database_path),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.settings.jyd_root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise JYDBridgeError("启动 JYD H3 导入失败") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-500:]
            raise JYDBridgeError(f"JYD H3 导入失败：{detail or '未知错误'}")
        try:
            value = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise JYDBridgeError("JYD H3 导入返回格式错误") from exc
        if not isinstance(value, dict) or not value.get("project_id"):
            raise JYDBridgeError("JYD H3 导入缺少项目编号")
        return value
