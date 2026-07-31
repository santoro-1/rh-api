#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${RUNNINGHUB_APP_DIR:-/opt/runninghub-video}"
MODE="${1:-}"
CONFIRM="${2:-}"

if [[ "$APP_DIR" != "/opt/runninghub-video" ]]; then
    echo "该脚本只允许操作 /opt/runninghub-video" >&2
    exit 1
fi
if [[ "$MODE" != "remote" && "$MODE" != "local" ]]; then
    echo "用法：configure-remote-media-worker.sh remote|local --confirm" >&2
    exit 2
fi
if [[ "$CONFIRM" != "--confirm" ]]; then
    echo "必须显式提供 --confirm" >&2
    exit 2
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "找不到 $APP_DIR/.env" >&2
    exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/var/backups/runninghub-video/remote-worker-env-$timestamp"
install -d -m 700 -o root -g root /var/backups/runninghub-video
install -m 600 -o root -g root "$APP_DIR/.env" "$backup"

restore_on_error() {
    exit_code="$?"
    trap - ERR
    echo "切换失败，正在恢复修改前 .env。" >&2
    install -m 600 -o rhvideo -g rhvideo "$backup" "$APP_DIR/.env"
    systemctl restart \
        runninghub-video-web.service \
        runninghub-video-media.service >/dev/null 2>&1 || true
    exit "$exit_code"
}
trap restore_on_error ERR

upsert_env() {
    local key="$1"
    local value="$2"
    local temporary
    temporary="$(mktemp "$APP_DIR/.env.remote-worker.XXXXXX")"
    awk -v key="$key" -v value="$value" '
        BEGIN { replaced=0 }
        index($0, key "=") == 1 {
            if (!replaced) {
                print key "=" value
                replaced=1
            }
            next
        }
        { print }
        END {
            if (!replaced) print key "=" value
        }
    ' "$APP_DIR/.env" > "$temporary"
    install -m 600 -o rhvideo -g rhvideo "$temporary" "$APP_DIR/.env"
    rm -f -- "$temporary"
}

if [[ "$MODE" == "remote" ]]; then
    token="$(awk -F= '$1 == "MEDIA_WORKER_TOKEN" {print substr($0, index($0, "=") + 1); exit}' "$APP_DIR/.env")"
    if [[ ${#token} -lt 32 || "$token" == replace-* || "$token" == change-* ]]; then
        token="$(openssl rand -hex 32)"
    fi
    upsert_env "MEDIA_PROCESSING_MODE" "remote"
    upsert_env "MEDIA_WORKER_TOKEN" "$token"
    upsert_env "MEDIA_WORKER_LEASE_SECONDS" "1800"
    upsert_env "MEDIA_WORKER_ARCHIVE_LIMIT_MB" "500"
    upsert_env "LONG_AUDIO_ALIGNMENT_PROVIDER" "funasr_http"
else
    token=""
    upsert_env "MEDIA_PROCESSING_MODE" "local"
    upsert_env "LONG_AUDIO_ALIGNMENT_PROVIDER" "heuristic"
fi

systemctl restart runninghub-video-web.service runninghub-video-media.service
for attempt in {1..20}; do
    if systemctl is-active --quiet runninghub-video-web.service &&
       systemctl is-active --quiet runninghub-video-media.service &&
       curl --connect-timeout 2 --max-time 5 -fsS \
           -H 'Host: video.lanyingjk01.com' \
           http://127.0.0.1:18083/healthz >/dev/null; then
        break
    fi
    if [[ "$attempt" -eq 20 ]]; then
        echo "Web 或媒体 Worker 在 20 秒内没有就绪" >&2
        false
    fi
    sleep 1
done
trap - ERR

echo "媒体处理模式已切换为：$MODE"
echo "修改前 .env 备份：$backup"
if [[ "$MODE" == "remote" ]]; then
    echo "请立即复制下面一行到笔记本 .env.worker，随后清理终端滚屏："
    printf 'MEDIA_WORKER_TOKEN=%s\n' "$token"
fi
