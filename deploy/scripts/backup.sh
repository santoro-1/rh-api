#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${RUNNINGHUB_APP_DIR:-/opt/runninghub}"
BACKUP_DIR="${RUNNINGHUB_BACKUP_DIR:-/var/backups/runninghub}"
DATA_DIR="$APP_DIR/data"
DATABASE="$DATA_DIR/app.db"

if [[ "$APP_DIR" != /* || "$BACKUP_DIR" != /* ]]; then
    echo "应用目录和备份目录必须是绝对路径" >&2
    exit 1
fi
if [[ ! -f "$DATABASE" || ! -f "$APP_DIR/.env" ]]; then
    echo "缺少数据库或 .env，拒绝创建不完整备份" >&2
    exit 1
fi
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "未安装 sqlite3" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
TEMP_DIR="$(mktemp -d "$BACKUP_DIR/.snapshot.XXXXXX")"
trap 'rm -rf -- "$TEMP_DIR"' EXIT
mkdir -p "$TEMP_DIR/data"

sqlite3 "$DATABASE" ".timeout 30000" ".backup '$TEMP_DIR/data/app.db'"
for directory in uploads outputs; do
    if [[ -d "$DATA_DIR/$directory" ]]; then
        cp -a "$DATA_DIR/$directory" "$TEMP_DIR/data/"
    fi
done
cp "$APP_DIR/.env" "$TEMP_DIR/.env"
chmod 600 "$TEMP_DIR/.env" "$TEMP_DIR/data/app.db"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$BACKUP_DIR/runninghub-$TIMESTAMP.tar.gz"
tar -C "$TEMP_DIR" -czf "$ARCHIVE" .env data
chmod 600 "$ARCHIVE"

echo "备份已创建：$ARCHIVE"
