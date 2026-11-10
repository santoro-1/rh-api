from __future__ import annotations

from pathlib import Path

from jyd_probe.project_video_source import (
    build_project_video_source,
    project_segment_boundaries,
)


def test_project_segment_boundaries_preserve_next_segment_script_and_time(
    tmp_path: Path,
) -> None:
    first = tmp_path / "segment-1.mp4"
    second = tmp_path / "segment-2.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    item = {
        "outputs": {
            "base_video": {
                "managed_path": str(tmp_path / "base.mp4"),
                "metadata": {"segment_count": 2},
                "external_ref": {"source_task_ids": ["task-1", "task-2"]},
            },
            "original_video_segments": [
                {
                    "asset_id": "segment-1",
                    "status": "READY",
                    "managed_path": str(first),
                    "external_ref": {"video_index": 1, "remote_task_id": "task-1"},
                    "metadata": {
                        "start_seconds": 0.0,
                        "end_seconds": 2.5,
                        "actual_duration_us": 2_200_000,
                        "script_text": "第一句。",
                    },
                },
                {
                    "asset_id": "segment-2",
                    "status": "READY",
                    "managed_path": str(second),
                    "external_ref": {"video_index": 2, "remote_task_id": "task-2"},
                    "metadata": {
                        "start_seconds": 2.5,
                        "end_seconds": 5.0,
                        "actual_duration_us": 2_700_000,
                        "script_text": "第二句吃牛肉。",
                    },
                },
            ],
        }
    }

    assert project_segment_boundaries(item) == [
        {
            "boundary_us": 2_200_000,
            "segment_index": 2,
            "segment_start_us": 2_200_000,
            "segment_end_us": 4_900_000,
            "script_text": "第二句吃牛肉。",
        }
    ]
    source = build_project_video_source(item)
    assert [entry["target_duration_us"] for entry in source["items"]] == [
        2_200_000,
        2_700_000,
    ]


def test_provider_tails_are_trimmed_at_seams_and_kept_on_final_segment(
    tmp_path: Path,
) -> None:
    first = tmp_path / "padded-1.mp4"
    second = tmp_path / "padded-2.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    item = {
        "outputs": {
            "base_video": {
                "managed_path": str(tmp_path / "base.mp4"),
                "metadata": {"segment_count": 2},
                "external_ref": {"source_task_ids": ["task-1", "task-2"]},
            },
            "original_video_segments": [
                {
                    "status": "READY",
                    "managed_path": str(first),
                    "external_ref": {"video_index": 1, "remote_task_id": "task-1"},
                    "metadata": {
                        "start_seconds": 0.0,
                        "end_seconds": 2.5,
                        "speech_duration_seconds": 2.5,
                        "generation_tail_seconds": 2.0,
                        "actual_duration_us": 4_600_000,
                    },
                },
                {
                    "status": "READY",
                    "managed_path": str(second),
                    "external_ref": {"video_index": 2, "remote_task_id": "task-2"},
                    "metadata": {
                        "start_seconds": 2.5,
                        "end_seconds": 5.0,
                        "speech_duration_seconds": 2.5,
                        "generation_tail_seconds": 2.0,
                        "actual_duration_us": 4_700_000,
                    },
                },
            ],
        }
    }

    source = build_project_video_source(item)
    assert [entry["target_duration_us"] for entry in source["items"]] == [
        2_500_000,
        4_500_000,
    ]
    assert project_segment_boundaries(item)[0]["boundary_us"] == 2_500_000


def test_legacy_short_provider_clips_fall_back_to_normalized_base_video(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.mp4"
    first = tmp_path / "legacy-1.mp4"
    second = tmp_path / "legacy-2.mp4"
    base.write_bytes(b"normalized")
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    item = {
        "row_key": "账号9-励志赋能类-5",
        "outputs": {
            "base_video": {
                "managed_path": str(base),
                "metadata": {
                    "segment_count": 2,
                    "duration_us": 5_010_000,
                    "planned_duration_us": 5_000_000,
                    "normalized_to_approved_audio": True,
                },
                "external_ref": {"source_task_ids": ["task-1", "task-2"]},
            },
            "original_video_segments": [
                {
                    "status": "READY",
                    "managed_path": str(first),
                    "external_ref": {"video_index": 1, "remote_task_id": "task-1"},
                    "metadata": {
                        "start_seconds": 0.0,
                        "end_seconds": 2.5,
                        "actual_duration_us": 2_200_000,
                        "script_text": "第一句。",
                    },
                },
                {
                    "status": "READY",
                    "managed_path": str(second),
                    "external_ref": {"video_index": 2, "remote_task_id": "task-2"},
                    "metadata": {
                        "start_seconds": 2.5,
                        "end_seconds": 5.0,
                        "actual_duration_us": 2_700_000,
                        "script_text": "第二句。",
                    },
                },
            ],
        },
    }

    assert build_project_video_source(item) == {
        "type": "video",
        "media_path": str(base.resolve()),
    }
    assert project_segment_boundaries(item) == [
        {
            "boundary_us": 2_500_000,
            "segment_index": 2,
            "segment_start_us": 2_500_000,
            "segment_end_us": 5_000_000,
            "script_text": "第二句。",
        }
    ]
