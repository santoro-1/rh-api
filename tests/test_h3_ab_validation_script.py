from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.run_h3_ab_validation import (
    _assert_submission_slot,
    _ensure_join_artifact,
    _ensure_zero_cost_retry,
    _load_state,
    _select_video,
    _upload_once,
)


def _plan(limit: int) -> dict:
    return {
        "workflow_id": "workflow-1",
        "user_id": 1,
        "reference_video": {"path": "video.mp4", "sha256": "a" * 64},
        "segment_1": {"path": "one.wav", "sha256": "b" * 64, "duration": 5.23},
        "segment_2": {"path": "two.wav", "sha256": "c" * 64, "duration": 3.52102},
        "script": "原稿",
        "segment_texts": ["第一段", "第二段"],
        "aspect_ratio": "16:9 (Widescreen)",
        "megapixels": 1.0,
        "multiple": 32,
        "generation_tail_seconds": 0.5,
        "instance_type": "plus",
        "seed": 0,
        "authorized_paid_call_limit": limit,
    }


def test_ab_video_selector_requires_node_387_mp4() -> None:
    selected = _select_video(
        {
            "results": [
                {"nodeId": "100", "outputType": "mp4", "url": "https://x/preview.mp4"},
                {"nodeId": "387", "outputType": "mp4", "url": "https://x/final.mp4"},
            ]
        }
    )
    assert selected["url"] == "https://x/final.mp4"

    with pytest.raises(RuntimeError, match="节点 387 的 MP4"):
        _select_video(
            {
                "results": [
                    {"nodeId": "100", "outputType": "mp4", "url": "https://x/preview.mp4"},
                    {"nodeId": "387", "outputType": "mov", "url": "https://x/final.mov"},
                ]
            }
        )


def test_checkpoint_explicitly_migrates_three_to_four_and_adds_replacement(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "version": "h3.ab.real.v1",
                "plan": _plan(3),
                "calls": [
                    {
                        "number": 1,
                        "name": "segment_01_fast",
                        "status": "unrecoverable_no_saved_output",
                    },
                    {"number": 2, "name": "segment_02_fast", "status": "planned"},
                    {"number": 3, "name": "segment_02_soft_chain", "status": "planned"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = _load_state(checkpoint, _plan(4))

    assert state["plan"]["authorized_paid_call_limit"] == 4
    assert state["authorization_updates"][-1]["previous_paid_call_limit"] == 3
    assert state["calls"][-1] == {
        "number": 4,
        "name": "segment_01_fast_replacement",
        "status": "planned",
        "replaces_call_number": 1,
    }
    assert len(_load_state(checkpoint, _plan(4))["calls"]) == 4


def test_checkpoint_never_migrates_an_immutable_plan_change(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    old_plan = _plan(3)
    checkpoint.write_text(
        json.dumps(
            {
                "version": "h3.ab.real.v1",
                "plan": old_plan,
                "calls": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    changed = copy.deepcopy(_plan(4))
    changed["seed"] = 1

    with pytest.raises(RuntimeError, match="计划不一致"):
        _load_state(checkpoint, changed)


def test_checkpoint_allows_only_explicit_adjacent_limit_increase(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    state = {
        "version": "h3.ab.real.v1",
        "plan": _plan(4),
        "calls": [],
    }
    checkpoint.write_text(json.dumps(state), encoding="utf-8")

    migrated = _load_state(checkpoint, _plan(5))

    assert migrated["plan"]["authorized_paid_call_limit"] == 5
    assert migrated["authorization_updates"][-1]["previous_paid_call_limit"] == 4
    with pytest.raises(RuntimeError, match="只能.*增加 1"):
        _load_state(checkpoint, _plan(3))


def test_zero_cost_failure_consumes_one_remaining_authorized_submission(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    state = {
        "version": "h3.ab.real.v1",
        "plan": _plan(4),
        "calls": [
            {"number": 1, "name": "old", "status": "success"},
            {
                "number": 2,
                "name": "segment_02_fast",
                "status": "remote_failed",
                "query_result": {
                    "usage": {
                        "consumeCoins": None,
                        "consumeMoney": None,
                        "thirdPartyConsumeMoney": None,
                        "taskCostTime": "0",
                    }
                },
            },
            {"number": 3, "name": "soft", "status": "planned"},
            {"number": 4, "name": "replacement", "status": "success"},
        ],
    }

    retry = _ensure_zero_cost_retry(
        checkpoint,
        state,
        original_name="segment_02_fast",
        retry_name="segment_02_fast_retry",
    )

    assert retry["number"] == 5
    assert retry["status"] == "planned"
    assert (
        _ensure_zero_cost_retry(
            checkpoint,
            state,
            original_name="segment_02_fast",
            retry_name="segment_02_fast_retry",
        )
        is retry
    )


def test_zero_cost_retry_is_rejected_after_authorized_attempts_are_exhausted(
    tmp_path: Path,
) -> None:
    state = {
        "plan": _plan(3),
        "calls": [
            {"number": 1, "name": "one", "status": "success"},
            {
                "number": 2,
                "name": "segment_02_fast",
                "status": "remote_failed",
                "query_result": {"usage": {"taskCostTime": "0"}},
            },
            {"number": 3, "name": "three", "status": "success"},
        ],
    }

    with pytest.raises(RuntimeError, match="额度已经用完"):
        _ensure_zero_cost_retry(
            tmp_path / "checkpoint.json",
            state,
            original_name="segment_02_fast",
            retry_name="segment_02_fast_retry",
        )


def test_join_artifact_preserves_h3_generated_audio_and_records_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    for path, content in ((first, b"first"), (second, b"second")):
        path.write_bytes(content)
    commands: list[list[str]] = []

    def fake_ffmpeg(command: list[str], message: str) -> None:
        del message
        commands.append(command)
        Path(command[-1]).write_bytes(f"artifact-{len(commands)}".encode())

    monkeypatch.setattr("scripts.run_h3_ab_validation._run_ffmpeg", fake_ffmpeg)
    state: dict = {}
    checkpoint = tmp_path / "checkpoint.json"

    target = _ensure_join_artifact(
        checkpoint,
        state,
        output_dir=tmp_path,
        artifact_name="soft_chain_join",
        first_video=first,
        second_video=second,
    )

    assert target == tmp_path / "soft_chain_join.mp4"
    assert len(commands) == 1
    filter_graph = commands[0][commands[0].index("-filter_complex") + 1]
    assert "[0:v]setpts=PTS-STARTPTS[v0]" in filter_graph
    assert "[0:a]asetpts=PTS-STARTPTS[a0]" in filter_graph
    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]" in filter_graph
    assert commands[0][commands[0].index("-x264-params") + 1] == "bframes=0"
    assert state["artifacts"]["soft_chain_join"]["audio_policy"] == (
        "h3_generated_audio_preserved"
    )
    assert len(state["artifacts"]["soft_chain_join"]["sha256"]) == 64


def test_upload_checkpoint_reuses_fresh_media_and_reuploads_expired_media(
    tmp_path: Path,
) -> None:
    media = tmp_path / "anchor.png"
    media.write_bytes(b"anchor")
    digest = __import__("hashlib").sha256(b"anchor").hexdigest()

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def upload_file(self, path: Path) -> str:
            assert path == media
            self.calls += 1
            return f"openapi/new-{self.calls}.png"

    client = Client()
    checkpoint = tmp_path / "checkpoint.json"
    state = {
        "uploads": {
            "anchor": {
                "path": str(media),
                "sha256": digest,
                "remote_name": "openapi/fresh.png",
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
        }
    }

    assert _upload_once(client, checkpoint, state, "anchor", media) == "openapi/fresh.png"
    assert client.calls == 0
    state["uploads"]["anchor"]["uploaded_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=21)
    ).isoformat()

    assert _upload_once(client, checkpoint, state, "anchor", media) == "openapi/new-1.png"
    assert client.calls == 1
    assert state["upload_history"]["anchor"][0]["remote_name"] == "openapi/fresh.png"


def test_soft_anchor_upload_is_blocked_before_external_io_when_limit_is_full() -> None:
    state = {
        "plan": _plan(4),
        "calls": [
            {"status": "success"},
            {"status": "remote_failed"},
            {"status": "planned", "name": "soft"},
            {"status": "success"},
            {"status": "success"},
        ],
    }

    with pytest.raises(RuntimeError, match="累计 4 次"):
        _assert_submission_slot(state, state["calls"][2])
