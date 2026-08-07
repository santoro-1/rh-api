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
