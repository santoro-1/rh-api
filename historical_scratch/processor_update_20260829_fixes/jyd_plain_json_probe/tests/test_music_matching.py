from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.music_matching import (  # noqa: E402
    MUSIC_MATCHER_HARD_FILTERS_V1,
    MUSIC_MATCHER_VERSION,
    MUSIC_MATCHER_WEIGHTS_V1,
    MUSIC_TAXONOMY_VERSION,
    MAX_RECENT_USE_PENALTY,
    RECENCY_CANDIDATE_SCORE_GAP,
    RECENT_USE_PENALTY,
    MusicProfileError,
    MusicProfileMatcher,
    NoEligibleMusicError,
)


AUDIO_ROOT = PROJECT_ROOT / "data" / "libraries" / "audio_library"


def health_intent(**changes):
    value = {
        "primary_scene": "health_education",
        "secondary_scenes": ["habit_lifestyle"],
        "content_format": "knowledge_explanation",
        "topics": ["general_health", "science_education"],
        "primary_mood": "calm",
        "secondary_moods": ["warm"],
        "valence": "positive",
        "energy": 2,
        "pace": "medium_slow",
        "seriousness": 3,
        "warmth": 4,
        "tension": 1,
        "speech_density": "high",
        "vocal_preference": "prefer_instrumental",
        "opening_preference": "soft",
        "avoid_traits": ["strong_vocals", "dense_arrangement"],
        "confidence": 0.9,
    }
    value.update(changes)
    return value


class MusicProfileMatcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matcher = MusicProfileMatcher(AUDIO_ROOT)
        cls.snapshot = cls.matcher.snapshot()

    def test_shipped_manifest_maps_all_48_stable_audio_identities(self) -> None:
        self.assertEqual(self.snapshot["profile_count"], 48)
        self.assertEqual(self.snapshot["auto_eligible_count"], 44)
        self.assertEqual(self.snapshot["needs_review_count"], 4)
        self.assertEqual(self.snapshot["taxonomy_version"], MUSIC_TAXONOMY_VERSION)
        self.assertEqual(self.snapshot["matcher_version"], MUSIC_MATCHER_VERSION)
        identities = {profile["identity"] for profile in self.snapshot["profiles"]}
        self.assertEqual(len(identities), 48)
        self.assertTrue(all(identity.startswith("music_id:") for identity in identities))
        self.assertEqual(identities, set(self.snapshot["assets_by_identity"]))

    def test_only_four_unconfirmed_profiles_require_review(self) -> None:
        needs_review = {
            profile["source_bgm_id"]
            for profile in self.snapshot["profiles"]
            if profile["profile_review_status"] == "needs_review"
        }
        self.assertEqual(needs_review, {"BGM-010", "BGM-019", "BGM-020", "BGM-031"})
        self.assertTrue(
            all(
                not profile["auto_eligible"]
                for profile in self.snapshot["profiles"]
                if profile["source_bgm_id"] in needs_review
            )
        )

    def test_matcher_returns_one_health_top1_without_candidate_list(self) -> None:
        result = self.matcher.recommend(
            health_intent(),
            video_duration_us=30_000_000,
        )
        self.assertEqual(result["bgm_identity"], "music_id:6874387537750657031")
        self.assertEqual(result["name"], "美丽的神话 钢琴演奏")
        self.assertEqual(result["selection_source"], "ai")
        self.assertNotIn("candidates", result)
        self.assertNotIn("top3", result)
        self.assertAlmostEqual(
            sum(result["score_breakdown"].values()),
            result["score_before_recency"],
            places=4,
        )

    def test_weight_checkin_intent_selects_instrumental_apollo(self) -> None:
        intent = health_intent(
            primary_scene="weight_management",
            secondary_scenes=["fitness_exercise"],
            content_format="progress_checkin",
            topics=["weight_loss", "fitness", "motivation"],
            primary_mood="inspiring",
            secondary_moods=["confident"],
            energy=4,
            pace="medium_fast",
            seriousness=3,
            warmth=2,
            tension=3,
            speech_density="medium",
            opening_preference="immediate",
        )
        result = self.matcher.recommend(intent, video_duration_us=30_000_000)
        self.assertEqual(result["bgm_identity"], "music_id:7035666773449902110")
        self.assertEqual(result["name"], "Apollo's Triumph (抖音原版)")

    def test_family_story_intent_can_prefer_a_vocal_track(self) -> None:
        intent = health_intent(
            primary_scene="family_relationship",
            secondary_scenes=["interview_conversation", "emotional_story"],
            content_format="personal_story",
            topics=["family", "emotional_wellbeing"],
            primary_mood="warm",
            secondary_moods=["emotional", "healing"],
            valence="positive",
            energy=2,
            pace="medium_slow",
            seriousness=4,
            warmth=5,
            tension=1,
            speech_density="low",
            vocal_preference="prefer_vocal",
            avoid_traits=[],
        )
        result = self.matcher.recommend(intent, video_duration_us=30_000_000)
        self.assertEqual(result["bgm_identity"], "music_id:7585886063152842794")
        self.assertEqual(result["name"], "愿妈妈平安健康（剪辑版）")

    def test_short_music_remains_eligible_because_export_can_loop(self) -> None:
        result = self.matcher.recommend(health_intent(), video_duration_us=451_000_000)
        selected_asset = self.snapshot["assets_by_identity"][result["bgm_identity"]]
        self.assertLess(selected_asset["duration_us"], 451_000_000)
        self.assertGreater(result["eligible_count"], 0)
        self.assertNotIn("duration_covers_video", result["filtered_counts"])

    def test_new_draft_music_profiles_are_narrow_vocal_story_tracks(self) -> None:
        expected = {
            "music_id:7645200539881769012": {"personal_growth", "emotional_story"},
            "music_id:6926801235057002497": {"personal_growth", "emotional_story"},
        }
        profiles = {item["identity"]: item for item in self.snapshot["profiles"]}
        for identity, scenes in expected.items():
            profile = profiles[identity]
            self.assertEqual(set(profile["scenes"]), scenes)
            self.assertEqual(profile["vocal_profile"], "vocal")
            self.assertIn("strong_vocals", profile["traits"])
            self.assertNotIn("health_education", profile["scenes"])

    def test_tightened_daylily_profile_is_not_general_health_knowledge(self) -> None:
        profile = next(
            item
            for item in self.snapshot["profiles"]
            if item["identity"] == "music_id:6931987094940174343"
        )
        self.assertEqual(
            profile["scenes"],
            ["interview_conversation", "emotional_story", "family_relationship"],
        )
        self.assertEqual(profile["content_formats"], ["personal_story", "interview"])
        self.assertEqual(profile["topics"], ["family", "emotional_wellbeing"])

    def test_strong_vocals_are_volume_managed_instead_of_hard_filtered(self) -> None:
        managed = self.matcher.recommend(
            health_intent(avoid_traits=["strong_vocals"]),
            video_duration_us=30_000_000,
        )
        unrestricted = self.matcher.recommend(
            health_intent(avoid_traits=[]),
            video_duration_us=30_000_000,
        )
        self.assertEqual(managed["eligible_count"], unrestricted["eligible_count"])
        self.assertEqual(managed["filtered_counts"]["forbidden_traits_absent"], 0)
        self.assertEqual(managed["volume_managed_traits"], ["strong_vocals"])

    def test_exclusion_and_recent_use_are_deterministic(self) -> None:
        first = self.matcher.recommend(health_intent(), video_duration_us=30_000_000)
        excluded = self.matcher.recommend(
            health_intent(),
            video_duration_us=30_000_000,
            excluded_identities=[first["bgm_identity"]],
        )
        repeated = self.matcher.recommend(
            health_intent(),
            video_duration_us=30_000_000,
            excluded_identities=[first["bgm_identity"]],
        )
        self.assertNotEqual(excluded["bgm_identity"], first["bgm_identity"])
        self.assertEqual(excluded["bgm_identity"], repeated["bgm_identity"])
        self.assertEqual(excluded["filtered_counts"]["excluded_identity"], 1)

        penalized = self.matcher.recommend(
            health_intent(),
            video_duration_us=30_000_000,
            recent_identity_counts={first["bgm_identity"]: 1},
        )
        if penalized["bgm_identity"] == first["bgm_identity"]:
                self.assertEqual(penalized["recency_penalty"], RECENT_USE_PENALTY)
        else:
            self.assertNotEqual(penalized["bgm_identity"], first["bgm_identity"])

    def test_recency_only_rotates_inside_semantic_near_top_pool(self) -> None:
        result = self.matcher.recommend(
            health_intent(),
            video_duration_us=30_000_000,
            recent_identity_counts={
                profile["identity"]: 100 for profile in self.snapshot["profiles"]
            },
        )
        self.assertLessEqual(result["recency_penalty"], MAX_RECENT_USE_PENALTY)
        self.assertGreaterEqual(
            result["score_before_recency"],
            result["semantic_top_score"] - RECENCY_CANDIDATE_SCORE_GAP,
        )
        self.assertLessEqual(result["rotation_candidate_count"], result["eligible_count"])

    def test_intent_enums_ranges_and_unknown_fields_are_strict(self) -> None:
        invalid_values = [
            health_intent(energy="2"),
            health_intent(primary_scene="unknown_scene"),
            {**health_intent(), "bgm_identity": "music_id:not-allowed"},
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(MusicProfileError):
                    self.matcher.recommend(value, video_duration_us=30_000_000)

    def test_profile_weights_and_hard_filters_cannot_drift(self) -> None:
        self.assertEqual(sum(MUSIC_MATCHER_WEIGHTS_V1.values()), 100)
        self.assertEqual(
            MUSIC_MATCHER_WEIGHTS_V1,
            {
                "scene": 25,
                "content_format": 20,
                "mood_valence": 20,
                "energy_pace": 15,
                "expression_axes": 10,
                "speech_vocal": 10,
            },
        )
        self.assertNotIn("duration_covers_video", MUSIC_MATCHER_HARD_FILTERS_V1)

    def test_manifest_rejects_weight_drift_before_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest").mkdir(parents=True)
            shutil.copy2(
                AUDIO_ROOT / "manifest" / "audio_manifest.json",
                root / "manifest" / "audio_manifest.json",
            )
            document = json.loads(
                (AUDIO_ROOT / "manifest" / "music_profiles.v1.json").read_text(
                    encoding="utf-8"
                )
            )
            document["weights"]["scene"] = 24
            (root / "manifest" / "music_profiles.v1.json").write_text(
                json.dumps(document, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(MusicProfileError):
                MusicProfileMatcher(root).snapshot()


if __name__ == "__main__":
    unittest.main()
