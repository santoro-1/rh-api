from __future__ import annotations

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "apps" / "processor" / "frontend" / "new" / "index.html"


class NewMusicCaptureFrontendTest(unittest.TestCase):
    def test_new_workbench_exposes_music_collection_entry(self) -> None:
        page = FRONTEND.read_text(encoding="utf-8")

        self.assertIn("音乐采集", page)
        self.assertIn('id="music-capture-modal"', page)
        self.assertIn("openMusicCaptureModal()", page)
        self.assertIn("/api/drafts/collect-personal-assets", page)
        self.assertIn("kinds: ['audio']", page)
        self.assertIn("upload: true", page)
        self.assertIn("server_url: window.location.origin", page)
        self.assertIn("await loadPostprocessOptions();", page)
        self.assertIn("updateTableLayout();", page)
        self.assertIn("暂不参与自动选歌", page)


if __name__ == "__main__":
    unittest.main()
