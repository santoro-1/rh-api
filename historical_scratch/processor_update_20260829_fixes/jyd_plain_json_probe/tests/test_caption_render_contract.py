from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from jyd_probe.content_replace import _apply_text_material_overrides
from jyd_probe.cli import load_plain_draft_json
from jyd_probe.draft_factory import VideoSequenceItem, create_plain_draft_from_videos
from jyd_probe.project_postprocess import (
    _caption_display_text,
    _postprocess_target_items,
    _split_one_line,
    _unsafe_break_offsets,
)
from jyd_probe.project_video_source import (
    build_normalized_project_video_source,
    build_project_speech_audio,
    build_project_video_source,
)


class _UnitWidthMetrics:
    @staticmethod
    def text_width_em(text: str) -> float:
        return float(len(text))


class CaptionRenderContractTest(unittest.TestCase):
    def test_preview_retry_targets_only_requested_script_row(self) -> None:
        project = {
            "items": [
                {"item_id": "one", "outputs": {"composition_video": None}},
                {"item_id": "two", "outputs": {"composition_video": None}},
                {"item_id": "exported", "outputs": {"composition_video": {"asset_id": "v"}}},
            ]
        }
        self.assertEqual(
            [item["item_id"] for item in _postprocess_target_items(project, {"two"})],
            ["two"],
        )

    def test_black_stroke_is_written_to_jianying_text_style(self) -> None:
        material = {
            "content": json.dumps(
                {"text": "测试字幕", "styles": [{"range": [0, 4], "size": 8}]},
                ensure_ascii=False,
            )
        }
        _apply_text_material_overrides(
            material,
            size=11,
            color="#FFFFFF",
            stroke_color="#000000",
            stroke_width=0.06,
            line_max_width=0.8,
        )
        style = json.loads(material["content"])["styles"][0]
        self.assertEqual(style["size"], 11)
        self.assertEqual(style["strokes"][0]["content"]["solid"]["color"], [0, 0, 0])
        self.assertEqual(style["strokes"][0]["width"], 0.06)

    def test_width_limited_split_does_not_create_orphan_punctuation_phrase(self) -> None:
        text = "一部分糖原就在血液循环成为血糖，另外一部分就叫到肌肉和肝脏成为肌糖原和肝糖原。"
        chunks = _split_one_line(text, _UnitWidthMetrics(), maximum_width_em=14)
        self.assertEqual("".join(chunks), "一部分糖原就在血液循环成为血糖另外一部分就叫到肌肉和肝脏成为肌糖原和肝糖原")
        self.assertTrue(all(not any(symbol in chunk for symbol in "，。！？；：、") for chunk in chunks))
        self.assertTrue(all(len(chunk) >= 4 for chunk in chunks))

    def test_width_limit_does_not_balance_by_splitting_modifiers(self) -> None:
        metrics = _UnitWidthMetrics()

        self.assertEqual(
            _split_one_line(
                "蛋白质很高的五种好食物",
                metrics,
                maximum_width_em=10.21,
            ),
            ["蛋白质很高的", "五种好食物"],
        )
        self.assertEqual(
            _split_one_line(
                "再也不要吃甜蛋糕软面包了",
                metrics,
                maximum_width_em=10.21,
            ),
            ["再也不要吃", "甜蛋糕软面包了"],
        )

    def test_connector_moves_to_next_caption_and_display_hides_punctuation(self) -> None:
        text = "而肝糖原没被利用完的话，那么一部分糖原就会转化成脂肪。那么这些脂肪又怎么排出去呢？"
        chunks = _split_one_line(text, _UnitWidthMetrics(), maximum_width_em=14)
        self.assertEqual(chunks[0], "而肝糖原没被利用完的话")
        self.assertTrue(all(not chunk.endswith("那么") for chunk in chunks[:-1]))
        self.assertTrue(any(chunk.startswith("那么") for chunk in chunks[1:]))
        self.assertTrue(all(not any(symbol in chunk for symbol in "，。！？；：、") for chunk in chunks))

    def test_numeric_separators_remain_visible(self) -> None:
        display, _breaks = _caption_display_text("24.4 秒，8:30 开始. Next.")
        self.assertEqual(display, "24.4 秒8:30 开始 Next")

    def test_verb_result_and_directional_complements_are_hard_protected(self) -> None:
        cases = {
            "把脂肪拿出来": len("把脂肪拿"),
            "把动作做完再休息": len("把动作做"),
            "把坏习惯改掉": len("把坏习惯改"),
            "把重点说清再继续": len("把重点说"),
        }
        for text, boundary in cases.items():
            with self.subTest(text=text):
                self.assertIn(boundary, _unsafe_break_offsets(text))

    def test_multi_segment_project_uses_independent_main_track_source(self) -> None:
        item = {
            "row_key": "1",
            "outputs": {
                "audio": {"managed_path": "D:/voice.mp3"},
                "base_video": {
                    "managed_path": "D:/base.mp4",
                    "metadata": {"segment_count": 2},
                },
                "original_video_segments": [
                    {
                        "status": "READY",
                        "managed_path": "D:/segment-2.mp4",
                        "external_ref": {"video_index": 2},
                        "metadata": {
                            "start_seconds": 1.25,
                            "end_seconds": 3.0,
                            "actual_duration_us": 1_750_000,
                        },
                    },
                    {
                        "status": "READY",
                        "managed_path": "D:/segment-1.mp4",
                        "external_ref": {"video_index": 1},
                        "metadata": {
                            "start_seconds": 0,
                            "end_seconds": 1.25,
                            "actual_duration_us": 1_250_000,
                        },
                    },
                ],
            },
        }
        source = build_project_video_source(item)
        self.assertEqual(source["type"], "video_sequence")
        self.assertEqual(
            [entry["video_index"] for entry in source["items"]],
            [1, 2],
        )
        self.assertEqual(
            [entry["target_duration_us"] for entry in source["items"]],
            [1_250_000, 1_750_000],
        )
        normalized = build_normalized_project_video_source(item)
        self.assertEqual(normalized["type"], "video")
        self.assertEqual(
            normalized["media_path"],
            str(Path("D:/base.mp4").resolve()),
        )
        self.assertEqual(source["items"][0]["transition_after_us"], 250_000)
        self.assertNotIn("transition_after_us", source["items"][1])
        self.assertEqual([entry["volume"] for entry in source["items"]], [0.0, 0.0])
        speech = build_project_speech_audio(item)
        self.assertEqual(speech["type"], "add")
        self.assertEqual(speech["media_path"], str(Path("D:/voice.mp3").resolve()))
        self.assertTrue(speech["fit_to_video"])
        self.assertEqual(speech["volume"], 1.0)

    def test_multi_segment_source_ignores_historical_runninghub_segments(self) -> None:
        item = {
            "row_key": "6",
            "outputs": {
                "base_video": {
                    "managed_path": "D:/base-current.mp4",
                    "metadata": {"segment_count": 2},
                    "external_ref": {"source_task_ids": ["current-1", "current-2"]},
                },
                "original_video_segments": [
                    {
                        "status": "READY",
                        "managed_path": "D:/historical-1.mp4",
                        "external_ref": {"video_index": 1, "remote_task_id": "old-1"},
                        "metadata": {"start_seconds": 0, "end_seconds": 1},
                    },
                    {
                        "status": "READY",
                        "managed_path": "D:/historical-2.mp4",
                        "external_ref": {"video_index": 2, "remote_task_id": "old-2"},
                        "metadata": {"start_seconds": 1, "end_seconds": 2},
                    },
                    {
                        "status": "READY",
                        "managed_path": "D:/current-2.mp4",
                        "external_ref": {"video_index": 2, "remote_task_id": "current-2"},
                        "metadata": {
                            "start_seconds": 1.25,
                            "end_seconds": 3,
                            "actual_duration_us": 1_750_000,
                        },
                    },
                    {
                        "status": "READY",
                        "managed_path": "D:/current-1.mp4",
                        "external_ref": {"video_index": 1, "remote_task_id": "current-1"},
                        "metadata": {
                            "start_seconds": 0,
                            "end_seconds": 1.25,
                            "actual_duration_us": 1_250_000,
                        },
                    },
                ],
            },
        }

        source = build_project_video_source(item)

        self.assertEqual(source["type"], "video_sequence")
        self.assertEqual(
            [entry["media_path"] for entry in source["items"]],
            [str(Path("D:/current-1.mp4").resolve()), str(Path("D:/current-2.mp4").resolve())],
        )
        self.assertEqual(
            [entry["target_duration_us"] for entry in source["items"]],
            [1_250_000, 1_750_000],
        )

    def test_multi_segment_source_uses_latest_seedvr2_revision_per_index(self) -> None:
        item = {
            "row_key": "15",
            "outputs": {
                "base_video": {
                    "managed_path": "D:/base-seedvr2.mp4",
                    "metadata": {"segment_count": 2},
                    "external_ref": {"source_task_ids": ["task-1", "task-2"]},
                },
                "original_video_segments": [
                    {
                        "asset_id": "enhanced-1",
                        "version": 3,
                        "status": "READY",
                        "managed_path": "D:/enhanced-1.mp4",
                        "external_ref": {"video_index": 1, "remote_task_id": "task-1"},
                        "metadata": {
                            "start_seconds": 0,
                            "end_seconds": 1.25,
                            "actual_duration_us": 1_250_000,
                        },
                    },
                    {
                        "asset_id": "raw-1",
                        "version": 1,
                        "status": "READY",
                        "managed_path": "D:/raw-1.mp4",
                        "external_ref": {"video_index": 1, "remote_task_id": "task-1"},
                        "metadata": {"start_seconds": 0, "end_seconds": 1.25},
                    },
                    {
                        "asset_id": "enhanced-2",
                        "version": 4,
                        "status": "READY",
                        "managed_path": "D:/enhanced-2.mp4",
                        "external_ref": {"video_index": 2, "remote_task_id": "task-2"},
                        "metadata": {
                            "start_seconds": 1.25,
                            "end_seconds": 3,
                            "actual_duration_us": 1_750_000,
                        },
                    },
                    {
                        "asset_id": "raw-2",
                        "version": 2,
                        "status": "READY",
                        "managed_path": "D:/raw-2.mp4",
                        "external_ref": {"video_index": 2, "remote_task_id": "task-2"},
                        "metadata": {"start_seconds": 1.25, "end_seconds": 3},
                    },
                ],
            },
        }

        source = build_project_video_source(item)

        self.assertEqual(
            [entry["media_path"] for entry in source["items"]],
            [str(Path("D:/enhanced-1.mp4").resolve()), str(Path("D:/enhanced-2.mp4").resolve())],
        )

    def test_real_draft_keeps_sequence_as_two_main_track_segments(self) -> None:
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory(prefix="jyd-sequence-") as directory:
            root = Path(directory)
            videos: list[Path] = []
            for index, value in enumerate((40, 180), start=1):
                path = root / f"segment-{index}.avi"
                writer = cv2.VideoWriter(
                    str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 96)
                )
                self.assertTrue(writer.isOpened())
                for _ in range(10):
                    writer.write(np.full((96, 64, 3), value, dtype=np.uint8))
                writer.release()
                videos.append(path)

            created = create_plain_draft_from_videos(
                [VideoSequenceItem(path, target_duration_us=1_000_000) for path in videos],
                root / "drafts",
                draft_name="two-independent-segments",
            )
            data = load_plain_draft_json(created.draft_dir)
            video_track = next(track for track in data["tracks"] if track["type"] == "video")
            self.assertEqual(len(video_track["segments"]), 2)
            self.assertEqual(
                [segment["target_timerange"]["start"] for segment in video_track["segments"]],
                [0, 1_000_000],
            )
            material_by_id = {
                material["id"]: material for material in data["materials"]["videos"]
            }
            self.assertEqual(
                [
                    Path(material_by_id[segment["material_id"]]["path"]).name
                    for segment in video_track["segments"]
                ],
                ["segment-1.avi", "segment-2.avi"],
            )

    def test_native_dissolve_is_attached_directly_between_real_clips(self) -> None:
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory(prefix="jyd-dissolve-") as directory:
            root = Path(directory)
            videos: list[Path] = []
            for index, value in enumerate((40, 180), start=1):
                path = root / f"segment-{index}.avi"
                writer = cv2.VideoWriter(
                    str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 96)
                )
                self.assertTrue(writer.isOpened())
                for _ in range(10):
                    writer.write(np.full((96, 64, 3), value, dtype=np.uint8))
                writer.release()
                videos.append(path)

            created = create_plain_draft_from_videos(
                [
                    VideoSequenceItem(
                        videos[0],
                        target_duration_us=1_100_000,
                        transition_after_us=250_000,
                    ),
                    VideoSequenceItem(videos[1], target_duration_us=1_000_000),
                ],
                root / "drafts",
                draft_name="two-segments-with-dissolve",
            )
            data = load_plain_draft_json(created.draft_dir)
            video_track = next(track for track in data["tracks"] if track["type"] == "video")
            transitions = data["materials"]["transitions"]

            self.assertEqual(len(video_track["segments"]), 2)
            self.assertEqual(
                [segment["target_timerange"]["duration"] for segment in video_track["segments"]],
                [1_100_000, 1_000_000],
            )
            self.assertAlmostEqual(video_track["segments"][0]["speed"], 1 / 1.1, places=5)
            self.assertEqual(video_track["segments"][1]["speed"], 1.0)
            self.assertEqual(len(transitions), 1)
            self.assertEqual(transitions[0]["name"], "叠化")
            self.assertEqual(transitions[0]["duration"], 250_000)
            self.assertIn(
                transitions[0]["id"],
                video_track["segments"][0]["extra_material_refs"],
            )
            self.assertFalse((created.draft_dir / "_segment_holds").exists())

    def test_video_fade_out_is_attached_only_to_last_main_track_segment(self) -> None:
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory(prefix="jyd-fade-out-") as directory:
            root = Path(directory)
            videos: list[Path] = []
            for index, value in enumerate((40, 180), start=1):
                path = root / f"segment-{index}.avi"
                writer = cv2.VideoWriter(
                    str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 96)
                )
                self.assertTrue(writer.isOpened())
                for _ in range(10):
                    writer.write(np.full((96, 64, 3), value, dtype=np.uint8))
                writer.release()
                videos.append(path)

            created = create_plain_draft_from_videos(
                [
                    VideoSequenceItem(videos[0], target_duration_us=1_000_000),
                    VideoSequenceItem(videos[1], target_duration_us=3_000_000),
                ],
                root / "drafts",
                draft_name="two-segments-with-fade-out",
                fade_out_us=2_000_000,
            )
            data = load_plain_draft_json(created.draft_dir)
            video_track = next(track for track in data["tracks"] if track["type"] == "video")
            animations = data["materials"]["material_animations"]

            self.assertEqual(len(animations), 1)
            animation = animations[0]["animations"][0]
            self.assertEqual(animation["name"], "渐隐")
            self.assertEqual(animation["duration"], 2_000_000)
            self.assertEqual(animation["start"], 1_000_000)
            self.assertNotIn(animations[0]["id"], video_track["segments"][0]["extra_material_refs"])
            self.assertIn(animations[0]["id"], video_track["segments"][1]["extra_material_refs"])


if __name__ == "__main__":
    unittest.main()
