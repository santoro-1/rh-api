#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${RUNNINGHUB_APP_DIR:-/opt/runninghub}"

if [[ ! -d "$APP_DIR" || ! -f "$APP_DIR/.env" ]]; then
    echo "缺少项目目录或 $APP_DIR/.env" >&2
    exit 1
fi

cd "$APP_DIR"

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    echo "缺少 Python 虚拟环境：$APP_DIR/.venv" >&2
    exit 1
fi

if ! command -v ffprobe >/dev/null 2>&1; then
    echo "未安装 ffprobe，请安装 ffmpeg" >&2
    exit 1
fi

"$APP_DIR/.venv/bin/python" - <<'PY'
from app.config import get_settings

settings = get_settings()
if settings.app_env != "production":
    raise SystemExit("APP_ENV 必须为 production")
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
settings.outputs_dir.mkdir(parents=True, exist_ok=True)
print("生产配置有效")
print(f"数据库：{settings.database_url.split('@')[-1]}")
print(f"数据目录：{settings.data_dir}")
print(f"可信域名：{', '.join(settings.allowed_hosts)}")
PY

echo "ffprobe：$(command -v ffprobe)"
echo "部署前检查通过"
