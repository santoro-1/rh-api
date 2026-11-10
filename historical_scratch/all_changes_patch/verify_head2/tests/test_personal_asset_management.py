from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import WebApiSettings, create_app  # noqa: E402
from jyd_probe.template_library import TemplateLibrary  # noqa: E402


class LocalAssetManagementTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.root = root / f"personal_asset_management_{uuid.uuid4().hex}"
        self.root.mkdir()
        self.personal_root = self.root / "personal"
        self.public_effect_root = self.root / "public-effects"
        self.effect_root_patch = patch(
            "jyd_probe.web_api.EFFECT_LIBRARY_ROOT", self.public_effect_root
        )
        self.effect_root_patch.start()
        settings = WebApiSettings(
            storage_root=self.root / "storage",
            template_library_root=self.root / "templates",
            default_draft_root=self.root / "drafts",
            audio_library_root=self.root / "audio",
            admin_password="internal-password",
            admin_session_secret="test-session-secret",
            site_password="operator-password",
            site_session_secret="test-site-session-secret",
            execution_mode="agent",
            agent_token="test-agent-token",
            database_path=self.root / "control.db",
            personal_library_root=self.personal_root,
            auth_authority=True,
            allow_local_file_access=True,
        )
        for directory in (
            settings.storage_root,
            settings.template_library_root,
            settings.default_draft_root,
            settings.audio_library_root,
            self.personal_root / "effect_library",
            self.public_effect_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.identity = f"resource_id:personal-{uuid.uuid4().hex}"
        self.public_identity = f"resource_id:public-{uuid.uuid4().hex}"
        (self.personal_root / "effect_library" / "personal-effect.json").write_text(
            json.dumps(
                {
                    "schema": "jyd_probe.effect.v1",
                    "material": {
                        "name": "个人测试特效",
                        "resource_id": self.identity.removeprefix("resource_id:"),
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.public_effect_root / "public-effect.json").write_text(
            json.dumps(
                {
                    "schema": "jyd_probe.effect.v1",
                    "material": {
                        "name": "基础库测试特效",
                        "resource_id": self.public_identity.removeprefix("resource_id:"),
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        template_source = self.root / "template-source"
        template_source.mkdir()
        (template_source / "draft_content.json").write_text(
            json.dumps({"duration": 1_000_000, "tracks": [], "materials": {}}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.template = TemplateLibrary(settings.template_library_root).import_template(
            template_source,
            template_id="managed-template",
            name="测试母版",
            import_info={"source": "local_collector"},
            expires_at=(datetime.now() + timedelta(hours=48)).isoformat(timespec="seconds"),
        )
        self.app = create_app(settings)
        self.client = TestClient(self.app, client=("127.0.0.1", 50000))
        response = self.client.post(
            "/api/auth/login",
            json={"username": "operator", "password": "operator-password", "next": "/app"},
        )
        self.assertEqual(response.status_code, 200)

    def tearDown(self) -> None:
        self.client.close()
        self.effect_root_patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_local_operator_manages_collected_and_base_assets(self) -> None:
        listed = self.client.get("/api/local-assets?include_deleted=true")
        self.assertEqual(listed.status_code, 200)
        items = {value["identity"]: value for value in listed.json()["items"]}
        self.assertEqual(items[self.identity]["library_scope"], "personal")
        self.assertEqual(items[self.public_identity]["library_scope"], "public")
        self.assertEqual(items[self.template.template_id]["kind"], "template")

        endpoint = f"/api/local-assets/effect/{self.public_identity}"
        disabled = self.client.patch(endpoint, json={"enabled": False})
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["enabled"])

        deleted = self.client.delete(endpoint)
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])
        self.assertFalse(deleted.json()["enabled"])

        restored = self.client.post(f"{endpoint}/restore")
        self.assertEqual(restored.status_code, 200)
        self.assertFalse(restored.json()["deleted"])
        self.assertTrue(restored.json()["enabled"])

        template_endpoint = f"/api/local-assets/template/{self.template.template_id}"
        template_deleted = self.client.delete(template_endpoint)
        self.assertEqual(template_deleted.status_code, 200)
        self.assertTrue(template_deleted.json()["deleted"])
        self.assertFalse(
            any(item["template_id"] == self.template.template_id for item in self.client.get("/api/templates").json())
        )
        self.app.state.storage_lifecycle.cleanup(now=datetime.now() + timedelta(days=3))
        self.assertTrue(self.template.root_dir.is_dir())
        template_restored = self.client.post(f"{template_endpoint}/restore")
        self.assertEqual(template_restored.status_code, 200)
        self.assertTrue(
            any(item["template_id"] == self.template.template_id for item in self.client.get("/api/templates").json())
        )

    def test_recycle_bin_purges_actual_asset_file_after_seven_days(self) -> None:
        endpoint = f"/api/local-assets/effect/{self.public_identity}"
        deleted = self.client.delete(endpoint)
        self.assertEqual(deleted.status_code, 200)
        effect_path = self.public_effect_root / "public-effect.json"
        self.assertTrue(effect_path.is_file())

        report = self.app.state.storage_lifecycle.cleanup(
            now=datetime.now() + timedelta(days=8)
        )

        self.assertEqual(report["purged_assets"], 1)
        self.assertFalse(effect_path.exists())
        listed = self.client.get("/api/local-assets?include_deleted=true").json()["items"]
        self.assertFalse(any(item["identity"] == self.public_identity for item in listed))

    def test_remote_operator_cannot_manage_processor_assets(self) -> None:
        with TestClient(self.app, client=("192.168.1.88", 50000)) as remote:
            logged_in = remote.post(
                "/api/auth/login",
                json={
                    "username": "operator",
                    "password": "operator-password",
                    "next": "/app",
                },
            )
            self.assertEqual(logged_in.status_code, 200)
            self.assertFalse(remote.get("/api/health").json()["local_file_access"])
            response = remote.delete(
                f"/api/local-assets/effect/{self.public_identity}"
            )
        self.assertEqual(response.status_code, 403)

    def test_local_management_rejects_unknown_asset(self) -> None:
        response = self.client.delete(
            "/api/local-assets/effect/resource_id:not-a-local-asset"
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
