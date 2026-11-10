from __future__ import annotations

from pathlib import Path
import sys
import unittest
import os
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.runtime_paths import (  # noqa: E402
    _best_populated_draft_root,
    detect_jianying_draft_root,
)


class RuntimePathsTests(unittest.TestCase):
    def test_configured_draft_root_has_priority(self) -> None:
        configured = PROJECT_ROOT / "runtime" / "test_tmp" / "Custom JianyingPro Drafts"
        self.assertEqual(detect_jianying_draft_root(configured), configured.resolve())

    def test_fallback_is_used_when_no_known_directory_exists(self) -> None:
        fallback = PROJECT_ROOT / "runtime" / "test_tmp" / "fallback"
        detected = detect_jianying_draft_root("", fallback=fallback)
        known = Path(r"D:\剪映草稿\JianyingPro Drafts")
        expected = known.resolve() if known.is_dir() else fallback.resolve()
        self.assertEqual(detected, expected)

    def test_best_populated_root_prefers_recently_used_draft_directory(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp" / "runtime_paths_activity"
        older = root / "C-drive-drafts"
        newer = root / "D-drive-drafts"
        for directory in (older / "old-draft", newer / "new-draft"):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "draft_content.json").write_text("{}", encoding="utf-8")
        now = time.time()
        os.utime(older / "old-draft" / "draft_content.json", (now - 3600, now - 3600))
        os.utime(newer / "new-draft" / "draft_content.json", (now, now))

        self.assertEqual(_best_populated_draft_root([older, newer]), newer.resolve())


if __name__ == "__main__":
    unittest.main()
