#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${RUNNINGHUB_APP_DIR:-/opt/runninghub}"
ARCHIVE="${1:-}"
CONFIRM="${2:-}"

if [[ -z "$ARCHIVE" || "$CONFIRM" != "--confirm" ]]; then
    echo "用法：restore.sh /absolute/path/runninghub-*.tar.gz --confirm" >&2
    exit 2
fi
if [[ "$APP_DIR" != /* || "$ARCHIVE" != /* || ! -f "$ARCHIVE" ]]; then
    echo "应用目录和备份文件必须是有效的绝对路径" >&2
    exit 1
fi
if systemctl is-active --quiet runninghub-web.service ||
   systemctl is-active --quiet runninghub-worker.service; then
    echo "恢复前必须停止 runninghub-web 和 runninghub-worker" >&2
    exit 1
fi

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_DIR"' EXIT

while IFS= read -r entry; do
    clean_entry="${entry#./}"
    case "$clean_entry" in
        .env|data|data/*) ;;
        *)
            echo "备份包含预期范围外的路径：$entry" >&2
            exit 1
            ;;
    esac
    if [[ "$clean_entry" == /* || "$clean_entry" == ".." ||
          "$clean_entry" == ../* || "$clean_entry" == */../* ]]; then
        echo "备份包含不安全路径：$entry" >&2
        exit 1
    fi
done < <(tar -tzf "$ARCHIVE")

tar -xzf "$ARCHIVE" -C "$TEMP_DIR"

if [[ ! -f "$TEMP_DIR/.env" || ! -f "$TEMP_DIR/data/app.db" ]]; then
    echo "备份缺少 .env 或 data/app.db" >&2
    exit 1
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SAFETY_DIR="$APP_DIR/pre-restore-$TIMESTAMP"
mkdir -p "$SAFETY_DIR"
if [[ -d "$APP_DIR/data" ]]; then
    mv "$APP_DIR/data" "$SAFETY_DIR/data"
fi
if [[ -f "$APP_DIR/.env" ]]; then
    mv "$APP_DIR/.env" "$SAFETY_DIR/.env"
fi

mv "$TEMP_DIR/data" "$APP_DIR/data"
mv "$TEMP_DIR/.env" "$APP_DIR/.env"
chown -R runninghub:runninghub "$APP_DIR/data" "$APP_DIR/.env" "$SAFETY_DIR"
chmod 600 "$APP_DIR/.env"

echo "恢复完成。原数据保存在：$SAFETY_DIR"
echo "启动服务前请运行数据库迁移和 preflight.sh。"
