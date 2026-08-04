from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.services.processes import hidden_creation_flags
from media_node.apply_portable_update import (
    PortableUpdateError,
    apply_update,
)
from media_node.create_portable_zip import create_archive
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


def test_windows_media_node_is_isolated_and_has_one_launcher():
    node = PROJECT_ROOT / "media_node"
    launcher = (node / "launcher.py").read_text(encoding="utf-8")
    start = (node / "启动媒体节点.cmd").read_text(encoding="utf-8")
    installer = (node / "install-media-node.ps1").read_text(encoding="utf-8")
    environment = (node / ".env.example").read_text(encoding="utf-8")
    assert "media_node.asr_service.app:app" in launcher
    assert "127.0.0.1" in launcher
    assert "media_node.launcher" in start
    assert 'Join-Path $nodeRoot ".runtime"' in installer
    assert "ffmpeg" in installer and "ffprobe" in installer
    assert "MEDIA_WORKER_SERVER_URL" in environment
    assert "MEDIA_WORKER_TOKEN" in environment
    assert not (PROJECT_ROOT / "启动ASR服务.cmd").exists()
    assert not (
        PROJECT_ROOT / "deploy" / "systemd" / "runninghub-video-asr.service"
    ).exists()


def test_windows_media_node_has_a_self_contained_portable_builder():
    node = PROJECT_ROOT / "media_node"
    builder = (node / "build-portable-media-node.ps1").read_text(
        encoding="utf-8"
    )
    zip_builder = (node / "create_portable_zip.py").read_text(
        encoding="utf-8"
    )
    launcher = (node / "launcher.py").read_text(encoding="utf-8")
    worker = (node / "worker.py").read_text(encoding="utf-8")
    environment = (node / ".env.example").read_text(encoding="utf-8")
    portable_start = (node / "portable" / "启动媒体节点.cmd").read_text(
        encoding="utf-8"
    )

    assert 'Join-Path $sourceRuntime "Scripts\\python.exe"' in builder
    assert "sys.base_prefix" in builder
    assert "Get-Command ffmpeg.exe" in builder
    assert "Get-Command ffprobe.exe" in builder
    assert 'Join-Path $projectRoot "app"' in builder
    assert 'Join-Path $nodeRoot ".runtime\\models"' in builder
    assert "create_portable_zip.py" in builder
    assert "UpdateOnly" in builder
    assert '"rh-media-$packageKind-$revision"' in builder
    assert '"--flat"' in builder
    assert "allowZip64=True" in zip_builder
    assert "path.relative_to(root).as_posix()" in zip_builder
    assert '"import fastapi, funasr, modelscope, mutagen' in builder
    assert '$env:PYTHONNOUSERSITE = "1"' in builder
    assert builder.count('"-s"') >= 2
    assert "IncludeLocalConfig" in builder
    assert '"MEDIA_WORKER_ID="' in builder
    assert '"$revision-dirty"' in builder
    assert "MEDIA_NODE_PORTABLE" in launcher
    assert "MEDIA_NODE_PYTHON" in launcher
    assert "except ModuleNotFoundError as exc" in launcher
    assert "install-media-node.ps1" in launcher
    assert "python\\python.exe" in portable_start
    assert "ffmpeg\\bin" in portable_start
    assert "portable-runtime.txt" in portable_start
    assert "runtime-required.txt" in portable_start
    assert 'os.getenv("MEDIA_WORKER_ID", "").strip()' in worker
    assert "MEDIA_WORKER_ID=\n" in environment

    installer = (node / "install-media-node.ps1").read_text(encoding="utf-8")
    assert '"import fastapi, funasr, modelscope, mutagen' in installer
    assert '"-s"' in installer

    for command_file in (node / "portable").glob("*.cmd"):
        command_bytes = command_file.read_bytes()
        assert all(byte < 128 for byte in command_bytes), (
            f"{command_file.name} must stay ASCII-only for cmd.exe compatibility"
        )


def test_portable_zip_preserves_chinese_launcher_names(tmp_path):
    source = tmp_path / "portable-node"
    source.mkdir()
    (source / "启动媒体节点.cmd").write_text("echo ok", encoding="utf-8")
    (source / "使用说明.txt").write_text("说明", encoding="utf-8")
    archive = tmp_path / "portable-node.zip"

    create_archive(source, archive)

    with zipfile.ZipFile(archive) as package:
        assert package.testzip() is None
        assert "portable-node/启动媒体节点.cmd" in package.namelist()
        assert "portable-node/使用说明.txt" in package.namelist()

    flat_archive = tmp_path / "portable-node-flat.zip"
    create_archive(source, flat_archive, include_root=False)
    with zipfile.ZipFile(flat_archive) as package:
        assert "启动媒体节点.cmd" in package.namelist()
        assert not any(name.startswith("portable-node/") for name in package.namelist())


def test_portable_code_update_preserves_runtime_config_and_creates_backup(tmp_path):
    root = tmp_path / "rh"
    (root / "python").mkdir(parents=True)
    (root / "python" / "python.exe").write_bytes(b"portable")
    (root / "portable-runtime.txt").write_text("runtime-v1\n", encoding="utf-8")
    (root / "app").mkdir()
    (root / "app" / "old.py").write_text("old", encoding="utf-8")
    (root / "media_node").mkdir()
    (root / "media_node" / "worker.py").write_text("old", encoding="utf-8")
    (root / "media_node" / ".env").write_text("SECRET=keep", encoding="utf-8")
    archive = tmp_path / "rh-media-update-test.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(
            "portable-update.json",
            '{"formatVersion":1,"revision":"test","runtimeId":"runtime-v1"}',
        )
        package.writestr("app/new.py", "new")
        package.writestr("media_node/worker.py", "updated")
        package.writestr("media_node/.env.example", "EXAMPLE=1")

    backup = apply_update(archive, root)

    assert backup.is_file()
    assert (root / "app" / "new.py").read_text(encoding="utf-8") == "new"
    assert (root / "media_node" / "worker.py").read_text(encoding="utf-8") == "updated"
    assert (root / "media_node" / ".env").read_text(encoding="utf-8") == "SECRET=keep"


def test_portable_code_update_rejects_runtime_mismatch_and_private_files(tmp_path):
    root = tmp_path / "rh"
    (root / "python").mkdir(parents=True)
    (root / "python" / "python.exe").write_bytes(b"portable")
    (root / "portable-runtime.txt").write_text("runtime-v1\n", encoding="utf-8")
    mismatch = tmp_path / "mismatch.zip"
    with zipfile.ZipFile(mismatch, "w") as package:
        package.writestr(
            "portable-update.json",
            '{"formatVersion":1,"revision":"test","runtimeId":"runtime-v2"}',
        )
        package.writestr("media_node/worker.py", "updated")
    with pytest.raises(PortableUpdateError, match="完整包"):
        apply_update(mismatch, root)

    private = tmp_path / "private.zip"
    with zipfile.ZipFile(private, "w") as package:
        package.writestr(
            "portable-update.json",
            '{"formatVersion":1,"revision":"test","runtimeId":"runtime-v1"}',
        )
        package.writestr("media_node/.env", "SECRET=overwrite")
    with pytest.raises(PortableUpdateError, match="配置或数据"):
        apply_update(private, root)


def test_remote_media_worker_switch_is_explicit_and_reversible():
    switcher = (
        PROJECT_ROOT
        / "deploy"
        / "scripts"
        / "configure-remote-media-worker.sh"
    ).read_text(encoding="utf-8")
    assert '"$APP_DIR" != "/opt/runninghub-video"' in switcher
    assert '"$CONFIRM" != "--confirm"' in switcher
    assert '"$MODE" != "remote" && "$MODE" != "local"' in switcher
    assert "remote-worker-env-" in switcher
    assert (
        "install -d -m 700 -o rhvideo -g rhvideo "
        "/var/backups/runninghub-video"
    ) in switcher
    assert (
        "install -d -m 700 -o root -g root "
        "/var/backups/runninghub-video"
    ) not in switcher
    assert "restore_on_error" in switcher
    assert "切换失败，正在恢复修改前 .env" in switcher
    assert "for attempt in {1..20}" in switcher
    assert "Web 或媒体 Worker 在 20 秒内没有就绪" in switcher
    assert 'upsert_env "MEDIA_PROCESSING_MODE" "remote"' in switcher
    assert 'upsert_env "MEDIA_PROCESSING_MODE" "local"' in switcher
    assert 'upsert_env "LONG_AUDIO_ALIGNMENT_PROVIDER" "funasr_http"' in switcher
    assert "openssl rand -hex 32" in switcher
    assert "runninghub-video-asr.service" not in switcher


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
    assert "media_node.asr_service.app:app" in launcher
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
    assert "new DOMParser()" in batch_detail
    assert "batch-items-content" in batch_detail
    assert "batch-actions-content" in batch_detail
    assert "data.viewRevision !== renderedViewRevision" in batch_detail
    assert "location.reload()" not in operations
    assert "location.reload()" not in operations_script
    assert "/admin/operations/updates" in operations_script
    assert "data-log-cursor" in operations
    assert 'rows="7"' in batch_script
    assert 'rows="5"' in batch_script
    assert "MiniMax 句级时间戳" not in batch_detail
    long_audio_detail = (
        PROJECT_ROOT / "app" / "templates" / "long_audio_detail.html"
    ).read_text(encoding="utf-8")
    long_audio_script = (
        PROJECT_ROOT / "app" / "static" / "long_audio.js"
    ).read_text(encoding="utf-8")
    assert 'id="long-audio-reanalyze-form"' in long_audio_detail
    assert 'reanalyzeForm?.classList.toggle("hidden", !(review || failed))' in (
        long_audio_script
    )
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


def test_documentation_has_one_current_developer_entrypoint():
    guide = (PROJECT_ROOT / "DEVELOPER_GUIDE.md").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    status = (PROJECT_ROOT / "PROJECT_STATUS.md").read_text(encoding="utf-8")

    assert "## 2. 运行架构" in guide
    assert "## 3. 核心业务流程" in guide
    assert "## 4. 状态机与重试语义" in guide
    assert "## 8. 自动化测试与质量门槛" in guide
    assert "## 9. Windows 媒体节点开发与分发" in guide
    assert "## 11. 生产发布与回滚" in guide
    assert "0018_segment_video_merge" in guide
    assert "MEDIA_PROCESSING_MODE" in guide
    assert "RUNNINGHUB_AUTO_RETRY_LIMIT" in guide
    assert "rh-media-update-*.zip" in guide
    assert "FunASR" in guide
    assert "[DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)" in readme
    assert "当前技术事实以开发者指南" in readme
    assert "当前架构、状态机、开发、测试和部署总说明" in status
    assert "当前没有接入外部强制对齐或 ASR 服务" not in status


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
    assert "runninghub-video-asr.service" not in restore


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
    local_text_function = updater.split("function Invoke-RemoteScript", 1)[0]
    assert "[Console]::OutputEncoding" in local_text_function
    assert "$exitCode = $LASTEXITCODE" in local_text_function
    assert "--max-time 8" in updater
    assert "ServerAliveCountMax=3" in updater
    assert "install -d -m 700 -o '$LinuxUser' -g '$LinuxUser'" in updater
    assert (
        "install -d -m 700 -o '$LinuxUser' -g '$LinuxUser' '$BackupDir'"
        in updater
    )
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
    assert "systemctl enable runninghub-video-asr.service" not in updater
    assert "/bin/bash deploy/scripts/install-asr.sh" not in updater
    assert "http://127.0.0.1:18084/healthz" not in updater
    assert "不会安装或启动服务器 ASR" in updater
    assert "systemctl enable" in updater
    assert "cd '$AppDir'" in updater
    assert "requirements.txt 有变化" in updater
    assert "systemctl restart" not in updater
    assert "systemctl reload" not in updater

    preflight = (
        PROJECT_ROOT / "deploy" / "scripts" / "preflight.sh"
    ).read_text(encoding="utf-8")
    assert 'export PATH="$APP_DIR/tools/ffmpeg/bin:$PATH"' in preflight
