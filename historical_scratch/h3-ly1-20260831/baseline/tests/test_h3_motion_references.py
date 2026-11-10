from __future__ import annotations

from pathlib import Path

import pytest

from app.services.h3 import motion_references
from app.services.h3.motion_references import (
    H3MotionReference,
    assign_h3_motion_references,
    split_h3_motion_reference,
)


@pytest.mark.parametrize(
    ("duration", "expected_ranges"),
    [
        (
            15.0,
            [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0), (9.0, 12.0), (12.0, 15.0)],
        ),
        (
            16.0,
            [
                (0.0, 3.0),
                (3.0, 6.0),
                (6.0, 9.0),
                (9.0, 12.0),
                (12.0, 15.0),
                (15.0, 16.0),
            ],
        ),
        (
            15.4,
            [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0), (9.0, 12.0), (12.0, 15.4)],
        ),
        (3.49, [(0.0, 3.49)]),
        (3.5, [(0.0, 3.0), (3.0, 3.5)]),
        (2.25, [(0.0, 2.25)]),
        (6.0, [(0.0, 3.0), (3.0, 6.0)]),
    ],
)
def test_split_h3_motion_reference_merges_a_tail_shorter_than_half_a_second(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duration: float,
    expected_ranges: list[tuple[float, float]],
) -> None:
    source = tmp_path / "reference.mp4"
    source.write_bytes(b"source-video")
    cuts: list[tuple[float, float]] = []

    monkeypatch.setattr(
        motion_references, "inspect_media_duration", lambda _path: duration
    )

    def fake_cut(
        _source: Path,
        target: Path,
        *,
        start_seconds: float,
        duration_seconds: float,
    ) -> None:
        cuts.append((start_seconds, start_seconds + duration_seconds))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"visual-only-{start_seconds}-{duration_seconds}".encode())

    monkeypatch.setattr(motion_references, "cut_video_segment", fake_cut)

    clips = split_h3_motion_reference(source, tmp_path / "clips")

    assert cuts == expected_ranges
    assert [(clip.start_seconds, clip.end_seconds) for clip in clips] == expected_ranges
    assert [clip.path.name for clip in clips] == [
        f"motion-{index + 1:03d}.mp4" for index in range(len(expected_ranges))
    ]
    assert all(len(clip.sha256) == 64 for clip in clips)


def test_assign_h3_motion_references_is_balanced_deterministic_and_non_adjacent(
    tmp_path: Path,
) -> None:
    clips = [
        H3MotionReference(
            index=index,
            start_seconds=index * 3.0,
            end_seconds=(index + 1) * 3.0,
            path=tmp_path / f"motion-{index + 1:03d}.mp4",
            sha256=f"{index + 1:064x}",
        )
        for index in range(5)
    ]

    first = assign_h3_motion_references(clips, 12, seed_material="stable-input")
    second = assign_h3_motion_references(clips, 12, seed_material="stable-input")
    indices = [clip.index for clip in first]

    assert indices == [clip.index for clip in second]
    assert all(left != right for left, right in zip(indices, indices[1:]))
    counts = [indices.count(index) for index in range(5)]
    assert max(counts) - min(counts) <= 1
    assert set(indices[:5]) == set(range(5))
    assert set(indices[5:10]) == set(range(5))


def test_assign_h3_motion_references_reuses_the_only_clip(tmp_path: Path) -> None:
    clip = H3MotionReference(0, 0.0, 1.0, tmp_path / "motion-001.mp4", "a" * 64)

    assert assign_h3_motion_references(
        [clip], 4, seed_material="one-clip"
    ) == [clip, clip, clip, clip]
