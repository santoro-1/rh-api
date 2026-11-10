from __future__ import annotations

import os
import subprocess


def hidden_creation_flags() -> int:
    """Prevent ffmpeg and ffprobe from opening console windows on Windows."""

    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
