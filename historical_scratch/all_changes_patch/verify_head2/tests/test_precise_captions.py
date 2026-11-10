from __future__ import annotations

import unittest

from jyd_probe.subtitles import (
    caption_cues_from_payload,
    parse_srt_cues,
    validate_caption_cues,
)


class PreciseCaptionTests(unittest.TestCase):
    def test_json_cues_keep_provider_timestamps(self) -> None:
        cues = caption_cues_from_payload(
            [
                {"start_us": 0, "end_us": 1_250_000, "text": "第一句。"},
                {
                    "start_us": 1_400_000,
                    "duration_us": 1_600_000,
                    "text": "第二句。",
                },
            ]
        )
        self.assertEqual(cues[0].duration_us, 1_250_000)
        self.assertEqual(cues[1].end_us, 3_000_000)

    def test_srt_text_is_accepted_without_reallocating_time(self) -> None:
        cues = parse_srt_cues(
            "1\n00:00:00,000 --> 00:00:01,250\n第一句。\n\n"
            "2\n00:00:01,400 --> 00:00:03,000\n第二句。\n"
        )
        self.assertEqual([cue.start_us for cue in cues], [0, 1_400_000])
        self.assertEqual([cue.end_us for cue in cues], [1_250_000, 3_000_000])

    def test_small_provider_tail_overflow_is_clipped_to_video(self) -> None:
        cues = caption_cues_from_payload(
            [{"start_us": 28_000_000, "end_us": 30_060_000, "text": "结尾。"}]
        )
        clipped = validate_caption_cues(cues, maximum_end_us=29_800_000)
        self.assertEqual(clipped[0].end_us, 29_800_000)

    def test_overlapping_timeline_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "重叠"):
            caption_cues_from_payload(
                [
                    {"start_us": 0, "end_us": 2_000_000, "text": "第一句。"},
                    {"start_us": 1_900_000, "end_us": 3_000_000, "text": "第二句。"},
                ]
            )


if __name__ == "__main__":
    unittest.main()
