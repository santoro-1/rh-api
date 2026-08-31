from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Set test-only storage before importing the application. This keeps pytest from
# ever creating, dropping, or modifying the local development SQLite database.
TEST_RUNTIME_ROOT = Path(__file__).resolve().parent / ".runtime"
TEST_DATABASE = TEST_RUNTIME_ROOT / "test_app.db"
TEST_DATA_DIR = TEST_RUNTIME_ROOT / "data"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DATABASE.as_posix()}"
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["APP_ENV"] = "test"
# Developer .env switches must not silently change the default test contract.
# Dedicated direct-delivery tests explicitly enable the feature themselves.
os.environ["H3_DIRECT_DELIVERY_ENABLED"] = "false"

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.main import create_app
from app.models import RunningHubConfig, User
from app.services.runninghub_pool import create_execution_account
from app.services.security import encrypt_secret, hash_password


@pytest.fixture(autouse=True)
def clean_database():
    TEST_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    settings = get_settings()
    shutil.rmtree(settings.data_dir, ignore_errors=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    yield
    Base.metadata.drop_all(bind=engine)
    shutil.rmtree(settings.data_dir, ignore_errors=True)


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def create_user(
    username: str,
    password: str = "password123",
    *,
    is_admin: bool = False,
    with_config: bool = True,
    h3_access_enabled: bool = False,
) -> User:
    with SessionLocal() as db:
        user = User(
            username=username,
            password_hash=hash_password(password),
            is_admin=is_admin,
            is_active=True,
            h3_access_enabled=h3_access_enabled,
        )
        if with_config:
            RunningHubConfig(
                user=user,
                api_key_encrypted=encrypt_secret("test-runninghub-key"),
                base_url="https://www.runninghub.cn",
                ai_app_id="2062251097452007426",
                instance_type="plus",
                default_prompt="测试提示词",
                max_concurrent_tasks=2,
            )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user


def assign_runninghub_account(username: str) -> int:
    """Assign one complete execution account to a test user."""

    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        account = create_execution_account(
            db,
            label=f"{username}-pool",
            api_key=f"{username}-pool-key",
            base_url="https://rh.example",
            digital_human_ai_app_id=f"{username}-app",
            max_concurrent_tasks=2,
            is_enabled=True,
            admin_user_ids=[user.id],
        )
        db.commit()
        return account.id


def login(client: TestClient, username: str, password: str = "password123") -> None:
    login_page = client.get("/login")
    token_match = re.search(
        r'name="csrf_token" value="([^"]+)"',
        login_page.text,
    )
    assert token_match is not None
    client.headers["X-CSRF-Token"] = token_match.group(1)
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
