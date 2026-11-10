from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.cli import add_effect_json_to_video  # noqa: E402
from jyd_probe.render_job import _build_effect_additions  # noqa: E402


class EffectFullTimelineTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.root = root / f"effect_full_timeline_{uuid.uuid4().hex}"
        self.root.mkdir()
        self.effect_path = self.root / "effect.json"
        self.effect_payload = {
            "schema": "jyd_probe.video_effect.v1",
            "material": {"id": "old-effect", "name": "测试特效"},
            "segment_template": {
                "id": "old-segment",
                "material_id": "old-effect",
                "target_timerange": {"start": 0, "duration": 1_000_000},
            },
        }
        self.effect_path.write_text(json.dumps(self.effect_payload), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_web_default_effect_covers_complete_multi_segment_timeline(self) -> None:
        timeline_duration = 12_000_000
        additions = _build_effect_additions(
            {
                "effects": [
                    {
                        "effect_json_path": str(self.effect_path),
                        "target_video_track_index": 0,
                        "target_video_segment_index": 0,
                        "start_us": -1,
                        "duration_us": 0,
                    }
                ]
            },
            timeline_duration_us=timeline_duration,
        )
        self.assertEqual(additions[0].start_us, 0)
        self.assertEqual(additions[0].duration_us, timeline_duration)

        data = {
            "duration": timeline_duration,
            "materials": {"video_effects": []},
            "tracks": [
                {
                    "type": "video",
                    "segments": [
                        {"target_timerange": {"start": 0, "duration": 4_000_000}},
                        {"target_timerange": {"start": 4_000_000, "duration": 3_000_000}},
                        {"target_timerange": {"start": 7_000_000, "duration": 5_000_000}},
                    ],
                }
            ],
        }
        addition = additions[0]
        add_effect_json_to_video(
            data,
            self.effect_payload,
            addition.target_video_track_index,
            addition.target_video_segment_index,
            addition.start_us,
            addition.duration_us,
        )

        effect_track = next(track for track in data["tracks"] if track["type"] == "effect")
        self.assertEqual(
            effect_track["segments"][0]["target_timerange"],
            {"start": 0, "duration": timeline_duration},
        )


if __name__ == "__main__":
    unittest.main()
