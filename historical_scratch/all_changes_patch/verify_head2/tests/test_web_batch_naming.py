from __future__ import annotations

from pathlib import Path
import sys
import unittest

from fastapi import HTTPException


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import (  # noqa: E402
    _expand_dimension_batch_payload,
    _reject_mother_composite_text_changes,
)


class WebBatchNamingTest(unittest.TestCase):
    def test_uses_first_two_characters_of_each_selected_element(self) -> None:
        jobs, variants = _expand_dimension_batch_payload(
            {
                "job": {"source": {"type": "video"}, "output": {}},
                "dimensions": [
                    self._dimension("bgm", "起风了（剪辑版）", {"audios": [{}]}),
                    self._dimension("effect", "浪漫氛围", {"effects": [{}]}),
                    self._dimension("style", "抖音美好体", {"styles": [{}]}),
                ],
                "max_jobs": 10,
            }
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(variants[0]["display_name"], "起风+浪漫+抖音")
        self.assertTrue(jobs[0]["output"]["draft_name"].startswith("起风+浪漫+抖音_"))

    def test_results_that_differ_in_one_candidate_are_both_kept(self) -> None:
        _, variants = _expand_dimension_batch_payload(
            {
                "job": {"source": {"type": "video"}, "output": {}},
                "dimensions": [
                    {
                        "key": "bgm",
                        "label": "音乐",
                        "mode": "product",
                        "candidates": [
                            {"id": "1", "label": "蓝色天空", "append": {"audios": [{}]}},
                            {"id": "2", "label": "蓝色海洋", "append": {"audios": [{}]}},
                        ],
                    },
                    {
                        "key": "effect",
                        "label": "特效",
                        "mode": "fixed",
                        "candidates": [
                            {"id": "effect", "label": "光斑", "append": {"effects": [{}]}},
                        ],
                    },
                ],
                "max_jobs": 10,
            }
        )
        self.assertEqual(
            [item["display_name"] for item in variants],
            ["光斑+蓝色", "光斑+蓝色-02"],
        )

    def test_mother_draft_rejects_composite_text_additions(self) -> None:
        _reject_mother_composite_text_changes({"text_templates": []})
        with self.assertRaises(HTTPException) as context:
            _reject_mother_composite_text_changes(
                {"text_templates": [{"template_json_path": "template.json"}]}
            )
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("只能原样保留", str(context.exception.detail))

    @staticmethod
    def _dimension(key: str, label: str, append: dict[str, list[dict]]) -> dict:
        return {
            "key": key,
            "label": key,
            "mode": "product",
            "candidates": [{"id": key, "label": label, "append": append}],
        }


if __name__ == "__main__":
    unittest.main()
