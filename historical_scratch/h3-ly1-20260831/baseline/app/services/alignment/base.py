from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.services.alignment.script_timestamps import AlignedScriptToken
from app.services.media_segmentation import SegmentPlan


@dataclass(frozen=True)
class AlignmentResult:
    """Provider-neutral timeline consumed by the shared segment planner."""

    provider: str
    plans: tuple[SegmentPlan, ...]
    tokens: tuple[AlignedScriptToken, ...] = ()
    match_ratio: float | None = None


class AudioAlignmentProvider(Protocol):
    """Pluggable boundary provider for uploaded long-form speech."""

    name: str

    def align(self, audio_path: Path, script: str) -> AlignmentResult:
        """Return ordered, gap-free segments carrying original script text."""
