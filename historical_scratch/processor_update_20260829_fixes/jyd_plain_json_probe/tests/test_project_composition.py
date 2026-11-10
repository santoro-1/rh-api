from __future__ import annotations

from pathlib import Path
import hashlib
import json
import shutil
import sqlite3
import sys
import threading
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.project_store import ProjectStore  # noqa: E402
from jyd_probe.project_composition import REMOTE_COMPOSITION_ACTIVE  # noqa: E402
from jyd_probe.project_store import ACTIVE_ITEM_STATUSES, PROJECT_ITEM_STATUSES  # noqa: E402
from jyd_probe.web_api import WebApiSettings, create_app  # noqa: E402


class ProjectCompositionApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            PROJECT_ROOT
            / "runtime"
            / "test_tmp"
            / f"project_composition_{uuid.uuid4().hex}"
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
            enable_legacy_new_workbench_video_engines=True,
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

    def test_seedvr2_backfill_is_allowed_when_only_digital_human_source_exists(self) -> None:
        actions = ProjectStore._item_actions(
            {
                "status": "COMPOSITION_FAILED",
                "outputs": {
                    "audio": {"asset_id": "audio-1"},
                    "base_video": None,
                    "composition_video": None,
                    "original_video_segments": [
                        {
                            "external_ref": {"video_index": 1},
                            "metadata": {"quality_variant": "digital_human_source"},
                        }
                    ],
                    "variants": [],
                },
                "subtitles": {},
                "content_analysis": {},
                "visual_analysis": {},
            }
        )

        self.assertTrue(actions["backfill_seedvr2"])
        self.assertTrue(actions["retry_composition"])

    def test_startup_retires_only_unstarted_legacy_composition_operations(self) -> None:
        store = ProjectStore(self.root / "retire_legacy.sqlite3")
        project = store.create_project(
            owner_user_id="user-1",
            owner_username="tester",
            name="旧画面启动恢复",
            items=[
                {"row_key": "1", "script_text": "未进入云端。"},
                {"row_key": "2", "script_text": "已经进入云端。"},
            ],
            settings={"generation_mode": "minimax_h3_ref2va"},
        )
        project_id = project["project_id"]
        first_id = project["items"][0]["item_id"]
        second_id = project["items"][1]["item_id"]
        for index, item_id in enumerate((first_id, second_id), start=1):
            audio_path = self.root / f"audio-{index}.mp3"
            audio_path.write_bytes(b"ID3audio")
            store.add_asset(
                owner_user_id="user-1",
                project_id=project_id,
                item_id=item_id,
                asset_type="audio",
                source_type="minimax",
                status="READY",
                filename=audio_path.name,
                managed_path=str(audio_path),
                make_current=True,
            )
        first = store.create_operation(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=first_id,
            operation_type="COMPOSITION_GENERATE",
            idempotency_key="legacy-starting",
        )
        second = store.create_operation(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=second_id,
            operation_type="COMPOSITION_GENERATE",
            idempotency_key="legacy-running",
        )
        store.claim_pending_operation(
            "user-1",
            project_id,
            first["operation_id"],
            operation_type="COMPOSITION_GENERATE",
        )
        store.transition_operation(
            "user-1",
            project_id,
            second_id,
            operation_id=second["operation_id"],
            operation_type="COMPOSITION_GENERATE",
            status="RUNNING",
            item_status="DIGITAL_HUMAN_RUNNING",
        )

        self.assertEqual(store.retire_unstarted_legacy_composition_operations(), 1)
        refreshed = store.get_project("user-1", project_id)
        operations = {item["operation_id"]: item for item in refreshed["operations"]}
        items = {item["item_id"]: item for item in refreshed["items"]}
        self.assertEqual(operations[first["operation_id"]]["status"], "FAILED")
        self.assertEqual(
            operations[first["operation_id"]]["error_code"],
            "NEW_WORKBENCH_H3_ONLY",
        )
        self.assertEqual(items[first_id]["status"], "AUDIO_READY")
        self.assertEqual(operations[second["operation_id"]]["status"], "RUNNING")
        self.assertEqual(items[second_id]["status"], "DIGITAL_HUMAN_RUNNING")

    def _wait_for_project(
        self,
        client: TestClient,
        project_id: str,
        predicate,
        *,
        timeout: float = 3.0,
    ) -> dict:
        deadline = time.monotonic() + timeout
        latest: dict = {}
        while time.monotonic() < deadline:
            response = client.get(
                f"/api/new/projects/{project_id}/composition/status"
            )
            self.assertEqual(response.status_code, 200, response.text)
            latest = response.json()
            if predicate(latest):
                return latest
            time.sleep(0.02)
        self.fail(f"后台画面协调器未在期限内达到预期状态: {latest}")

    def test_retry_remote_rejection_does_not_leave_fake_pending_operation(self) -> None:
        from jyd_probe.project_composition import ProjectCompositionCoordinator

        store = MagicMock()
        store.get_project.return_value = {
            "settings": {"digital_human": {"resolution": "1024"}},
            "items": [
                {
                    "item_id": "item-1",
                    "row_key": "2",
                    "status": "COMPOSITION_FAILED",
                    "settings": {},
                    "allowed_actions": {"retry_composition": True},
                }
            ],
            "links": [
                {
                    "system": "runninghub",
                    "relation": "digital_human_audio_item",
                    "item_id": "item-1",
                    "external_id": "remote-item-1",
                }
            ],
        }
        store.create_operation.return_value = {
            "operation_id": "operation-1",
            "status": "PENDING",
        }
        client = MagicMock()
        client.retry_workbench_composition.side_effect = RuntimeError(
            "当前画面任务没有可重试的失败阶段"
        )
        coordinator = ProjectCompositionCoordinator(
            store,
            client,
            storage_root=self.settings.storage_root,
            max_video_bytes=1024,
        )

        with self.assertRaisesRegex(RuntimeError, "没有可重试"):
            coordinator.retry(
                "composition-user",
                "project-1",
                "item-1",
                "token",
                idempotency_key="retry-key",
            )

        client.retry_workbench_composition.assert_called_once_with(
            "token", "remote-item-1", resolution="1024"
        )
        failure = store.transition_operation.call_args
        self.assertEqual(failure.kwargs["status"], "FAILED")
        self.assertEqual(failure.kwargs["item_status"], "COMPOSITION_FAILED")

    def test_retry_preserves_paid_image_snapshot_and_rejects_changed_image(self) -> None:
        from jyd_probe.project_composition import ProjectCompositionCoordinator

        frozen_sha256 = hashlib.sha256(b"frozen-image").hexdigest()
        store = MagicMock()
        project = {
            "settings": {"digital_human": {"resolution": "1024"}},
            "items": [
                {
                    "item_id": "item-1",
                    "row_key": "1",
                    "status": "COMPOSITION_FAILED",
                    "settings": {},
                    "inputs": {"image": {"metadata": {"sha256": frozen_sha256}}},
                    "allowed_actions": {"retry_composition": True},
                }
            ],
            "links": [
                {
                    "system": "runninghub",
                    "relation": "digital_human_audio_item",
                    "item_id": "item-1",
                    "external_id": "remote-item-1",
                }
            ],
            "operations": [
                {
                    "operation_type": "COMPOSITION_GENERATE",
                    "status": "FAILED",
                    "payload": {
                        "remote_item_id": "remote-item-1",
                        "input_image_asset_id": "frozen-image-asset",
                        "input_image_sha256": frozen_sha256,
                    },
                    "result": {"remote_item_id": "remote-item-1"},
                }
            ],
        }
        store.get_project.return_value = project
        store.create_operation.return_value = {
            "operation_id": "retry-operation",
            "status": "PENDING",
        }
        client = MagicMock()
        coordinator = ProjectCompositionCoordinator(
            store,
            client,
            storage_root=self.settings.storage_root,
            max_video_bytes=1024,
        )

        coordinator.retry(
            "composition-user",
            "project-1",
            "item-1",
            "token",
            idempotency_key="retry-frozen-image",
        )

        payload = store.create_operation.call_args.kwargs["payload"]
        self.assertEqual(payload["input_image_asset_id"], "frozen-image-asset")
        self.assertEqual(payload["input_image_sha256"], frozen_sha256)
        client.retry_workbench_composition.assert_called_once_with(
            "token", "remote-item-1", resolution="1024"
        )

        project["items"][0]["inputs"]["image"]["metadata"]["sha256"] = hashlib.sha256(
            b"changed-image"
        ).hexdigest()
        store.create_operation.reset_mock()
        client.retry_workbench_composition.reset_mock()
        with self.assertRaisesRegex(ValueError, "冻结图片不一致"):
            coordinator.retry(
                "composition-user",
                "project-1",
                "item-1",
                "token",
                idempotency_key="retry-changed-image",
            )
        store.create_operation.assert_not_called()
        client.retry_workbench_composition.assert_not_called()

    def test_background_start_maps_cloud_audio_failure_to_audio_retry(self) -> None:
        from jyd_probe.project_composition import ProjectCompositionCoordinator

        image_path = self.settings.storage_root / "approved-image.png"
        image_path.write_bytes(b"approved-image")
        image_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
        store = MagicMock()
        store.claim_pending_operation.return_value = {
            "operation_id": "operation-audio-failed",
            "item_id": "item-audio-failed",
            "idempotency_key": "start-audio-failed",
            "correlation_id": "correlation-audio-failed",
            "payload": {
                "batch_id": "batch-audio-failed",
                "remote_item_id": "remote-audio-failed",
                "resolution": "1024",
                "scope": "base_video_only",
                "input_image_asset_id": "image-audio-failed",
                "input_image_sha256": image_sha256,
                "runninghub_execution_account_ids": [3],
                "seedvr2_execution_account_ids": None,
                "execution_mode": "same_account_v1",
            },
        }
        store.get_project.return_value = {
            "items": [
                {
                    "item_id": "item-audio-failed",
                    "asset_history": {
                        "input_image": [
                            {
                                "asset_id": "image-audio-failed",
                                "managed_path": str(image_path),
                                "filename": "approved-image.png",
                            }
                        ]
                    },
                }
            ]
        }
        client = MagicMock()
        client.upload_workbench_batch_asset.return_value = {
            "asset_id": "staged-audio-failed"
        }
        client.start_workbench_composition.return_value = {
            "composition": {
                "status": "COMPOSITION_FAILED",
                "failure_stage": "audio",
                "error_code": "CONFIGURATION_ERROR",
                "error_message": "MiniMax 账号配置缺失或已更换",
                "execution_mode": "same_account_v1",
            }
        }
        coordinator = ProjectCompositionCoordinator(
            store,
            client,
            storage_root=self.settings.storage_root,
            max_video_bytes=1024,
        )

        coordinator.start_pending_operation(
            "composition-user",
            "project-audio-failed",
            "operation-audio-failed",
            "token",
        )

        transition = store.transition_operation.call_args
        self.assertEqual(transition.kwargs["status"], "FAILED")
        self.assertEqual(transition.kwargs["item_status"], "AUDIO_FAILED")
        self.assertEqual(transition.kwargs["error_code"], "CONFIGURATION_ERROR")

    def test_resolution_change_without_source_restarts_with_current_inputs(self) -> None:
        from jyd_probe.project_composition import ProjectCompositionCoordinator

        store = MagicMock()
        store.get_project.return_value = {
            "settings": {"digital_human": {"resolution": "1024"}},
            "items": [
                {
                    "item_id": "item-2",
                    "row_key": "2",
                    "status": "COMPOSITION_FAILED",
                    "settings": {
                        "composition_invalidated_reason": (
                            "DIGITAL_HUMAN_RESOLUTION_CHANGED"
                        )
                    },
                    "outputs": {"original_video_segments": []},
                    "allowed_actions": {
                        "start_composition": True,
                        "retry_composition": True,
                    },
                }
            ],
            "links": [
                {
                    "system": "runninghub",
                    "relation": "digital_human_audio_item",
                    "item_id": "item-2",
                    "external_id": "remote-item-2",
                }
            ],
        }
        coordinator = ProjectCompositionCoordinator(
            store,
            MagicMock(),
            storage_root=self.settings.storage_root,
            max_video_bytes=1024,
        )

        with patch.object(
            coordinator, "start", return_value={"project_id": "project-2"}
        ) as restart:
            result = coordinator.retry(
                "composition-user",
                "project-2",
                "item-2",
                "token",
                idempotency_key="retry-current-inputs",
            )

        self.assertEqual(result["project_id"], "project-2")
        restart.assert_called_once_with(
            "composition-user",
            "project-2",
            "token",
            idempotency_key="retry-current-inputs",
            resolution="1024",
            item_ids=["item-2"],
        )

    def test_dual_pool_mode_and_both_account_snapshots_are_immutable(self) -> None:
        from jyd_probe.project_composition import ProjectCompositionCoordinator

        image_path = self.settings.storage_root / "dual-pool-person.png"
        image_path.write_bytes(b"dual-pool-image")
        image_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
        project = {
            "allowed_actions": {"start_composition": True},
            "operations": [],
            "items": [
                {
                    "item_id": "dual-item-1",
                    "row_key": "1",
                    "settings": {},
                    "outputs": {},
                    "inputs": {
                        "image": {
                            "asset_id": "dual-image-1",
                            "managed_path": str(image_path),
                            "metadata": {"sha256": image_sha256},
                        }
                    },
                }
            ],
            "links": [
                {
                    "system": "runninghub",
                    "relation": "digital_human_audio_item",
                    "item_id": "dual-item-1",
                    "external_id": "remote-dual-item-1",
                    "metadata": {"batch_id": "remote-dual-batch-1"},
                }
            ],
        }
        store = MagicMock()
        store.get_project.return_value = project

        def create_operation(**kwargs):
            operation = {
                "operation_id": "dual-operation-1",
                "operation_type": kwargs["operation_type"],
                "idempotency_key": kwargs["idempotency_key"],
                "payload": kwargs["payload"],
            }
            project["operations"] = [operation]
            return operation

        store.create_operation.side_effect = create_operation
        coordinator = ProjectCompositionCoordinator(
            store,
            MagicMock(),
            storage_root=self.settings.storage_root,
            max_video_bytes=1024,
        )
        with self.assertRaisesRegex(ValueError, "必须分别选择"):
            coordinator.start(
                "dual-user",
                "dual-project",
                "token",
                idempotency_key="dual-operation-missing-seed",
                runninghub_execution_account_ids=[11],
                execution_mode="dual_pool_v1",
            )
        coordinator.start(
            "dual-user",
            "dual-project",
            "token",
            idempotency_key="dual-operation-key",
            runninghub_execution_account_ids=[22, 11],
            seedvr2_execution_account_ids=[42, 31],
            execution_mode="dual_pool_v1",
        )
        payload = store.create_operation.call_args.kwargs["payload"]
        self.assertEqual(payload["runninghub_execution_account_ids"], [11, 22])
        self.assertEqual(payload["seedvr2_execution_account_ids"], [31, 42])
        self.assertEqual(payload["execution_mode"], "dual_pool_v1")

        with self.assertRaisesRegex(ValueError, "快照已锁定"):
            coordinator.start(
                "dual-user",
                "dual-project",
                "token",
                idempotency_key="dual-operation-key",
                runninghub_execution_account_ids=[11, 22],
                seedvr2_execution_account_ids=[31],
                execution_mode="dual_pool_v1",
            )

        project["operations"] = [
            {
                "operation_id": "legacy-same-account-operation",
                "operation_type": "COMPOSITION_GENERATE",
                "idempotency_key": "legacy-same-account-key",
                "payload": {
                    "resolution": "1024",
                    "runninghub_execution_account_ids": [11, 22],
                    "seedvr2_execution_account_ids": None,
                },
            }
        ]
        coordinator.start(
            "dual-user",
            "dual-project",
            "token",
            idempotency_key="legacy-same-account-key",
            runninghub_execution_account_ids=[22, 11],
            execution_mode="same_account_v1",
        )

    def test_start_does_not_reuse_another_items_cloud_account_snapshot(self) -> None:
        from jyd_probe.project_composition import ProjectCompositionCoordinator

        image_path = self.settings.storage_root / "locked-batch-person.png"
        image_path.write_bytes(b"locked-batch-image")
        image_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
        project = {
            "allowed_actions": {"start_composition": True},
            "operations": [
                {
                    "operation_id": "accepted-operation",
                    "operation_type": "COMPOSITION_GENERATE",
                    "item_id": "item-accepted",
                    "status": "RUNNING",
                    "payload": {
                        "batch_id": "shared-batch",
                        "remote_item_id": "remote-accepted",
                        "runninghub_execution_account_ids": [3],
                        "seedvr2_execution_account_ids": None,
                        "execution_mode": "same_account_v1",
                    },
                    "result": {"remote_item_id": "remote-accepted"},
                },
                {
                    "operation_id": "rejected-operation",
                    "operation_type": "COMPOSITION_GENERATE",
                    "item_id": "item-rejected",
                    "status": "FAILED",
                    "payload": {
                        "batch_id": "shared-batch",
                        "runninghub_execution_account_ids": [3, 5],
                        "seedvr2_execution_account_ids": None,
                        "execution_mode": "same_account_v1",
                    },
                    "result": {},
                },
            ],
            "items": [
                {
                    "item_id": "item-new",
                    "row_key": "3",
                    "settings": {},
                    "outputs": {},
                    "inputs": {
                        "image": {
                            "asset_id": "image-new",
                            "managed_path": str(image_path),
                            "metadata": {"sha256": image_sha256},
                        }
                    },
                    "allowed_actions": {"start_composition": True},
                }
            ],
            "links": [
                {
                    "system": "runninghub",
                    "relation": "digital_human_audio_item",
                    "item_id": "item-new",
                    "external_id": "remote-new",
                    "metadata": {"batch_id": "shared-batch"},
                }
            ],
        }
        store = MagicMock()
        store.get_project.return_value = project
        store.create_operation.side_effect = lambda **kwargs: {
            "operation_id": "new-operation",
            "operation_type": kwargs["operation_type"],
            "idempotency_key": kwargs["idempotency_key"],
            "payload": kwargs["payload"],
        }
        coordinator = ProjectCompositionCoordinator(
            store,
            MagicMock(),
            storage_root=self.settings.storage_root,
            max_video_bytes=1024,
        )

        coordinator.start(
            "composition-user",
            "project-locked-batch",
            "token",
            idempotency_key="new-operation-key",
            runninghub_execution_account_ids=[3, 5],
            execution_mode="same_account_v1",
            item_ids=["item-new"],
        )

        payload = store.create_operation.call_args.kwargs["payload"]
        self.assertEqual(payload["runninghub_execution_account_ids"], [3, 5])
        self.assertIsNone(payload["seedvr2_execution_account_ids"])
        self.assertEqual(payload["execution_mode"], "same_account_v1")

    def test_precloud_failure_can_be_restarted_with_a_new_account_selection(self) -> None:
        from jyd_probe.project_composition import ProjectCompositionCoordinator

        image_path = self.settings.storage_root / "retry-precloud-image.png"
        image_path.write_bytes(b"retry-image")
        image_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
        failed_payload = {
            "batch_id": "shared-batch",
            "remote_item_id": "remote-failed",
            "scope": "base_video_only",
            "resolution": "1024",
            "input_image_asset_id": "image-failed",
            "input_image_sha256": image_sha256,
            "runninghub_execution_account_ids": [3],
            "seedvr2_execution_account_ids": None,
            "execution_mode": "same_account_v1",
        }
        project = {
            "settings": {"digital_human": {"resolution": "1024"}},
            "items": [
                {
                    "item_id": "item-failed",
                    "row_key": "2",
                    "status": "COMPOSITION_FAILED",
                    "settings": {},
                    "inputs": {
                        "image": {
                            "asset_id": "image-failed",
                            "managed_path": str(image_path),
                            "metadata": {"sha256": image_sha256},
                        }
                    },
                    "allowed_actions": {"retry_composition": True},
                }
            ],
            "links": [
                {
                    "system": "runninghub",
                    "relation": "digital_human_audio_item",
                    "item_id": "item-failed",
                    "external_id": "remote-failed",
                    "metadata": {
                        "batch_id": "shared-batch",
                        "correlation_id": "correlation-failed",
                    },
                }
            ],
            "operations": [
                {
                    "operation_id": "accepted-operation",
                    "operation_type": "COMPOSITION_GENERATE",
                    "item_id": "item-accepted",
                    "status": "SUCCEEDED",
                    "payload": {
                        "batch_id": "shared-batch",
                        "remote_item_id": "remote-accepted",
                        "runninghub_execution_account_ids": [3],
                        "seedvr2_execution_account_ids": None,
                        "execution_mode": "same_account_v1",
                    },
                    "result": {"remote_item_id": "remote-accepted"},
                },
                {
                    "operation_id": "failed-operation",
                    "operation_type": "COMPOSITION_GENERATE",
                    "item_id": "item-failed",
                    "status": "FAILED",
                    "payload": failed_payload,
                    "result": {},
                    "error_code": "AuthCenterError",
                },
            ],
        }
        store = MagicMock()
        store.get_project.return_value = project
        store.create_operation.return_value = {
            "operation_id": "retry-operation",
            "status": "PENDING",
        }
        client = MagicMock()
        coordinator = ProjectCompositionCoordinator(
            store,
            client,
            storage_root=self.settings.storage_root,
            max_video_bytes=1024,
        )

        coordinator.retry(
            "composition-user",
            "project-locked-batch",
            "item-failed",
            "token",
            idempotency_key="retry-precloud",
        )

        payload = store.create_operation.call_args.kwargs["payload"]
        self.assertEqual(payload["runninghub_execution_account_ids"], [3])
        self.assertEqual(payload["retry_of_operation_id"], "failed-operation")
        self.assertTrue(payload["retry"])
        client.retry_workbench_composition.assert_not_called()

        store.create_operation.reset_mock()
        store.create_operation.side_effect = lambda **kwargs: {
            "operation_id": "restart-operation",
            "status": "PENDING",
            "payload": kwargs["payload"],
        }
        coordinator.start(
            "composition-user",
            "project-locked-batch",
            "token",
            idempotency_key="restart-precloud-with-new-pool",
            runninghub_execution_account_ids=[5, 3],
            execution_mode="same_account_v1",
            item_ids=["item-failed"],
        )
        restarted_payload = store.create_operation.call_args.kwargs["payload"]
        self.assertEqual(
            restarted_payload["runninghub_execution_account_ids"], [3, 5]
        )

    def test_generate_downloads_original_segments_and_base_video_only(self) -> None:
        user = {
            "user_id": "composition-user",
            "username": "tester",
            "enabled": True,
            "is_admin": True,
        }
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id=user["user_id"],
            owner_username=user["username"],
            name="4A 测试",
            items=[{"row_key": "1", "script_text": "第一条。"}],
        )
        item = project["items"][0]
        image_path = self.settings.storage_root / "seed-person.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\npayload")
        store.register_input_image(
            owner_user_id=user["user_id"],
            project_id=project["project_id"],
            filename="seed-person.png",
            content_type="image/png",
            size_bytes=image_path.stat().st_size,
            sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
            managed_path=str(image_path),
        )
        store.apply_image_strategy(
            user["user_id"],
            project["project_id"],
            strategy="loop",
            reuse_count=1,
        )
        audio_path = self.settings.storage_root / "seed-audio.mp3"
        audio_path.write_bytes(b"audio")
        store.add_asset(
            owner_user_id=user["user_id"],
            project_id=project["project_id"],
            item_id=item["item_id"],
            asset_type="audio",
            source_type="minimax",
            status="READY",
            filename="1.mp3",
            managed_path=str(audio_path),
            make_current=True,
        )
        replacement_path = self.settings.storage_root / "latest-person.png"
        replacement_path.write_bytes(b"\x89PNG\r\n\x1a\nlatest")
        replacement = store.register_input_image(
            owner_user_id=user["user_id"],
            project_id=project["project_id"],
            filename="latest-person.png",
            content_type="image/png",
            size_bytes=replacement_path.stat().st_size,
            sha256=hashlib.sha256(replacement_path.read_bytes()).hexdigest(),
            managed_path=str(replacement_path),
        )
        store.replace_item_image(
            user["user_id"],
            project["project_id"],
            item["item_id"],
            replacement["image_id"],
        )
        store.add_link(
            owner_user_id=user["user_id"],
            project_id=project["project_id"],
            item_id=item["item_id"],
            system="runninghub",
            relation="digital_human_audio_item",
            external_id="remote-item-1",
            metadata={
                "batch_id": "remote-batch-1",
                "correlation_id": "composition-correlation-1",
            },
        )

        remote_ready = {
            "item_id": "remote-item-1",
            "updated_at": "2026-08-04T12:00:00+08:00",
            "source": {
                "type": "single_video",
                "videos": [
                    {
                        "index": 1,
                        "task_id": "runninghub-task-1",
                        "status": "SUCCESS",
                        "script_text": "第一条。",
                        "start_seconds": 0.0,
                        "end_seconds": 3.0,
                        "speech_duration_seconds": 3.0,
                        "generation_tail_seconds": 2.0,
                        "quality_variant": "seedvr2_upscaled",
                        "source_download_url": (
                            "/api/workbench/tasks/remote-item-1/videos/1/source"
                        ),
                    }
                ],
            },
            "composition": {
                "status": "BASE_VIDEO_READY",
                "segment_count": 1,
                "base_video_ready": True,
                "execution_mode": "same_account_v1",
                "execution_assignments": [
                    {
                        "segment_index": 1,
                        "digital_human": {
                            "status": "SUCCESS",
                            "account": {"id": 11, "label": "测试一号"},
                        },
                        "seedvr2": {
                            "status": "SUCCESS",
                            "account": {"id": 11, "label": "测试一号"},
                        },
                    }
                ],
                "image_sha256": hashlib.sha256(
                    replacement_path.read_bytes()
                ).hexdigest(),
            },
        }

        def write_segment(_self, _token, _item_id, _index, target, *, max_bytes):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(b"original-segment")
            return len(b"original-segment")

        def write_base(_self, _token, _item_id, target, *, max_bytes):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(b"normalized-base")
            return len(b"normalized-base")

        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "token", "user": user},
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.verify", return_value=user
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.list_workbench_execution_accounts",
            return_value={
                "accounts": [
                    {"id": 11, "name": "pool-11"},
                    {"id": 22, "name": "pool-22"},
                ],
                "default_selected_account_ids": [11, 22],
            },
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.upload_workbench_batch_asset",
            return_value={
                "asset_id": "staged-image-1",
                "original_name": "seed-person.png",
            },
        ) as upload_remote, patch(
            "jyd_probe.auth_center.AuthCenterClient.start_workbench_composition",
            return_value={
                "item_id": "remote-item-1",
                "composition": {"status": "COMPOSITION_QUEUED"},
            },
        ) as start_remote, patch(
            "jyd_probe.auth_center.AuthCenterClient.backfill_workbench_video_enhancement",
            return_value={
                "item_id": "remote-item-1",
                "composition": {"status": "VIDEO_ENHANCING"},
            },
        ) as backfill_remote, patch(
            "jyd_probe.auth_center.AuthCenterClient.get_workbench_task",
            return_value=remote_ready,
        ) as get_remote, patch(
            "jyd_probe.auth_center.AuthCenterClient.download_workbench_video",
            new=write_segment,
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.download_workbench_base_video",
            new=write_base,
        ), patch(
            "jyd_probe.project_composition.probe_video_duration_us",
            return_value=2_990_000,
        ):
            with TestClient(create_app(self.settings)) as client:
                login = client.post(
                    "/api/auth/login",
                    json={"username": "tester", "password": "pass123"},
                )
                self.assertEqual(login.status_code, 200, login.text)
                pool_summary = client.get(
                    "/api/new/runninghub-execution-accounts"
                )
                self.assertEqual(pool_summary.status_code, 200, pool_summary.text)
                self.assertEqual(
                    pool_summary.json()["default_selected_account_ids"], [11, 22]
                )
                missing_selection = client.post(
                    f"/api/new/projects/{project['project_id']}/composition/generate",
                    json={
                        "cost_confirmed": True,
                        "idempotency_key": "composition-missing-selection",
                    },
                )
                self.assertEqual(missing_selection.status_code, 422)
                invalid_selection = client.post(
                    f"/api/new/projects/{project['project_id']}/composition/generate",
                    json={
                        "cost_confirmed": True,
                        "idempotency_key": "composition-invalid-selection",
                        "runninghub_execution_account_ids": ["11"],
                    },
                )
                self.assertEqual(invalid_selection.status_code, 422)
                generated = client.post(
                    f"/api/new/projects/{project['project_id']}/composition/generate",
                    json={
                        "cost_confirmed": True,
                        "idempotency_key": "composition-1",
                        "resolution": "2048",
                        "runninghub_execution_account_ids": [22, 11],
                    },
                )
                self.assertEqual(generated.status_code, 200, generated.text)
                payload = generated.json()
                current = payload["items"][0]
                self.assertEqual(payload["status"], "PROCESSING")
                self.assertEqual(current["status"], "COMPOSITION_QUEUED")
                payload = self._wait_for_project(
                    client,
                    project["project_id"],
                    lambda value: value["items"][0]["status"]
                    == "BASE_VIDEO_READY",
                )
                current = payload["items"][0]
                self.assertIsNotNone(current["outputs"]["base_video"])
                self.assertEqual(
                    current["outputs"]["base_video"]["metadata"]["duration_us"],
                    2_990_000,
                )
                self.assertEqual(
                    current["outputs"]["base_video"]["metadata"]["planned_duration_us"],
                    3_000_000,
                )
                self.assertEqual(len(current["outputs"]["original_video_segments"]), 1)
                segment_asset = current["outputs"]["original_video_segments"][0]
                self.assertEqual(
                    segment_asset["metadata"]["quality_variant"],
                    "seedvr2_upscaled",
                )
                self.assertEqual(
                    segment_asset["metadata"]["enhanced_by"],
                    "runninghub_seedvr2",
                )
                self.assertEqual(
                    segment_asset["metadata"]["actual_duration_us"],
                    2_990_000,
                )
                self.assertEqual(
                    segment_asset["metadata"]["speech_duration_seconds"], 3.0
                )
                self.assertEqual(
                    segment_asset["metadata"]["generation_tail_seconds"], 2.0
                )
                self.assertTrue(
                    segment_asset["metadata"]["source_is_available_on_cloud"]
                )
                self.assertIsNone(current["outputs"]["composition_video"])
                self.assertEqual(current["outputs"]["variants"], [])
                self.assertFalse(payload["allowed_actions"]["generate_variants"])
                start_remote.assert_called_once()
                self.assertEqual(
                    Path(upload_remote.call_args.args[1]), replacement_path.resolve()
                )
                self.assertEqual(
                    start_remote.call_args.kwargs["image_asset_id"],
                    "staged-image-1",
                )
                self.assertEqual(
                    start_remote.call_args.kwargs["image_sha256"],
                    hashlib.sha256(replacement_path.read_bytes()).hexdigest(),
                )
                self.assertEqual(
                    start_remote.call_args.kwargs["correlation_id"],
                    "composition-correlation-1",
                )
                self.assertEqual(
                    start_remote.call_args.kwargs["resolution"],
                    "2048",
                )
                self.assertEqual(
                    start_remote.call_args.kwargs[
                        "runninghub_execution_account_ids"
                    ],
                    [11, 22],
                )
                operation = next(
                    value
                    for value in payload["operations"]
                    if value["operation_type"] == "COMPOSITION_GENERATE"
                )
                self.assertEqual(
                    operation["payload"]["runninghub_execution_account_ids"],
                    [11, 22],
                )
                self.assertEqual(operation["payload"]["resolution"], "2048")
                self.assertEqual(
                    operation["result"]["execution_mode"], "same_account_v1"
                )
                self.assertEqual(
                    operation["result"]["execution_assignments"][0][
                        "digital_human"
                    ]["account"]["label"],
                    "测试一号",
                )

                synced = client.get(
                    f"/api/new/projects/{project['project_id']}/composition/status"
                )
                self.assertEqual(synced.status_code, 200, synced.text)
                synced_item = synced.json()["items"][0]
                self.assertEqual(
                    len(synced_item["asset_history"]["original_video_segment"]), 1
                )
                self.assertEqual(len(synced_item["asset_history"]["base_video"]), 1)
                self.assertEqual(
                    get_remote.call_count,
                    1,
                    "已完成的画面操作不应在后续状态轮询中再次请求云端",
                )

                reused = client.post(
                    f"/api/new/projects/{project['project_id']}/composition/generate",
                    json={
                        "cost_confirmed": True,
                        "idempotency_key": "composition-single-reuse",
                        "runninghub_execution_account_ids": [11, 22],
                        "item_ids": [item["item_id"]],
                    },
                )
                self.assertEqual(reused.status_code, 200, reused.text)
                self.assertEqual(
                    reused.json()["items"][0]["outputs"]["base_video"]["asset_id"],
                    synced_item["outputs"]["base_video"]["asset_id"],
                )
                start_remote.assert_called_once()

                historical_composition_path = (
                    self.settings.storage_root / "historical-composition.mp4"
                )
                historical_composition_path.write_bytes(b"historical-composition")
                store.add_asset(
                    owner_user_id=user["user_id"],
                    project_id=project["project_id"],
                    item_id=item["item_id"],
                    asset_type="composition_video",
                    source_type="jianying_postprocess",
                    status="READY",
                    filename="historical-composition.mp4",
                    managed_path=str(historical_composition_path),
                    make_current=True,
                )
                with sqlite3.connect(
                    self.settings.storage_root / "control.db"
                ) as connection:
                    rows = connection.execute(
                        """
                        SELECT asset_id, asset_type, external_ref_json, metadata_json
                        FROM project_assets
                        WHERE item_id=? AND asset_type IN ('base_video', 'original_video_segment')
                        """,
                        (item["item_id"],),
                    ).fetchall()
                    for asset_id, _asset_type, external_ref_json, metadata_json in rows:
                        external_ref = json.loads(external_ref_json or "{}")
                        metadata = json.loads(metadata_json or "{}")
                        external_ref.pop("quality_variant", None)
                        external_ref.pop("source_quality_variants", None)
                        metadata.pop("quality_variant", None)
                        metadata.pop("enhanced_by", None)
                        connection.execute(
                            """
                            UPDATE project_assets
                            SET external_ref_json=?, metadata_json=?
                            WHERE asset_id=?
                            """,
                            (
                                json.dumps(external_ref, ensure_ascii=False),
                                json.dumps(metadata, ensure_ascii=False),
                                asset_id,
                            ),
                        )

                historical = client.get(
                    f"/api/new/projects/{project['project_id']}"
                )
                historical_item = historical.json()["items"][0]
                self.assertTrue(
                    historical_item["allowed_actions"]["backfill_seedvr2"]
                )
                self.assertIsNotNone(
                    historical_item["outputs"]["composition_video"]
                )
                unconfirmed_backfill = client.post(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/composition/seedvr2-backfill",
                    json={"idempotency_key": "historical-seedvr2-unconfirmed"},
                )
                self.assertEqual(unconfirmed_backfill.status_code, 409)
                historical_backfill = client.post(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/composition/seedvr2-backfill",
                    json={
                        "cost_confirmed": True,
                        "idempotency_key": "historical-seedvr2-backfill",
                    },
                )
                self.assertEqual(
                    historical_backfill.status_code, 200, historical_backfill.text
                )
                historical_backfill_payload = self._wait_for_project(
                    client,
                    project["project_id"],
                    lambda value: any(
                        operation["idempotency_key"]
                        == "historical-seedvr2-backfill"
                        and operation["status"] == "SUCCEEDED"
                        for operation in value["operations"]
                    ),
                )
                backfill_remote.assert_called_once_with(
                    "token",
                    "remote-item-1",
                    idempotency_key=(
                        f"historical-seedvr2-backfill:{item['item_id']}"
                    ),
                )
                backfilled_item = historical_backfill_payload["items"][0]
                self.assertEqual(backfilled_item["status"], "BASE_VIDEO_READY")
                self.assertEqual(
                    backfilled_item["outputs"]["base_video"]["metadata"][
                        "quality_variant"
                    ],
                    "seedvr2_upscaled",
                )
                self.assertIsNone(
                    backfilled_item["outputs"]["composition_video"]
                )
                self.assertFalse(
                    backfilled_item["allowed_actions"]["backfill_seedvr2"]
                )
                enhanced_segments = [
                    asset
                    for asset in backfilled_item["outputs"][
                        "original_video_segments"
                    ]
                    if asset["metadata"].get("quality_variant")
                    == "seedvr2_upscaled"
                ]
                self.assertEqual(len(enhanced_segments), 1)
                backfill_remote.reset_mock()

                downloaded = client.get(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/base-video"
                )
                self.assertEqual(downloaded.status_code, 200, downloaded.text)
                self.assertEqual(downloaded.content, b"normalized-base")

                preserved = store.set_digital_human_resolution(
                    user["user_id"],
                    project["project_id"],
                    resolution="1920",
                )
                preserved_item = preserved["items"][0]
                self.assertEqual(preserved_item["status"], "BASE_VIDEO_READY")
                self.assertIsNotNone(preserved_item["outputs"]["base_video"])
                self.assertIsNone(preserved_item["outputs"]["composition_video"])
                self.assertNotIn(
                    "composition_invalidated_reason", preserved_item["settings"]
                )
                self.assertEqual(
                    len(preserved_item["asset_history"]["base_video"]), 2
                )

                # Existing installs can still contain the old marker. Keep that
                # migration path compatible without creating new invalidations.
                invalidated = store.invalidate_item_composition(
                    user["user_id"],
                    project["project_id"],
                    item["item_id"],
                    reason="DIGITAL_HUMAN_RESOLUTION_CHANGED",
                )
                invalidated_item = invalidated["items"][0]
                self.assertIsNone(invalidated_item["outputs"]["base_video"])

                active_remote = {
                    **remote_ready,
                    "composition": {
                        **remote_ready["composition"],
                        "status": "VIDEO_ENHANCING",
                        "base_video_ready": False,
                    },
                }
                get_remote.return_value = active_remote
                regenerated = client.post(
                    f"/api/new/projects/{project['project_id']}/composition/generate",
                    json={
                        "cost_confirmed": True,
                        "idempotency_key": "composition-resolution-1920",
                        "resolution": "1920",
                        "runninghub_execution_account_ids": [11, 22],
                        "item_ids": [item["item_id"]],
                    },
                )
                self.assertEqual(regenerated.status_code, 200, regenerated.text)
                regenerated_payload = self._wait_for_project(
                    client,
                    project["project_id"],
                    lambda value: any(
                        operation["idempotency_key"]
                        == "composition-resolution-1920"
                        and operation["status"] == "RUNNING"
                        for operation in value["operations"]
                    ),
                )
                start_remote.assert_called_once()
                upload_remote.assert_called_once()
                backfill_remote.assert_called_once_with(
                    "token",
                    "remote-item-1",
                    idempotency_key=(
                        f"composition-resolution-1920:{item['item_id']}"
                    ),
                )
                backfill_operation = next(
                    value
                    for value in regenerated_payload["operations"]
                    if value["operation_type"] == "COMPOSITION_GENERATE"
                    and value["idempotency_key"] == "composition-resolution-1920"
                )
                self.assertEqual(
                    backfill_operation["payload"]["scope"],
                    "seedvr2_backfill_only",
                )
                self.assertNotIn(
                    "input_image_sha256", backfill_operation["payload"]
                )

                store.transition_operation(
                    user["user_id"],
                    project["project_id"],
                    item["item_id"],
                    operation_type="COMPOSITION_GENERATE",
                    status="FAILED",
                    item_status="COMPOSITION_FAILED",
                    error_code="TEST_FAILURE",
                    error_message="模拟失败",
                )
                retried = client.post(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/composition/retry",
                    json={
                        "cost_confirmed": True,
                        "idempotency_key": "composition-resolution-1920-retry",
                    },
                )
                self.assertEqual(retried.status_code, 200, retried.text)
                start_remote.assert_called_once()
                self.assertEqual(backfill_remote.call_count, 2)
                backfill_remote.assert_called_with(
                    "token",
                    "remote-item-1",
                    idempotency_key=(
                        f"composition-resolution-1920-retry:{item['item_id']}"
                    ),
                )

                resolution_ready = {
                    **remote_ready,
                    "updated_at": "2026-08-04T12:30:00+08:00",
                    "source": {
                        **remote_ready["source"],
                        "videos": [
                            {
                                **remote_ready["source"]["videos"][0],
                                "task_id": "runninghub-task-2",
                            }
                        ],
                    },
                    "composition": {
                        **remote_ready["composition"],
                        "status": "BASE_VIDEO_READY",
                        "base_video_ready": True,
                    },
                }
                get_remote.return_value = resolution_ready
                completed_resolution = client.get(
                    f"/api/new/projects/{project['project_id']}/composition/status"
                )
                self.assertEqual(
                    completed_resolution.status_code, 200, completed_resolution.text
                )
                completed_item = completed_resolution.json()["items"][0]
                self.assertIsNotNone(completed_item["outputs"]["base_video"])
                self.assertNotIn(
                    "composition_invalidated_reason", completed_item["settings"]
                )

    def test_video_enhancing_is_a_supported_active_remote_status(self) -> None:
        self.assertIn("VIDEO_ENHANCING", REMOTE_COMPOSITION_ACTIVE)
        self.assertIn("VIDEO_ENHANCING", PROJECT_ITEM_STATUSES)
        self.assertIn("VIDEO_ENHANCING", ACTIVE_ITEM_STATUSES)

    def test_hundred_rows_start_in_background_with_bounded_concurrency(self) -> None:
        user = {
            "user_id": "bulk-admin",
            "username": "bulk-admin",
            "enabled": True,
            "is_admin": True,
        }
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id=user["user_id"],
            owner_username=user["username"],
            name="100 行后台启动",
            items=[
                {"row_key": str(index), "script_text": f"第 {index} 条。"}
                for index in range(1, 101)
            ],
        )
        image_path = self.settings.storage_root / "bulk-person.png"
        image_path.write_bytes(b"bulk-image")
        store.register_input_image(
            owner_user_id=user["user_id"],
            project_id=project["project_id"],
            filename=image_path.name,
            content_type="image/png",
            size_bytes=image_path.stat().st_size,
            sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
            managed_path=str(image_path),
        )
        store.apply_image_strategy(
            user["user_id"], project["project_id"], strategy="loop", reuse_count=1
        )
        project = store.get_project(user["user_id"], project["project_id"])
        for index, item in enumerate(project["items"], start=1):
            audio_path = self.settings.storage_root / f"bulk-audio-{index}.mp3"
            audio_path.write_bytes(b"audio")
            store.add_asset(
                owner_user_id=user["user_id"],
                project_id=project["project_id"],
                item_id=item["item_id"],
                asset_type="audio",
                source_type="minimax",
                status="READY",
                filename=audio_path.name,
                managed_path=str(audio_path),
                make_current=True,
            )
            store.add_link(
                owner_user_id=user["user_id"],
                project_id=project["project_id"],
                item_id=item["item_id"],
                system="runninghub",
                relation="digital_human_audio_item",
                external_id=f"remote-{index}",
                metadata={"batch_id": "remote-batch", "correlation_id": f"c-{index}"},
            )

        release_uploads = threading.Event()
        counter_lock = threading.Lock()
        active_uploads = 0
        max_active_uploads = 0

        def blocked_upload(_token, _path, *, kind, filename):
            nonlocal active_uploads, max_active_uploads
            with counter_lock:
                active_uploads += 1
                max_active_uploads = max(max_active_uploads, active_uploads)
            try:
                release_uploads.wait(timeout=5)
                return {"asset_id": f"staged-{filename}"}
            finally:
                with counter_lock:
                    active_uploads -= 1

        def start_remote(_token, _batch_id, remote_item_id, **_kwargs):
            if remote_item_id == "remote-50":
                raise RuntimeError("模拟单行明确失败")
            return {"composition": {"status": "COMPOSITION_QUEUED"}}

        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "bulk-token", "user": user},
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.verify", return_value=user
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.upload_workbench_batch_asset",
            side_effect=blocked_upload,
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.start_workbench_composition",
            side_effect=start_remote,
        ) as start_mock, patch(
            "jyd_probe.auth_center.AuthCenterClient.get_workbench_task",
            return_value={"composition": {"status": "COMPOSITION_QUEUED"}},
        ):
            with TestClient(create_app(self.settings)) as client:
                login = client.post(
                    "/api/auth/login",
                    json={"username": user["username"], "password": "pass123"},
                )
                self.assertEqual(login.status_code, 200, login.text)
                started_at = time.monotonic()
                response = client.post(
                    f"/api/new/projects/{project['project_id']}/composition/generate",
                    json={
                        "cost_confirmed": True,
                        "idempotency_key": "bulk-100",
                        "runninghub_execution_account_ids": [11, 22],
                    },
                )
                elapsed = time.monotonic() - started_at
                self.assertEqual(response.status_code, 200, response.text)
                self.assertLess(elapsed, 1.0)
                self.assertEqual(
                    len(
                        [
                            operation
                            for operation in response.json()["operations"]
                            if operation["operation_type"] == "COMPOSITION_GENERATE"
                        ]
                    ),
                    100,
                )
                # Repeated status requests must not enqueue duplicate paid
                # handoffs while the first four rows are blocked in upload.
                status_started_at = time.monotonic()
                status = client.get(
                    f"/api/new/projects/{project['project_id']}/composition/status"
                )
                self.assertEqual(status.status_code, 200, status.text)
                self.assertLess(time.monotonic() - status_started_at, 1.0)
                release_uploads.set()

                deadline = time.monotonic() + 20
                operations: list[dict] = []
                while time.monotonic() < deadline:
                    current = store.get_project(
                        user["user_id"], project["project_id"]
                    )
                    operations = [
                        operation
                        for operation in current["operations"]
                        if operation["operation_type"] == "COMPOSITION_GENERATE"
                    ]
                    if all(
                        operation["status"] in {"RUNNING", "FAILED"}
                        for operation in operations
                    ):
                        break
                    time.sleep(0.02)
                self.assertEqual(len(operations), 100)
                self.assertEqual(
                    sum(operation["status"] == "RUNNING" for operation in operations),
                    99,
                )
                self.assertEqual(
                    sum(operation["status"] == "FAILED" for operation in operations),
                    1,
                )
                self.assertEqual(start_mock.call_count, 100)
                self.assertLessEqual(max_active_uploads, 4)

    def test_restart_recovers_only_interrupted_local_handoff(self) -> None:
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id="restart-owner",
            owner_username="restart-owner",
            name="重启恢复",
            items=[{"row_key": "1", "script_text": "重启恢复。"}],
        )
        item = project["items"][0]
        operation = store.create_operation(
            owner_user_id="restart-owner",
            project_id=project["project_id"],
            item_id=item["item_id"],
            operation_type="COMPOSITION_GENERATE",
            idempotency_key="restart-safe-key",
            payload={"batch_id": "batch", "remote_item_id": "remote"},
        )
        claimed = store.claim_pending_operation(
            "restart-owner",
            project["project_id"],
            operation["operation_id"],
            operation_type="COMPOSITION_GENERATE",
        )
        self.assertEqual(claimed["status"], "STARTING")

        app = create_app(self.settings)
        recovered = app.state.project_store.get_project(
            "restart-owner", project["project_id"]
        )
        recovered_operation = next(
            value
            for value in recovered["operations"]
            if value["operation_id"] == operation["operation_id"]
        )
        self.assertEqual(recovered_operation["status"], "PENDING")
        self.assertEqual(recovered_operation["idempotency_key"], "restart-safe-key")
        app.state.composition_start_dispatcher.shutdown()
        app.state.storage_lifecycle.stop()


if __name__ == "__main__":
    unittest.main()
