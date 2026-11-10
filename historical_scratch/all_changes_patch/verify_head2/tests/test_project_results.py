from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import WebApiSettings, create_app  # noqa: E402


class ProjectResultLibraryApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = PROJECT_ROOT / "runtime" / "test_tmp" / f"results_{uuid.uuid4().hex}"
        self.settings = WebApiSettings(
            storage_root=self.root / "storage",
            template_library_root=self.root / "templates",
            default_draft_root=self.root / "drafts",
            audio_library_root=self.root / "audio",
            result_library_root=self.root / "auto",
            admin_password="admin-pass",
            admin_session_secret="admin-secret",
            auth_authority=False,
            auth_server_url="http://127.0.0.1:8000",
            execution_mode="agent",
        )
        for directory in (
            self.settings.storage_root,
            self.settings.template_library_root,
            self.settings.default_draft_root,
            self.settings.audio_library_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_daily_batch_archive_gallery_filter_and_zip_are_real(self) -> None:
        user = {"user_id": "user-1", "username": "tester", "enabled": True}

        def verify(_client, token):
            return user if token == "center-token" else None

        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "center-token", "user": user},
        ), patch("jyd_probe.auth_center.AuthCenterClient.verify", new=verify):
            app = create_app(self.settings)
            store = app.state.project_store
            library = app.state.project_result_library
            project = store.create_project(
                owner_user_id="user-1",
                owner_username="tester",
                name="八月五日批次",
                items=[{"row_key": "1", "script_text": "真实成果脚本"}],
            )
            source = self.root / "上传脚本.xlsx"
            source.write_bytes(b"xlsx-source")
            store.add_script_source(
                "user-1",
                project["project_id"],
                filename=source.name,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                size_bytes=source.stat().st_size,
                sha256="sha",
                managed_path=str(source),
            )
            first = library.prepare_batch(
                "user-1",
                project["project_id"],
                operation_type="VARIANT_GENERATE",
                now=datetime.fromisoformat("2026-08-05T09:00:00+08:00"),
            )
            second = library.prepare_batch(
                "user-1",
                project["project_id"],
                operation_type="VARIANT_SUPPLEMENT",
                now=datetime.fromisoformat("2026-08-05T10:00:00+08:00"),
            )
            self.assertEqual(Path(first["export_path"]), self.root / "auto" / "8.5" / "1")
            self.assertEqual(Path(second["export_path"]), self.root / "auto" / "8.5" / "2")
            self.assertTrue((Path(first["export_path"]) / "上传脚本.xlsx").is_file())
            output = Path(first["export_path"]) / "任务-1-变体-001.mp4"
            output.write_bytes(b"real-result")
            item = project["items"][0]
            asset = store.add_asset(
                owner_user_id="user-1",
                project_id=project["project_id"],
                item_id=item["item_id"],
                asset_type="variant_video",
                source_type="jianying_variant",
                status="READY",
                filename=output.name,
                managed_path=str(output),
                external_ref={"batch_id": "jianying-1", "render_job_id": "job-1"},
                metadata={
                    "result_batch_id": first["result_batch_id"],
                    "source_video_asset_id": "source-v3",
                },
                make_current=True,
            )
            store.update_result_batch(
                "user-1", first["result_batch_id"], status="SUCCEEDED", jianying_batch_id="jianying-1"
            )

            with TestClient(app) as client:
                client.post(
                    "/api/auth/login",
                    json={"username": "tester", "password": "pass123"},
                )
                response = client.get(
                    "/api/new/gallery?date_key=20260805&batch_no=1&keyword=真实成果"
                )
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["total_batches"], 1)
                self.assertEqual(payload["total_videos"], 1)
                video = payload["batches"][0]["videos"][0]
                self.assertEqual(video["metadata"]["source_video_asset_id"], "source-v3")
                self.assertTrue(video["available"])

                archive = client.post(
                    "/api/new/gallery/downloads", json={"asset_ids": [asset["asset_id"]]}
                )
                self.assertEqual(archive.status_code, 200, archive.text)
                with zipfile.ZipFile(BytesIO(archive.content)) as package:
                    self.assertEqual(package.namelist(), [output.name])
                    self.assertEqual(package.read(output.name), b"real-result")

                deleted = client.post(
                    "/api/new/gallery/deletions",
                    json={"asset_ids": [asset["asset_id"]]},
                )
                self.assertEqual(deleted.status_code, 200, deleted.text)
                self.assertEqual(
                    deleted.json(),
                    {"deleted_count": 1, "file_deleted_count": 1},
                )
                self.assertFalse(output.exists())
                empty_gallery = client.get("/api/new/gallery")
                self.assertEqual(empty_gallery.status_code, 200, empty_gallery.text)
                self.assertEqual(empty_gallery.json()["total_videos"], 0)

    def test_gallery_bulk_delete_is_account_scoped_and_atomic(self) -> None:
        user = {"user_id": "user-1", "username": "tester", "enabled": True}

        def verify(_client, token):
            return user if token == "center-token" else None

        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "center-token", "user": user},
        ), patch("jyd_probe.auth_center.AuthCenterClient.verify", new=verify):
            app = create_app(self.settings)
            store = app.state.project_store
            owned_project = store.create_project(
                owner_user_id="user-1",
                owner_username="tester",
                name="当前账号成果",
                items=[{"row_key": "1", "script_text": "当前账号脚本"}],
            )
            foreign_project = store.create_project(
                owner_user_id="user-2",
                owner_username="other",
                name="其他账号成果",
                items=[{"row_key": "1", "script_text": "其他账号脚本"}],
            )
            owned_path = self.root / "auto" / "8.5" / "1" / "owned.mp4"
            foreign_path = self.root / "auto" / "8.5" / "2" / "foreign.mp4"
            owned_path.parent.mkdir(parents=True, exist_ok=True)
            foreign_path.parent.mkdir(parents=True, exist_ok=True)
            owned_path.write_bytes(b"owned")
            foreign_path.write_bytes(b"foreign")
            owned_asset = store.add_asset(
                owner_user_id="user-1",
                project_id=owned_project["project_id"],
                item_id=owned_project["items"][0]["item_id"],
                asset_type="variant_video",
                source_type="jianying_variant",
                status="READY",
                filename=owned_path.name,
                managed_path=str(owned_path),
                make_current=True,
            )
            foreign_asset = store.add_asset(
                owner_user_id="user-2",
                project_id=foreign_project["project_id"],
                item_id=foreign_project["items"][0]["item_id"],
                asset_type="variant_video",
                source_type="jianying_variant",
                status="READY",
                filename=foreign_path.name,
                managed_path=str(foreign_path),
                make_current=True,
            )

            with TestClient(app) as client:
                client.post(
                    "/api/auth/login",
                    json={"username": "tester", "password": "pass123"},
                )
                denied = client.post(
                    "/api/new/gallery/deletions",
                    json={
                        "asset_ids": [
                            owned_asset["asset_id"],
                            foreign_asset["asset_id"],
                        ]
                    },
                )
                self.assertEqual(denied.status_code, 404, denied.text)
                self.assertTrue(owned_path.is_file())
                self.assertTrue(foreign_path.is_file())
                self.assertEqual(len(store.list_gallery_records("user-1")["videos"]), 1)
                self.assertEqual(len(store.list_gallery_records("user-2")["videos"]), 1)

                deleted = client.post(
                    "/api/new/gallery/deletions",
                    json={"asset_ids": [owned_asset["asset_id"]]},
                )
                self.assertEqual(deleted.status_code, 200, deleted.text)
                self.assertFalse(owned_path.exists())
                self.assertTrue(foreign_path.is_file())
                self.assertEqual(len(store.list_gallery_records("user-1")["videos"]), 0)
                self.assertEqual(len(store.list_gallery_records("user-2")["videos"]), 1)


if __name__ == "__main__":
    unittest.main()
