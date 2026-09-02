from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import Settings


def _set_valid_production_environment(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_SECRET_KEY", "a-secure-random-secret-with-more-than-32-characters")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.setenv("ALLOWED_HOSTS", "video.example.com")


def test_production_settings_require_secure_cookie(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("COOKIE_SECURE", "false")
    with pytest.raises(ValueError, match="COOKIE_SECURE=true"):
        Settings.from_environment()


def test_production_settings_reject_wildcard_host(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("ALLOWED_HOSTS", "*")
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        Settings.from_environment()


def test_production_settings_accept_explicit_domain(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    settings = Settings.from_environment()
    assert settings.app_env == "production"
    assert settings.cookie_secure is True
    assert settings.allowed_hosts == ("video.example.com",)
    assert settings.log_retention_days == 7
    assert settings.log_max_bytes == 10 * 1024 * 1024
    assert settings.runninghub_remote_watchdog_seconds == 14400
    assert settings.ark_max_concurrency == 10
    assert settings.ark_queue_max == 200
    assert settings.ark_analysis_total_timeout_seconds == 540
    assert settings.ark_queue_wait_timeout_seconds == 120


def test_ark_hard_limit_cannot_exceed_ten(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("ARK_MAX_CONCURRENCY", "11")
    with pytest.raises(ValueError, match="1-10"):
        Settings.from_environment()


def test_process_ark_manager_rejects_multiple_web_workers(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("ARK_REQUEST_MANAGER_ENABLED", "true")
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(ValueError, match="单 Web 进程"):
        Settings.from_environment()


def test_ark_queue_budget_must_finish_before_total_budget(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("ARK_QUEUE_WAIT_TIMEOUT_SECONDS", "540")
    monkeypatch.setenv("ARK_ANALYSIS_TOTAL_TIMEOUT_SECONDS", "540")
    with pytest.raises(ValueError, match="排队预算"):
        Settings.from_environment()


def test_default_image_upload_limit_is_200_mb(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.delenv("MAX_IMAGE_SIZE_MB", raising=False)

    settings = Settings.from_environment()

    assert settings.max_image_size_mb == 200


def test_image_upload_limit_keeps_explicit_environment_override(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("MAX_IMAGE_SIZE_MB", "64")

    settings = Settings.from_environment()

    assert settings.max_image_size_mb == 64


def test_runninghub_watchdog_ignores_removed_one_hour_timeout(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.delenv("RUNNINGHUB_REMOTE_WATCHDOG_SECONDS", raising=False)
    monkeypatch.setenv("RUNNINGHUB_TASK_TIMEOUT_SECONDS", "3600")

    settings = Settings.from_environment()

    assert settings.runninghub_remote_watchdog_seconds == 14400


def test_runninghub_watchdog_accepts_explicit_override(monkeypatch):
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv("RUNNINGHUB_REMOTE_WATCHDOG_SECONDS", "21600")

    settings = Settings.from_environment()

    assert settings.runninghub_remote_watchdog_seconds == 21600
