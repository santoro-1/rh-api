from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = (
    PROJECT_ROOT
    / "apps"
    / "processor"
    / "frontend"
    / "new"
    / "project-script-template.xlsx"
)
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import WebApiSettings, create_app  # noqa: E402
from jyd_probe.project_inputs import (  # noqa: E402
    MAX_PROJECT_IMAGE_BYTES,
    detect_project_image,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ProjectInputsApiTest(unittest.TestCase):
    def test_project_image_limit_is_200_mb(self) -> None:
        self.assertEqual(MAX_PROJECT_IMAGE_BYTES, 200 * 1024 * 1024)

        class OversizedImage:
            def __len__(self) -> int:
                return MAX_PROJECT_IMAGE_BYTES + 1

        with self.assertRaisesRegex(ValueError, "单张图片不能超过 200 MB"):
            detect_project_image(OversizedImage(), "large.png")  # type: ignore[arg-type]

    def setUp(self) -> None:
        self.root = (
            PROJECT_ROOT / "runtime" / "test_tmp" / f"project_inputs_{uuid.uuid4().hex}"
        )
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
        self.user = {"user_id": "user-1", "username": "tester", "enabled": True}

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _client_context(self):
        user = self.user

        def verify(_client, token):
            return user if token == "center-token" else None

        return (
            patch(
                "jyd_probe.auth_center.AuthCenterClient.login",
                return_value={"access_token": "center-token", "user": user},
            ),
            patch("jyd_probe.auth_center.AuthCenterClient.verify", new=verify),
        )

    def _login(self, client: TestClient) -> None:
        response = client.post(
            "/api/auth/login",
            json={"username": "tester", "password": "pass123"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_project_digital_human_resolution_is_persisted(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "分辨率测试",
                    "items": [{"row_key": "1", "script_text": "测试脚本"}],
                },
            )
            self.assertEqual(project.status_code, 201, project.text)
            project_id = project.json()["project_id"]

            updated = client.put(
                f"/api/new/projects/{project_id}/digital-human-settings",
                json={"resolution": "1536"},
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(
                updated.json()["settings"]["digital_human"]["resolution"],
                "1536",
            )

            invalid = client.put(
                f"/api/new/projects/{project_id}/digital-human-settings",
                json={"resolution": "0"},
            )
            self.assertEqual(invalid.status_code, 409, invalid.text)
            self.assertIn("正整数", invalid.json()["detail"])

    def test_two_and_four_column_csv_and_downloadable_xlsx_template_are_accepted(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            template = client.get("/api/new/script-template")
            self.assertEqual(template.status_code, 200)
            self.assertEqual(template.content, TEMPLATE_PATH.read_bytes())

            xlsx_preview = client.post(
                "/api/new/script-imports/preview?filename=template.xlsx",
                content=template.content,
            )
            self.assertEqual(xlsx_preview.status_code, 200, xlsx_preview.text)
            self.assertEqual(xlsx_preview.json()["total_rows"], 3)
            self.assertEqual(xlsx_preview.json()["rows"][0]["row_key"], "1")
            self.assertEqual(xlsx_preview.json()["rows"][0]["article_type"], "干货类")
            self.assertEqual(xlsx_preview.json()["rows"][0]["assigned_account"], "1")

            csv_preview = client.post(
                "/api/new/script-imports/preview?filename=scripts.csv",
                content="任务ID,脚本内容\n1,第一条口播\n2,第二条口播\n".encode(
                    "utf-8-sig"
                ),
            )
            self.assertEqual(csv_preview.status_code, 200, csv_preview.text)
            self.assertEqual(
                csv_preview.json()["rows"],
                [
                    {"row_key": "1", "script_text": "第一条口播"},
                    {"row_key": "2", "script_text": "第二条口播"},
                ],
            )

            four_column_preview = client.post(
                "/api/new/script-imports/preview?filename=scripts.csv",
                content=(
                    "任务ID,脚本内容,文章类型,分配账号\n"
                    "1,第一条口播,干货类,2\n"
                    "2,第二条口播,鸡汤文,5\n"
                ).encode("utf-8-sig"),
            )
            self.assertEqual(four_column_preview.status_code, 200, four_column_preview.text)
            self.assertEqual(
                four_column_preview.json()["rows"][1],
                {
                    "row_key": "2",
                    "script_text": "第二条口播",
                    "article_type": "鸡汤文",
                    "assigned_account": "5",
                },
            )

            duplicate = client.post(
                "/api/new/script-imports/preview?filename=bad.csv",
                content="任务ID,脚本内容\n1,甲\n1,乙\n".encode("utf-8"),
            )
            self.assertEqual(duplicate.status_code, 422)
            self.assertIn("重复", duplicate.json()["detail"])

            extra_column = client.post(
                "/api/new/script-imports/preview?filename=bad.csv",
                content="任务ID,脚本内容,备注\n1,甲,多余\n".encode("utf-8"),
            )
            self.assertEqual(extra_column.status_code, 422)
            self.assertIn("两列", extra_column.json()["detail"])
            self.assertEqual(client.get("/api/new/projects").json()["total"], 0)

    def test_four_column_metadata_backfill_preserves_current_scripts_and_generation_state(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            created = client.post(
                "/api/new/projects",
                json={
                    "name": "分类回填",
                    "items": [
                        {"row_key": "1", "script_text": "当前脚本一"},
                        {"row_key": "2", "script_text": "当前脚本二"},
                    ],
                },
            ).json()
            project_id = created["project_id"]
            database_path = self.settings.database_path or (
                self.settings.storage_root / "control.db"
            )
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE project_items SET status='VIDEO_ENHANCING' WHERE project_id=?",
                    (project_id,),
                )
                before = connection.execute(
                    """
                    SELECT row_key, script_text, status, current_audio_asset_id,
                           current_base_video_asset_id, current_video_asset_id,
                           subtitles_json, content_analysis_json, visual_analysis_json
                      FROM project_items WHERE project_id=? ORDER BY position
                    """,
                    (project_id,),
                ).fetchall()

            content = (
                "任务ID,脚本内容,文章类型,分配账号\n"
                "1,表格里的旧脚本一,干货类,2\n"
                "2,表格里的旧脚本二,鸡汤文,5\n"
            ).encode("utf-8-sig")
            response = client.put(
                f"/api/new/projects/{project_id}/metadata-import?filename=四列脚本.csv",
                content=content,
            )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["items"][0]["script_text"], "当前脚本一")
            self.assertEqual(payload["items"][0]["status"], "VIDEO_ENHANCING")
            self.assertEqual(
                payload["items"][0]["settings"]["source_metadata"],
                {"article_type": "干货类", "assigned_account": "2"},
            )
            self.assertEqual(payload["script_source"]["filename"], "四列脚本.csv")
            self.assertEqual(
                Path(payload["script_source"]["managed_path"]).read_bytes(), content
            )

            with sqlite3.connect(database_path) as connection:
                after = connection.execute(
                    """
                    SELECT row_key, script_text, status, current_audio_asset_id,
                           current_base_video_asset_id, current_video_asset_id,
                           subtitles_json, content_analysis_json, visual_analysis_json
                      FROM project_items WHERE project_id=? ORDER BY position
                    """,
                    (project_id,),
                ).fetchall()
                settings = [
                    json.loads(row[0])
                    for row in connection.execute(
                        "SELECT settings_json FROM project_items WHERE project_id=? ORDER BY position",
                        (project_id,),
                    ).fetchall()
                ]
            self.assertEqual(after, before)
            self.assertEqual(settings[1]["source_metadata"]["assigned_account"], "5")

    def test_original_script_file_is_retained_for_result_batch_archives(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            content = "任务ID,脚本内容\n1,第一条口播\n2,第二条口播\n".encode("utf-8-sig")
            preview = client.post(
                "/api/new/script-imports/preview?filename=原始脚本.csv", content=content
            ).json()
            project = client.post(
                "/api/new/projects",
                json={"name": "脚本归档", "items": preview["rows"]},
            ).json()

            saved = client.put(
                f"/api/new/projects/{project['project_id']}/script-source?filename=原始脚本.csv",
                content=content,
            )

            self.assertEqual(saved.status_code, 200, saved.text)
            source = saved.json()["script_source"]
            self.assertEqual(source["filename"], "原始脚本.csv")
            self.assertEqual(Path(source["managed_path"]).read_bytes(), content)
            self.assertEqual(len(saved.json()["script_source_history"]), 1)

    def test_image_pool_count_loop_manual_replace_and_refresh_are_persistent(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "图片映射测试",
                    "items": [
                        {"row_key": str(index), "script_text": f"脚本 {index}"}
                        for index in range(1, 8)
                    ],
                },
            ).json()
            project_id = project["project_id"]

            images = []
            for name in ("a.png", "b.png", "c.png"):
                response = client.post(
                    f"/api/new/projects/{project_id}/images?filename={name}",
                    content=PNG_1X1 + name.encode("ascii"),
                )
                self.assertEqual(response.status_code, 201, response.text)
                images.append(response.json())

            counted = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={"strategy": "count", "reuse_count": 2},
            )
            self.assertEqual(counted.status_code, 200, counted.text)
            self.assertEqual(
                [
                    item["inputs"]["image"]["external_ref"]["input_image_id"]
                    for item in counted.json()["items"]
                ],
                [
                    images[0]["image_id"],
                    images[0]["image_id"],
                    images[1]["image_id"],
                    images[1]["image_id"],
                    images[2]["image_id"],
                    images[2]["image_id"],
                    images[0]["image_id"],
                ],
            )

            looped = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={"strategy": "loop", "reuse_count": 9},
            )
            self.assertEqual(looped.status_code, 200, looped.text)
            self.assertEqual(
                [
                    item["inputs"]["image"]["external_ref"]["input_image_id"]
                    for item in looped.json()["items"]
                ],
                [images[index % 3]["image_id"] for index in range(7)],
            )

            first_item = looped.json()["items"][0]
            replaced = client.put(
                f"/api/new/projects/{project_id}/items/{first_item['item_id']}/image",
                json={"image_id": images[1]["image_id"]},
            )
            self.assertEqual(replaced.status_code, 200, replaced.text)
            first = replaced.json()["items"][0]
            self.assertEqual(first["inputs"]["image"]["version"], 2)
            self.assertEqual(len(first["asset_history"]["input_image"]), 2)

            refreshed = client.get(f"/api/new/projects/{project_id}").json()
            self.assertEqual(
                refreshed["items"][0]["inputs"]["image"]["external_ref"][
                    "input_image_id"
                ],
                images[1]["image_id"],
            )
            in_use = client.delete(
                f"/api/new/projects/{project_id}/images/{images[1]['image_id']}"
            )
            self.assertEqual(in_use.status_code, 200, in_use.text)
            after_in_use_delete = client.get(
                f"/api/new/projects/{project_id}"
            ).json()
            self.assertNotIn(
                images[1]["image_id"],
                [image["image_id"] for image in after_in_use_delete["input_images"]],
            )
            for item in after_in_use_delete["items"]:
                self.assertNotEqual(
                    item["inputs"]["image"]["external_ref"]["input_image_id"],
                    images[1]["image_id"],
                )

            remapped = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={"strategy": "count", "reuse_count": 100},
            )
            self.assertEqual(remapped.status_code, 200, remapped.text)
            self.assertTrue(
                all(
                    item["inputs"]["image"]["external_ref"]["input_image_id"]
                    == images[0]["image_id"]
                    for item in remapped.json()["items"]
                )
            )
            self.assertFalse(Path(images[1]["managed_path"]).exists())
            for item in remapped.json()["items"]:
                self.assertFalse(
                    any(
                        asset["external_ref"].get("input_image_id")
                        == images[1]["image_id"]
                        for asset in item["asset_history"].get("input_image", [])
                    )
                )

            unused = client.post(
                f"/api/new/projects/{project_id}/images?filename=unused.png",
                content=PNG_1X1,
            ).json()
            unused_path = Path(unused["managed_path"])
            self.assertTrue(unused_path.is_file())
            removed = client.delete(
                f"/api/new/projects/{project_id}/images/{unused['image_id']}"
            )
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertFalse(unused_path.exists())

    def test_active_row_does_not_block_other_row_image_replacement(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "逐行图片权限",
                    "items": [
                        {"row_key": "1", "script_text": "第一条"},
                        {"row_key": "2", "script_text": "第二条"},
                    ],
                },
            ).json()
            project_id = project["project_id"]
            first_item, second_item = project["items"]

            initial = client.post(
                f"/api/new/projects/{project_id}/images?filename=initial.png",
                content=PNG_1X1 + b"initial",
            ).json()
            mapped = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={"strategy": "loop", "reuse_count": 1},
            )
            self.assertEqual(mapped.status_code, 200, mapped.text)

            client.app.state.project_store.create_operation(
                owner_user_id="user-1",
                project_id=project_id,
                item_id=first_item["item_id"],
                operation_type="AUDIO_GENERATE",
                idempotency_key="active-row-image-isolation",
            )

            replacement = client.post(
                f"/api/new/projects/{project_id}/images?filename=replacement.png",
                content=PNG_1X1 + b"replacement",
            )
            self.assertEqual(replacement.status_code, 201, replacement.text)
            replacement_id = replacement.json()["image_id"]

            replaced = client.put(
                f"/api/new/projects/{project_id}/items/{second_item['item_id']}/image",
                json={"image_id": replacement_id},
            )
            self.assertEqual(replaced.status_code, 200, replaced.text)
            self.assertEqual(
                replaced.json()["items"][1]["inputs"]["image"]["external_ref"][
                    "input_image_id"
                ],
                replacement_id,
            )
            self.assertTrue(replaced.json()["allowed_actions"]["manage_input_images"])
            self.assertFalse(replaced.json()["allowed_actions"]["apply_image_mapping"])

            active_replace = client.put(
                f"/api/new/projects/{project_id}/items/{first_item['item_id']}/image",
                json={"image_id": replacement_id},
            )
            self.assertEqual(active_replace.status_code, 422, active_replace.text)

            active_delete = client.delete(
                f"/api/new/projects/{project_id}/images/{initial['image_id']}"
            )
            self.assertEqual(active_delete.status_code, 409, active_delete.text)
            self.assertIn("生成中", active_delete.json()["detail"])

            unused = client.post(
                f"/api/new/projects/{project_id}/images?filename=unused-active.png",
                content=PNG_1X1 + b"unused-active",
            )
            self.assertEqual(unused.status_code, 201, unused.text)
            removed = client.delete(
                f"/api/new/projects/{project_id}/images/{unused.json()['image_id']}"
            )
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertNotEqual(initial["image_id"], replacement_id)

    def test_locked_mapping_scope_receives_new_images_and_preserves_other_rows(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "分批换图锁定",
                    "items": [
                        {"row_key": str(index), "script_text": f"脚本 {index}"}
                        for index in range(1, 4)
                    ],
                },
            ).json()
            project_id = project["project_id"]
            old_image = client.post(
                f"/api/new/projects/{project_id}/images?filename=old.png",
                content=PNG_1X1 + b"old",
            ).json()
            mapped = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={"strategy": "loop", "reuse_count": 1},
            ).json()
            target_item_ids = [item["item_id"] for item in mapped["items"][1:]]

            scoped = client.put(
                f"/api/new/projects/{project_id}/image-mapping-scope",
                json={"item_ids": target_item_ids},
            )
            self.assertEqual(scoped.status_code, 200, scoped.text)
            self.assertFalse(
                scoped.json()["items"][0]["inputs"]["image_mapping_target"]
            )
            self.assertTrue(
                all(
                    item["inputs"]["image_mapping_target"]
                    for item in scoped.json()["items"][1:]
                )
            )

            new_images = []
            for name in ("new-a.png", "new-b.png"):
                response = client.post(
                    f"/api/new/projects/{project_id}/images?filename={name}",
                    content=PNG_1X1 + name.encode("ascii"),
                )
                self.assertEqual(response.status_code, 201, response.text)
                new_images.append(response.json())
            remapped = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={
                    "strategy": "loop",
                    "reuse_count": 1,
                    "image_ids": [image["image_id"] for image in new_images],
                },
            )
            self.assertEqual(remapped.status_code, 200, remapped.text)
            items = remapped.json()["items"]
            self.assertEqual(
                items[0]["inputs"]["image"]["external_ref"]["input_image_id"],
                old_image["image_id"],
            )
            self.assertEqual(
                [
                    item["inputs"]["image"]["external_ref"]["input_image_id"]
                    for item in items[1:]
                ],
                [image["image_id"] for image in new_images],
            )

            target_replace = client.put(
                f"/api/new/projects/{project_id}/items/{target_item_ids[0]}/image",
                json={"image_id": new_images[1]["image_id"]},
            )
            self.assertEqual(target_replace.status_code, 200, target_replace.text)
            blocked_delete = client.delete(
                f"/api/new/projects/{project_id}/images/{old_image['image_id']}"
            )
            self.assertEqual(blocked_delete.status_code, 409, blocked_delete.text)
            self.assertIn("换图范围外", blocked_delete.json()["detail"])

            refreshed = client.get(f"/api/new/projects/{project_id}").json()
            self.assertTrue(
                all(
                    item["inputs"]["image_mapping_target"]
                    for item in refreshed["items"][1:]
                )
            )
            cleared = client.put(
                f"/api/new/projects/{project_id}/image-mapping-scope",
                json={"item_ids": []},
            )
            self.assertEqual(cleared.status_code, 200, cleared.text)
            self.assertFalse(
                any(
                    item["inputs"]["image_mapping_target"]
                    for item in cleared.json()["items"]
                )
            )

    def test_reapplying_same_image_identity_preserves_existing_videos(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "重复映射保护",
                    "items": [{"row_key": "1", "script_text": "第一条"}],
                },
            ).json()
            project_id = project["project_id"]
            item_id = project["items"][0]["item_id"]
            image = client.post(
                f"/api/new/projects/{project_id}/images?filename=person.png",
                content=PNG_1X1 + b"same-identity",
            ).json()
            mapped = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={"strategy": "loop", "reuse_count": 1},
            ).json()
            first_image_asset_id = mapped["items"][0]["inputs"]["image"]["asset_id"]
            base_path = self.settings.storage_root / "base.mp4"
            base_path.write_bytes(b"base")
            base = client.app.state.project_store.add_asset(
                owner_user_id="user-1",
                project_id=project_id,
                item_id=item_id,
                asset_type="base_video",
                source_type="runninghub_single",
                status="READY",
                filename="1-base.mp4",
                managed_path=str(base_path),
                metadata={"input_image_asset_id": first_image_asset_id},
                make_current=True,
            )
            video_path = self.settings.storage_root / "composition.mp4"
            video_path.write_bytes(b"composition")
            video = client.app.state.project_store.add_asset(
                owner_user_id="user-1",
                project_id=project_id,
                item_id=item_id,
                asset_type="composition_video",
                source_type="jianying_export",
                status="READY",
                filename="1-composition.mp4",
                managed_path=str(video_path),
                metadata={"base_video_asset_id": base["asset_id"]},
                make_current=True,
            )

            remapped = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={
                    "strategy": "loop",
                    "reuse_count": 1,
                    "image_ids": [image["image_id"]],
                },
            )

            self.assertEqual(remapped.status_code, 200, remapped.text)
            item = remapped.json()["items"][0]
            self.assertEqual(item["status"], "COMPOSITION_READY")
            self.assertEqual(item["inputs"]["image"]["asset_id"], first_image_asset_id)
            self.assertEqual(item["outputs"]["base_video"]["asset_id"], base["asset_id"])
            self.assertEqual(
                item["outputs"]["composition_video"]["asset_id"], video["asset_id"]
            )

    def test_paid_snapshot_image_cannot_be_deleted(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "付费图片快照",
                    "items": [{"row_key": "1", "script_text": "第一条"}],
                },
            ).json()
            project_id = project["project_id"]
            image = client.post(
                f"/api/new/projects/{project_id}/images?filename=paid.png",
                content=PNG_1X1 + b"paid-snapshot",
            ).json()
            mapped = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={"strategy": "loop", "reuse_count": 1},
            ).json()
            item = mapped["items"][0]
            client.app.state.project_store.create_operation(
                owner_user_id="user-1",
                project_id=project_id,
                item_id=item["item_id"],
                operation_type="COMPOSITION_GENERATE",
                idempotency_key="paid-image-snapshot",
                payload={
                    "remote_item_id": "remote-paid-image",
                    # Historical installs may already have removed the exact
                    # per-row asset record. The immutable content hash must
                    # still protect the underlying project image.
                    "input_image_asset_id": "historical-missing-image-asset",
                    "input_image_sha256": item["inputs"]["image"]["metadata"]["sha256"],
                },
            )

            removed = client.delete(
                f"/api/new/projects/{project_id}/images/{image['image_id']}"
            )

            self.assertEqual(removed.status_code, 409, removed.text)
            self.assertIn("付费画面任务冻结", removed.json()["detail"])

    def test_active_row_outside_mapping_scope_does_not_block_target_rows(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "锁定运行行",
                    "items": [
                        {"row_key": "1", "script_text": "第一条"},
                        {"row_key": "2", "script_text": "第二条"},
                    ],
                },
            ).json()
            project_id = project["project_id"]
            old_image = client.post(
                f"/api/new/projects/{project_id}/images?filename=old.png",
                content=PNG_1X1 + b"old-active",
            ).json()
            mapped = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={"strategy": "loop", "reuse_count": 1},
            ).json()
            first_item_id = mapped["items"][0]["item_id"]
            second_item_id = mapped["items"][1]["item_id"]
            client.put(
                f"/api/new/projects/{project_id}/image-mapping-scope",
                json={"item_ids": [second_item_id]},
            )
            client.app.state.project_store.create_operation(
                owner_user_id="user-1",
                project_id=project_id,
                item_id=first_item_id,
                operation_type="AUDIO_GENERATE",
                idempotency_key="locked-active-row",
            )
            new_image = client.post(
                f"/api/new/projects/{project_id}/images?filename=new.png",
                content=PNG_1X1 + b"new-active",
            ).json()
            remapped = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={
                    "strategy": "loop",
                    "reuse_count": 1,
                    "image_ids": [new_image["image_id"]],
                },
            )
            self.assertEqual(remapped.status_code, 200, remapped.text)
            self.assertEqual(
                remapped.json()["settings"]["image_mapping"]["image_ids"],
                [new_image["image_id"]],
            )
            by_id = {item["item_id"]: item for item in remapped.json()["items"]}
            self.assertEqual(
                by_id[first_item_id]["inputs"]["image"]["external_ref"]["input_image_id"],
                old_image["image_id"],
            )
            self.assertEqual(
                by_id[second_item_id]["inputs"]["image"]["external_ref"]["input_image_id"],
                new_image["image_id"],
            )
            self.assertTrue(remapped.json()["allowed_actions"]["apply_image_mapping"])

    def test_append_item_remains_available_after_existing_voice_generation_starts(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "追加分段",
                    "items": [{"row_key": "1", "script_text": "已开始生成的脚本"}],
                },
            ).json()
            first_item = project["items"][0]
            client.app.state.project_store.create_operation(
                owner_user_id="user-1",
                project_id=project["project_id"],
                item_id=first_item["item_id"],
                operation_type="AUDIO_GENERATE",
                idempotency_key="append-after-audio-start",
            )

            appended = client.post(
                f"/api/new/projects/{project['project_id']}/items",
                json={"row_key": "2", "script_text": "新增测试脚本"},
            )

            self.assertEqual(appended.status_code, 201, appended.text)
            self.assertEqual(len(appended.json()["items"]), 2)
            self.assertEqual(appended.json()["items"][0]["status"], "AUDIO_QUEUED")
            self.assertEqual(appended.json()["items"][1]["status"], "DRAFT")
            self.assertEqual(appended.json()["items"][1]["script_text"], "新增测试脚本")

    def test_batch_append_is_atomic_and_preserves_existing_rows(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "批量追加",
                    "items": [{"row_key": "1", "script_text": "原测试脚本"}],
                },
            ).json()
            project_id = project["project_id"]

            appended = client.post(
                f"/api/new/projects/{project_id}/items/batch",
                json={
                    "items": [
                        {"row_key": "2", "script_text": "第二条脚本"},
                        {"row_key": "3", "script_text": "第三条脚本"},
                    ]
                },
            )
            self.assertEqual(appended.status_code, 201, appended.text)
            self.assertEqual(
                [item["row_key"] for item in appended.json()["items"]],
                ["1", "2", "3"],
            )
            self.assertEqual(
                appended.json()["items"][0]["script_text"], "原测试脚本"
            )

            rejected = client.post(
                f"/api/new/projects/{project_id}/items/batch",
                json={
                    "items": [
                        {"row_key": "4", "script_text": "本条也不应写入"},
                        {"row_key": "2", "script_text": "冲突任务ID"},
                    ]
                },
            )
            self.assertEqual(rejected.status_code, 422, rejected.text)
            current = client.get(f"/api/new/projects/{project_id}").json()
            self.assertEqual(
                [item["row_key"] for item in current["items"]],
                ["1", "2", "3"],
            )

    def test_each_selected_file_upload_creates_a_new_project_image(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "图片去重",
                    "items": [{"row_key": "1", "script_text": "测试"}],
                },
            ).json()
            project_id = project["project_id"]
            first = client.post(
                f"/api/new/projects/{project_id}/images?filename=同名.png",
                content=PNG_1X1 + b"first",
            )
            same_name = client.post(
                f"/api/new/projects/{project_id}/images?filename=同名.png",
                content=PNG_1X1 + b"changed",
            )
            same_content = client.post(
                f"/api/new/projects/{project_id}/images?filename=另一个名字.png",
                content=PNG_1X1 + b"first",
            )

            self.assertEqual(first.status_code, 201, first.text)
            self.assertEqual(same_name.status_code, 201, same_name.text)
            self.assertEqual(same_content.status_code, 201, same_content.text)
            self.assertFalse(same_name.json().get("deduplicated", False))
            self.assertFalse(same_content.json().get("deduplicated", False))
            self.assertNotEqual(first.json()["image_id"], same_name.json()["image_id"])
            self.assertNotEqual(first.json()["image_id"], same_content.json()["image_id"])
            refreshed = client.get(f"/api/new/projects/{project_id}").json()
            self.assertEqual(len(refreshed["input_images"]), 3)

    def test_script_reimport_is_atomic_and_project_can_be_cleared(self) -> None:
        login_patch, verify_patch = self._client_context()
        with login_patch, verify_patch, TestClient(create_app(self.settings)) as client:
            self._login(client)
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "重新导入",
                    "items": [
                        {"row_key": "1", "script_text": "原脚本一"},
                        {"row_key": "2", "script_text": "原脚本二"},
                    ],
                },
            ).json()
            project_id = project["project_id"]
            first_id = project["items"][0]["item_id"]

            replaced = client.put(
                f"/api/new/projects/{project_id}/inputs",
                json={
                    "items": [
                        {
                            "item_id": first_id,
                            "row_key": "10",
                            "script_text": "保留并修改",
                        },
                        {"row_key": "20", "script_text": "新脚本"},
                    ]
                },
            )
            self.assertEqual(replaced.status_code, 200, replaced.text)
            self.assertEqual(
                [item["row_key"] for item in replaced.json()["items"]], ["10", "20"]
            )
            self.assertEqual(replaced.json()["items"][0]["item_id"], first_id)

            invalid = client.put(
                f"/api/new/projects/{project_id}/inputs",
                json={
                    "items": [
                        {"row_key": "x", "script_text": "甲"},
                        {"row_key": "x", "script_text": "乙"},
                    ]
                },
            )
            self.assertEqual(invalid.status_code, 422)
            unchanged = client.get(f"/api/new/projects/{project_id}").json()
            self.assertEqual(
                [item["row_key"] for item in unchanged["items"]], ["10", "20"]
            )

            deleted = client.delete(f"/api/new/projects/{project_id}")
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual(client.get("/api/new/projects").json()["total"], 0)


if __name__ == "__main__":
    unittest.main()
