from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_web_service_is_production_safe():
    service = (
        PROJECT_ROOT / "deploy" / "systemd" / "runninghub-web.service"
    ).read_text(encoding="utf-8")
    assert "--reload" not in service
    assert "--host 127.0.0.1" in service
    assert "--forwarded-allow-ips=127.0.0.1" in service
    assert "EnvironmentFile=/opt/runninghub/.env" in service


def test_nginx_template_supports_current_upload_limit_and_proxy_headers():
    config = (
        PROJECT_ROOT / "deploy" / "nginx" / "runninghub.conf.template"
    ).read_text(encoding="utf-8")
    assert "client_max_body_size 550M;" in config
    assert "proxy_pass http://127.0.0.1:8000;" in config
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in config
    assert "Content-Security-Policy" in config


def test_backup_timer_and_restore_confirmation_are_present():
    timer = (
        PROJECT_ROOT / "deploy" / "systemd" / "runninghub-backup.timer"
    ).read_text(encoding="utf-8")
    restore = (
        PROJECT_ROOT / "deploy" / "scripts" / "restore.sh"
    ).read_text(encoding="utf-8")
    assert "Persistent=true" in timer
    assert '"$CONFIRM" != "--confirm"' in restore
