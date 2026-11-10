from __future__ import annotations

from pathlib import Path
import io
import json
import shutil
import sqlite3
import sys
import unittest
import uuid
import zipfile
from unittest.mock import patch

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.project_store import PROJECT_SCHEMA_VERSION, ProjectStore  # noqa: E402
from jyd_probe.task_store import SQLiteTaskStore  # noqa: E402
from jyd_probe.web_api import WebApiSettings, create_app  # noqa: E402


class ProjectApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            PROJECT_ROOT / "runtime" / "test_tmp" / f"project_api_{uuid.uuid4().hex}"
        )
        self.root.mkdir(parents=True)
        self.settings = WebApiSettings(
            storage_root=self.root / "storage",
            template_library_root=self.root / "templates",
            default_draft_root=self.root / "drafts",
            audio_library_root=self.root / "audio",
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

    @staticmethod
    def _login(client: TestClient, user: dict[str, object]) -> None:
        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": f"token-{user['user_id']}", "user": user},
        ):
            response = client.post(
                "/api/auth/login",
                json={"username": user["username"], "password": "pass123"},
            )
        if response.status_code != 200:
            raise AssertionError(response.text)

    def test_project_contract_is_owned_and_contains_backend_actions(self) -> None:
        first_user = {"user_id": "user-1", "username": "tester-1", "enabled": True}
        second_user = {"user_id": "user-2", "username": "tester-2", "enabled": True}
        active_user = first_user

        def verify(_client, _token):
            return active_user

        with patch("jyd_probe.auth_center.AuthCenterClient.verify", new=verify):
            app = create_app(self.settings)
            with TestClient(app) as client:
                self._login(client, first_user)
                created = client.post(
                    "/api/new/projects",
                    json={
                        "name": "八月数字人口播",
                        "items": [
                            {"row_key": "001", "script_text": "第一条口播。"},
                            {"row_key": "002", "script_text": "第二条口播。"},
                        ],
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                project = created.json()
                self.assertEqual(project["schema"], "jyd.project.v1")
                self.assertRegex(project["project_no"], r"^DH-\d{8}-\d{4}$")
                self.assertEqual(project["status"], "DRAFT")
                self.assertEqual(len(project["items"]), 2)
                self.assertTrue(project["allowed_actions"]["edit_inputs"])
                self.assertTrue(project["allowed_actions"]["generate_audio"])
                self.assertFalse(project["allowed_actions"]["start_composition"])
                self.assertFalse(project["allowed_actions"]["generate_variants"])

                item = project["items"][0]
                self.assertIsNone(item["outputs"]["audio"])
                self.assertIsNone(item["outputs"]["composition_video"])
                self.assertEqual(item["outputs"]["variants"], [])
                self.assertEqual(item["subtitles"]["status"], "NOT_AVAILABLE")
                self.assertEqual(item["subtitles"]["raw_cues"], [])
                self.assertEqual(item["subtitles"]["render_cues"], [])
                self.assertEqual(item["subtitles"]["style"]["max_lines"], 1)
                self.assertEqual(item["subtitles"]["style"]["max_width_ratio"], 0.8)
                self.assertEqual(item["subtitles"]["style"]["bottom_offset_ratio"], 0.3)

                detail = client.get(f"/api/new/projects/{project['project_id']}")
                self.assertEqual(detail.status_code, 200)
                listing = client.get("/api/new/projects")
                self.assertEqual(listing.status_code, 200)
                self.assertEqual(listing.json()["total"], 1)

                active_user = second_user
                client.cookies.clear()
                self._login(client, second_user)
                hidden = client.get(f"/api/new/projects/{project['project_id']}")
                self.assertEqual(hidden.status_code, 404)
                self.assertEqual(client.get("/api/new/projects").json()["total"], 0)

    def test_delete_finished_project_cleans_only_managed_batch_artifacts(self) -> None:
        user = {"user_id": "user-1", "username": "tester-1", "enabled": True}

        def verify(_client, _token):
            return user

        with patch("jyd_probe.auth_center.AuthCenterClient.verify", new=verify):
            app = create_app(self.settings)
            with TestClient(app) as client:
                self._login(client, user)
                project = client.post(
                    "/api/new/projects",
                    json={"name": "可删除完成批次", "items": [{"row_key": "1", "script_text": "测试脚本"}]},
                ).json()
                project_id = project["project_id"]
                item_id = project["items"][0]["item_id"]
                store = app.state.project_store
                store.create_operation(
                    owner_user_id=user["user_id"],
                    project_id=project_id,
                    item_id=item_id,
                    operation_type="AUDIO_GENERATE",
                    idempotency_key="finished-project-delete",
                )
                blocked = client.delete(f"/api/new/projects/{project_id}")
                self.assertEqual(blocked.status_code, 409, blocked.text)
                self.assertIn("正在生成", blocked.json()["detail"])
                store.transition_audio_operation(
                    user["user_id"],
                    project_id,
                    item_id,
                    status="FAILED",
                    item_status="AUDIO_FAILED",
                    error_code="TEST_FINISHED",
                    error_message="测试完成态",
                )

                audio_path = self.settings.storage_root / "new_projects" / project_id / "audio" / "voice.mp3"
                audio_path.parent.mkdir(parents=True, exist_ok=True)
                audio_path.write_bytes(b"audio")
                store.add_asset(
                    owner_user_id=user["user_id"],
                    project_id=project_id,
                    item_id=item_id,
                    asset_type="audio",
                    source_type="minimax",
                    status="READY",
                    filename=audio_path.name,
                    managed_path=str(audio_path),
                    make_current=True,
                )

                result_batch = store.allocate_result_batch(
                    user["user_id"],
                    project_id,
                    export_root=app.state.project_result_library.root,
                    operation_type="VARIANT_GENERATE",
                )
                result_directory = Path(result_batch["export_path"])
                result_directory.mkdir(parents=True)
                (result_directory / "result.mp4").write_bytes(b"video")
                store.update_result_batch(
                    user["user_id"], result_batch["result_batch_id"], status="SUCCEEDED"
                )

                outside_batch = store.allocate_result_batch(
                    user["user_id"],
                    project_id,
                    export_root=self.root / "outside-result-root",
                    operation_type="VARIANT_GENERATE",
                )
                outside_directory = Path(outside_batch["export_path"])
                outside_directory.mkdir(parents=True)
                (outside_directory / "must-remain.txt").write_text("safe", encoding="utf-8")
                store.update_result_batch(
                    user["user_id"], outside_batch["result_batch_id"], status="SUCCEEDED"
                )

                deleted = client.delete(f"/api/new/projects/{project_id}")
                self.assertEqual(deleted.status_code, 200, deleted.text)
                self.assertEqual(deleted.json()["deleted_directory_count"], 1)
                self.assertFalse(audio_path.exists())
                self.assertFalse(result_directory.exists())
                self.assertTrue(outside_directory.is_dir())
                self.assertEqual(client.get(f"/api/new/projects/{project_id}").status_code, 404)

    def test_project_diagnostics_are_owned_redacted_and_project_scoped(self) -> None:
        first_user = {"user_id": "diag-1", "username": "tester-1", "enabled": True}
        second_user = {"user_id": "diag-2", "username": "tester-2", "enabled": True}
        active_user = first_user

        def verify(_client, _token):
            return active_user

        with patch("jyd_probe.auth_center.AuthCenterClient.verify", new=verify):
            app = create_app(self.settings)
            with TestClient(app) as client:
                self._login(client, first_user)
                project = client.post(
                    "/api/new/projects",
                    json={
                        "name": "诊断测试项目",
                        "items": [{"row_key": "001", "script_text": "不可导出的脚本文本"}],
                    },
                ).json()
                image_path = self.settings.storage_root / "diagnostic-image.png"
                image_path.parent.mkdir(parents=True, exist_ok=True)
                image_path.write_bytes(b"diagnostic-image")
                app.state.project_store.register_input_image(
                    owner_user_id=first_user["user_id"],
                    project_id=project["project_id"],
                    filename="diagnostic-image.png",
                    content_type="image/png",
                    size_bytes=image_path.stat().st_size,
                    sha256="d" * 64,
                    managed_path=str(image_path),
                )
                operation = app.state.project_store.create_operation(
                    owner_user_id=first_user["user_id"],
                    project_id=project["project_id"],
                    item_id=project["items"][0]["item_id"],
                    operation_type="AUDIO_GENERATE",
                    idempotency_key="diagnostics-test",
                    payload={"script_text": "同样不可导出"},
                    correlation_id="corr-diagnostics-1",
                )
                logs_root = self.settings.storage_root.parent / "logs"
                logs_root.mkdir(parents=True, exist_ok=True)
                (logs_root / "workbench.log").write_text(
                    "2026-08-07 INFO app: [EVENT workbench.test] matched "
                    f'{{"project_id":"{project["project_id"]}",'
                    '"access_token":"top-secret"}}\n'
                    "2026-08-07 INFO app: [EVENT workbench.test] unsafe-content "
                    f'{{"project_id":"{project["project_id"]}",'
                    '"script_text":"不可泄露的日志脚本"}}\n'
                    "2026-08-07 INFO app: [EVENT workbench.test] path "
                    f'{{"project_id":"{project["project_id"]}",'
                    '"output_path":"D:\\\\private folder\\\\result.mp4"}}\n'
                    "2026-08-07 INFO app: [EVENT workbench.test] unrelated "
                    '{"project_id":"another-project","access_token":"leak"}\n',
                    encoding="utf-8",
                )

                response = client.get(
                    f'/api/new/projects/{project["project_id"]}/diagnostics'
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.headers["content-type"], "application/zip")
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    summary_text = archive.read("项目诊断摘要.json").decode("utf-8")
                    summary = json.loads(summary_text)
                    log_text = archive.read("项目相关日志.txt").decode("utf-8")
                self.assertEqual(summary["schema"], "jyd.project-diagnostics.v1")
                self.assertEqual(
                    summary["operations"][0]["correlation_id"],
                    operation["correlation_id"],
                )
                self.assertEqual(
                    summary["items"][0]["output_files"]["base_video"],
                    {"recorded": False, "file_exists": False, "size_bytes": 0},
                )
                self.assertEqual(summary["items"][0]["h3"]["segments"], [])
                self.assertEqual(
                    summary["input_images"][0]["file"],
                    {
                        "recorded": True,
                        "file_exists": True,
                        "size_bytes": len(b"diagnostic-image"),
                    },
                )
                self.assertNotIn("script_text", summary_text)
                self.assertNotIn("不可导出的脚本文本", summary_text)
                self.assertIn(project["project_id"], log_text)
                self.assertIn('"access_token":"***"', log_text)
                self.assertNotIn("top-secret", log_text)
                self.assertNotIn("不可泄露的日志脚本", log_text)
                self.assertNotIn("D:\\private folder", log_text)
                self.assertIn("<redacted-path>", log_text)
                self.assertNotIn("another-project", log_text)
                self.assertNotIn("leak", log_text)

                active_user = second_user
                client.cookies.clear()
                self._login(client, second_user)
                hidden = client.get(
                    f'/api/new/projects/{project["project_id"]}/diagnostics'
                )
                self.assertEqual(hidden.status_code, 404)

    def test_draft_project_and_item_can_be_updated(self) -> None:
        user = {"user_id": "user-1", "username": "tester", "enabled": True}
        with patch("jyd_probe.auth_center.AuthCenterClient.verify", return_value=user):
            app = create_app(self.settings)
            with TestClient(app) as client:
                self._login(client, user)
                project = client.post(
                    "/api/new/projects",
                    json={
                        "name": "旧名称",
                        "items": [{"row_key": "001", "script_text": "旧脚本"}],
                    },
                ).json()
                renamed = client.patch(
                    f"/api/new/projects/{project['project_id']}",
                    json={"name": "新名称", "expected_revision": project["revision"]},
                )
                self.assertEqual(renamed.status_code, 200, renamed.text)
                item = renamed.json()["items"][0]
                edited = client.patch(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}",
                    json={"script_text": "修改后的脚本", "row_key": "A-001"},
                )
                self.assertEqual(edited.status_code, 200, edited.text)
                self.assertEqual(edited.json()["items"][0]["script_text"], "修改后的脚本")
                self.assertEqual(edited.json()["items"][0]["row_key"], "A-001")

    def test_inactive_project_items_can_be_deleted_to_empty_but_active_item_cannot(self) -> None:
        user = {"user_id": "delete-user", "username": "tester", "enabled": True}
        with patch("jyd_probe.auth_center.AuthCenterClient.verify", return_value=user):
            app = create_app(self.settings)
            with TestClient(app) as client:
                self._login(client, user)
                project = client.post(
                    "/api/new/projects",
                    json={
                        "name": "删除任务测试",
                        "items": [
                            {"row_key": "001", "script_text": "保留任务"},
                            {"row_key": "002", "script_text": "待删除任务"},
                        ],
                    },
                ).json()
                project_id = project["project_id"]
                first_item, second_item = project["items"]
                store = app.state.project_store
                store.create_operation(
                    owner_user_id=user["user_id"],
                    project_id=project_id,
                    item_id=second_item["item_id"],
                    operation_type="AUDIO_GENERATE",
                    idempotency_key="delete-active-audio",
                )

                active_delete = client.delete(
                    f"/api/new/projects/{project_id}/items/{second_item['item_id']}"
                )
                self.assertEqual(active_delete.status_code, 409, active_delete.text)
                self.assertIn("正在生成", active_delete.json()["detail"])

                store.transition_audio_operation(
                    user["user_id"],
                    project_id,
                    second_item["item_id"],
                    status="FAILED",
                    item_status="AUDIO_FAILED",
                    error_code="TEST_FAILED",
                    error_message="测试结束运行态",
                )
                audio_path = (
                    self.settings.storage_root
                    / "new_projects"
                    / project_id
                    / "audio"
                    / "delete-me.mp3"
                )
                audio_path.parent.mkdir(parents=True, exist_ok=True)
                audio_path.write_bytes(b"local-audio")
                store.add_asset(
                    owner_user_id=user["user_id"],
                    project_id=project_id,
                    item_id=second_item["item_id"],
                    asset_type="audio",
                    source_type="minimax",
                    status="READY",
                    filename="delete-me.mp3",
                    managed_path=str(audio_path),
                    make_current=True,
                )

                deleted = client.delete(
                    f"/api/new/projects/{project_id}/items/{second_item['item_id']}"
                )
                self.assertEqual(deleted.status_code, 200, deleted.text)
                self.assertEqual(len(deleted.json()["items"]), 1)
                self.assertEqual(deleted.json()["items"][0]["item_id"], first_item["item_id"])
                self.assertEqual(deleted.json()["items"][0]["position"], 1)
                self.assertTrue(
                    deleted.json()["items"][0]["allowed_actions"]["delete_item"]
                )
                self.assertFalse(audio_path.exists())

                last_delete = client.delete(
                    f"/api/new/projects/{project_id}/items/{first_item['item_id']}"
                )
                self.assertEqual(last_delete.status_code, 200, last_delete.text)
                self.assertEqual(last_delete.json()["items"], [])
                self.assertEqual(last_delete.json()["status"], "DRAFT")

                restored = client.put(
                    f"/api/new/projects/{project_id}/inputs",
                    json={
                        "items": [
                            {"row_key": "001", "script_text": "删除后重新添加"}
                        ]
                    },
                )
                self.assertEqual(restored.status_code, 200, restored.text)
                self.assertEqual(len(restored.json()["items"]), 1)

    def test_asset_versions_preserve_history_and_change_current_pointer(self) -> None:
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="素材版本测试",
            items=[{"row_key": "001", "script_text": "测试口播。"}],
        )
        item_id = project["items"][0]["item_id"]

        first = store.add_asset(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=item_id,
            asset_type="composition_video",
            source_type="auto_composition",
            status="READY",
            filename="auto.mp4",
            make_current=True,
        )
        second = store.add_asset(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=item_id,
            asset_type="composition_video",
            source_type="user_upload",
            status="READY",
            filename="rough-cut.mp4",
            make_current=True,
        )

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        detail = store.get_project("user-1", project["project_id"])
        current = detail["items"][0]["outputs"]["composition_video"]
        self.assertEqual(current["asset_id"], second["asset_id"])
        history = detail["items"][0]["asset_history"]["composition_video"]
        self.assertEqual([asset["version"] for asset in history], [1, 2])
        self.assertTrue(detail["allowed_actions"]["generate_variants"])

    def test_v12_stores_managed_media_as_portable_storage_references(self) -> None:
        current_storage = self.root / "current-install" / "data" / "web_storage"
        current_storage.mkdir(parents=True)
        database_path = current_storage / "control.db"
        store = ProjectStore(database_path)
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="安装目录迁移测试",
            items=[{"row_key": "001", "script_text": "测试口播。"}],
        )
        project_id = project["project_id"]
        item_id = project["items"][0]["item_id"]

        image_relative = Path("new_projects") / project_id / "input_images" / "face.png"
        image_path = current_storage / image_relative
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"image")
        old_storage = self.root / "old-install" / "data" / "web_storage"
        image = store.register_input_image(
            owner_user_id="user-1",
            project_id=project_id,
            filename="face.png",
            content_type="image/png",
            size_bytes=5,
            sha256="a" * 64,
            managed_path=str(old_storage / image_relative),
        )

        reference_relative = (
            Path("new_projects") / project_id / item_id / "h3_reference" / "ref.mp4"
        )
        reference_path = current_storage / reference_relative
        reference_path.parent.mkdir(parents=True)
        reference_path.write_bytes(b"reference")
        store.add_h3_reference_video(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=item_id,
            filename="ref.mp4",
            managed_path=str(old_storage / reference_relative),
            metadata={"sha256": "b" * 64},
        )

        with sqlite3.connect(database_path) as connection:
            stored_image_path = connection.execute(
                "SELECT managed_path FROM project_input_images WHERE image_id=?",
                (image["image_id"],),
            ).fetchone()[0]
            stored_reference_path = connection.execute(
                "SELECT managed_path FROM project_assets "
                "WHERE item_id=? AND asset_type='h3_reference_video'",
                (item_id,),
            ).fetchone()[0]
        self.assertEqual(
            stored_image_path,
            "storage://" + image_relative.as_posix(),
        )
        self.assertEqual(
            stored_reference_path,
            "storage://" + reference_relative.as_posix(),
        )

        moved_storage = self.root / "moved-install" / "data" / "web_storage"
        shutil.copytree(current_storage, moved_storage)
        reopened = ProjectStore(moved_storage / "control.db")
        rebound_image = reopened.get_input_image("user-1", project_id, image["image_id"])
        self.assertEqual(
            Path(rebound_image["managed_path"]),
            (moved_storage / image_relative).resolve(),
        )
        self.assertTrue(rebound_image["file_exists"])
        rebound_project = reopened.get_project("user-1", project_id)
        reference = rebound_project["items"][0]["inputs"]["h3_reference_video"]
        self.assertEqual(
            Path(reference["managed_path"]),
            (moved_storage / reference_relative).resolve(),
        )
        self.assertTrue(reference["file_exists"])

    def test_v12_backs_up_and_migrates_legacy_absolute_paths(self) -> None:
        current_storage = self.root / "current-install" / "data" / "web_storage"
        current_storage.mkdir(parents=True)
        database_path = current_storage / "control.db"
        store = ProjectStore(database_path)
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="旧路径迁移测试",
            items=[{"row_key": "001", "script_text": "测试口播。"}],
        )
        project_id = project["project_id"]
        item_id = project["items"][0]["item_id"]
        image_relative = Path("new_projects") / project_id / "input_images" / "face.png"
        image_path = current_storage / image_relative
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"image")
        image = store.register_input_image(
            owner_user_id="user-1",
            project_id=project_id,
            filename="face.png",
            content_type="image/png",
            size_bytes=5,
            sha256="a" * 64,
            managed_path=str(image_path),
        )

        old_storage = self.root / "old-install" / "data" / "web_storage"
        legacy_absolute = str(old_storage / image_relative)
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE project_input_images SET managed_path=? WHERE image_id=?",
                (legacy_absolute, image["image_id"]),
            )
            connection.execute(
                "UPDATE project_schema_meta SET value='11' WHERE key='version'"
            )
            connection.commit()

        reopened = ProjectStore(database_path)
        backup_path = Path(str(reopened.startup_database_backup_path))
        self.assertTrue(backup_path.is_file())
        self.assertEqual(reopened.startup_storage_path_migration_count, 1)
        rebound = reopened.get_input_image("user-1", project_id, image["image_id"])
        self.assertEqual(Path(rebound["managed_path"]), image_path.resolve())
        self.assertTrue(rebound["file_exists"])
        with sqlite3.connect(database_path) as connection:
            stored_path = connection.execute(
                "SELECT managed_path FROM project_input_images WHERE image_id=?",
                (image["image_id"],),
            ).fetchone()[0]
            schema_version = connection.execute(
                "SELECT value FROM project_schema_meta WHERE key='version'"
            ).fetchone()[0]
        self.assertEqual(stored_path, "storage://" + image_relative.as_posix())
        self.assertEqual(schema_version, "12")

        reopened_again = ProjectStore(database_path)
        self.assertIsNone(reopened_again.startup_database_backup_path)
        self.assertEqual(reopened_again.startup_storage_path_migration_count, 0)
        self.assertEqual(reopened_again.startup_relocated_managed_path_count, 0)

    def test_storage_references_reject_traversal_and_preserve_external_files(self) -> None:
        storage_root = self.root / "data" / "web_storage"
        store = ProjectStore(storage_root / "control.db")
        with self.assertRaisesRegex(ValueError, "素材相对路径无效"):
            store.resolve_managed_path("storage://../outside.mp4")
        with self.assertRaisesRegex(ValueError, "素材相对路径无效"):
            store.encode_managed_path("storage://folder/../../outside.mp4")

        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="外部交接素材测试",
            items=[{"row_key": "001", "script_text": "测试口播。"}],
        )
        external = self.root / "handoff" / "external.mp4"
        external.parent.mkdir(parents=True)
        external.write_bytes(b"external")
        asset = store.add_asset(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=project["items"][0]["item_id"],
            asset_type="base_video",
            source_type="h3_handoff",
            status="READY",
            filename=external.name,
            managed_path=str(external),
            make_current=True,
        )
        self.assertEqual(Path(asset["managed_path"]), external.resolve())
        with sqlite3.connect(storage_root / "control.db") as connection:
            stored_path = connection.execute(
                "SELECT managed_path FROM project_assets WHERE asset_id=?",
                (asset["asset_id"],),
            ).fetchone()[0]
        self.assertEqual(stored_path, str(external.resolve()))

    def test_project_voice_applies_atomically_and_preserves_old_audio_history(self) -> None:
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="项目默认音色测试",
            items=[
                {"row_key": "001", "script_text": "第一条。"},
                {"row_key": "002", "script_text": "第二条。"},
            ],
        )
        first_item_id = project["items"][0]["item_id"]
        selected = store.configure_project_voice(
            "user-1",
            project["project_id"],
            voice_asset_id="saved-voice-1",
        )
        self.assertEqual(
            [item["settings"]["voice_asset_id"] for item in selected["items"]],
            ["saved-voice-1", "saved-voice-1"],
        )
        self.assertEqual(
            selected["settings"]["default_voice_asset_id"], "saved-voice-1"
        )
        self.assertEqual(
            store.get_voice_preferences("user-1")["default_voice_asset_id"],
            "saved-voice-1",
        )

        old_audio = store.add_asset(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=first_item_id,
            asset_type="audio",
            source_type="minimax",
            status="READY",
            filename="old.mp3",
            make_current=True,
        )
        changed = store.configure_project_voice(
            "user-1",
            project["project_id"],
            voice_asset_id="saved-voice-2",
        )
        first = changed["items"][0]
        self.assertEqual(first["status"], "DRAFT")
        self.assertIsNone(first["outputs"]["audio"])
        self.assertEqual(
            first["asset_history"]["audio"][0]["asset_id"], old_audio["asset_id"]
        )
        self.assertTrue(
            all(
                item["settings"]["voice_asset_id"] == "saved-voice-2"
                for item in changed["items"]
            )
        )

    def test_project_voice_scope_preserves_audio_outside_requested_items(self) -> None:
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="按文章类型切换音色",
            items=[
                {"row_key": "1", "script_text": "鸡汤文。"},
                {"row_key": "2", "script_text": "干货文。"},
            ],
        )
        project = store.configure_project_voice(
            "user-1", project["project_id"], voice_asset_id="voice-chicken"
        )
        chicken_item = project["items"][0]
        dry_item = project["items"][1]
        chicken_audio = store.add_asset(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=chicken_item["item_id"],
            asset_type="audio",
            source_type="minimax",
            status="READY",
            filename="chicken.mp3",
            make_current=True,
        )

        changed = store.configure_project_voice(
            "user-1",
            project["project_id"],
            voice_asset_id="voice-dry",
            item_ids=[dry_item["item_id"]],
        )

        first, second = changed["items"]
        self.assertEqual(first["settings"]["voice_asset_id"], "voice-chicken")
        self.assertEqual(first["outputs"]["audio"]["asset_id"], chicken_audio["asset_id"])
        self.assertEqual(first["status"], "AUDIO_READY")
        self.assertEqual(second["settings"]["voice_asset_id"], "voice-dry")
        self.assertIsNone(second["outputs"]["audio"])
        self.assertEqual(
            changed["settings"]["default_voice_asset_id"], "voice-dry"
        )
        self.assertEqual(
            store.get_voice_preferences("user-1")["default_voice_asset_id"],
            "voice-dry",
        )

    def test_project_tables_do_not_modify_existing_render_queue_schema_or_rows(self) -> None:
        database = self.settings.storage_root / "control.db"
        task_store = SQLiteTaskStore(database)
        task_store.add_batch(
            {"batch_id": "existing-batch", "created_at": "2026-08-04T00:00:00"}
        )
        task_store.add_job(
            "existing-job",
            {"schema": "jyd.render_job.v1"},
            {
                "job_id": "existing-job",
                "batch_id": "existing-batch",
                "status": "pending",
                "created_at": "2026-08-04T00:00:00",
            },
        )

        ProjectStore(database)

        self.assertEqual(task_store.get_status("existing-job")["status"], "pending")
        with sqlite3.connect(database) as connection:
            render_schema_version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()[0]
            project_schema_version = connection.execute(
                "SELECT value FROM project_schema_meta WHERE key='version'"
            ).fetchone()[0]
        self.assertEqual(render_schema_version, "1")
        self.assertEqual(project_schema_version, str(PROJECT_SCHEMA_VERSION))

    def test_operations_are_idempotent_and_external_links_are_preserved(self) -> None:
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="编排关系测试",
            items=[{"row_key": "001", "script_text": "测试口播。"}],
        )
        item_id = project["items"][0]["item_id"]

        first_operation = store.create_operation(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=item_id,
            operation_type="AUDIO_GENERATE",
            idempotency_key="audio-request-1",
            payload={"voice_asset_id": "voice-1"},
        )
        repeated_operation = store.create_operation(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=item_id,
            operation_type="AUDIO_GENERATE",
            idempotency_key="audio-request-1",
            payload={"voice_asset_id": "voice-1"},
        )
        self.assertEqual(
            first_operation["operation_id"], repeated_operation["operation_id"]
        )
        self.assertTrue(first_operation["correlation_id"])
        self.assertEqual(
            first_operation["correlation_id"], repeated_operation["correlation_id"]
        )
        self.assertEqual(
            store.get_project("user-1", project["project_id"])["status"],
            "PROCESSING",
        )

        first_link = store.add_link(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=item_id,
            system="digital_human",
            relation="generation_batch_item",
            external_id="digital-item-1",
        )
        repeated_link = store.add_link(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=item_id,
            system="digital_human",
            relation="generation_batch_item",
            external_id="digital-item-1",
        )
        self.assertEqual(first_link["link_id"], repeated_link["link_id"])
        detail = store.get_project("user-1", project["project_id"])
        self.assertEqual(len(detail["operations"]), 1)
        self.assertEqual(len(detail["links"]), 1)

        restarted_store = ProjectStore(self.settings.storage_root / "control.db")
        operation_after_restart = restarted_store.create_operation(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=item_id,
            operation_type="AUDIO_GENERATE",
            idempotency_key="audio-request-1",
            payload={"voice_asset_id": "voice-1"},
        )
        link_after_restart = restarted_store.add_link(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=item_id,
            system="digital_human",
            relation="generation_batch_item",
            external_id="digital-item-1",
        )
        self.assertEqual(
            operation_after_restart["operation_id"], first_operation["operation_id"]
        )
        self.assertEqual(
            operation_after_restart["correlation_id"],
            first_operation["correlation_id"],
        )
        self.assertEqual(link_after_restart["link_id"], first_link["link_id"])
        restarted_detail = restarted_store.get_project("user-1", project["project_id"])
        self.assertEqual(len(restarted_detail["operations"]), 1)
        self.assertEqual(len(restarted_detail["links"]), 1)

    def test_project_read_recovers_durable_audio_and_postprocess_completion(self) -> None:
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="持久化结果状态恢复",
            items=[
                {"row_key": "001", "script_text": "声音状态恢复。"},
                {"row_key": "002", "script_text": "后期状态恢复。"},
            ],
        )
        project_id = project["project_id"]
        audio_item_id = project["items"][0]["item_id"]
        postprocess_item_id = project["items"][1]["item_id"]

        audio_operation = store.create_operation(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=audio_item_id,
            operation_type="AUDIO_GENERATE",
            idempotency_key="audio-durable-result",
        )
        store.transition_audio_operation(
            "user-1",
            project_id,
            audio_item_id,
            status="RUNNING",
            item_status="AUDIO_RUNNING",
            result={"batch_id": "batch-1", "item_id": "remote-audio-1"},
        )
        audio_asset = store.add_asset(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=audio_item_id,
            asset_type="audio",
            source_type="minimax",
            status="READY",
            filename="audio.mp3",
            external_ref={"batch_id": "batch-1", "remote_item_id": "remote-audio-1"},
            make_current=True,
        )
        store.add_asset(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=audio_item_id,
            asset_type="base_video",
            source_type="h3",
            status="READY",
            filename="audio-row-base.mp4",
            make_current=True,
        )
        store.set_item_subtitles(
            "user-1",
            project_id,
            audio_item_id,
            {
                "source": "test",
                "raw_cues": [],
                "render_cues": [],
                "bound_audio_asset_id": audio_asset["asset_id"],
                "status": "PREVIEW_READY",
            },
        )

        store.add_asset(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=postprocess_item_id,
            asset_type="base_video",
            source_type="h3",
            status="READY",
            filename="postprocess-row-base.mp4",
            make_current=True,
        )
        store.set_item_subtitles(
            "user-1",
            project_id,
            postprocess_item_id,
            {
                "source": "test",
                "raw_cues": [],
                "render_cues": [],
                "status": "PREVIEW_READY",
            },
        )
        postprocess_operation = store.create_operation(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=postprocess_item_id,
            operation_type="POSTPROCESS_GENERATE",
            idempotency_key="postprocess-durable-result",
        )

        with store._transaction() as connection:
            connection.execute(
                "UPDATE project_operations SET status='SUCCEEDED' WHERE operation_id=?",
                (postprocess_operation["operation_id"],),
            )
            connection.execute(
                "UPDATE project_items SET status='AUDIO_RUNNING' WHERE item_id=?",
                (audio_item_id,),
            )
            connection.execute(
                "UPDATE project_items SET status='POSTPROCESS_RUNNING' WHERE item_id=?",
                (postprocess_item_id,),
            )
            connection.execute(
                "UPDATE projects SET status='PROCESSING' WHERE project_id=?",
                (project_id,),
            )

        recovered = store.get_project("user-1", project_id)
        recovered_items = {item["item_id"]: item for item in recovered["items"]}
        recovered_operations = {
            operation["operation_id"]: operation
            for operation in recovered["operations"]
        }
        self.assertEqual(recovered["status"], "COMPOSITION_READY")
        self.assertEqual(
            recovered_items[audio_item_id]["status"], "COMPOSITION_READY"
        )
        self.assertEqual(
            recovered_items[postprocess_item_id]["status"], "COMPOSITION_READY"
        )
        self.assertEqual(
            recovered_operations[audio_operation["operation_id"]]["status"],
            "SUCCEEDED",
        )
        self.assertEqual(
            recovered_operations[postprocess_operation["operation_id"]]["status"],
            "SUCCEEDED",
        )

    def test_project_read_keeps_unfinished_same_item_audio_retry_active(self) -> None:
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="新声音仍在生成",
            items=[{"row_key": "001", "script_text": "不要误收旧声音。"}],
        )
        project_id = project["project_id"]
        item_id = project["items"][0]["item_id"]
        operation = store.create_operation(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=item_id,
            operation_type="AUDIO_GENERATE",
            idempotency_key="new-audio-generation",
            payload={"retry": True, "remote_item_id": "remote-shared"},
        )
        store.transition_audio_operation(
            "user-1",
            project_id,
            item_id,
            status="RUNNING",
            item_status="AUDIO_RUNNING",
            result={"batch_id": "batch-1", "item_id": "remote-shared"},
        )
        store.add_asset(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=item_id,
            asset_type="audio",
            source_type="minimax",
            status="READY",
            filename="old-audio.mp3",
            external_ref={
                "batch_id": "batch-1",
                "remote_item_id": "remote-shared",
                "generation_version": 1,
            },
            make_current=True,
        )
        with store._transaction() as connection:
            connection.execute(
                "UPDATE project_items SET status='AUDIO_RUNNING' WHERE item_id=?",
                (item_id,),
            )
            connection.execute(
                "UPDATE projects SET status='PROCESSING' WHERE project_id=?",
                (project_id,),
            )

        active = store.get_project("user-1", project_id)
        active_operation = next(
            value
            for value in active["operations"]
            if value["operation_id"] == operation["operation_id"]
        )
        self.assertEqual(active["status"], "PROCESSING")
        self.assertEqual(active["items"][0]["status"], "AUDIO_RUNNING")
        self.assertEqual(active_operation["status"], "RUNNING")

    def test_transition_operation_can_finish_a_superseded_attempt(self) -> None:
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="旧任务状态回收测试",
            items=[{"row_key": "001", "script_text": "测试口播。"}],
        )
        item_id = project["items"][0]["item_id"]
        first = store.create_operation(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=item_id,
            operation_type="POSTPROCESS_EXPORT",
            idempotency_key="export-1",
            payload={},
        )
        second = store.create_operation(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=item_id,
            operation_type="POSTPROCESS_EXPORT",
            idempotency_key="export-2",
            payload={},
        )

        store.transition_operation(
            "user-1",
            project["project_id"],
            item_id,
            operation_id=first["operation_id"],
            operation_type="POSTPROCESS_EXPORT",
            status="FAILED",
            item_status="DRAFT",
            error_code="OLD_ATTEMPT_FAILED",
            error_message="旧尝试失败",
        )

        operations = store.get_project("user-1", project["project_id"])["operations"]
        by_id = {operation["operation_id"]: operation for operation in operations}
        self.assertEqual(by_id[first["operation_id"]]["status"], "FAILED")
        self.assertEqual(by_id[first["operation_id"]]["error_code"], "OLD_ATTEMPT_FAILED")
        self.assertEqual(by_id[second["operation_id"]]["status"], "PENDING")

    def test_current_audio_binds_subtitle_placeholder_and_enables_composition(self) -> None:
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="字幕绑定测试",
            items=[{"row_key": "001", "script_text": "测试口播。"}],
        )
        item_id = project["items"][0]["item_id"]
        audio = store.add_asset(
            owner_user_id="user-1",
            project_id=project["project_id"],
            item_id=item_id,
            asset_type="audio",
            source_type="minimax",
            status="READY",
            filename="speech.mp3",
            make_current=True,
        )
        detail = store.get_project("user-1", project["project_id"])
        item = detail["items"][0]
        self.assertEqual(detail["status"], "AUDIO_READY")
        self.assertEqual(
            item["subtitles"]["bound_audio_asset_id"], audio["asset_id"]
        )
        self.assertEqual(item["subtitles"]["status"], "PENDING_TIMESTAMPS")
        self.assertTrue(detail["allowed_actions"]["start_composition"])

    def test_completed_item_edits_preserve_history_and_restart_only_downstream(self) -> None:
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="版本化修改测试",
            items=[{"row_key": "001", "script_text": "旧脚本。"}],
        )
        project_id = project["project_id"]
        item_id = project["items"][0]["item_id"]

        first_image_path = self.settings.storage_root / "first.png"
        first_image_path.write_bytes(b"first")
        first_image = store.register_input_image(
            owner_user_id="user-1",
            project_id=project_id,
            filename="first.png",
            content_type="image/png",
            size_bytes=5,
            sha256="first",
            managed_path=str(first_image_path),
        )
        second_image_path = self.settings.storage_root / "second.png"
        second_image_path.write_bytes(b"second")
        second_image = store.register_input_image(
            owner_user_id="user-1",
            project_id=project_id,
            filename="second.png",
            content_type="image/png",
            size_bytes=6,
            sha256="second",
            managed_path=str(second_image_path),
        )
        store.replace_item_image("user-1", project_id, item_id, first_image["image_id"])

        def add_chain(suffix: str) -> tuple[dict, dict, dict]:
            audio = store.add_asset(
                owner_user_id="user-1",
                project_id=project_id,
                item_id=item_id,
                asset_type="audio",
                source_type="minimax",
                status="READY",
                filename=f"{suffix}.mp3",
                make_current=True,
            )
            base = store.add_asset(
                owner_user_id="user-1",
                project_id=project_id,
                item_id=item_id,
                asset_type="base_video",
                source_type="runninghub_merge",
                status="READY",
                filename=f"{suffix}-base.mp4",
                make_current=True,
            )
            video = store.add_asset(
                owner_user_id="user-1",
                project_id=project_id,
                item_id=item_id,
                asset_type="composition_video",
                source_type="jianying_postprocess",
                status="READY",
                filename=f"{suffix}-final.mp4",
                make_current=True,
            )
            return audio, base, video

        first_audio, first_base, first_video = add_chain("first")
        edited = store.update_item(
            "user-1", project_id, item_id, script_text="新脚本。"
        )["items"][0]
        self.assertEqual(edited["status"], "DRAFT")
        self.assertIsNone(edited["outputs"]["audio"])
        self.assertIsNone(edited["outputs"]["base_video"])
        self.assertIsNone(edited["outputs"]["composition_video"])
        self.assertEqual(edited["asset_history"]["audio"][0]["asset_id"], first_audio["asset_id"])
        self.assertEqual(edited["asset_history"]["base_video"][0]["asset_id"], first_base["asset_id"])
        self.assertEqual(edited["asset_history"]["composition_video"][0]["asset_id"], first_video["asset_id"])

        second_audio, _second_base, _second_video = add_chain("second")
        changed_image = store.replace_item_image(
            "user-1", project_id, item_id, second_image["image_id"]
        )["items"][0]
        self.assertEqual(changed_image["status"], "AUDIO_READY")
        self.assertEqual(changed_image["outputs"]["audio"]["asset_id"], second_audio["asset_id"])
        self.assertIsNone(changed_image["outputs"]["base_video"])
        self.assertIsNone(changed_image["outputs"]["composition_video"])

        _third_audio, third_base, _third_video = add_chain("third")
        postprocess = store.configure_item_postprocess(
            "user-1",
            project_id,
            item_id,
            font_identity="system:simhei.ttf",
            bgm_identity="bgm:test",
            text_color="#FFFFFF",
        )["items"][0]
        self.assertEqual(postprocess["status"], "BASE_VIDEO_READY")
        self.assertEqual(postprocess["outputs"]["base_video"]["asset_id"], third_base["asset_id"])
        self.assertIsNone(postprocess["outputs"]["composition_video"])
        self.assertEqual(postprocess["settings"]["postprocess"]["bgm_identity"], "bgm:test")

        add_chain("fourth")
        changed_voice = store.configure_item_voice(
            "user-1",
            project_id,
            item_id,
            voice_asset_id="new-voice",
        )["items"][0]
        self.assertEqual(changed_voice["status"], "DRAFT")
        self.assertIsNone(changed_voice["outputs"]["audio"])
        self.assertIsNone(changed_voice["outputs"]["base_video"])
        self.assertIsNone(changed_voice["outputs"]["composition_video"])


if __name__ == "__main__":
    unittest.main()
