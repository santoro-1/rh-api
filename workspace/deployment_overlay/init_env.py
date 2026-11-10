from __future__ import annotations

import secrets
from pathlib import Path

from cryptography.fernet import Fernet


target = Path("/opt/runninghub-video/.env")
if target.exists():
    raise SystemExit(f"{target} already exists; refusing to overwrite")

content = f"""APP_ENV=production
APP_SECRET_KEY={secrets.token_urlsafe(48)}
APP_ENCRYPTION_KEY={Fernet.generate_key().decode("ascii")}
ALLOWED_HOSTS=video.lanyingjk01.com

DATABASE_URL=sqlite:////opt/runninghub-video/data/app.db
DATA_DIR=/opt/runninghub-video/data

RUNNINGHUB_BASE_URL=https://www.runninghub.cn
DEFAULT_RUNNINGHUB_AI_APP_ID=2062251097452007426
DEFAULT_RUNNINGHUB_INSTANCE_TYPE=default
DEFAULT_USE_PERSONAL_QUEUE=false

POLL_INTERVAL_SECONDS=5
RUNNINGHUB_TASK_TIMEOUT_SECONDS=3600

MAX_IMAGE_SIZE_MB=20
MAX_AUDIO_SIZE_MB=100
MAX_VIDEO_SIZE_MB=500
UPLOAD_RETENTION_DAYS=2
OUTPUT_RETENTION_DAYS=7
COOKIE_SECURE=true
LOGIN_RATE_LIMIT_PER_MINUTE=10
TASK_CREATE_RATE_LIMIT_PER_MINUTE=10
MAX_BATCH_ITEMS=50
MAX_BATCH_TOTAL_UPLOAD_MB=5120
STAGED_ASSET_RETENTION_HOURS=24
MINIMAX_DEFAULT_BASE_URL=https://api.minimaxi.com
MINIMAX_REQUEST_TIMEOUT_SECONDS=120
TEMPORARY_VOICE_RETENTION_HOURS=48
LOG_RETENTION_DAYS=14
"""

target.write_text(content, encoding="utf-8")
target.chmod(0o600)
