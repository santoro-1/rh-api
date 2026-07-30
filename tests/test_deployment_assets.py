from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.services.processes import hidden_creation_flags
from scripts.local_services import _process_alive


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_web_service_is_production_safe():
    service = (
        PROJECT_ROOT / "deploy" / "systemd" / "runninghub-video-web.service"
    ).read_text(encoding="utf-8")
    assert "--reload" not in service
    assert "--host 127.0.0.1" in service
    assert "--forwarded-allow-ips=127.0.0.1" in service
    assert "--port 18083" in service
    assert "EnvironmentFile=/opt/runninghub-video/.env" in service


def test_audio_worker_has_its_own_restartable_service():
    service = (
        PROJECT_ROOT / "deploy" / "systemd" / "runninghub-video-audio.service"
    ).read_text(encoding="utf-8")
    assert "-m app.workers.audio_worker" in service
    assert "Restart=on-failure" in service
    assert "ReadWritePaths=/opt/runninghub-video/data" in service
    assert "CPUQuota=50%" in service
    assert "MemoryMax=1536M" in service


def test_media_worker_is_isolated_and_resource_limited():
    service = (
        PROJECT_ROOT / "deploy" / "systemd" / "runninghub-video-media.service"
    ).read_text(encoding="utf-8")
    assert "-m app.workers.media_worker" in service
    assert "Restart=on-failure" in service
    assert "ReadWritePaths=/opt/runninghub-video/data" in service
    assert "CPUQuota=100%" in service
    assert "MemoryMax=2048M" in service


def test_asr_service_is_loopback_only_and_strictly_resource_limited():
    service = (
        PROJECT_ROOT / "deploy" / "systemd" / "runninghub-video-asr.service"
    ).read_text(encoding="utf-8")
    assert ".asr-runtime/venv/bin/python -m uvicorn asr_service.app:app" in service
    assert "--host 127.0.0.1" in service
    assert "--port 18084" in service
    assert "EnvironmentFile=/opt/runninghub-video/.env" in service
    assert "ASR_DEVICE=cpu" in service
    assert "OMP_NUM_THREADS=2" in service
    assert "CPUQuota=150%" in service
    assert "CPUWeight=10" in service
    assert "MemoryHigh=2048M" in service
    assert "MemoryMax=2560M" in service
    assert "OOMScoreAdjust=500" in service
    assert "ReadWritePaths=/opt/runninghub-video/data" in service


def test_asr_installer_is_idempotent_and_scoped_to_project():
    installer = (
        PROJECT_ROOT / "deploy" / "scripts" / "install-asr.sh"
    ).read_text(encoding="utf-8")
    assert 'APP_DIR="${RUNNINGHUB_APP_DIR:-/opt/runninghub-video}"' in installer
    assert 'if [[ "$APP_DIR" != "/opt/runninghub-video" ]]' in installer
    assert 'RUNTIME_DIR="$APP_DIR/.asr-runtime"' in installer
    assert 'MODEL_DIR="$APP_DIR/data/asr-models"' in installer
    assert "requirements.sha256" in installer
    assert '"$installed_hash" == "$desired_hash"' in installer
    assert 'sha256sum "$REQUIREMENTS" | awk' in installer
    assert "importlib.util.find_spec" in installer
    assert "跳过安装" in installer
    assert "torch==$TORCH_VERSION+cpu" in installer
    assert "https://mirrors.aliyun.com/pypi/simple" in installer
    assert 'PIP_CACHE_DIR="$RUNTIME_DIR/pip-cache"' in installer
    assert 'PIP_TIMEOUT="${ASR_PIP_TIMEOUT_SECONDS:-300}"' in installer
    assert 'PIP_RETRIES="${ASR_PIP_RETRIES:-12}"' in installer
    assert "ASR_SHARED_TOKEN" in installer
    assert "openssl rand -hex 32" in installer


def test_windows_one_click_launcher_starts_all_local_services():
    launcher = (PROJECT_ROOT / "scripts" / "local_services.py").read_text(
        encoding="utf-8"
    )
    start = (PROJECT_ROOT / "启动系统.cmd").read_text(encoding="utf-8")
    stop = (PROJECT_ROOT / "停止系统.cmd").read_text(encoding="utf-8")
    web_runner = (PROJECT_ROOT / "scripts" / "serve_web.py").read_text(
        encoding="utf-8"
    )
    assert "app.workers.audio_worker" in launcher
    assert "app.workers.media_worker" in launcher
    assert "app.workers.task_worker" in launcher
    assert "asr_service.app:app" in launcher
    assert "scripts.serve_web" in launcher
    assert "uvicorn.run" in web_runner
    assert "alembic" in launcher
    assert "scripts.local_services run" in start
    assert "scripts.local_services stop" in stop


def test_local_launcher_detects_live_and_missing_processes():
    assert _process_alive(os.getpid()) is True
    assert _process_alive(2_147_483_647) is False


def test_media_processes_use_hidden_windows_flag_on_windows():
    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    assert hidden_creation_flags() == expected


def test_live_status_pages_do_not_force_full_page_reload():
    batch_detail = (PROJECT_ROOT / "app" / "templates" / "batch_detail.html").read_text(
        encoding="utf-8"
    )
    operations = (PROJECT_ROOT / "app" / "templates" / "operations.html").read_text(
        encoding="utf-8"
    )
    operations_script = (
        PROJECT_ROOT / "app" / "static" / "operations.js"
    ).read_text(encoding="utf-8")
    batch_script = (
        PROJECT_ROOT / "app" / "static" / "batch_generate.js"
    ).read_text(encoding="utf-8")
    assert "location.reload()" not in batch_detail
    assert "location.reload()" not in operations
    assert "location.reload()" not in operations_script
    assert "/admin/operations/updates" in operations_script
    assert "data-log-cursor" in operations
    assert 'rows="7"' in batch_script
    assert 'rows="5"' in batch_script
    assert "MiniMax 句级时间戳" in batch_detail
    assert "segment-remote-id" in batch_detail
    assert 'segment.runninghubTaskId || "等待提交"' in batch_detail


def test_batch_voice_picker_keeps_custom_and_system_voices_separate():
    template = (
        PROJECT_ROOT / "app" / "templates" / "batch_generate.html"
    ).read_text(encoding="utf-8")
    script = (
        PROJECT_ROOT / "app" / "static" / "batch_generate.js"
    ).read_text(encoding="utf-8")

    assert 'data-voice-source="custom"' in template
    assert 'data-voice-source="system"' in template
    assert 'id="speech-custom-voice"' in template
    assert 'id="speech-system-category"' in template
    assert 'class="speech-system-voice' in template
    assert "<optgroup" not in template
    assert "initializeVoicePicker" in script
    assert 'method === "system"' in script


def test_batch_segment_table_keeps_remote_status_readable():
    stylesheet = (PROJECT_ROOT / "app" / "static" / "app.css").read_text(
        encoding="utf-8"
    )

    assert ".segment-status-cell" in stylesheet
    assert "overflow-wrap:anywhere" in stylesheet
    assert ".segment-action-links" in stylesheet
    assert ".child-task-table th:nth-child(4) { width:205px; }" in stylesheet


def test_nginx_template_supports_current_upload_limit_and_proxy_headers():
    config = (
        PROJECT_ROOT / "deploy" / "nginx" / "runninghub-video.conf.template"
    ).read_text(encoding="utf-8")
    assert "client_max_body_size 550M;" in config
    assert "proxy_pass http://127.0.0.1:18083;" in config
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in config
    assert "Content-Security-Policy" in config


def test_backup_timer_and_restore_confirmation_are_present():
    timer = (
        PROJECT_ROOT / "deploy" / "systemd" / "runninghub-video-backup.timer"
    ).read_text(encoding="utf-8")
    restore = (
        PROJECT_ROOT / "deploy" / "scripts" / "restore.sh"
    ).read_text(encoding="utf-8")
    assert "Persistent=true" in timer
    assert '"$CONFIRM" != "--confirm"' in restore
    assert "runninghub-video-media.service" in restore
    assert "runninghub-video-asr.service" in restore


def test_windows_production_update_script_is_explicit_and_scoped():
    updater = (
        PROJECT_ROOT / "deploy" / "deploy-update.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "[switch]$Deploy" in updater
    assert "if (-not $Deploy)" in updater
    assert 'Confirm-Exact' in updater
    assert '"BACKUP $shortCommit"' in updater
    assert '"DEPLOY $shortCommit"' in updater
    assert '"/opt/runninghub-video"' in updater
    assert '"/var/backups/runninghub-video"' in updater
    assert "Assert-QueuesIdle" in updater
    assert updater.count("git -c core.quotePath=false diff --name-only") == 2
    assert '$Script.Replace("`r`n", "`n").Replace("`r", "`n")' in updater
    assert "& ssh -n -T -i $SshKey" in updater
    assert "[Console]::OutputEncoding" in updater
    assert "--max-time 8" in updater
    assert "ServerAliveCountMax=3" in updater
    assert "install -d -m 700 -o '$LinuxUser' -g '$LinuxUser'" in updater
    assert "chown '${LinuxUser}:$LinuxUser'" in updater
    assert "chmod 600" in updater
    assert "sudo -u '$LinuxUser' env" in updater
    assert "migration-check.db" in updater
    assert "pre-deploy-app.db" in updater
    assert "pre-deploy-app.db.partial" in updater
    assert "failed-app.db" in updater
    assert "正在恢复发布前代码和数据库" in updater
    assert "systemctl daemon-reload" in updater
    assert "runninghub-video-media.service" in updater
    assert "runninghub-video-media.service.before" in updater
    assert "runninghub-video-asr.service" in updater
    assert "runninghub-video-asr.service.before" in updater
    assert "deploy/scripts/install-asr.sh" in updater
    assert "http://127.0.0.1:18084/healthz" in updater
    assert "systemctl enable" in updater
    assert "cd '$AppDir'" in updater
    assert "requirements.txt 有变化" in updater
    assert "systemctl restart" not in updater
    assert "systemctl reload" not in updater

    preflight = (
        PROJECT_ROOT / "deploy" / "scripts" / "preflight.sh"
    ).read_text(encoding="utf-8")
    assert 'export PATH="$APP_DIR/tools/ffmpeg/bin:$PATH"' in preflight
