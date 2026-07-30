from __future__ import annotations

from pathlib import Path

from app.services.alignment.base import AlignmentResult
from app.services.media_segmentation import build_segment_plan


class HeuristicAlignmentProvider:
    """Fast first version using script punctuation and measured silence."""

    name = "heuristic"

    def align(self, audio_path: Path, script: str) -> AlignmentResult:
        return AlignmentResult(
            provider=self.name,
            plans=tuple(build_segment_plan(audio_path, script)),
        )

