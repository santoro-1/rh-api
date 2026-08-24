from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

from app.services.media_segmentation import cut_video_segment, inspect_media_duration


H3_MOTION_CLIP_SECONDS = 3.0
H3_MOTION_ASSIGNMENT_VERSION = "balanced_bag_v1"
_DURATION_EPSILON_SECONDS = 0.001


@dataclass(frozen=True)
class H3MotionReference:
    index: int
    start_seconds: float
    end_seconds: float
    path: Path
    sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_h3_motion_reference(
    source: Path,
    target_dir: Path,
) -> list[H3MotionReference]:
    """Split one uploaded reference into fixed visual-only three-second clips."""

    duration = inspect_media_duration(source)
    clip_count = max(
        1,
        math.ceil((duration - _DURATION_EPSILON_SECONDS) / H3_MOTION_CLIP_SECONDS),
    )
    clips: list[H3MotionReference] = []
    for index in range(clip_count):
        start_seconds = index * H3_MOTION_CLIP_SECONDS
        end_seconds = min(duration, start_seconds + H3_MOTION_CLIP_SECONDS)
        target = target_dir / f"motion-{index + 1:03d}.mp4"
        cut_video_segment(
            source,
            target,
            start_seconds=start_seconds,
            duration_seconds=end_seconds - start_seconds,
        )
        clips.append(
            H3MotionReference(
                index=index,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                path=target,
                sha256=_sha256_file(target),
            )
        )
    return clips


def assign_h3_motion_references(
    clips: list[H3MotionReference],
    segment_count: int,
    *,
    seed_material: str,
) -> list[H3MotionReference]:
    """Assign balanced deterministic bags and avoid adjacent repeats when possible."""

    if not clips:
        raise ValueError("H3 动作参考片段不能为空")
    if segment_count < 1:
        raise ValueError("H3 音频分段数量必须大于 0")
    if len(clips) == 1:
        return [clips[0]] * segment_count

    seed = hashlib.sha256(
        f"{H3_MOTION_ASSIGNMENT_VERSION}\0{seed_material}".encode("utf-8")
    ).hexdigest()
    assigned: list[H3MotionReference] = []
    round_index = 0
    while len(assigned) < segment_count:
        bag = sorted(
            clips,
            key=lambda clip: hashlib.sha256(
                f"{seed}:{round_index}:{clip.index}".encode("ascii")
            ).digest(),
        )
        if assigned and bag[0].index == assigned[-1].index:
            bag[0], bag[1] = bag[1], bag[0]
        assigned.extend(bag)
        round_index += 1
    return assigned[:segment_count]


__all__ = [
    "H3_MOTION_ASSIGNMENT_VERSION",
    "H3_MOTION_CLIP_SECONDS",
    "H3MotionReference",
    "assign_h3_motion_references",
    "split_h3_motion_reference",
]
