from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


WORKBENCH = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe")
sys.path.insert(0, str(WORKBENCH / "src"))
import jyd_probe.audio_catalog  # noqa: E402,F401

module_path = Path(__file__).with_name("music_matching.py")
spec = importlib.util.spec_from_file_location("jyd_probe.music_matching", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

root = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\module2_runtime\audio_library")
matcher = module.MusicProfileMatcher(root)
print(json.dumps({k: v for k, v in matcher.snapshot().items() if k not in {"profiles", "assets_by_identity"}}, ensure_ascii=False, indent=2))


def intent(**changes):
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


cases = {
    "health": intent(),
    "weight": intent(
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
    ),
    "family": intent(
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
    ),
}
for label, value in cases.items():
    result = matcher.recommend(value, video_duration_us=30_000_000)
    print(label, json.dumps(result, ensure_ascii=False, indent=2))
