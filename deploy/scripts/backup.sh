#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${RUNNINGHUB_APP_DIR:-/opt/runninghub-video}"
BACKUP_DIR="${RUNNINGHUB_BACKUP_DIR:-/var/backups/runninghub-video}"
BACKUP_KEEP_COUNT="${RUNNINGHUB_BACKUP_KEEP_COUNT:-2}"
DATA_DIR="$APP_DIR/data"
DATABASE="$DATA_DIR/app.db"
MODE="${1:-}"

if [[ "$APP_DIR" != /* || "$BACKUP_DIR" != /* ]]; then
    echo "应用目录和备份目录必须是绝对路径" >&2
    exit 1
fi
if [[ "$MODE" != "" && "$MODE" != "--rotate-only" ]] || (( $# > 1 )); then
    echo "用法：backup.sh [--rotate-only]" >&2
    exit 1
fi
if [[ ! "$BACKUP_KEEP_COUNT" =~ ^[1-9][0-9]*$ ]]; then
    echo "RUNNINGHUB_BACKUP_KEEP_COUNT 必须是大于 0 的整数" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

rotate_full_backups() {
    local remove_count index filename
    local -a full_backups

    # Only rotate complete data archives created by this script. Code-only
    # archives, database snapshots and configuration backups use other names.
    mapfile -t full_backups < <(
        find "$BACKUP_DIR" -maxdepth 1 -regextype posix-extended -type f \
            -regex '.*/runninghub-video-[0-9]{8}T[0-9]{6}Z\.tar\.gz' \
            -printf '%f\n' | LC_ALL=C sort
    )
    remove_count=$((${#full_backups[@]} - BACKUP_KEEP_COUNT))
    for ((index = 0; index < remove_count; index++)); do
        filename="${full_backups[$index]}"
        if [[ ! "$filename" =~ ^runninghub-video-[0-9]{8}T[0-9]{6}Z\.tar\.gz$ ]]; then
            echo "备份文件名不符合安全轮换规则，拒绝删除：$filename" >&2
            exit 1
        fi
        rm -f -- "$BACKUP_DIR/$filename"
        echo "已轮换旧完整备份：$BACKUP_DIR/$filename"
    done
}

if [[ "$MODE" == "--rotate-only" ]]; then
    rotate_full_backups
    exit 0
fi

if [[ ! -f "$DATABASE" || ! -f "$APP_DIR/.env" ]]; then
    echo "缺少数据库或 .env，拒绝创建不完整备份" >&2
    exit 1
fi
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "未安装 sqlite3" >&2
    exit 1
fi

TEMP_DIR="$(mktemp -d "$BACKUP_DIR/.snapshot.XXXXXX")"
PARTIAL_ARCHIVE=""
cleanup() {
    rm -rf -- "$TEMP_DIR"
    if [[ -n "$PARTIAL_ARCHIVE" ]]; then
        rm -f -- "$PARTIAL_ARCHIVE"
    fi
}
trap cleanup EXIT
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
ARCHIVE="$BACKUP_DIR/runninghub-video-$TIMESTAMP.tar.gz"
PARTIAL_ARCHIVE="$ARCHIVE.partial"
tar -C "$TEMP_DIR" -czf "$PARTIAL_ARCHIVE" .env data
chmod 600 "$PARTIAL_ARCHIVE"
mv -f -- "$PARTIAL_ARCHIVE" "$ARCHIVE"
PARTIAL_ARCHIVE=""

echo "备份已创建：$ARCHIVE"
rotate_full_backups
