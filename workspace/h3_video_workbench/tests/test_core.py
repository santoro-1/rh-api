from __future__ import annotations

from pathlib import Path

import pytest

from h3_workbench.contracts import parse_project_input
from h3_workbench.postprocess import build_jyd_handoff, visible_tail_plan
from h3_workbench import postprocess as postprocess_module
from h3_workbench.scheduling import (
    H3SegmentJob,
    invalidate_downstream,
    ready_jobs,
)
from h3_workbench.segmentation import RawCue, plan_segments


def test_project_contract_keeps_images_batch_shared_and_video_per_item() -> None:
    project = parse_project_input(
        {
            "project_id": "p1",
            "batch_identity_images": ["person-front.png", "person-side.png"],
            "defaults": {
                "continuity_mode": "fast",
                "resolution": {
                    "aspect_ratio": "9:16 (Portrait Widescreen)",
                    "megapixels": 1,
                    "multiple": 32,
                },
            },
            "items": [
                {
                    "item_id": "row-1",
                    "script_text": "第一条。",
                    "reference_video": "ref-a.mp4",
                    "full_audio": "a.mp3",
                    "raw_cues": [{"text": "第一条。", "start_us": 0, "end_us": 1}],
                },
                {
                    "item_id": "row-2",
                    "script_text": "第二条。",
                    "reference_video": "ref-b.mp4",
                    "full_audio": "b.mp3",
                    "raw_cues": [{"text": "第二条。", "start_us": 0, "end_us": 1}],
                },
            ],
        }
    )

    assert project.batch_identity_images == ("person-front.png", "person-side.png")
    assert [item.reference_video for item in project.items] == ["ref-a.mp4", "ref-b.mp4"]
    assert project.resolution.multiple == 32


def test_project_contract_rejects_six_user_images() -> None:
    with pytest.raises(ValueError, match="最多 5 个"):
        parse_project_input(
            {
                "project_id": "p1",
                "batch_identity_images": [f"{index}.png" for index in range(6)],
                "items": [],
            }
        )


def _cues() -> list[RawCue]:
    return [
        RawCue("第一句。", 0, 4_000_000),
        RawCue("第二句，", 4_000_000, 8_000_000),
        RawCue("继续说明。", 8_000_000, 12_000_000),
        RawCue("最后一句。", 12_000_000, 20_000_000),
    ]


def test_segmentation_balances_globally_and_never_exceeds_h3_request_window() -> None:
    segments = plan_segments(
        "第一句。第二句，继续说明。最后一句。",
        _cues(),
        audio_duration_us=20_000_000,
    )

    assert [(segment.start_us, segment.end_us) for segment in segments] == [
        (0, 12_000_000),
        (12_000_000, 20_000_000),
    ]
    assert "".join(segment.text for segment in segments) == "第一句。第二句，继续说明。最后一句。"
    assert all(4 <= segment.requested_generation_duration_seconds <= 15 for segment in segments)


def test_segmentation_rejects_raw_cues_that_do_not_bind_to_script() -> None:
    with pytest.raises(ValueError, match="与冻结原稿不一致"):
        plan_segments("另一份原稿。", _cues(), audio_duration_us=20_000_000)


def test_fast_runs_all_pending_segments_but_soft_chain_advances_one_wave() -> None:
    jobs = [
        H3SegmentJob("a0", "a", 0, "fast"),
        H3SegmentJob("a1", "a", 1, "fast"),
        H3SegmentJob("b0", "b", 0, "soft_chain", status="SUCCESS"),
        H3SegmentJob("b1", "b", 1, "soft_chain", continuity_anchor="anchor-b0.png"),
        H3SegmentJob("b2", "b", 2, "soft_chain"),
    ]

    assert [job.segment_id for job in ready_jobs(jobs)] == ["a0", "a1", "b1"]


def test_regenerating_soft_chain_segment_invalidates_only_downstream_records() -> None:
    jobs = [
        H3SegmentJob("a0", "a", 0, "soft_chain", status="SUCCESS"),
        H3SegmentJob("a1", "a", 1, "soft_chain", status="SUCCESS", continuity_anchor="0.png"),
        H3SegmentJob("a2", "a", 2, "soft_chain", status="SUCCESS", continuity_anchor="1.png"),
    ]

    updated = invalidate_downstream(jobs, item_id="a", regenerated_segment_index=1)
    assert updated[0] == jobs[0]
    assert updated[1] == jobs[1]
    assert updated[2].status == "INVALIDATED"
    assert updated[2].continuity_anchor is None


def test_generated_result_is_preserved_and_anchor_uses_its_final_frame() -> None:
    plan = visible_tail_plan(8.0)

    assert plan.trim_start_seconds == 0
    assert plan.trim_end_seconds == 8
    assert plan.anchor_frame_seconds == pytest.approx(8 - 1 / 24)


def test_jyd_handoff_reuses_h3_generated_audio_once_in_split_track_layout() -> None:
    handoff = build_jyd_handoff(
        project_id="p1",
        silent_base_video="merged-silent.mp4",
        authoritative_h3_audio="h3-full.wav",
        h3_segment_cues_asset="h3-cue-windows.json",
        h3_master_av="h3-master-av.mp4",
        segment_ids=["s0", "s1"],
        row_key="ROW-001",
        script_text="第一句。第二句。",
    )

    assert handoff["schema_version"] == "h3.jyd_handoff.v2"
    assert handoff["h3_master"]["audio_video_pair"] == "h3_generated"
    assert handoff["base_video"]["audio_policy"] == "separate_h3_generated_audio"
    assert handoff["authoritative_audio"]["source"] == "h3_generated_audio"
    assert handoff["authoritative_audio"]["timeline_start_seconds"] == 0
    assert handoff["authoritative_audio"]["reuse_once"] is True
    assert handoff["subtitles"]["timing_source"] == "h3_segment_windows_then_funasr"
    assert len(handoff["handoff_id"]) == 64
    assert handoff["handoff_id"] == build_jyd_handoff(
        project_id="p1",
        silent_base_video="another-local-copy.mp4",
        authoritative_h3_audio="another-local-copy.wav",
        h3_segment_cues_asset="another-h3-cues.json",
        h3_master_av="another-master.mp4",
        segment_ids=["s0", "s1"],
        row_key="ROW-001",
        script_text="第一句。第二句。",
    )["handoff_id"]


def test_merge_master_preserves_h3_generated_audio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    segments = [tmp_path / "one.mp4", tmp_path / "two.mp4"]
    for path in segments:
        path.write_bytes(b"h3-generated-av-segment")
    observed: list[list[str]] = []

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        del kwargs
        observed.append(command)
        Path(command[-1]).write_bytes(b"merged-h3-av")
        return Completed()

    monkeypatch.setattr(postprocess_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        postprocess_module,
        "probe_av_stream_starts",
        lambda path: (0.031006, 0.0),
    )
    target = postprocess_module.merge_generated_av_segments(
        segments,
        tmp_path / "base.mp4",
    )

    assert target.read_bytes() == b"merged-h3-av"
    assert len(observed) == 2
    assert all("-an" not in command for command in observed)
    assert all("-c" in command and "copy" in command for command in observed)
    assert "0:a:0" in observed[0]
    assert observed[0][observed[0].index("-f") + 1] == "concat"
    assert observed[1][observed[1].index("-itsoffset") + 1] == "-0.031006000"
    assert observed[1].count("-itsoffset") == 2
    assert "1:a:0" in observed[1]


def test_h3_segment_cues_follow_generated_segment_durations() -> None:
    cues = postprocess_module.build_h3_segment_cues(
        ["第一句。", "第二句。"],
        [5.875, 4.458333],
        script_text="第一句。第二句。",
    )

    assert cues[0]["start_us"] == 0
    assert cues[0]["end_us"] == 5_875_000
    assert cues[1]["start_us"] == 5_875_000
    assert cues[1]["end_us"] == 10_333_333
