from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.caption_alignment import (  # noqa: E402
    CaptionAlignmentError,
    FunASRCaptionAligner,
    _ASRAudioChunk,
    alignment_matches,
    build_alignment,
    retime_render_cues,
)


class CaptionAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = "你吃得越少，它消耗得越少。"
        self.raw_cues = [
            {
                "text": self.script,
                "start_us": 1_000_000,
                "duration_us": 4_000_000,
            }
        ]
        spoken = [character for character in self.script if character not in "，。"]
        self.payload = {
            "model": "paraformer-zh",
            "device": "cpu",
            "processingSeconds": 0.2,
            "tokens": [
                {
                    "text": character,
                    "startSeconds": 1.2 + index * 0.25,
                    "endSeconds": 1.4 + index * 0.25,
                }
                for index, character in enumerate(spoken)
            ],
        }

    def _alignment(self) -> dict[str, object]:
        return build_alignment(
            self.script,
            self.raw_cues,
            self.payload,
            audio_asset_id="audio-1",
            audio_version=2,
        )

    def test_exact_asr_tokens_retime_final_semantic_captions(self) -> None:
        alignment = self._alignment()
        cues = retime_render_cues(
            self.script,
            self.raw_cues,
            [
                {"text": "你吃得越少", "start_us": 1_000_000, "duration_us": 2_000_000},
                {"text": "它消耗得越少", "start_us": 3_000_000, "duration_us": 2_000_000},
            ],
            alignment,
        )

        self.assertEqual(cues[0]["start_us"], 1_200_000)
        self.assertEqual(cues[0]["start_us"] + cues[0]["duration_us"], 2_425_000)
        self.assertEqual(cues[1]["start_us"], 2_425_000)
        self.assertEqual(cues[1]["start_us"] + cues[1]["duration_us"], 3_900_000)
        self.assertEqual(alignment["exact_match_ratio"], 1.0)

    def test_alignment_cache_is_bound_to_script_audio_and_version(self) -> None:
        alignment = self._alignment()
        self.assertTrue(
            alignment_matches(
                alignment,
                script=self.script,
                audio_asset_id="audio-1",
                audio_version=2,
            )
        )
        self.assertFalse(
            alignment_matches(
                alignment,
                script=self.script + "变更",
                audio_asset_id="audio-1",
                audio_version=2,
            )
        )
        self.assertFalse(
            alignment_matches(
                alignment,
                script=self.script,
                audio_asset_id="audio-2",
                audio_version=2,
            )
        )

    def test_low_script_match_is_rejected_instead_of_shifting_timeline(self) -> None:
        payload = {
            **self.payload,
            "tokens": [
                {"text": "错", "startSeconds": 1.0, "endSeconds": 1.2}
                for _ in self.payload["tokens"]
            ],
        }
        with self.assertRaisesRegex(CaptionAlignmentError, "精确命中率"):
            build_alignment(
                self.script,
                self.raw_cues,
                payload,
                audio_asset_id="audio-1",
                audio_version=2,
            )

    def test_alignment_cache_does_not_store_recognized_text(self) -> None:
        alignment = self._alignment()
        self.assertNotIn("text", alignment)
        self.assertEqual(
            alignment["script_sha256"],
            hashlib.sha256(self.script.encode("utf-8")).hexdigest(),
        )

    def test_h3_segment_windows_allow_funasr_to_follow_generated_audio_timing(self) -> None:
        script = "第一段。第二段。"
        h3_segment_windows = [
            {"text": "第一段。", "start_us": 0, "duration_us": 5_875_000},
            {
                "text": "第二段。",
                "start_us": 5_875_000,
                "duration_us": 4_458_333,
            },
        ]
        payload = {
            "model": "paraformer-zh",
            "tokens": [
                {"text": "第", "startSeconds": 1.0, "endSeconds": 1.2},
                {"text": "一", "startSeconds": 1.3, "endSeconds": 1.5},
                {"text": "段", "startSeconds": 1.6, "endSeconds": 1.8},
                {"text": "第", "startSeconds": 7.0, "endSeconds": 7.2},
                {"text": "二", "startSeconds": 7.3, "endSeconds": 7.5},
                {"text": "段", "startSeconds": 7.6, "endSeconds": 7.8},
            ],
        }

        alignment = build_alignment(
            script,
            h3_segment_windows,
            payload,
            audio_asset_id="h3-generated-audio",
            audio_version=1,
        )

        self.assertEqual(alignment["ranges"][0]["start_us"], 1_000_000)
        self.assertEqual(alignment["ranges"][3]["start_us"], 7_000_000)
        self.assertEqual(alignment["ranges"][3]["raw_cue_index"], 1)
        self.assertEqual(alignment["exact_match_ratio"], 1.0)

    def test_empty_full_result_retries_contextual_chunks_and_merges_timestamps(self) -> None:
        script = "你好世界"
        raw_cues = [{"text": script, "start_us": 0, "duration_us": 4_000_000}]
        first_payload = {
            "tokens": [
                {"text": "你", "startSeconds": 0.2, "endSeconds": 0.4},
                {"text": "好", "startSeconds": 1.2, "endSeconds": 1.4},
                {"text": "世", "startSeconds": 2.1, "endSeconds": 2.3},
            ]
        }
        second_payload = {
            "tokens": [
                {"text": "好", "startSeconds": 0.2, "endSeconds": 0.4},
                {"text": "世", "startSeconds": 1.2, "endSeconds": 1.4},
                {"text": "界", "startSeconds": 2.2, "endSeconds": 2.4},
            ]
        }

        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            audio = root / "full.mp3"
            audio.write_bytes(b"audio")
            chunks = [
                _ASRAudioChunk(root / "chunk-0.wav", 0.0, 0.0, 2.0, False),
                _ASRAudioChunk(root / "chunk-1.wav", 1.0, 2.0, 4.0, True),
            ]

            @contextmanager
            def fake_chunks(_source: Path, *, timeout_seconds: int):
                self.assertGreater(timeout_seconds, 0)
                yield chunks

            aligner = FunASRCaptionAligner("http://127.0.0.1:18084")
            with patch(
                "jyd_probe.caption_alignment._asr_audio_chunks", fake_chunks
            ), patch.object(
                aligner,
                "_transcribe",
                side_effect=[
                    CaptionAlignmentError(
                        "ASR_TIMESTAMPS_MISSING", "FunASR 没有返回字词时间戳"
                    ),
                    first_payload,
                    second_payload,
                ],
            ) as transcribe:
                alignment = aligner.align(
                    audio,
                    script=script,
                    raw_cues=raw_cues,
                    audio_asset_id="audio-chunked",
                    audio_version=1,
                )

        self.assertEqual(transcribe.call_count, 3)
        self.assertEqual(alignment["exact_match_ratio"], 1.0)
        self.assertEqual(
            [entry["start_us"] for entry in alignment["ranges"]],
            [200_000, 1_200_000, 2_200_000, 3_200_000],
        )


if __name__ == "__main__":
    unittest.main()
