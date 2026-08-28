from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    GenerationTask,
    H3RemoteAsrJobStatus,
    RunningHubExecutionAccount,
    RunningHubPoolMembership,
    TaskStatus,
    User,
)
from app.services.h3_pool import configure_h3_capability
from app.services.runninghub import RunningHubError
from app.services.security import encrypt_secret, secret_fingerprint
from app.services.h3.postprocess import (
    H3HeadTrimDecision,
    H3NormalizedResult,
    H3PostprocessError,
)
from app.services.task_cancellation import cancel_generation_task
from app.workers import task_worker
from tests.conftest import create_user
from tests.test_h3_workbench import (
    SCRIPT,
    _enable_h3_pool,
    _fake_cut,
    _fake_motion_references,
    _finished_audio,
    _stage,
    _token,
)


class _FakeH3RunningHub:
    def __init__(self) -> None:
        self.ai_app_id = ""
        self.submission_type = ""
        self.last_payload: dict[str, object] | None = None
        self.submissions = 0
        self.access_password = ""

    def set_access_password(self, value: str) -> None:
        self.access_password = value

    def get_account_current_task_count(self) -> int:
        return 0

    def upload_file(self, path: Path) -> str:
        return f"openapi/{path.name}"

    def submit_task(self, payload: dict[str, object]) -> str:
        self.last_payload = payload
        self.submissions += 1
        return f"h3-remote-task-{self.submissions:03d}"

    def query_task(self, task_id: str) -> dict[str, object]:
        return {
            "taskId": task_id,
            "status": "SUCCESS",
            "results": [
                {
                    "nodeId": "387",
                    "outputType": "mp4",
                    "url": "https://example.invalid/h3.mp4",
                }
            ],
        }

    def download_result(self, url: str, destination: Path) -> None:
        del url
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"h3-provider-video")


def _prepare_confirmed_h3_task(
    client,
    monkeypatch,
    username: str,
    *,
    continuity_mode: str = "fast",
    workflow_access_password: str | None = None,
    prompt_override: str = "",
) -> str:
    class _ControlledUploadDirectory:
        def __enter__(self) -> str:
            path = get_settings().data_dir / f"h3-runtime-upload-{username}"
            path.mkdir(parents=True, exist_ok=True)
            return str(path)

        def __exit__(self, *args: object) -> None:
            return None

    def _controlled_upload_directory(root: Path) -> _ControlledUploadDirectory:
        expected_root = get_settings().runtime_dir / "runninghub-uploads"
        assert root == expected_root
        assert expected_root.is_dir()
        return _ControlledUploadDirectory()

    monkeypatch.setattr(
        task_worker,
        "_upload_scratch_directory",
        _controlled_upload_directory,
    )
    create_user(username)
    token = _token(client, username)
    account_id = _enable_h3_pool(
        username,
        access_password=workflow_access_password,
    )
    image_id = _stage(client, token, "image", "identity.png", "image/png")
    video_id = _stage(client, token, "video", "reference.mp4", "video/mp4")
    audio_batch_id, audio_item_id = _finished_audio(client, token, username)
    approved = client.post(
        "/api/workbench/h3-audio-sources/approve",
        json={
            "access_token": token,
            "audio_batch_id": audio_batch_id,
            "audio_item_id": audio_item_id,
            "audio_generation_version": 1,
        },
    )
    assert approved.status_code == 200, approved.text
    monkeypatch.setattr(
        "app.services.h3_workbench.inspect_audio_duration",
        lambda path: 10.0 if "segment-" in Path(path).name else 20.0,
    )
    monkeypatch.setattr("app.services.h3_workbench.cut_audio_segment", _fake_cut)
    monkeypatch.setattr(
        "app.services.h3_workbench.split_h3_motion_reference",
        _fake_motion_references,
    )
    prepared = client.post(
        "/api/workbench/h3-batches/prepare",
        json={
            "access_token": token,
            "request_key": f"runtime-{username}",
            "reference_image_asset_ids": [image_id],
            "selected_account_ids": [account_id],
            "defaults": {
                "continuity_mode": continuity_mode,
                "prompt_override": prompt_override,
            },
            "rows": [
                {
                    "row_id": "ROW-001",
                    "script_text": SCRIPT,
                    "video_asset_id": video_id,
                    "audio_batch_id": audio_batch_id,
                    "audio_item_id": audio_item_id,
                    "audio_generation_version": 1,
                }
            ],
        },
    )
    assert prepared.status_code == 201, prepared.text
    batch_id = prepared.json()["batch_id"]
    confirmed = client.post(
        f"/api/workbench/h3-batches/{batch_id}/confirm",
        json={"access_token": token, "cost_confirmed": True},
    )
    assert confirmed.status_code == 200, confirmed.text
    return batch_id


def test_h3_worker_uses_h3_capability_and_persists_dynamic_graph_hash(
    client,
    monkeypatch,
) -> None:
    batch_id = _prepare_confirmed_h3_task(client, monkeypatch, "h3-worker-runtime")
    fake = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)

    with SessionLocal() as db:
        task_id = task_worker.claim_next_pending_task(db)
        assert task_id is not None
        task_worker.process_task(db, task_id)
        task = db.get(GenerationTask, task_id)
        assert task.status == TaskStatus.SUBMITTED.value
        assert task.runninghub_task_id == "h3-remote-task-001"
        assert task.execution_account is not None
        assert fake.ai_app_id == "h3-raw-workflow-test"
        assert fake.submission_type == "raw-workflow"
        assert fake.last_payload is not None
        assert fake.last_payload["instanceType"] == "plus"
        workflow_json = fake.last_payload["workflow"]
        assert isinstance(workflow_json, str)
        graph = json.loads(workflow_json)
        assert graph["84"]["inputs"]["value"] == 10.1
        assert graph["105"]["inputs"] == {
            "aspect_ratio": "9:16 (Portrait Widescreen)",
            "megapixels": 1.0,
            "multiple": 32,
        }
        assert task.segment.h3_config.dynamic_workflow_sha256 == hashlib.sha256(
            workflow_json.encode("utf-8")
        ).hexdigest()

    token = _token(client, "h3-worker-runtime")
    status = client.post(
        f"/api/workbench/h3-batches/{batch_id}",
        json={"access_token": token},
    )
    segment_payload = status.json()["items"][0]["segments"][0]
    assert segment_payload["normalized_video_download_url"] is None


def test_h3_remote_mode_queues_asr_then_resumes_postprocess(
    client,
    monkeypatch,
) -> None:
    _prepare_confirmed_h3_task(client, monkeypatch, "h3-remote-asr-runtime")
    fake = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    monkeypatch.setattr(task_worker, "H3_HEAD_TRIM_ENABLED", True)
    remote_settings = replace(get_settings(), media_processing_mode="remote")
    monkeypatch.setattr(task_worker, "get_settings", lambda: remote_settings)

    def fake_postprocess(
        source: Path,
        *,
        script_text: str,
        head_trim_decision: H3HeadTrimDecision,
        needs_continuity_anchor: bool,
    ) -> H3NormalizedResult:
        assert script_text
        assert head_trim_decision.trim_seconds == 0.18
        assert needs_continuity_anchor is False
        video = source.with_name("remote-asr-normalized.mp4")
        video.write_bytes(b"remote-asr-normalized")
        return H3NormalizedResult(
            video_path=video,
            video_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
            anchor_path=None,
            anchor_sha256=None,
            head_trim=head_trim_decision,
            normalized_duration_seconds=5.7,
        )

    monkeypatch.setattr(task_worker, "postprocess_h3_result", fake_postprocess)
    with SessionLocal() as db:
        task_id = task_worker.claim_next_pending_task(db)
        assert task_id is not None
        task_worker.process_task(db, task_id)
        task_worker.process_task(db, task_id)
        db.expire_all()
        task = db.get(GenerationTask, task_id)
        assert task.status == TaskStatus.RUNNING.value
        assert task.h3_remote_asr_job is not None
        assert task.h3_remote_asr_job.status == H3RemoteAsrJobStatus.PENDING.value
        assert (remote_settings.data_dir / task.h3_remote_asr_job.source_path).is_file()

        task.h3_remote_asr_job.status = H3RemoteAsrJobStatus.SUCCESS.value
        task.h3_remote_asr_job.result_json = json.dumps(
            {
                "mode": "asr_adaptive",
                "trimSeconds": 0.18,
                "firstScriptTokenStartSeconds": 0.22,
                "alignmentProvider": "funasr_http",
                "alignmentMatchRatio": 1.0,
                "matchedPrefixTokens": 3,
                "fallbackReason": None,
            }
        )
        db.commit()
        task_worker.process_task(db, task_id)
        db.expire_all()
        task = db.get(GenerationTask, task_id)
        assert task.status == TaskStatus.SUCCESS.value
        assert task.result_path.endswith("remote-asr-normalized.mp4")


def test_h3_remote_mode_skips_head_trim_queue_when_disabled(
    client,
    monkeypatch,
) -> None:
    _prepare_confirmed_h3_task(client, monkeypatch, "h3-head-trim-disabled-runtime")
    fake = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    monkeypatch.setattr(task_worker, "H3_HEAD_TRIM_ENABLED", False)
    remote_settings = replace(get_settings(), media_processing_mode="remote")
    monkeypatch.setattr(task_worker, "get_settings", lambda: remote_settings)

    def fail_if_alignment_provider_is_requested(provider_name: str):
        raise AssertionError(f"禁用 H3 片头裁切后不应请求 ASR provider：{provider_name}")

    monkeypatch.setattr(
        task_worker,
        "get_alignment_provider",
        fail_if_alignment_provider_is_requested,
    )

    def fake_postprocess(
        source: Path,
        *,
        script_text: str,
        alignment_provider,
        needs_continuity_anchor: bool,
    ) -> H3NormalizedResult:
        assert script_text
        assert alignment_provider is None
        assert needs_continuity_anchor is False
        video = source.with_name("head-trim-disabled-normalized.mp4")
        video.write_bytes(b"head-trim-disabled-normalized")
        decision = H3HeadTrimDecision(
            mode="disabled",
            trim_seconds=0.0,
            first_script_token_start_seconds=None,
            alignment_provider=None,
            alignment_match_ratio=None,
            matched_prefix_tokens=0,
            fallback_reason="feature_disabled",
        )
        return H3NormalizedResult(
            video_path=video,
            video_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
            anchor_path=None,
            anchor_sha256=None,
            head_trim=decision,
            normalized_duration_seconds=6.0,
        )

    monkeypatch.setattr(task_worker, "postprocess_h3_result", fake_postprocess)
    with SessionLocal() as db:
        task_id = task_worker.claim_next_pending_task(db)
        assert task_id is not None
        task_worker.process_task(db, task_id)
        task_worker.process_task(db, task_id)
        db.expire_all()
        task = db.get(GenerationTask, task_id)
        assert task.status == TaskStatus.SUCCESS.value
        assert task.h3_remote_asr_job is None
        assert task.result_path.endswith("head-trim-disabled-normalized.mp4")
        metadata = json.loads(task.output_metadata or "{}")
        assert metadata["output_contract_version"] == (
            "h3.output.generated-av-head-trim-disabled.v4"
        )
        assert metadata["provider_audio_head_trimmed"] is False
        assert metadata["provider_duration_preserved"] is True
        assert metadata["head_trim"] == {
            "enabled": False,
            "mode": "disabled",
            "trim_seconds": 0.0,
            "first_script_token_start_seconds": None,
            "preroll_seconds": 0.04,
            "fallback_seconds": 0.3,
            "alignment_provider": None,
            "alignment_match_ratio": None,
            "matched_prefix_tokens": 0,
            "fallback_reason": "feature_disabled",
        }


def test_h3_worker_decrypts_private_workflow_password_only_into_client(
    client,
    monkeypatch,
) -> None:
    _prepare_confirmed_h3_task(
        client,
        monkeypatch,
        "h3-private-workflow-runtime",
        workflow_access_password="private-h3-password",
    )
    fake = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)

    class _ControlledUploadDirectory:
        def __enter__(self) -> str:
            path = get_settings().data_dir / "h3-private-workflow-upload"
            path.mkdir(parents=True, exist_ok=True)
            return str(path)

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        task_worker,
        "_upload_scratch_directory",
        lambda root: _ControlledUploadDirectory(),
    )

    with SessionLocal() as db:
        task_id = task_worker.claim_next_pending_task(db)
        assert task_id is not None
        task_worker.process_task(db, task_id)
        task = db.get(GenerationTask, task_id)
        assert task.status == TaskStatus.SUBMITTED.value
        assert fake.access_password == "private-h3-password"
        assert "private-h3-password" not in str(task.input_payload)
        assert "private-h3-password" not in str(task.output_metadata)


def test_h3_remote_failure_is_not_automatically_resubmitted(
    client,
    monkeypatch,
) -> None:
    _prepare_confirmed_h3_task(client, monkeypatch, "h3-no-paid-auto-retry")

    class _FailedH3RunningHub(_FakeH3RunningHub):
        def query_task(self, task_id: str) -> dict[str, object]:
            return {
                "taskId": task_id,
                "status": "FAILED",
                "errorCode": "REMOTE_FAILED",
                "errorMessage": "injected provider failure",
                "failedReason": {"node": "injected"},
                "usage": {"consumeMoney": "1"},
            }

    fake = _FailedH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task_id = task_worker.claim_next_pending_task(db)
        assert task_id is not None
        task_worker.process_task(db, task_id)
        task_worker.process_task(db, task_id)
        db.expire_all()
        task = db.get(GenerationTask, task_id)
        assert task.status == TaskStatus.FAILED.value
        assert task.segment.status == TaskStatus.FAILED.value
        assert task.runninghub_task_id == "h3-remote-task-001"
        assert task.runninghub_auto_retry_count == 0
        assert task.runninghub_auto_retry_after is None
        assert "不会自动重复付费" in task.error_message
        assert fake.submissions == 1


def test_h3_insufficient_power_switches_only_failed_segment_to_frozen_account(
    client,
    monkeypatch,
) -> None:
    username = "h3-insufficient-power-failover"
    _prepare_confirmed_h3_task(client, monkeypatch, username)

    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        task = db.query(GenerationTask).filter_by(
            workflow_type="minimax_h3_ref2va"
        ).first()
        assert task is not None
        first_account_id = json.loads(
            task.segment.batch_item.runninghub_execution_account_ids_json
        )[0]
        second_key = "h3-runninghub-failover-second"
        second = RunningHubExecutionAccount(
            label="H3 余额切换二号",
            api_key_encrypted=encrypt_secret(second_key),
            credential_fingerprint=secret_fingerprint(second_key),
            base_url="https://runninghub.example",
            digital_human_ai_app_id="must-not-be-used-for-h3",
            max_concurrent_tasks=5,
            is_enabled=True,
        )
        db.add(second)
        db.flush()
        db.add(RunningHubPoolMembership(execution_account=second, admin_user=user))
        db.add(
            configure_h3_capability(
                second,
                workflow_id="h3-raw-workflow-test-second",
                instance_type="plus",
                max_concurrent_tasks=3,
                is_enabled=True,
            )
        )
        task.segment.batch_item.runninghub_execution_account_ids_json = json.dumps(
            sorted([first_account_id, second.id])
        )
        second_account_id = second.id
        task_id = task.id
        db.commit()

    class _NoPowerH3RunningHub(_FakeH3RunningHub):
        def submit_task(self, payload: dict[str, object]) -> str:
            self.last_payload = payload
            self.submissions += 1
            raise RunningHubError(
                "提交 H3 动态工作流失败：TASK_CREATE_FAILED_BY_NOT_ENOUGH_POWER_VALUE",
                error_code="TASK_CREATE_FAILED_BY_NOT_ENOUGH_POWER_VALUE",
            )

    first = _NoPowerH3RunningHub()
    second = _FakeH3RunningHub()
    clients = {first_account_id: first, second_account_id: second}
    monkeypatch.setattr(task_worker, "_make_client", lambda config: clients[config.id])

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == task_id
        assert db.get(GenerationTask, task_id).execution_account_id == first_account_id
        task_worker.process_task(db, task_id)
        task = db.get(GenerationTask, task_id)
        assert task.status == TaskStatus.PENDING.value
        assert task.runninghub_task_id is None
        assert task.execution_account_id is None
        assert task.runninghub_attempts[-1].error_code == (
            "TASK_CREATE_FAILED_BY_NOT_ENOUGH_POWER_VALUE"
        )
        assert task.runninghub_attempts[-1].execution_account_id == first_account_id
        task.runninghub_auto_retry_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        assert task_worker.claim_next_pending_task(db) == task_id
        assert db.get(GenerationTask, task_id).execution_account_id == second_account_id
        task_worker.process_task(db, task_id)
        task = db.get(GenerationTask, task_id)
        assert task.status == TaskStatus.SUBMITTED.value
        assert task.runninghub_task_id == "h3-remote-task-001"
        assert [attempt.execution_account_id for attempt in task.runninghub_attempts] == [
            first_account_id,
            second_account_id,
        ]
        assert first.submissions == 1
        assert second.submissions == 1


def test_h3_remote_cancel_uses_h3_workflow_id(client, monkeypatch) -> None:
    _prepare_confirmed_h3_task(client, monkeypatch, "h3-cancel-runtime")
    fake_submit = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake_submit)
    with SessionLocal() as db:
        task_id = task_worker.claim_next_pending_task(db)
        assert task_id is not None
        task_worker.process_task(db, task_id)

    captured: dict[str, object] = {}

    class _CancelClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def cancel_task(self, remote_task_id: str) -> None:
            captured["remote_task_id"] = remote_task_id

    monkeypatch.setattr(
        "app.services.task_cancellation.RunningHubClient",
        _CancelClient,
    )
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        cancel_generation_task(db, task)
        db.commit()
        assert task.status == TaskStatus.CANCELLED.value
    assert captured["ai_app_id"] == "h3-raw-workflow-test"
    assert captured["submission_type"] == "raw-workflow"
    assert captured["remote_task_id"] == "h3-remote-task-001"


def test_h3_soft_chain_preserves_full_av_then_unlocks_next_segment_with_last_slot_anchor(
    client,
    monkeypatch,
) -> None:
    manual_prompt = "Manual soft-chain H3 prompt used unchanged for every segment."
    batch_id = _prepare_confirmed_h3_task(
        client,
        monkeypatch,
        "h3-soft-chain-runtime",
        continuity_mode="soft_chain",
        prompt_override=manual_prompt,
    )
    fake = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)

    def fake_postprocess(
        source: Path,
        *,
        script_text: str,
        alignment_provider: object,
        needs_continuity_anchor: bool,
    ) -> H3NormalizedResult:
        assert script_text
        assert alignment_provider is None
        assert needs_continuity_anchor is True
        video = source.with_name("normalized.mp4")
        anchor = source.with_name("last-visible.png")
        video.write_bytes(b"generated-audio-video")
        anchor.write_bytes(b"last-visible-frame")
        return H3NormalizedResult(
            video_path=video,
            video_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
            anchor_path=anchor,
            anchor_sha256=hashlib.sha256(anchor.read_bytes()).hexdigest(),
            head_trim=H3HeadTrimDecision(
                mode="disabled",
                trim_seconds=0.0,
                first_script_token_start_seconds=None,
                alignment_provider=None,
                alignment_match_ratio=None,
                matched_prefix_tokens=0,
                fallback_reason="feature_disabled",
            ),
            normalized_duration_seconds=5.7,
        )

    monkeypatch.setattr(task_worker, "postprocess_h3_result", fake_postprocess)
    with SessionLocal() as db:
        first_task_id = task_worker.claim_next_pending_task(db)
        assert first_task_id is not None
        task_worker.process_task(db, first_task_id)
        task_worker.process_task(db, first_task_id)
        tasks = (
            db.query(GenerationTask)
            .filter_by(workflow_type="minimax_h3_ref2va")
            .order_by(GenerationTask.created_at)
            .all()
        )
        assert len(tasks) == 2
        first, second = tasks
        assert first.status == TaskStatus.SUCCESS.value
        assert first.prompt == manual_prompt
        assert first.result_path.endswith("normalized.mp4")
        metadata = json.loads(first.output_metadata)
        assert metadata["output_contract_version"] == (
            "h3.output.generated-av-head-trim-disabled.v4"
        )
        assert metadata["provider_audio_preserved"] is True
        assert metadata["provider_audio_head_trimmed"] is False
        assert metadata["provider_duration_preserved"] is True
        assert metadata["head_trim"]["enabled"] is False
        assert metadata["head_trim"]["mode"] == "disabled"
        assert metadata["head_trim"]["trim_seconds"] == 0
        assert metadata["speech_timeline_duration_seconds"] == pytest.approx(
            first.audio_duration_seconds
        )
        assert metadata["normalized_timeline_duration_seconds"] == pytest.approx(5.7)
        assert second.status == TaskStatus.PENDING.value
        assert second.prompt == manual_prompt
        second_payload = json.loads(second.input_payload)
        assert second_payload["assets"]["continuity_anchor"]["path"].endswith(
            "last-visible.png"
        )
        assert second_payload["parameters"]["has_continuity_anchor"] is True
        assert second.segment.h3_config.continuity_anchor_sha256 == hashlib.sha256(
            b"last-visible-frame"
        ).hexdigest()

        assert task_worker.claim_next_pending_task(db) == second.id
        task_worker.process_task(db, second.id)
        graph = json.loads(fake.last_payload["workflow"])
        assert graph["83"]["inputs"]["text"] == manual_prompt
        assert graph["97"]["inputs"]["image"].startswith("openapi/")
        assert graph["178"]["inputs"]["image"].endswith("last-visible.png")
        assert graph["108"]["inputs"]["ref_images.ref_image_5"] == ["176", 0]
        assert "ref_images.ref_image_1" not in graph["108"]["inputs"]

    token = _token(client, "h3-soft-chain-runtime")
    status = client.post(
        f"/api/workbench/h3-batches/{batch_id}",
        json={"access_token": token},
    )
    item_payload = status.json()["items"][0]
    assert item_payload["segments"][0]["head_trim"]["trim_seconds"] == 0
    assert item_payload["segments"][0][
        "normalized_timeline_duration_seconds"
    ] == pytest.approx(metadata["normalized_timeline_duration_seconds"])
    first_url = item_payload["segments"][0]["normalized_video_download_url"]
    assert first_url.endswith("/video")
    assert item_payload["segments"][0]["normalized_video_sha256"] == hashlib.sha256(
        b"generated-audio-video"
    ).hexdigest()
    assert item_payload["segments"][0]["completed_at"]
    downloaded = client.get(
        first_url,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"generated-audio-video"
    cues = client.get(
        item_payload["raw_cues_download_url"],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert cues.status_code == 200
    assert "真正的优势" in cues.text


def test_h3_postprocess_failure_preserves_frozen_sources_and_keeps_chain_blocked(
    client,
    monkeypatch,
) -> None:
    _prepare_confirmed_h3_task(
        client,
        monkeypatch,
        "h3-postprocess-failure",
        continuity_mode="soft_chain",
    )
    fake = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)

    with SessionLocal() as db:
        tasks = (
            db.query(GenerationTask)
            .filter_by(workflow_type="minimax_h3_ref2va")
            .order_by(GenerationTask.created_at)
            .all()
        )
        assert len(tasks) == 1
        first = tasks[0]
        first_segment, second_segment = first.segment.batch_item.segments
        assert second_segment.generation_task is None
        item_config = first.segment.batch_item.h3_config
        frozen_relative_paths = [
            item_config.reference_video_path,
            item_config.full_audio_path,
            item_config.raw_cues_path,
            first.segment.audio_path,
        ]
        frozen_sources = {
            relative_path: (get_settings().data_dir / relative_path).read_bytes()
            for relative_path in frozen_relative_paths
        }
        first_id = first.id
        second_segment_id = second_segment.id

    def fail_postprocess(
        source: Path,
        *,
        script_text: str,
        alignment_provider: object,
        needs_continuity_anchor: bool,
    ) -> H3NormalizedResult:
        assert script_text
        assert alignment_provider is None
        assert source.read_bytes() == b"h3-provider-video"
        assert needs_continuity_anchor is True
        raise H3PostprocessError("injected H3 mux failure")

    monkeypatch.setattr(task_worker, "postprocess_h3_result", fail_postprocess)
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == first_id
        task_worker.process_task(db, first_id)
        task_worker.process_task(db, first_id)
        db.expire_all()

        first = db.get(GenerationTask, first_id)
        second_segment = next(
            candidate
            for candidate in first.segment.batch_item.segments
            if candidate.id == second_segment_id
        )
        assert first.status == TaskStatus.DOWNLOAD_FAILED.value
        assert first.error_code == "H3_POSTPROCESS_FAILED"
        assert first.result_path is None
        assert first.segment.video_path is None
        assert first.segment.h3_config.normalized_video_path is None
        assert second_segment.status == "WAITING_DEPENDENCY"
        assert second_segment.generation_task is None
        assert fake.submissions == 1
        assert task_worker.claim_next_pending_task(db) is None

        for relative_path, expected_bytes in frozen_sources.items():
            assert (get_settings().data_dir / relative_path).read_bytes() == expected_bytes


def test_h3_successful_segment_regeneration_requires_quote_and_invalidates_chain(
    client,
    monkeypatch,
) -> None:
    username = "h3-regeneration-runtime"
    batch_id = _prepare_confirmed_h3_task(
        client,
        monkeypatch,
        username,
        continuity_mode="soft_chain",
    )
    fake = _FakeH3RunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    postprocess_count = 0

    def fake_postprocess(
        source: Path,
        *,
        script_text: str,
        alignment_provider: object,
        needs_continuity_anchor: bool,
    ) -> H3NormalizedResult:
        assert script_text
        assert alignment_provider is None
        nonlocal postprocess_count
        postprocess_count += 1
        video = source.with_name("normalized.mp4")
        video.write_bytes(f"normalized-{postprocess_count}".encode())
        anchor = source.with_name("last-visible.png") if needs_continuity_anchor else None
        if anchor is not None:
            anchor.write_bytes(f"anchor-{postprocess_count}".encode())
        return H3NormalizedResult(
            video_path=video,
            video_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
            anchor_path=anchor,
            anchor_sha256=(
                hashlib.sha256(anchor.read_bytes()).hexdigest()
                if anchor is not None
                else None
            ),
            head_trim=H3HeadTrimDecision(
                mode="disabled",
                trim_seconds=0.0,
                first_script_token_start_seconds=None,
                alignment_provider=None,
                alignment_match_ratio=None,
                matched_prefix_tokens=0,
                fallback_reason="feature_disabled",
            ),
            normalized_duration_seconds=5.8,
        )

    monkeypatch.setattr(task_worker, "postprocess_h3_result", fake_postprocess)
    with SessionLocal() as db:
        first_id = task_worker.claim_next_pending_task(db)
        task_worker.process_task(db, first_id)
        task_worker.process_task(db, first_id)
        second_id = task_worker.claim_next_pending_task(db)
        assert second_id is not None and second_id != first_id
        task_worker.process_task(db, second_id)
        task_worker.process_task(db, second_id)
        db.expire_all()
        tasks = (
            db.query(GenerationTask)
            .filter_by(workflow_type="minimax_h3_ref2va")
            .order_by(GenerationTask.created_at)
            .all()
        )
        assert [task.status for task in tasks] == ["SUCCESS", "SUCCESS"]
        target_segment_id = tasks[0].segment_id
        old_normalized = get_settings().data_dir / tasks[0].result_path
        assert old_normalized.read_bytes() == b"normalized-1"

    token = _token(client, username)
    preview = client.post(
        f"/api/workbench/h3-segments/{target_segment_id}/regeneration/prepare",
        json={"access_token": token},
    )
    assert preview.status_code == 200, preview.text
    quote = preview.json()
    assert quote["cascade_required"] is True
    assert quote["affected_segment_indexes"] == [0, 1]
    assert quote["estimated_paid_calls"] == 2

    rejected = client.post(
        f"/api/workbench/h3-segments/{target_segment_id}/regeneration/confirm",
        json={
            "access_token": token,
            "request_key": "regen-001",
            "quote_token": quote["quote_token"],
            "cost_confirmed": False,
        },
    )
    assert rejected.status_code == 409

    confirmed = client.post(
        f"/api/workbench/h3-segments/{target_segment_id}/regeneration/confirm",
        json={
            "access_token": token,
            "request_key": "regen-001",
            "quote_token": quote["quote_token"],
            "cost_confirmed": True,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["regeneration"]["affected_segment_ids"] == quote[
        "affected_segment_ids"
    ]
    segments = confirmed.json()["items"][0]["segments"]
    assert [segment["status"] for segment in segments] == [
        "PENDING",
        "WAITING_REGENERATION_DEPENDENCY",
    ]
    assert all(segment["normalized_video_download_url"] is None for segment in segments)

    repeated = client.post(
        f"/api/workbench/h3-segments/{target_segment_id}/regeneration/confirm",
        json={
            "access_token": token,
            "request_key": "regen-001",
            "quote_token": quote["quote_token"],
            "cost_confirmed": True,
        },
    )
    assert repeated.status_code == 200, repeated.text

    with SessionLocal() as db:
        target = db.query(GenerationTask).filter_by(segment_id=target_segment_id).one()
        payload = json.loads(target.input_payload)
        history_path = (
            get_settings().data_dir
            / payload["_h3_regeneration_history"][0]["normalized_video_path"]
        )
        assert history_path.read_bytes() == b"normalized-1"
        assert target.status == "PENDING"
        assert target.segment.h3_config.dynamic_workflow_sha256 is None
        first_regenerated_id = task_worker.claim_next_pending_task(db)
        assert first_regenerated_id == target.id
        task_worker.process_task(db, target.id)
        task_worker.process_task(db, target.id)
        db.expire_all()
        tasks = (
            db.query(GenerationTask)
            .filter_by(workflow_type="minimax_h3_ref2va")
            .order_by(GenerationTask.segment_id)
            .all()
        )
        by_index = {task.segment.segment_index: task for task in tasks}
        assert by_index[0].status == "SUCCESS"
        assert by_index[0].segment.h3_config.invalidated_at is None
        assert by_index[1].status == "PENDING"
        assert by_index[1].segment.h3_config.invalidated_at is not None
        next_payload = json.loads(by_index[1].input_payload)
        assert next_payload["assets"]["continuity_anchor"]["path"].endswith(
            "last-visible.png"
        )
        assert next_payload["_h3_manual_regeneration"]["request_key"] == "regen-001"


def test_h3_manual_retry_distinguishes_paid_resubmit_from_free_redownload(
    client,
    monkeypatch,
) -> None:
    username = "h3-manual-retry"
    _prepare_confirmed_h3_task(client, monkeypatch, username, continuity_mode="fast")
    token = _token(client, username)
    with SessionLocal() as db:
        tasks = (
            db.query(GenerationTask)
            .filter_by(workflow_type="minimax_h3_ref2va")
            .order_by(GenerationTask.segment_id)
            .all()
        )
        assert len(tasks) == 2
        paid_task, download_task = tasks
        paid_task.status = TaskStatus.FAILED.value
        paid_task.segment.status = TaskStatus.FAILED.value
        paid_task.error_code = "RUNNINGHUB_FAILED"
        paid_task.segment.h3_config.dynamic_workflow_sha256 = "a" * 64
        download_task.status = TaskStatus.DOWNLOAD_FAILED.value
        download_task.segment.status = TaskStatus.DOWNLOAD_FAILED.value
        download_task.runninghub_task_id = "remote-download-ready"
        download_task.error_code = "DOWNLOAD_FAILED"
        download_task.segment.h3_config.dynamic_workflow_sha256 = "b" * 64
        paid_segment_id = paid_task.segment_id
        download_segment_id = download_task.segment_id
        db.commit()

    paid_preview = client.post(
        f"/api/workbench/h3-segments/{paid_segment_id}/retry/prepare",
        json={"access_token": token},
    )
    assert paid_preview.status_code == 200, paid_preview.text
    assert paid_preview.json()["retry_scope"] == "provider_resubmit"
    assert paid_preview.json()["estimated_paid_calls"] == 1
    paid_rejected = client.post(
        f"/api/workbench/h3-segments/{paid_segment_id}/retry/confirm",
        json={
            "access_token": token,
            "request_key": "paid-retry-1",
            "quote_token": paid_preview.json()["quote_token"],
            "cost_confirmed": False,
        },
    )
    assert paid_rejected.status_code == 400
    paid_confirmed = client.post(
        f"/api/workbench/h3-segments/{paid_segment_id}/retry/confirm",
        json={
            "access_token": token,
            "request_key": "paid-retry-1",
            "quote_token": paid_preview.json()["quote_token"],
            "cost_confirmed": True,
        },
    )
    assert paid_confirmed.status_code == 200, paid_confirmed.text
    assert paid_confirmed.json()["retry"]["estimated_paid_calls"] == 1

    download_preview = client.post(
        f"/api/workbench/h3-segments/{download_segment_id}/retry/prepare",
        json={"access_token": token},
    )
    assert download_preview.status_code == 200, download_preview.text
    assert download_preview.json()["retry_scope"] == "download_only"
    assert download_preview.json()["estimated_paid_calls"] == 0
    download_confirmed = client.post(
        f"/api/workbench/h3-segments/{download_segment_id}/retry/confirm",
        json={
            "access_token": token,
            "request_key": "download-retry-1",
            "quote_token": download_preview.json()["quote_token"],
            "cost_confirmed": False,
        },
    )
    assert download_confirmed.status_code == 200, download_confirmed.text
    assert download_confirmed.json()["retry"]["estimated_paid_calls"] == 0

    with SessionLocal() as db:
        paid = db.query(GenerationTask).filter_by(segment_id=paid_segment_id).one()
        download = db.query(GenerationTask).filter_by(segment_id=download_segment_id).one()
        assert paid.status == TaskStatus.PENDING.value
        assert paid.runninghub_task_id is None
        assert paid.segment.h3_config.dynamic_workflow_sha256 is None
        assert download.status == TaskStatus.RUNNING.value
        assert download.runninghub_task_id == "remote-download-ready"
        assert download.segment.h3_config.dynamic_workflow_sha256 == "b" * 64


def test_h3_pending_segment_cancel_is_idempotent_and_becomes_retryable(
    client,
    monkeypatch,
) -> None:
    username = "h3-manual-cancel"
    _prepare_confirmed_h3_task(client, monkeypatch, username, continuity_mode="fast")
    token = _token(client, username)
    with SessionLocal() as db:
        task = (
            db.query(GenerationTask)
            .filter_by(workflow_type="minimax_h3_ref2va")
            .order_by(GenerationTask.segment_id)
            .first()
        )
        segment_id = task.segment_id

    cancelled = client.post(
        f"/api/workbench/h3-segments/{segment_id}/cancel",
        json={"access_token": token, "request_key": "cancel-001"},
    )
    assert cancelled.status_code == 200, cancelled.text
    segment = next(
        value
        for value in cancelled.json()["items"][0]["segments"]
        if value["segment_id"] == segment_id
    )
    assert segment["status"] == TaskStatus.CANCELLED.value
    assert segment["can_retry"] is True
    assert segment["can_cancel"] is False

    repeated = client.post(
        f"/api/workbench/h3-segments/{segment_id}/cancel",
        json={"access_token": token, "request_key": "cancel-001"},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["cancellation"]["request_key"] == "cancel-001"

    with SessionLocal() as db:
        other = (
            db.query(GenerationTask)
            .filter(
                GenerationTask.workflow_type == "minimax_h3_ref2va",
                GenerationTask.segment_id != segment_id,
            )
            .one()
        )
        other.status = TaskStatus.SUCCESS.value
        task_worker.sync_h3_task_hierarchy(other)
        db.commit()
        assert other.segment.batch_item.status == "FAILED"
        assert other.segment.batch_item.batch.status == "FAILED"

    retry_preview = client.post(
        f"/api/workbench/h3-segments/{segment_id}/retry/prepare",
        json={"access_token": token},
    )
    assert retry_preview.status_code == 200, retry_preview.text
    assert retry_preview.json()["estimated_paid_calls"] == 1
