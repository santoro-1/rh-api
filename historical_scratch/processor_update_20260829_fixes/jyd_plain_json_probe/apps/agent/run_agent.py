from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.render_agent import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
