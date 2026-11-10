from __future__ import annotations

import unittest
from pathlib import Path
import random
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import (
    _all_combination_indices,
    _balanced_combination_indices,
    _random_combination_indices,
    _compact_variant_name,
    _expand_batch_payload,
)


class BatchDimensionExpansionTest(unittest.TestCase):
    def test_expands_fixed_and_product_dimensions(self) -> None:
        payload = {
            "job": {
                "schema": "jyd.render_job.v1",
                "source": {"type": "video", "media_id": "video-1"},
                "output": {"skip_export": True},
                "captions": {"text": "固定字幕"},
            },
            "dimensions": [
                {
                    "key": "bgm",
                    "label": "BGM",
                    "mode": "fixed",
                    "candidates": [
                        {
                            "id": "music-a",
                            "label": "音乐 A",
                            "append": {"audios": [{"library_identity": "music-a"}]},
                        }
                    ],
                },
                {
                    "key": "effect",
                    "label": "视频特效",
                    "mode": "product",
                    "candidates": [
                        {
                            "id": "effect-a",
                            "label": "特效 A",
                            "append": {"effects": [{"effect_json_path": "a.json"}]},
                        },
                        {
                            "id": "effect-b",
                            "label": "特效 B",
                            "append": {"effects": [{"effect_json_path": "b.json"}]},
                        },
                    ],
                },
                {
                    "key": "title",
                    "label": "文字方案",
                    "mode": "product",
                    "candidates": [
                        {
                            "id": "plain",
                            "label": "普通文字",
                            "patch": {"texts": [{"text": "标题"}]},
                        },
                        {
                            "id": "flower",
                            "label": "花字",
                            "patch": {
                                "texts": [
                                    {"text": "标题", "text_effect_json_path": "flower.json"}
                                ]
                            },
                        },
                    ],
                },
                {
                    "key": "sound_effect",
                    "label": "音效",
                    "mode": "disabled",
                    "candidates": [],
                },
            ],
        }

        jobs, variants = _expand_batch_payload(payload)

        self.assertEqual(len(jobs), 4)
        self.assertEqual(len(variants), 4)
        self.assertTrue(all(job["audios"][0]["library_identity"] == "music-a" for job in jobs))
        self.assertEqual({job["effects"][0]["effect_json_path"] for job in jobs}, {"a.json", "b.json"})
        self.assertEqual(
            {job["texts"][0].get("text_effect_json_path", "") for job in jobs},
            {"", "flower.json"},
        )
        self.assertTrue(all(job["captions"]["text"] == "固定字幕" for job in jobs))
        self.assertTrue(all(variant["dimensions"]["bgm"]["mode"] == "fixed" for variant in variants))
        self.assertTrue(all(variant["change_count"] == 2 for variant in variants))
        self.assertTrue(all(variant["core_changed_elements"] == ["bgm", "effect"] for variant in variants))
        self.assertEqual(variants[0]["combination_filter"]["raw_total"], 4)
        self.assertEqual(variants[0]["combination_filter"]["filtered_total"], 4)
        self.assertEqual(variants[0]["combination_filter"]["removed_total"], 0)

    def test_requires_two_core_changes_from_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "至少改变两个核心元素"):
            _expand_batch_payload(
                {
                    "job": {"source": {"type": "video"}, "output": {}},
                    "dimensions": [
                        {
                            "key": "bgm",
                            "label": "BGM",
                            "mode": "product",
                            "candidates": [{"id": "music", "patch": {"audio": "music"}}],
                        },
                        {
                            "key": "font",
                            "label": "字体",
                            "mode": "product",
                            "candidates": [{"id": "font", "patch": {"font": "font"}}],
                        },
                    ],
                }
            )

    def test_fixed_dimension_rejects_multiple_candidates(self) -> None:
        with self.assertRaisesRegex(ValueError, "必须且只能选择 1 个"):
            _expand_batch_payload(
                {
                    "job": {"source": {"type": "video"}, "output": {}},
                    "dimensions": [
                        {
                            "key": "bgm",
                            "label": "BGM",
                            "mode": "fixed",
                            "candidates": [
                                {"id": "a", "patch": {"audios": [{"id": "a"}]}},
                                {"id": "b", "patch": {"audios": [{"id": "b"}]}},
                            ],
                        }
                    ],
                }
            )

    def test_rejects_batches_over_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "超过上限 1"):
            _expand_batch_payload(
                {
                    "job": {"source": {"type": "video"}, "output": {}},
                    "max_jobs": 1,
                    "dimensions": [
                        {
                            "key": "bgm",
                            "mode": "product",
                            "candidates": [
                                {"id": "a", "patch": {"value": "a"}},
                                {"id": "b", "patch": {"value": "b"}},
                            ],
                        },
                        {
                            "key": "effect",
                            "mode": "product",
                            "candidates": [
                                {"id": "c", "patch": {"other": "c"}},
                                {"id": "d", "patch": {"other": "d"}},
                            ],
                        },
                    ],
                }
            )

    def test_complete_product_keeps_all_combinations(self) -> None:
        selected, raw_total = _all_combination_indices([2, 2, 2])

        self.assertEqual(raw_total, 8)
        self.assertEqual(len(selected), 8)
        self.assertIn((0, 0, 0), selected)
        self.assertIn((0, 0, 1), selected)

    def test_complete_product_supports_different_axis_sizes(self) -> None:
        selected, raw_total = _all_combination_indices([2, 3, 4])

        self.assertEqual(raw_total, 24)
        self.assertEqual(len(selected), 24)

    def test_complete_product_keeps_every_value_on_one_varying_axis(self) -> None:
        selected, raw_total = _all_combination_indices([1, 5, 1])

        self.assertEqual(raw_total, 5)
        self.assertEqual(selected, [(0, 0, 0), (0, 1, 0), (0, 2, 0), (0, 3, 0), (0, 4, 0)])

    def test_balanced_selection_covers_candidates_evenly(self) -> None:
        selected, raw_total = _balanced_combination_indices([23, 12, 10], 100)

        self.assertEqual(raw_total, 2760)
        self.assertEqual(len(selected), 100)
        self.assertEqual(len(set(selected)), 100)
        for axis, candidate_count in enumerate([23, 12, 10]):
            usage = [sum(row[axis] == value for row in selected) for value in range(candidate_count)]
            self.assertTrue(all(usage))
            self.assertLessEqual(max(usage) - min(usage), 1)

    def test_payload_balanced_selection_limits_jobs_before_creation(self) -> None:
        dimensions = []
        for key, count in (("bgm", 2), ("effect", 2), ("sticker", 2)):
            dimensions.append(
                {
                    "key": key,
                    "label": key,
                    "mode": "product",
                    "candidates": [
                        {"id": f"{key}-{index}", "patch": {key: index}}
                        for index in range(count)
                    ],
                }
            )

        jobs, variants = _expand_batch_payload(
            {
                "job": {"source": {"type": "video"}, "output": {}},
                "dimensions": dimensions,
                "selection": {"mode": "balanced", "limit": 3},
                "max_jobs": 500,
            }
        )

        self.assertEqual(len(jobs), 3)
        self.assertEqual(len(variants), 3)
        metadata = variants[0]["combination_filter"]
        self.assertEqual(metadata["selection_mode"], "balanced")
        self.assertEqual(metadata["requested_total"], 3)
        self.assertEqual(metadata["raw_total"], 8)
        self.assertEqual(metadata["filtered_total"], 3)
        self.assertEqual(metadata["removed_total"], 5)

    def test_random_selection_is_unique_without_materializing_all_combinations(self) -> None:
        selected, raw_total = _random_combination_indices(
            [23, 12, 10],
            100,
            sampler=random.Random(20260719),
        )

        self.assertEqual(raw_total, 2760)
        self.assertEqual(len(selected), 100)
        self.assertEqual(len(set(selected)), 100)
        self.assertTrue(all(0 <= row[0] < 23 for row in selected))
        self.assertTrue(all(0 <= row[1] < 12 for row in selected))
        self.assertTrue(all(0 <= row[2] < 10 for row in selected))

    def test_payload_random_selection_records_random_mode(self) -> None:
        dimensions = []
        for key, count in (("bgm", 3), ("effect", 3), ("sticker", 3)):
            dimensions.append(
                {
                    "key": key,
                    "label": key,
                    "mode": "product",
                    "candidates": [
                        {"id": f"{key}-{index}", "patch": {key: index}}
                        for index in range(count)
                    ],
                }
            )

        jobs, variants = _expand_batch_payload(
            {
                "job": {"source": {"type": "video"}, "output": {}},
                "dimensions": dimensions,
                "selection": {"mode": "random", "limit": 8},
                "max_jobs": 500,
            }
        )

        self.assertEqual(len(jobs), 8)
        identities = {
            (job["bgm"], job["effect"], job["sticker"])
            for job in jobs
        }
        self.assertEqual(len(identities), 8)
        self.assertEqual(variants[0]["combination_filter"]["selection_mode"], "random")

    def test_balanced_selection_cannot_exceed_job_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "本次生成数量不能超过上限 2"):
            _expand_batch_payload(
                {
                    "job": {"source": {"type": "video"}, "output": {}},
                    "dimensions": [
                        {
                            "key": "bgm",
                            "mode": "product",
                            "candidates": [{"id": "a", "patch": {"a": 1}}],
                        },
                        {
                            "key": "effect",
                            "mode": "product",
                            "candidates": [{"id": "b", "patch": {"b": 1}}],
                        },
                    ],
                    "selection": {"mode": "balanced", "limit": 3},
                    "max_jobs": 2,
                }
            )

    def test_visual_suite_merges_settings_and_appends_four_corner_stickers(self) -> None:
        corners = ["top_left", "top_right", "bottom_left", "bottom_right"]
        jobs, variants = _expand_batch_payload(
            {
                "job": {"source": {"type": "video"}, "output": {}},
                "dimensions": [
                    {
                        "key": "mirror",
                        "label": "分段镜像",
                        "mode": "fixed",
                        "candidates": [
                            {
                                "id": "mirror-10",
                                "patch": {"visual_variant": {"mirror_interval_seconds": 10}},
                            }
                        ],
                    },
                    {
                        "key": "layout",
                        "label": "裁剪填色",
                        "mode": "product",
                        "candidates": [
                            {
                                "id": "square-black",
                                "patch": {
                                    "visual_variant": {
                                        "crop_ratio": "1:1",
                                        "background_color": "#000000",
                                    }
                                },
                            }
                        ],
                    },
                    {
                        "key": "corner_sticker",
                        "label": "四角贴纸",
                        "mode": "product",
                        "candidates": [
                            {
                                "id": "corners-a",
                                "append": {
                                    "stickers": [
                                        {"sticker_json_path": "sticker.json", "corner": corner}
                                        for corner in corners
                                    ]
                                },
                            }
                        ],
                    },
                ],
            }
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0]["visual_variant"],
            {
                "mirror_interval_seconds": 10,
                "crop_ratio": "1:1",
                "background_color": "#000000",
            },
        )
        self.assertEqual([item["corner"] for item in jobs[0]["stickers"]], corners)
        self.assertEqual(variants[0]["change_count"], 3)
        self.assertEqual(
            variants[0]["core_changed_elements"],
            ["mirror", "layout", "corner_sticker"],
        )

    def test_compact_name_prefers_candidate_short_name(self) -> None:
        name = _compact_variant_name(
            [
                ("mirror", "镜像", "fixed", {"label": "每 10 秒镜像", "short_name": "镜10"}),
                ("layout", "裁剪", "product", {"label": "1:1 + #000000", "short_name": "方黑"}),
            ]
        )

        self.assertEqual(name, "镜1+方黑")


if __name__ == "__main__":
    unittest.main()
