from __future__ import annotations

import base64
import hashlib
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
from jyd_probe.project_audio import ProjectAudioCoordinator, _current_audio_links  # noqa: E402
from jyd_probe.project_store import ProjectStore  # noqa: E402


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ProjectAudioApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = PROJECT_ROOT / "runtime" / "test_tmp" / f"project_audio_{uuid.uuid4().hex}"
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

    def test_only_latest_remote_audio_link_can_update_each_item(self) -> None:
        links = [
            {
                "system": "runninghub",
                "relation": "digital_human_audio_batch",
                "external_id": "old-batch",
            },
            {
                "system": "runninghub",
                "relation": "digital_human_audio_item",
                "external_id": "old-item",
                "item_id": "local-item",
                "metadata": {"batch_id": "old-batch"},
            },
            {
                "system": "runninghub",
                "relation": "digital_human_audio_batch",
                "external_id": "new-batch",
            },
            {
                "system": "runninghub",
                "relation": "digital_human_audio_item",
                "external_id": "new-item",
                "item_id": "local-item",
                "metadata": {"batch_id": "new-batch"},
            },
        ]

        batches, items = _current_audio_links(links)

        self.assertEqual([link["external_id"] for link in batches], ["new-batch"])
        self.assertEqual(list(items), ["new-item"])

    def test_terminal_audio_items_are_not_polled_again(self) -> None:
        links = [
            {
                "system": "runninghub",
                "relation": "digital_human_audio_batch",
                "external_id": "failed-batch",
            },
            {
                "system": "runninghub",
                "relation": "digital_human_audio_item",
                "external_id": "failed-item",
                "item_id": "local-failed",
                "metadata": {"batch_id": "failed-batch"},
            },
        ]

        batches, items = _current_audio_links(links, active_item_ids=set())

        self.assertEqual(batches, [])
        self.assertEqual(items, {})

    def test_existing_audio_can_recover_captions_without_redownload(self) -> None:
        store = ProjectStore(self.root / "caption-recovery.db")
        project = store.create_project(
            owner_user_id="recovery-user",
            owner_username="tester",
            name="字幕恢复测试",
            items=[{"row_key": "1", "script_text": "保留旧音频并恢复字幕。"}],
        )
        project_id = project["project_id"]
        item_id = project["items"][0]["item_id"]
        store.add_link(
            owner_user_id="recovery-user",
            project_id=project_id,
            system="runninghub",
            relation="digital_human_audio_batch",
            external_id="recovery-batch",
        )
        store.add_link(
            owner_user_id="recovery-user",
            project_id=project_id,
            item_id=item_id,
            system="runninghub",
            relation="digital_human_audio_item",
            external_id="recovery-remote-item",
            metadata={"batch_id": "recovery-batch"},
        )
        audio_path = self.root / "existing-v1.mp3"
        audio_path.write_bytes(b"ID3-existing-audio")
        asset = store.add_asset(
            owner_user_id="recovery-user",
            project_id=project_id,
            item_id=item_id,
            asset_type="audio",
            source_type="minimax",
            status="READY",
            filename="1.mp3",
            managed_path=str(audio_path),
            external_ref={
                "batch_id": "recovery-batch",
                "remote_item_id": "recovery-remote-item",
                "generation_version": 1,
            },
            make_current=True,
        )

        class RecoveryClient:
            def get_workbench_audio_batch(self, _token, _batch_id):
                return {
                    "batch_id": "recovery-batch",
                    "items": [
                        {
                            "item_id": "recovery-remote-item",
                            "status": "AWAITING_REVIEW",
                            "generation_version": 1,
                            "audio_ready": True,
                            "captions": {
                                "cues": [
                                    {
                                        "start_us": 0,
                                        "end_us": 1_500_000,
                                        "duration_us": 1_500_000,
                                        "text": "保留旧音频并恢复字幕。",
                                    }
                                ]
                            },
                        }
                    ],
                }

            def download_workbench_audio(self, *_args, **_kwargs):
                raise AssertionError("已有音频不应被重复下载")

        coordinator = ProjectAudioCoordinator(
            store,
            RecoveryClient(),
            storage_root=self.root / "storage",
            max_audio_bytes=1024 * 1024,
            visual_catalog=object(),
        )

        with patch(
            "jyd_probe.project_audio.refresh_saved_visual_item"
        ) as refresh_visual:
            recovered = coordinator.sync("recovery-user", project_id, "token")

        row = recovered["items"][0]
        self.assertEqual(row["outputs"]["audio"]["asset_id"], asset["asset_id"])
        self.assertEqual(row["subtitles"]["bound_audio_asset_id"], asset["asset_id"])
        self.assertEqual(row["subtitles"]["status"], "READY")
        self.assertEqual(row["subtitles"]["raw_cues"][0]["text"], "保留旧音频并恢复字幕。")
        refresh_visual.assert_called_once()
        self.assertEqual(
            refresh_visual.call_args.kwargs["item"]["subtitles"]["raw_cues"][0][
                "text"
            ],
            "保留旧音频并恢复字幕。",
        )

    def test_pending_audio_operation_resumes_after_application_restart(self) -> None:
        user = {"user_id": "restart-user", "username": "tester", "enabled": True}
        voices = [
            {
                "voice_asset_id": "official-voice-1",
                "provider_voice_id": "Chinese (Mandarin)_Reliable_Executive",
                "name": "沉稳男声",
                "source": "official",
                "method": "system",
                "selectable": True,
            }
        ]
        remote = {
            "batch_id": "restart-batch-1",
            "items": [
                {
                    "item_id": "restart-item-1",
                    "row_key": "1",
                    "status": "PENDING",
                    "generation_version": 1,
                    "audio_ready": False,
                    "captions": None,
                }
            ],
        }
        create_calls: list[dict] = []

        def fake_login(_self, _username, _password):
            return {"access_token": "restart-token", "user": user}

        def fake_verify(_self, _token):
            return user

        def fake_create(_self, _token, payload):
            create_calls.append(payload)
            return dict(remote)

        def fake_status(_self, _token, _batch_id):
            return dict(remote)

        def fake_download(_self, _token, _batch_id, _item_id, target, *, max_bytes):
            del max_bytes
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"ID3-restarted-audio")
            return len(b"ID3-restarted-audio")

        patches = [
            patch("jyd_probe.auth_center.AuthCenterClient.login", new=fake_login),
            patch("jyd_probe.auth_center.AuthCenterClient.verify", new=fake_verify),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.list_workbench_voices",
                return_value={"voices": voices, "creation_tasks": []},
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.create_workbench_audio_batch",
                new=fake_create,
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.get_workbench_audio_batch",
                new=fake_status,
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.download_workbench_audio",
                new=fake_download,
            ),
        ]
        for active in patches:
            active.start()
            self.addCleanup(active.stop)

        with TestClient(create_app(self.settings)) as first_client:
            first_client.post(
                "/api/auth/login",
                json={"username": "tester", "password": "pass123"},
            )
            project = first_client.post(
                "/api/new/projects",
                json={
                    "name": "重启恢复测试",
                    "items": [{"row_key": "1", "script_text": "进程重启后继续。"}],
                },
            ).json()
            project_id = project["project_id"]
            image = first_client.post(
                f"/api/new/projects/{project_id}/images?filename=person.png",
                content=PNG_1X1,
                headers={"Content-Type": "application/octet-stream"},
            )
            self.assertEqual(image.status_code, 201, image.text)
            mapped = first_client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={"strategy": "loop", "reuse_count": 1},
            )
            self.assertEqual(mapped.status_code, 200, mapped.text)
            started = first_client.post(
                f"/api/new/projects/{project_id}/audio/generate",
                json={
                    "default_voice_asset_id": "official-voice-1",
                    "voice_assignments": {},
                    "voice_settings": {"model": "speech-2.8-hd", "speed": 0.9},
                    "resolution": "2048",
                    "idempotency_key": "restart-audio-request-1",
                    "cost_confirmed": True,
                },
            )
            self.assertEqual(started.status_code, 200, started.text)
            self.assertEqual(started.json()["items"][0]["status"], "AUDIO_QUEUED")
            self.assertEqual(create_calls[0]["resolution"], "2048")

        remote["items"] = [
            {
                "item_id": "restart-item-1",
                "row_key": "1",
                "status": "AWAITING_REVIEW",
                "generation_version": 1,
                "audio_ready": True,
                "captions": {
                    "source": "minimax_timestamps",
                    "cues": [
                        {
                            "start_us": 0,
                            "end_us": 1_400_000,
                            "duration_us": 1_400_000,
                            "text": "进程重启后继续。",
                        }
                    ],
                },
            }
        ]

        with TestClient(create_app(self.settings)) as restarted_client:
            restarted_client.post(
                "/api/auth/login",
                json={"username": "tester", "password": "pass123"},
            )
            resumed = restarted_client.get(
                f"/api/new/projects/{project_id}/audio/status"
            )
            self.assertEqual(resumed.status_code, 200, resumed.text)
            row = resumed.json()["items"][0]
            self.assertEqual(row["status"], "AUDIO_READY")
            self.assertEqual(row["outputs"]["audio"]["filename"], "1_0.9倍速.mp3")
            self.assertEqual(row["outputs"]["audio"]["metadata"]["speed"], 0.9)
            self.assertEqual(row["subtitles"]["source"], "minimax_timestamps")
            self.assertEqual(len(create_calls), 1)

    def test_saved_voice_can_be_applied_to_every_project_item(self) -> None:
        user = {"user_id": "voice-user", "username": "tester", "enabled": True}
        voices = [
            {
                "voice_asset_id": "saved-voice-1",
                "provider_voice_id": "custom-provider-1",
                "name": "已保存克隆音色",
                "source": "custom",
                "method": "clone",
            },
            {
                "voice_asset_id": "saved-voice-2",
                "provider_voice_id": "custom-provider-2",
                "name": "第二个已保存音色",
                "source": "custom",
                "method": "clone",
            },
        ]

        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "voice-token", "user": user},
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.verify", return_value=user
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.list_workbench_voices",
            return_value={"voices": voices, "creation_tasks": []},
        ):
            with TestClient(create_app(self.settings)) as client:
                client.post(
                    "/api/auth/login",
                    json={"username": "tester", "password": "pass123"},
                )
                project = client.post(
                    "/api/new/projects",
                    json={
                        "name": "批量音色接口测试",
                        "items": [
                            {"row_key": "1", "script_text": "第一条。"},
                            {"row_key": "2", "script_text": "第二条。"},
                        ],
                    },
                ).json()

                selected = client.put(
                    f"/api/new/projects/{project['project_id']}/voice",
                    json={"voice_asset_id": "saved-voice-1"},
                )
                self.assertEqual(selected.status_code, 200, selected.text)
                payload = selected.json()
                self.assertEqual(
                    payload["preferences"]["default_voice_asset_id"],
                    "saved-voice-1",
                )
                self.assertTrue(
                    all(
                        item["settings"]["voice_asset_id"] == "saved-voice-1"
                        for item in payload["project"]["items"]
                    )
                )

                first_item_id = payload["project"]["items"][0]["item_id"]
                second_item_id = payload["project"]["items"][1]["item_id"]
                scoped = client.put(
                    f"/api/new/projects/{project['project_id']}/voice",
                    json={
                        "voice_asset_id": "saved-voice-2",
                        "item_ids": [second_item_id],
                    },
                )
                self.assertEqual(scoped.status_code, 200, scoped.text)
                scoped_items = scoped.json()["project"]["items"]
                self.assertEqual(scoped_items[0]["item_id"], first_item_id)
                self.assertEqual(
                    scoped_items[0]["settings"]["voice_asset_id"],
                    "saved-voice-1",
                )
                self.assertEqual(
                    scoped_items[1]["settings"]["voice_asset_id"],
                    "saved-voice-2",
                )

                invalid_scope = client.put(
                    f"/api/new/projects/{project['project_id']}/voice",
                    json={"voice_asset_id": "saved-voice-1", "item_ids": "bad"},
                )
                self.assertEqual(invalid_scope.status_code, 422)

                speed = client.put(
                    "/api/new/voices/default",
                    json={
                        "voice_asset_id": "saved-voice-1",
                        "voice_settings": {
                            "model": "speech-2.8-hd",
                            "speed": 0.9,
                            "volume": 1,
                            "pitch": 0,
                            "language_boost": "Chinese",
                            "output_format": "mp3",
                        },
                    },
                )
                self.assertEqual(speed.status_code, 200, speed.text)
                self.assertEqual(
                    speed.json()["preferences"]["voice_settings"]["speed"],
                    0.9,
                )

                invalid = client.put(
                    f"/api/new/projects/{project['project_id']}/voice",
                    json={"voice_asset_id": "other-account-voice"},
                )
                self.assertEqual(invalid.status_code, 422)

    def test_existing_voice_id_is_proxied_to_current_digital_human_account(self) -> None:
        user = {"user_id": "voice-import-user", "username": "tester", "enabled": True}
        imported_voice = {
            "voice_asset_id": "imported-voice-asset",
            "provider_voice_id": "ImportedCloneVoice01",
            "name": "抽卡音色",
            "source": "custom",
            "method": "clone",
            "selectable": False,
            "activation_required": True,
        }
        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "voice-token", "user": user},
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.verify", return_value=user
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.import_workbench_voice",
            return_value=imported_voice,
        ) as imported:
            with TestClient(create_app(self.settings)) as client:
                client.post(
                    "/api/auth/login",
                    json={"username": "tester", "password": "pass123"},
                )
                response = client.post(
                    "/api/new/voices/import",
                    json={
                        "voice_id": "ImportedCloneVoice01",
                        "name": "抽卡音色",
                        "already_activated": True,
                    },
                )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(response.json()["selectable"])
        imported.assert_called_once_with(
            "voice-token",
            voice_id="ImportedCloneVoice01",
            name="抽卡音色",
            already_activated=True,
        )

    def test_unactivated_voice_cannot_be_selected_and_used_voice_cannot_be_deleted(self) -> None:
        user = {"user_id": "voice-user", "username": "tester", "enabled": True}
        unactivated = {
            "voice_asset_id": "saved-voice-1",
            "provider_voice_id": "custom-provider-1",
            "name": "待激活音色",
            "source": "custom",
            "method": "clone",
            "selectable": False,
        }

        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "voice-token", "user": user},
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.verify", return_value=user
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.list_workbench_voices",
            return_value={"voices": [unactivated], "creation_tasks": []},
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.activate_workbench_voice",
            return_value={**unactivated, "selectable": True, "activated": True},
        ) as activate, patch(
            "jyd_probe.auth_center.AuthCenterClient.delete_workbench_voice",
            return_value={"deleted": True, "voice_asset_id": "saved-voice-1"},
        ) as delete:
            with TestClient(create_app(self.settings)) as client:
                client.post(
                    "/api/auth/login",
                    json={"username": "tester", "password": "pass123"},
                )
                project = client.post(
                    "/api/new/projects",
                    json={
                        "name": "激活与删除测试",
                        "items": [{"row_key": "1", "script_text": "第一条。"}],
                    },
                ).json()

                denied = client.put(
                    f"/api/new/projects/{project['project_id']}/voice",
                    json={"voice_asset_id": "saved-voice-1"},
                )
                self.assertEqual(denied.status_code, 422)

                activated = client.post(
                    "/api/new/voices/saved-voice-1/activate",
                    json={"cost_confirmed": True},
                )
                self.assertEqual(activated.status_code, 200, activated.text)
                activate.assert_called_once()

                unactivated["selectable"] = True
                selected = client.put(
                    f"/api/new/projects/{project['project_id']}/voice",
                    json={"voice_asset_id": "saved-voice-1"},
                )
                self.assertEqual(selected.status_code, 200, selected.text)
                blocked = client.delete("/api/new/voices/saved-voice-1")
                self.assertEqual(blocked.status_code, 409)
                delete.assert_not_called()

    def test_project_audio_uses_remote_batch_and_keeps_precise_captions(self) -> None:
        user = {"user_id": "voice-user", "username": "tester", "enabled": True}
        voices = [
            {
                "voice_asset_id": "official-voice-1",
                "provider_voice_id": "Chinese (Mandarin)_Reliable_Executive",
                "name": "沉稳男声",
                "description": "专业内容",
                "source": "official",
                "method": "system",
                "preview_available": False,
            }
        ]
        remote_status = {
            "value": {
                "batch_id": "remote-batch-1",
                "items": [
                    {
                        "item_id": "remote-item-1",
                        "row_key": "1",
                        "status": "PENDING",
                        "generation_version": 1,
                        "audio_ready": False,
                        "captions": None,
                    }
                ],
            }
        }
        created_payloads: list[dict] = []

        def fake_login(_self, _username, _password):
            return {"access_token": "voice-token", "user": user}

        def fake_verify(_self, _token):
            return user

        def fake_upload(_self, _token, _path, *, kind, filename):
            raise AssertionError(
                f"声音生成不应上传 {kind} 素材：{filename}"
            )

        def fake_create(_self, _token, payload):
            created_payloads.append(payload)
            self.assertEqual(len(payload["correlation_id"]), 32)
            self.assertTrue(payload["speech_options"]["costConfirmed"])
            self.assertEqual(payload["speech_options"]["voiceAssetId"], "official-voice-1")
            self.assertNotIn("asset_ids", payload)
            self.assertLessEqual(len(payload["request_key"]), 64)
            self.assertNotIn("image_asset_id", payload["rows"][0])
            self.assertNotIn("image_file", payload["rows"][0])
            self.assertNotIn("prompt", payload["rows"][0])
            return remote_status["value"]

        def fake_status(_self, _token, _batch_id):
            return remote_status["value"]

        def fake_download(_self, _token, _batch_id, _item_id, target, *, max_bytes):
            del max_bytes
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"ID3-real-audio")
            return len(b"ID3-real-audio")

        patches = [
            patch("jyd_probe.auth_center.AuthCenterClient.login", new=fake_login),
            patch("jyd_probe.auth_center.AuthCenterClient.verify", new=fake_verify),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.list_workbench_voices",
                return_value={"voices": voices, "creation_tasks": []},
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.upload_workbench_batch_asset",
                new=fake_upload,
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.create_workbench_audio_batch",
                new=fake_create,
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.get_workbench_audio_batch",
                new=fake_status,
            ),
            patch(
                "jyd_probe.auth_center.AuthCenterClient.download_workbench_audio",
                new=fake_download,
            ),
        ]
        for active in patches:
            active.start()
            self.addCleanup(active.stop)

        with TestClient(create_app(self.settings)) as client:
            login = client.post(
                "/api/auth/login", json={"username": "tester", "password": "pass123"}
            )
            self.assertEqual(login.status_code, 200)
            library = client.get("/api/new/voices")
            self.assertEqual(library.status_code, 200)
            self.assertEqual(
                library.json()["preferences"]["default_voice_asset_id"],
                "official-voice-1",
            )
            project = client.post(
                "/api/new/projects",
                json={
                    "name": "声音模块测试",
                    "items": [{"row_key": "1", "script_text": "第一条真实声音。"}],
                },
            ).json()
            project_id = project["project_id"]
            image = client.post(
                f"/api/new/projects/{project_id}/images?filename=person.png",
                content=PNG_1X1,
                headers={"Content-Type": "application/octet-stream"},
            )
            self.assertEqual(image.status_code, 201, image.text)
            mapped = client.put(
                f"/api/new/projects/{project_id}/image-mapping",
                json={"strategy": "loop", "reuse_count": 1},
            )
            self.assertEqual(mapped.status_code, 200)

            started = client.post(
                f"/api/new/projects/{project_id}/audio/generate",
                json={
                    "default_voice_asset_id": "official-voice-1",
                    "voice_assignments": {},
                    "voice_settings": {"model": "speech-2.8-hd", "speed": 0.9},
                    "idempotency_key": (
                        "audio-3af55f2822e3478bbf3f15905af89f36-"
                        "1722770000000"
                    ),
                    "cost_confirmed": True,
                },
            )
            self.assertEqual(started.status_code, 200, started.text)
            self.assertEqual(started.json()["items"][0]["status"], "AUDIO_QUEUED")
            self.assertIn(
                "digital_human_audio_batch",
                {link["relation"] for link in started.json()["links"]},
            )
            operation = next(
                value
                for value in started.json()["operations"]
                if value["operation_type"] == "AUDIO_GENERATE"
            )
            audio_link = next(
                value
                for value in started.json()["links"]
                if value["relation"] == "digital_human_audio_item"
            )
            self.assertEqual(
                operation["correlation_id"], created_payloads[0]["correlation_id"]
            )
            self.assertEqual(
                audio_link["metadata"]["correlation_id"],
                created_payloads[0]["correlation_id"],
            )

            remote_status["value"] = {
                "batch_id": "remote-batch-1",
                "items": [
                    {
                        "item_id": "remote-item-1",
                        "row_key": "1",
                        "status": "AWAITING_REVIEW",
                        "generation_version": 1,
                        "audio_ready": True,
                        "captions": {
                            "source": "minimax_timestamps",
                            "cues": [
                                {
                                    "start_us": 0,
                                    "end_us": 1_200_000,
                                    "duration_us": 1_200_000,
                                    "text": "第一条。",
                                }
                            ],
                        },
                    }
                ],
            }
            synced = client.get(f"/api/new/projects/{project_id}/audio/status")
            self.assertEqual(synced.status_code, 200, synced.text)
            row = synced.json()["items"][0]
            self.assertEqual(row["status"], "AUDIO_READY")
            self.assertEqual(row["outputs"]["audio"]["version"], 1)
            self.assertEqual(row["subtitles"]["source"], "minimax_timestamps")
            self.assertEqual(row["subtitles"]["raw_cues"][0]["end_us"], 1_200_000)
            self.assertEqual(
                row["outputs"]["audio"]["metadata"]["script_sha256"],
                hashlib.sha256("第一条真实声音。".encode("utf-8")).hexdigest(),
            )
            self.assertTrue(row["allowed_actions"]["replace_image"])
            reused = client.post(
                f"/api/new/projects/{project_id}/audio/generate",
                json={
                    "default_voice_asset_id": "official-voice-1",
                    "voice_assignments": {row["item_id"]: "official-voice-1"},
                    "voice_settings": {"model": "speech-2.8-hd", "speed": 1},
                    "item_ids": [row["item_id"]],
                    "idempotency_key": "audio-single-reuse-1",
                    "cost_confirmed": True,
                },
            )
            self.assertEqual(reused.status_code, 200, reused.text)
            self.assertEqual(
                reused.json()["items"][0]["outputs"]["audio"]["asset_id"],
                row["outputs"]["audio"]["asset_id"],
            )
            self.assertEqual(len(created_payloads), 1)
            replacement = client.post(
                f"/api/new/projects/{project_id}/images?filename=after-audio.png",
                content=PNG_1X1,
                headers={"Content-Type": "application/octet-stream"},
            )
            self.assertEqual(replacement.status_code, 201, replacement.text)
            replaced = client.put(
                f"/api/new/projects/{project_id}/items/{row['item_id']}/image",
                json={"image_id": replacement.json()["image_id"]},
            )
            self.assertEqual(replaced.status_code, 200, replaced.text)
            replaced_row = replaced.json()["items"][0]
            self.assertEqual(replaced_row["status"], "AUDIO_READY")
            self.assertEqual(
                replaced_row["inputs"]["image"]["external_ref"]["input_image_id"],
                replacement.json()["image_id"],
            )
            self.assertEqual(replaced_row["outputs"]["audio"]["asset_id"], row["outputs"]["audio"]["asset_id"])
            audio = client.get(
                f"/api/new/projects/{project_id}/items/{row['item_id']}/audio"
            )
            self.assertEqual(audio.status_code, 200)
            self.assertEqual(audio.content, b"ID3-real-audio")
            self.assertIn("1_0.9", audio.headers["content-disposition"])
            appended = client.post(
                f"/api/new/projects/{project_id}/items",
                json={"row_key": "2", "script_text": "尚未生成声音"},
            )
            self.assertEqual(appended.status_code, 201, appended.text)
            audio_bundle = client.get(
                f"/api/new/projects/{project_id}/audios/download"
            )
            self.assertEqual(audio_bundle.status_code, 200, audio_bundle.text)
            self.assertEqual(audio_bundle.headers["content-type"], "application/zip")
            with zipfile.ZipFile(BytesIO(audio_bundle.content)) as archive:
                self.assertEqual(archive.namelist(), ["1_0.9倍速.mp3"])
                self.assertEqual(archive.read("1_0.9倍速.mp3"), b"ID3-real-audio")
            removed = client.delete(
                f"/api/new/projects/{project_id}/items/{appended.json()['items'][-1]['item_id']}"
            )
            self.assertEqual(removed.status_code, 200, removed.text)

            remote_status["value"] = {
                "batch_id": "remote-batch-2",
                "items": [
                    {
                        "item_id": "remote-item-2",
                        "row_key": "1",
                        "status": "PENDING",
                        "generation_version": 1,
                        "audio_ready": False,
                        "captions": None,
                    }
                ],
            }
            regenerated = client.post(
                f"/api/new/projects/{project_id}/audio/generate",
                json={
                    "default_voice_asset_id": "official-voice-1",
                    "voice_assignments": {},
                    "voice_settings": {"model": "speech-2.8-hd", "speed": 1},
                    "idempotency_key": "audio-new-version-2",
                    "cost_confirmed": True,
                },
            )
            self.assertEqual(regenerated.status_code, 200, regenerated.text)
            regenerating_row = regenerated.json()["items"][0]
            self.assertEqual(regenerating_row["status"], "AUDIO_QUEUED")
            self.assertIsNone(regenerating_row["outputs"]["audio"])
            self.assertEqual(len(regenerating_row["asset_history"]["audio"]), 1)
            self.assertEqual(len(created_payloads), 2)

            remote_status["value"] = {
                "batch_id": "remote-batch-2",
                "items": [
                    {
                        "item_id": "remote-item-2",
                        "row_key": "1",
                        "status": "AWAITING_REVIEW",
                        "generation_version": 1,
                        "audio_ready": True,
                        "captions": {
                            "source": "minimax_timestamps",
                            "cues": [
                                {
                                    "start_us": 0,
                                    "end_us": 1_300_000,
                                    "duration_us": 1_300_000,
                                    "text": "第二版。",
                                }
                            ],
                        },
                    }
                ],
            }
            second_sync = client.get(f"/api/new/projects/{project_id}/audio/status")
            self.assertEqual(second_sync.status_code, 200, second_sync.text)
            second_row = second_sync.json()["items"][0]
            self.assertEqual(second_row["outputs"]["audio"]["version"], 2)
            self.assertEqual(len(second_row["asset_history"]["audio"]), 2)


if __name__ == "__main__":
    unittest.main()
