from __future__ import annotations

import unittest

from run_probe import ProbeError, TextStats, analyze_pace, inspect_text


class TextInspectionTests(unittest.TestCase):
    def test_counts_chinese_and_boundaries(self) -> None:
        stats = inspect_text("第一句话。第二句话！\n\n第三段开始，继续说明。")

        self.assertEqual(stats.han_characters, 17)
        self.assertEqual(stats.paragraph_gaps, 1)
        self.assertEqual(stats.sentence_gaps, 1)


class PaceAnalysisTests(unittest.TestCase):
    def stats(self, count: int, sentence_gaps: int = 4, paragraph_gaps: int = 1) -> TextStats:
        return TextStats(count, 0, 0, sentence_gaps, paragraph_gaps)

    def test_fast_voice_uses_pause_budget_without_speed_change(self) -> None:
        result = analyze_pace(self.stats(220), 50.0, target_cpm=220)

        self.assertEqual(result.result, "pause_only_is_enough")
        self.assertAlmostEqual(result.natural_cpm, 264.0)
        self.assertAlmostEqual(result.extra_pause_budget_seconds, 10.0)
        self.assertIsNone(result.estimated_native_speed_if_needed)

    def test_near_target_keeps_natural_speed(self) -> None:
        result = analyze_pace(self.stats(220), 61.0, target_cpm=220)

        self.assertEqual(result.result, "natural_speed_is_suitable")
        self.assertIsNone(result.estimated_native_speed_if_needed)

    def test_slow_voice_reports_that_pauses_cannot_help(self) -> None:
        result = analyze_pace(self.stats(220), 66.0, target_cpm=220)

        self.assertEqual(result.result, "pause_only_cannot_reach_target")
        self.assertAlmostEqual(result.natural_cpm, 200.0)
        self.assertAlmostEqual(result.estimated_native_speed_if_needed or 0, 1.1)

    def test_rejects_empty_chinese_count(self) -> None:
        with self.assertRaises(ProbeError):
            analyze_pace(self.stats(0), 60.0, target_cpm=220)


if __name__ == "__main__":
    unittest.main()
