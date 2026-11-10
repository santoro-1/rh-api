from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jyd_probe.render_job import run_render_job


class ExistingDraftExportTest(unittest.TestCase):
    def test_existing_draft_exports_without_rebuilding_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            draft_dir = root / "ready-draft"
            draft_dir.mkdir()
            (draft_dir / "draft_content.json").write_text("{}", encoding="utf-8")
            output = root / "composition.mp4"

            with patch("jyd_probe.render_job._export_mp4") as export_mp4:
                result = run_render_job(
                    {
                        "schema": "jyd.render_job.v1",
                        "source": {
                            "type": "existing_draft",
                            "draft_dir": str(draft_dir),
                            "draft_name": "ready-draft",
                        },
                        "output": {"mp4_path": str(output)},
                        "export": {"resolution": "1080P", "framerate": "30fps"},
                    }
                )

            export_mp4.assert_called_once_with(
                "ready-draft",
                output.resolve(),
                resolution="1080P",
                framerate="30fps",
                timeout=1200.0,
            )
            self.assertEqual(result.source_kind, "existing-draft")
            self.assertEqual(result.output_draft_dir, draft_dir.resolve())
            self.assertEqual(result.output_mp4, output.resolve())
            self.assertTrue(result.exported)


if __name__ == "__main__":
    unittest.main()
