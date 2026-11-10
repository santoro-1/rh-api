from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from jyd_probe.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
