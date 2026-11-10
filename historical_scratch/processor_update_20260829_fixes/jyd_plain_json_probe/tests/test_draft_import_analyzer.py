from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.draft_import_analyzer import analyze_draft_import  # noqa: E402


class DraftImportAnalyzerTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(exist_ok=True)
        self.temp = root / f"draft_import_{uuid.uuid4().hex}"
        self.temp.mkdir()
        self.draft_dir = self.temp / "draft"
        self.draft_dir.mkdir()
        self.workspace = self.temp / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_identifies_slots_and_resolves_central_assets(self) -> None:
        video_path = self.temp / "input.mp4"
        video_path.write_bytes(b"video-source")
        audio_path = self.temp / "music.mp3"
        audio_path.write_bytes(b"central-music")
        missing_effect = self.temp / "missing-video-effect"
        video_adjustment = self.temp / "color-correct-resource"
        video_adjustment.mkdir()
        missing_text_effect = self.temp / "missing-text-effect"
        missing_font = self.temp / "missing-font.ttf"
        missing_sticker = self.temp / "missing-sticker.png"
        ignored_transition = self.temp / "missing-transition"

        self._write_manifest(
            self.workspace / "audio_library" / "manifest" / "audio_manifest.json",
            "assets",
            [
                {
                    "identity": "music_id:1001",
                    "name": "中央音乐",
                    "music_id": "1001",
                    "checksum_sha256": "",
                }
            ],
        )
        self._write_manifest(
            self.workspace / "text_effect_library" / "manifest" / "text_effect_manifest.json",
            "effects",
            [
                {
                    "identity": "resource_id:3003",
                    "name": "中央花字",
                    "resource_id": "3003",
                }
            ],
        )
        self._write_manifest(
            self.workspace / "text_template_library" / "manifest" / "text_template_manifest.json",
            "templates",
            [
                {
                    "identity": "resource_id:4004",
                    "name": "中央复合文字",
                    "resource_id": "4004",
                }
            ],
        )
        effect_dir = self.workspace / "effect_library"
        effect_dir.mkdir()
        (effect_dir / "effect.json").write_text(
            json.dumps(
                {
                    "schema": "jyd_probe.video_effect.v1",
                    "material": {
                        "name": "中央视频特效",
                        "effect_id": "2002",
                        "resource_id": "2002",
                    },
                }
            ),
            encoding="utf-8",
        )

        ordinary_text_content = json.dumps(
            {
                "text": "普通标题",
                "styles": [
                    {
                        "font": {"path": str(missing_font)},
                        "effectStyle": {"id": "3003", "path": str(missing_text_effect)},
                    }
                ],
            },
            ensure_ascii=False,
        )
        template_text_content = json.dumps({"text": "复合标题", "styles": []}, ensure_ascii=False)
        draft = {
            "duration": 10_000_000,
            "canvas_config": {"width": 1080, "height": 1920, "ratio": "9:16"},
            "tracks": [
                {
                    "id": "video-track",
                    "type": "video",
                    "segments": [self._segment("video-segment", "video-material", 10_000_000)],
                },
                {
                    "id": "audio-track",
                    "type": "audio",
                    "name": "BGM",
                    "segments": [self._segment("audio-segment", "audio-material", 10_000_000)],
                },
                {
                    "id": "effect-track",
                    "type": "effect",
                    "segments": [self._segment("effect-segment", "effect-material", 10_000_000)],
                },
                {
                    "id": "text-track",
                    "type": "text",
                    "segments": [
                        self._segment("text-segment", "text-material", 3_000_000),
                        self._segment("template-segment", "template-material", 3_000_000),
                    ],
                },
            ],
            "materials": {
                "videos": [
                    {"id": "video-material", "path": str(video_path), "material_name": "主视频"}
                ],
                "audios": [
                    {
                        "id": "audio-material",
                        "path": str(audio_path),
                        "name": "中央音乐",
                        "music_id": "1001",
                        "type": "music",
                    }
                ],
                "video_effects": [
                    {
                        "id": "effect-material",
                        "path": str(missing_effect),
                        "name": "中央视频特效",
                        "effect_id": "2002",
                        "resource_id": "2002",
                    }
                ],
                "effects": [
                    {
                        "id": "color-correct-material",
                        "path": str(video_adjustment),
                        "name": "色彩校正",
                        "type": "color_correct",
                        "effect_id": "52439321",
                        "resource_id": "7348759227805995583",
                    }
                ],
                "texts": [
                    {"id": "text-material", "content": ordinary_text_content},
                    {"id": "template-text", "content": template_text_content},
                ],
                "text_templates": [
                    {
                        "id": "template-material",
                        "name": "中央复合文字",
                        "resource_id": "4004",
                        "path": str(self.temp / "missing-template"),
                        "text_info_resources": [{"text_material_id": "template-text"}],
                    }
                ],
                "stickers": [{"id": "sticker-material", "path": str(missing_sticker)}],
                "transitions": [
                    {
                        "id": "transition-material",
                        "name": "叠化",
                        "path": str(ignored_transition),
                    }
                ],
            },
        }

        report = analyze_draft_import(
            draft,
            source_draft_dir=self.draft_dir,
            workspace_root=self.workspace,
            hash_limit_bytes=None,
        )

        self.assertEqual(report["draft"]["canvas"]["width"], 1080)
        self.assertEqual(report["summary"]["slot_counts"]["audio"], 1)
        self.assertEqual(report["summary"]["slot_counts"]["video_effects"], 1)
        self.assertEqual(report["summary"]["slot_counts"]["texts"], 1)
        self.assertEqual(report["summary"]["slot_counts"]["text_templates"], 1)
        self.assertTrue(report["editable_slots"]["texts"][0]["has_flower_text"])
        self.assertEqual(
            report["editable_slots"]["text_templates"][0]["texts"],
            ["复合标题"],
        )

        by_original = {item["original_path"]: item for item in report["dependencies"]}
        self.assertEqual(by_original[str(video_path)]["status"], "upload_required")
        self.assertFalse(by_original[str(video_path)]["can_skip_if_replaced"])
        self.assertEqual(by_original[str(audio_path)]["status"], "central_library")
        self.assertEqual(by_original[str(missing_effect)]["status"], "central_library")
        self.assertEqual(by_original[str(video_adjustment)]["kind"], "video_adjustment")
        self.assertEqual(by_original[str(video_adjustment)]["status"], "upload_required")
        self.assertFalse(by_original[str(video_adjustment)]["can_skip_if_replaced"])
        self.assertEqual(by_original[str(missing_text_effect)]["status"], "central_library")
        self.assertEqual(
            by_original[str(self.temp / "missing-template")]["status"],
            "central_library",
        )
        self.assertEqual(by_original[str(missing_font)]["status"], "missing")
        self.assertTrue(by_original[str(missing_font)]["can_skip_if_replaced"])
        self.assertEqual(by_original[str(missing_sticker)]["status"], "missing")
        self.assertNotIn(str(ignored_transition), by_original)
        self.assertFalse(report["summary"]["ready_for_packaging"])
        self.assertEqual(report["summary"]["blocked_missing_count"], 1)

    def test_recognizes_text_materials_inside_mixed_tracks(self) -> None:
        ordinary_text_content = json.dumps(
            {"text": "新版普通文字", "styles": []},
            ensure_ascii=False,
        )
        template_text_content = json.dumps(
            {"text": "新版复合文字", "styles": []},
            ensure_ascii=False,
        )
        draft = {
            "tracks": [
                {
                    "id": "mixed-track-with-text",
                    "type": "mixed",
                    "segments": [
                        self._segment("ordinary-segment", "ordinary-text", 2_000_000),
                        self._segment("template-segment", "template-material", 2_000_000),
                        self._segment("video-segment", "video-material", 2_000_000),
                    ],
                },
                {
                    "id": "mixed-track-without-text",
                    "type": "mixed",
                    "segments": [
                        self._segment("other-video-segment", "other-video-material", 2_000_000),
                    ],
                },
            ],
            "materials": {
                "texts": [
                    {"id": "ordinary-text", "content": ordinary_text_content},
                    {"id": "template-text", "content": template_text_content},
                ],
                "text_templates": [
                    {
                        "id": "template-material",
                        "name": "新版复合文字模板",
                        "text_info_resources": [{"text_material_id": "template-text"}],
                    }
                ],
                "videos": [
                    {"id": "video-material"},
                    {"id": "other-video-material"},
                ],
            },
        }

        report = analyze_draft_import(
            draft,
            source_draft_dir=self.draft_dir,
            workspace_root=self.workspace,
            hash_limit_bytes=-1,
        )

        self.assertEqual(report["summary"]["slot_counts"]["texts"], 1)
        self.assertEqual(report["summary"]["slot_counts"]["text_templates"], 1)
        self.assertEqual(report["summary"]["track_type_counts"], {"mixed": 2})
        self.assertEqual(report["editable_slots"]["texts"][0]["text"], "新版普通文字")
        self.assertEqual(
            report["editable_slots"]["text_templates"][0]["texts"],
            ["新版复合文字"],
        )
        self.assertEqual(
            report["editable_slots"]["texts"][0]["selector"]["track_type"],
            "mixed",
        )
        self.assertTrue(
            any("非标准文字轨道（mixed=1）" in warning for warning in report["warnings"])
        )

    @staticmethod
    def _segment(segment_id: str, material_id: str, duration: int) -> dict[str, object]:
        return {
            "id": segment_id,
            "material_id": material_id,
            "target_timerange": {"start": 0, "duration": duration},
            "source_timerange": {"start": 0, "duration": duration},
        }

    @staticmethod
    def _write_manifest(path: Path, key: str, values: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({key: values}), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
