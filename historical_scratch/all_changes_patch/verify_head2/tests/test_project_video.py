from __future__ import annotations

from io import BytesIO
import json
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

from jyd_probe.project_store import ProjectStore  # noqa: E402
from jyd_probe.web_api import WebApiSettings, create_app  # noqa: E402


class ProjectVideoApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            PROJECT_ROOT / "runtime" / "test_tmp" / f"project_video_{uuid.uuid4().hex}"
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
            return_value={"access_token": "token", "user": user},
        ):
            response = client.post(
                "/api/auth/login",
                json={"username": user["username"], "password": "pass123"},
            )
        if response.status_code != 200:
            raise AssertionError(response.text)

    def _seed_current_video(
        self, store: ProjectStore, user: dict[str, object], project: dict, item: dict
    ) -> None:
        store.set_item_subtitles(
            str(user["user_id"]),
            project["project_id"],
            item["item_id"],
            {
                "source": "minimax_timestamps",
                "raw_cues": [{"start_us": 0, "end_us": 1_000_000, "text": "测试"}],
                "render_cues": [{"start_us": 0, "end_us": 1_000_000, "text": "测试"}],
                "bound_audio_asset_id": "audio-1",
                "bound_video_asset_id": None,
                "style": {},
                "status": "PREVIEW_READY",
                "overflow_risk": False,
            },
        )
        path = self.settings.storage_root / f"{item['row_key']}-composition.mp4"
        path.write_bytes(b"old-composition")
        store.add_asset(
            owner_user_id=str(user["user_id"]),
            project_id=project["project_id"],
            item_id=item["item_id"],
            asset_type="composition_video",
            source_type="jianying_postprocess",
            status="READY",
            filename=path.name,
            managed_path=str(path),
            make_current=True,
        )

    def test_original_material_download_and_uploaded_video_version(self) -> None:
        user = {"user_id": "video-user", "username": "tester", "enabled": True}
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id=str(user["user_id"]),
            owner_username=str(user["username"]),
            name="模块 5",
            items=[
                {"row_key": "1", "script_text": "多片段"},
                {"row_key": "2", "script_text": "单片段"},
                {"row_key": "3", "script_text": "尚未生成"},
            ],
        )
        for item in project["items"][:2]:
            self._seed_current_video(store, user, project, item)

        for sequence in (1, 2):
            path = self.settings.storage_root / f"multi-{sequence}.mp4"
            path.write_bytes(f"multi-{sequence}".encode("ascii"))
            store.add_asset(
                owner_user_id=str(user["user_id"]),
                project_id=project["project_id"],
                item_id=project["items"][0]["item_id"],
                asset_type="original_video_segment",
                source_type="runninghub",
                status="READY",
                filename=f"1-segment-{sequence:03d}.mp4",
                managed_path=str(path),
                external_ref={"video_index": sequence},
                metadata={"start_seconds": sequence - 1, "end_seconds": sequence},
            )
        single_path = self.settings.storage_root / "single.mp4"
        single_path.write_bytes(b"single-segment")
        store.add_asset(
            owner_user_id=str(user["user_id"]),
            project_id=project["project_id"],
            item_id=project["items"][1]["item_id"],
            asset_type="original_video_segment",
            source_type="runninghub",
            status="READY",
            filename="2-segment-001.mp4",
            managed_path=str(single_path),
            external_ref={"video_index": 1},
        )

        def verify(_client, _token):
            return user

        with patch("jyd_probe.auth_center.AuthCenterClient.verify", new=verify):
            with TestClient(create_app(self.settings)) as client:
                self._login(client, user)
                first_id = project["items"][0]["item_id"]
                second_id = project["items"][1]["item_id"]
                multi = client.get(
                    f"/api/new/projects/{project['project_id']}/items/{first_id}/original-materials"
                )
                self.assertEqual(multi.status_code, 200, multi.text)
                self.assertEqual(multi.headers["content-type"], "application/zip")
                with zipfile.ZipFile(BytesIO(multi.content)) as archive:
                    self.assertEqual(
                        archive.namelist(),
                        [
                            "1-segment-001.mp4",
                            "1-segment-002.mp4",
                            "片段顺序清单.json",
                        ],
                    )
                    manifest = json.loads(archive.read("片段顺序清单.json"))
                    self.assertEqual([entry["video_index"] for entry in manifest], [1, 2])

                single = client.get(
                    f"/api/new/projects/{project['project_id']}/items/{second_id}/original-materials"
                )
                self.assertEqual(single.status_code, 200, single.text)
                self.assertEqual(single.content, b"single-segment")
                self.assertEqual(single.headers["content-type"], "video/mp4")

                uploaded = client.post(
                    f"/api/new/projects/{project['project_id']}/items/{first_id}/current-video?filename=人工粗剪.webm",
                    content=b"new-uploaded-video",
                    headers={"content-type": "video/webm"},
                )
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                updated_item = next(
                    item for item in uploaded.json()["items"] if item["item_id"] == first_id
                )
                self.assertEqual(updated_item["outputs"]["composition_video"]["filename"], "人工粗剪.webm")
                self.assertEqual(updated_item["outputs"]["composition_video"]["source_type"], "user_upload")
                self.assertEqual(updated_item["subtitles"]["status"], "INVALIDATED")
                self.assertIsNone(updated_item["subtitles"]["bound_video_asset_id"])
                self.assertEqual(len(updated_item["asset_history"]["composition_video"]), 2)
                self.assertEqual(len(updated_item["outputs"]["original_video_segments"]), 2)

                current = client.get(
                    f"/api/new/projects/{project['project_id']}/items/{first_id}/current-video"
                )
                self.assertEqual(current.status_code, 200, current.text)
                self.assertEqual(current.headers["content-type"], "video/webm")
                self.assertEqual(current.content, b"new-uploaded-video")

                bundle = client.get(
                    f"/api/new/projects/{project['project_id']}/videos/download"
                )
                self.assertEqual(bundle.status_code, 200, bundle.text)
                self.assertEqual(bundle.headers["content-type"], "application/zip")
                with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
                    self.assertEqual(
                        archive.namelist(),
                        ["人工粗剪.webm", "2-composition.mp4"],
                    )
                    self.assertEqual(
                        archive.read("人工粗剪.webm"), b"new-uploaded-video"
                    )
                    self.assertEqual(
                        archive.read("2-composition.mp4"), b"old-composition"
                    )

                selected_bundle = client.get(
                    f"/api/new/projects/{project['project_id']}/videos/download",
                    params={"item_ids": second_id},
                )
                self.assertEqual(selected_bundle.status_code, 200, selected_bundle.text)
                with zipfile.ZipFile(BytesIO(selected_bundle.content)) as archive:
                    self.assertEqual(archive.namelist(), ["2-composition.mp4"])

                invalid_selection = client.get(
                    f"/api/new/projects/{project['project_id']}/videos/download",
                    params={"item_ids": "not-this-project"},
                )
                self.assertEqual(invalid_selection.status_code, 422)


if __name__ == "__main__":
    unittest.main()
