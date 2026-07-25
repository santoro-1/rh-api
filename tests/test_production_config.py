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
