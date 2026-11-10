from __future__ import annotations

import importlib
import os
from pathlib import Path
import shutil
import sys
import unittest
import uuid

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "apps" / "auth_center"


class CloudAuthCenterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = PROJECT_ROOT / "runtime" / "test_tmp" / f"cloud_auth_{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        sys.path.insert(0, str(APP_ROOT))
        os.environ["JYD_AUTH_DATA_DIR"] = str(self.root)
        os.environ["JYD_AUTH_ADMIN_PASSWORD"] = "admin-test-password"
        os.environ["JYD_AUTH_COOKIE_SECURE"] = "false"
        sys.modules.pop("app", None)
        sys.modules.pop("auth_store", None)
        self.module = importlib.import_module("app")
        self.client = TestClient(self.module.app)

    def tearDown(self) -> None:
        self.client.close()
        sys.modules.pop("app", None)
        sys.modules.pop("auth_store", None)
        if str(APP_ROOT) in sys.path:
            sys.path.remove(str(APP_ROOT))
        shutil.rmtree(self.root, ignore_errors=True)

    def _admin_login(self) -> None:
        response = self.client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "admin-test-password"},
        )
        self.assertEqual(response.status_code, 200)

    def test_admin_can_create_disable_and_revoke_user(self) -> None:
        self._admin_login()
        created = self.client.post(
            "/api/admin/users",
            json={"username": "tester", "display_name": "测试", "password": "password-123"},
        )
        self.assertEqual(created.status_code, 200)
        user = created.json()

        login = self.client.post(
            "/api/auth/center/login", json={"username": "tester", "password": "password-123"}
        )
        self.assertEqual(login.status_code, 200)
        token = login.json()["access_token"]
        self.assertEqual(
            self.client.post("/api/auth/center/verify", json={"access_token": token}).status_code,
            200,
        )

        disabled = self.client.patch(
            f"/api/admin/users/{user['user_id']}", json={"enabled": False}
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(
            self.client.post("/api/auth/center/verify", json={"access_token": token}).status_code,
            401,
        )

    def test_handoff_is_one_time(self) -> None:
        self._admin_login()
        self.client.post(
            "/api/admin/users", json={"username": "tester", "password": "password-123"}
        )
        token = self.client.post(
            "/api/auth/center/login", json={"username": "tester", "password": "password-123"}
        ).json()["access_token"]
        code = self.client.post(
            "/api/auth/center/handoff", json={"access_token": token}
        ).json()["handoff_code"]
        first = self.client.post(
            "/api/auth/center/handoff/consume", json={"handoff_code": code}
        )
        second = self.client.post(
            "/api/auth/center/handoff/consume", json={"handoff_code": code}
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 401)


if __name__ == "__main__":
    unittest.main()
