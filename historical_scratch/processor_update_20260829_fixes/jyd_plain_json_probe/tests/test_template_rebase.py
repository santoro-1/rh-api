from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.template_library import rebase_template_library_paths


class TemplateLibraryRebaseTest(unittest.TestCase):
    def test_rebases_plain_and_embedded_json_asset_paths(self) -> None:
        temporary = PROJECT_ROOT / "runtime" / "test_tmp" / f"template_rebase_{uuid.uuid4().hex}"
        try:
            root = temporary / "templates"
            record = root / "demo"
            draft = record / "draft"
            asset = record / "assets" / "video" / "clip.mp4"
            draft.mkdir(parents=True)
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"video")
            old_path = r"D:\old\template_library\demo\assets\video\clip.mp4"
            data = {
                "path": old_path,
                "content": json.dumps({"nested_path": old_path}, ensure_ascii=False),
            }
            (draft / "draft_content.json").write_text(
                json.dumps(data, ensure_ascii=False),
                encoding="utf-8",
            )
            original_user_path = r"C:\Users\editor\Downloads\clip.mp4"
            (draft / "draft_meta_info.json").write_text(
                json.dumps({"file_Path": original_user_path}, ensure_ascii=False),
                encoding="utf-8",
            )
            (record / "transfer_manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                "archive_path": "assets/video/clip.mp4",
                                "source_path": original_user_path,
                                "original_path": original_user_path.replace("\\", "/"),
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = rebase_template_library_paths(root)

            self.assertEqual(result["templates"], 1)
            self.assertEqual(result["files"], 2)
            self.assertEqual(result["paths"], 3)
            rewritten = json.loads((draft / "draft_content.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(rewritten["path"]), asset.resolve())
            self.assertEqual(Path(json.loads(rewritten["content"])["nested_path"]), asset.resolve())
            rewritten_meta = json.loads((draft / "draft_meta_info.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(rewritten_meta["file_Path"]), asset.resolve())
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
