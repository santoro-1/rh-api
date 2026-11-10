from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .audio_catalog import AudioCatalog


MUSIC_PROFILE_SCHEMA = "jyd.music_profiles.v1"
MUSIC_TAXONOMY_VERSION = "music-taxonomy.v1"
MUSIC_MATCHER_VERSION = "music-matcher.v1"
MUSIC_MATCHER_WEIGHTS_V1 = {
    "scene": 25,
    "content_format": 20,
    "mood_valence": 20,
    "energy_pace": 15,
    "expression_axes": 10,
    "speech_vocal": 10,
}
MUSIC_MATCHER_HARD_FILTERS_V1 = (
    "enabled_and_available",
    "auto_eligible",
    "rights_allowed",
    "duration_covers_video",
    "forbidden_traits_absent",
)
RECENT_USE_PENALTY = 3.0
MAX_RECENT_USE_PENALTY = 12.0

SCENES = {
    "health_education", "nutrition_food", "weight_management",
    "fitness_exercise", "habit_lifestyle", "personal_growth",
    "emotional_story", "family_relationship", "product_explanation",
    "interview_conversation", "general_knowledge",
}
CONTENT_FORMATS = {
    "knowledge_explanation", "practical_advice", "myth_busting",
    "risk_warning", "motivational_message", "personal_story",
    "progress_checkin", "interview", "product_introduction",
}
TOPICS = {
    "general_health", "nutrition", "weight_loss", "blood_sugar",
    "metabolism", "fitness", "sleep", "habits", "medical_risk",
    "emotional_wellbeing", "family", "self_growth", "motivation",
    "science_education", "product", "lifestyle", "other",
}
MOODS = {
    "calm", "warm", "healing", "hopeful", "encouraging", "cheerful",
    "lively", "confident", "serious", "rational", "urgent", "tense",
    "emotional", "nostalgic", "inspiring", "neutral",
}
VALENCES = {"positive", "neutral", "negative", "mixed"}
PACES = {"slow", "medium_slow", "medium", "medium_fast", "fast"}
PACE_LEVELS = {
    "slow": 1, "medium_slow": 2, "medium": 3, "medium_fast": 4, "fast": 5,
}
SPEECH_DENSITIES = {"low", "medium", "high"}
VOCAL_PREFERENCES = {
    "instrumental_only", "prefer_instrumental", "vocal_allowed", "prefer_vocal",
}
OPENING_PREFERENCES = {"soft", "immediate", "gradual", "no_preference"}
AVOID_TRAITS = {
    "strong_vocals", "dense_arrangement", "dramatic_drop",
    "excessive_tension", "excessive_sadness", "playful_comedy",
    "ceremonial_grandness", "fast_percussion", "slow_intro",
}
VOCAL_PROFILES = {"instrumental", "mixed", "vocal"}
PROFILE_REVIEW_STATUSES = {"reviewed", "needs_review"}
SOURCE_CONFIDENCES = {"high", "medium", "low"}
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

INTENT_FIELDS = {
    "primary_scene", "secondary_scenes", "content_format", "topics",
    "primary_mood", "secondary_moods", "valence", "energy", "pace",
    "seriousness", "warmth", "tension", "speech_density",
    "vocal_preference", "opening_preference", "avoid_traits", "confidence",
}
PROFILE_FIELDS = {
    "identity", "source_bgm_id", "name", "profile_review_status",
    "auto_eligible", "rights_status", "rights_allowed", "enabled", "scenes",
    "content_formats", "topics", "moods", "valence", "energy", "pace",
    "seriousness", "warmth", "tension", "speech_interference",
    "vocal_profile", "opening_strength", "loop_suitability", "traits",
    "style_tags", "legacy_scene_scores", "source_confidence", "source_confirmed",
}


class MusicProfileError(ValueError):
    """The runtime profile manifest or one analysis intent is invalid."""


class NoEligibleMusicError(RuntimeError):
    """No reviewed local track passed every hard filter."""


@dataclass(frozen=True)
class _ScoredProfile:
    profile: dict[str, Any]
    asset: dict[str, Any]
    score: float
    score_before_recency: float
    recency_penalty: float
    breakdown: dict[str, float]


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MusicProfileError(f"{label} 必须是 JSON 对象")
    return value


def _strict_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise MusicProfileError(f"{label} 必须是布尔值")
    return value


def _level(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 5:
        raise MusicProfileError(f"{label} 必须是 1 到 5 的整数")
    return value


def _choice(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise MusicProfileError(f"{label} 不在受控标签范围内: {value!r}")
    return value


def _choices(
    value: Any,
    allowed: set[str],
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MusicProfileError(f"{label} 必须是字符串数组")
    if len(value) < minimum or (maximum is not None and len(value) > maximum):
        raise MusicProfileError(f"{label} 数量不符合契约")
    if len(value) != len(set(value)):
        raise MusicProfileError(f"{label} 不能包含重复值")
    invalid = [item for item in value if item not in allowed]
    if invalid:
        raise MusicProfileError(f"{label} 包含未知标签: {invalid[0]}")
    return list(value)


def _closeness(left: int, right: int) -> float:
    return max(0.0, 1.0 - abs(left - right) / 4.0)


def _normalize_intent(raw: Mapping[str, Any]) -> dict[str, Any]:
    intent = dict(raw)
    missing = sorted(INTENT_FIELDS - set(intent))
    extras = sorted(set(intent) - INTENT_FIELDS)
    if missing or extras:
        detail = f"缺少字段 {missing}" if missing else f"未知字段 {extras}"
        raise MusicProfileError(f"music_intent 结构不正确：{detail}")
    normalized = {
        "primary_scene": _choice(intent["primary_scene"], SCENES, "primary_scene"),
        "secondary_scenes": _choices(
            intent["secondary_scenes"], SCENES, "secondary_scenes", maximum=3
        ),
        "content_format": _choice(
            intent["content_format"], CONTENT_FORMATS, "content_format"
        ),
        "topics": _choices(intent["topics"], TOPICS, "topics", minimum=1, maximum=5),
        "primary_mood": _choice(intent["primary_mood"], MOODS, "primary_mood"),
        "secondary_moods": _choices(
            intent["secondary_moods"], MOODS, "secondary_moods", maximum=3
        ),
        "valence": _choice(intent["valence"], VALENCES, "valence"),
        "energy": _level(intent["energy"], "energy"),
        "pace": _choice(intent["pace"], PACES, "pace"),
        "seriousness": _level(intent["seriousness"], "seriousness"),
        "warmth": _level(intent["warmth"], "warmth"),
        "tension": _level(intent["tension"], "tension"),
        "speech_density": _choice(
            intent["speech_density"], SPEECH_DENSITIES, "speech_density"
        ),
        "vocal_preference": _choice(
            intent["vocal_preference"], VOCAL_PREFERENCES, "vocal_preference"
        ),
        "opening_preference": _choice(
            intent["opening_preference"], OPENING_PREFERENCES, "opening_preference"
        ),
        "avoid_traits": _choices(
            intent["avoid_traits"], AVOID_TRAITS, "avoid_traits", maximum=5
        ),
    }
    confidence = intent["confidence"]
    if type(confidence) not in {int, float} or not 0.0 <= float(confidence) <= 1.0:
        raise MusicProfileError("confidence 必须在 0.0 到 1.0 之间")
    normalized["confidence"] = float(confidence)
    if normalized["primary_scene"] in normalized["secondary_scenes"]:
        raise MusicProfileError("secondary_scenes 不能重复 primary_scene")
    if normalized["primary_mood"] in normalized["secondary_moods"]:
        raise MusicProfileError("secondary_moods 不能重复 primary_mood")
    return normalized


def _validate_profile(raw: Any, index: int) -> dict[str, Any]:
    profile = _require_object(raw, f"profiles[{index}]")
    missing = sorted(PROFILE_FIELDS - set(profile))
    extras = sorted(set(profile) - PROFILE_FIELDS)
    if missing or extras:
        detail = f"缺少字段 {missing}" if missing else f"未知字段 {extras}"
        raise MusicProfileError(f"profiles[{index}] 结构不正确：{detail}")
    identity = profile["identity"]
    if not isinstance(identity, str) or not identity.startswith("music_id:"):
        raise MusicProfileError(f"profiles[{index}].identity 必须是稳定 music_id")
    for field in ("source_bgm_id", "name", "rights_status"):
        if not isinstance(profile[field], str) or not profile[field].strip():
            raise MusicProfileError(f"profiles[{index}].{field} 不能为空")
    _choice(profile["profile_review_status"], PROFILE_REVIEW_STATUSES, "profile_review_status")
    _choice(profile["source_confidence"], SOURCE_CONFIDENCES, "source_confidence")
    for field in ("auto_eligible", "rights_allowed", "enabled", "source_confirmed"):
        _strict_bool(profile[field], f"profiles[{index}].{field}")
    _choices(profile["scenes"], SCENES, "scenes", minimum=1)
    _choices(profile["content_formats"], CONTENT_FORMATS, "content_formats", minimum=1)
    _choices(profile["topics"], TOPICS, "topics", minimum=1, maximum=5)
    _choices(profile["moods"], MOODS, "moods", minimum=1)
    _choice(profile["valence"], VALENCES, "valence")
    _choice(profile["pace"], PACES, "pace")
    _choice(profile["vocal_profile"], VOCAL_PROFILES, "vocal_profile")
    _choices(profile["traits"], AVOID_TRAITS, "traits")
    for field in (
        "energy", "seriousness", "warmth", "tension", "speech_interference",
        "opening_strength", "loop_suitability",
    ):
        _level(profile[field], f"profiles[{index}].{field}")
    if not isinstance(profile["style_tags"], list) or not all(
        isinstance(item, str) and item for item in profile["style_tags"]
    ):
        raise MusicProfileError(f"profiles[{index}].style_tags 必须是非空字符串数组")
    legacy = _require_object(profile["legacy_scene_scores"], "legacy_scene_scores")
    if set(legacy) != {"chicken_soup_persona", "knowledge", "interview", "weight_checkin"}:
        raise MusicProfileError("legacy_scene_scores 字段不完整")
    if any(type(value) is not int or not 0 <= value <= 100 for value in legacy.values()):
        raise MusicProfileError("legacy_scene_scores 必须是 0 到 100 的整数")
    if profile["profile_review_status"] == "needs_review" and profile["auto_eligible"]:
        raise MusicProfileError("needs_review 曲目不能自动参与匹配")
    return profile


def _scene_quality(intent: dict[str, Any], profile: dict[str, Any]) -> float:
    profile_scenes = set(profile["scenes"])
    if intent["primary_scene"] in profile_scenes:
        return 1.0
    if profile_scenes.intersection(intent["secondary_scenes"]):
        return 0.75
    return 0.0


def _content_quality(intent: dict[str, Any], profile: dict[str, Any]) -> float:
    format_quality = 1.0 if intent["content_format"] in profile["content_formats"] else 0.0
    topic_quality = len(set(intent["topics"]).intersection(profile["topics"])) / len(intent["topics"])
    return 0.65 * format_quality + 0.35 * topic_quality


def _mood_quality(intent: dict[str, Any], profile: dict[str, Any]) -> float:
    profile_moods = set(profile["moods"])
    if intent["primary_mood"] in profile_moods:
        mood = 1.0
    elif profile_moods.intersection(intent["secondary_moods"]):
        mood = 0.7
    else:
        mood = 0.0
    if intent["valence"] == profile["valence"]:
        valence = 1.0
    elif "mixed" in {intent["valence"], profile["valence"]}:
        valence = 0.6
    elif "neutral" in {intent["valence"], profile["valence"]}:
        valence = 0.4
    else:
        valence = 0.0
    return 0.7 * mood + 0.3 * valence


def _energy_pace_quality(intent: dict[str, Any], profile: dict[str, Any]) -> float:
    return 0.55 * _closeness(intent["energy"], profile["energy"]) + 0.45 * _closeness(
        PACE_LEVELS[intent["pace"]], PACE_LEVELS[profile["pace"]]
    )


def _expression_quality(intent: dict[str, Any], profile: dict[str, Any]) -> float:
    return sum(
        _closeness(intent[field], profile[field])
        for field in ("seriousness", "warmth", "tension")
    ) / 3.0


def _speech_vocal_quality(intent: dict[str, Any], profile: dict[str, Any]) -> float:
    maximum_interference = {"low": 4, "medium": 3, "high": 1}[intent["speech_density"]]
    excess = max(0, profile["speech_interference"] - maximum_interference)
    speech_safety = max(0.0, 1.0 - excess / 4.0)
    vocal_scores = {
        "instrumental_only": {"instrumental": 1.0, "mixed": 0.35, "vocal": 0.0},
        "prefer_instrumental": {"instrumental": 1.0, "mixed": 0.7, "vocal": 0.2},
        "vocal_allowed": {"instrumental": 1.0, "mixed": 1.0, "vocal": 0.9},
        "prefer_vocal": {"instrumental": 0.5, "mixed": 0.8, "vocal": 1.0},
    }
    vocal = vocal_scores[intent["vocal_preference"]][profile["vocal_profile"]]
    opening_target = {
        "soft": 1,
        "gradual": 2,
        "immediate": 5,
        "no_preference": profile["opening_strength"],
    }[intent["opening_preference"]]
    opening = _closeness(opening_target, profile["opening_strength"])
    return 0.6 * speech_safety + 0.3 * vocal + 0.1 * opening


def _score(intent: dict[str, Any], profile: dict[str, Any]) -> dict[str, float]:
    qualities = {
        "scene": _scene_quality(intent, profile),
        "content_format": _content_quality(intent, profile),
        "mood_valence": _mood_quality(intent, profile),
        "energy_pace": _energy_pace_quality(intent, profile),
        "expression_axes": _expression_quality(intent, profile),
        "speech_vocal": _speech_vocal_quality(intent, profile),
    }
    return {
        key: round(qualities[key] * MUSIC_MATCHER_WEIGHTS_V1[key], 4)
        for key in MUSIC_MATCHER_WEIGHTS_V1
    }


class MusicProfileMatcher:
    """Validate the shipped profile library and return one deterministic Top1 track."""

    def __init__(self, audio_library_root: str | Path):
        self.root = Path(audio_library_root).expanduser().resolve()
        self.profile_path = self.root / "manifest" / "music_profiles.v1.json"
        self.audio_catalog = AudioCatalog(self.root)

    def snapshot(self) -> dict[str, Any]:
        if not self.profile_path.is_file():
            raise MusicProfileError(f"音乐标签清单不存在: {self.profile_path}")
        raw_bytes = self.profile_path.read_bytes()
        try:
            document = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MusicProfileError("音乐标签清单不是有效 UTF-8 JSON") from exc
        document = _require_object(document, "music profile manifest")
        if document.get("schema") != MUSIC_PROFILE_SCHEMA:
            raise MusicProfileError("不支持的音乐标签清单 schema")
        if document.get("taxonomy_version") != MUSIC_TAXONOMY_VERSION:
            raise MusicProfileError("音乐标签 taxonomy_version 不匹配")
        if document.get("matcher_version") != MUSIC_MATCHER_VERSION:
            raise MusicProfileError("音乐 matcher_version 不匹配")
        if document.get("weights") != MUSIC_MATCHER_WEIGHTS_V1:
            raise MusicProfileError("音乐评分权重与 music-matcher.v1 不一致")
        if tuple(document.get("hard_filters", ())) != MUSIC_MATCHER_HARD_FILTERS_V1:
            raise MusicProfileError("音乐硬过滤规则与 music-matcher.v1 不一致")
        profile_version = document.get("profile_version")
        if not isinstance(profile_version, str) or not profile_version:
            raise MusicProfileError("profile_version 不能为空")
        raw_profiles = document.get("profiles")
        if not isinstance(raw_profiles, list) or not raw_profiles:
            raise MusicProfileError("profiles 必须是非空数组")
        profiles = [_validate_profile(raw, index) for index, raw in enumerate(raw_profiles)]
        identities = [profile["identity"] for profile in profiles]
        if len(identities) != len(set(identities)):
            raise MusicProfileError("音乐标签清单包含重复 identity")
        source_ids = [profile["source_bgm_id"] for profile in profiles]
        if len(source_ids) != len(set(source_ids)):
            raise MusicProfileError("音乐标签清单包含重复 source_bgm_id")

        assets = self.audio_catalog.snapshot().get("assets", [])
        assets_by_identity = {str(asset.get("identity")): asset for asset in assets}
        for profile in profiles:
            asset = assets_by_identity.get(profile["identity"])
            if asset is None:
                raise MusicProfileError(f"标签没有对应音频素材: {profile['identity']}")
            if str(asset.get("name")) != profile["name"]:
                raise MusicProfileError(f"标签名称与音频 manifest 不一致: {profile['identity']}")
        return {
            "schema": MUSIC_PROFILE_SCHEMA,
            "taxonomy_version": MUSIC_TAXONOMY_VERSION,
            "matcher_version": MUSIC_MATCHER_VERSION,
            "profile_version": profile_version,
            "profile_hash": hashlib.sha256(raw_bytes).hexdigest(),
            "source": document.get("source", {}),
            "profile_count": len(profiles),
            "auto_eligible_count": sum(1 for profile in profiles if profile["auto_eligible"]),
            "needs_review_count": sum(
                1 for profile in profiles if profile["profile_review_status"] == "needs_review"
            ),
            "profiles": profiles,
            "assets_by_identity": assets_by_identity,
        }

    def recommend(
        self,
        music_intent: Mapping[str, Any],
        *,
        video_duration_us: int,
        excluded_identities: Iterable[str] = (),
        recent_identity_counts: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        if type(video_duration_us) is not int or video_duration_us < 0:
            raise MusicProfileError("video_duration_us 必须是非负整数")
        intent = _normalize_intent(music_intent)
        snapshot = self.snapshot()
        excluded = {str(identity) for identity in excluded_identities}
        recent_counts = dict(recent_identity_counts or {})
        filter_counts = {name: 0 for name in MUSIC_MATCHER_HARD_FILTERS_V1}
        filter_counts["excluded_identity"] = 0
        scored: list[_ScoredProfile] = []
        avoid_traits = set(intent["avoid_traits"])

        for profile in snapshot["profiles"]:
            identity = profile["identity"]
            asset = snapshot["assets_by_identity"][identity]
            if not profile["enabled"] or not asset.get("available"):
                filter_counts["enabled_and_available"] += 1
                continue
            if not profile["auto_eligible"]:
                filter_counts["auto_eligible"] += 1
                continue
            if not profile["rights_allowed"]:
                filter_counts["rights_allowed"] += 1
                continue
            duration_us = int(asset.get("duration_us") or 0)
            if duration_us < video_duration_us:
                filter_counts["duration_covers_video"] += 1
                continue
            if avoid_traits.intersection(profile["traits"]):
                filter_counts["forbidden_traits_absent"] += 1
                continue
            if identity in excluded:
                filter_counts["excluded_identity"] += 1
                continue
            recent_count = recent_counts.get(identity, 0)
            if type(recent_count) is not int or recent_count < 0:
                raise MusicProfileError("recent_identity_counts 必须使用非负整数")
            breakdown = _score(intent, profile)
            before_recency = round(sum(breakdown.values()), 4)
            recency_penalty = min(float(recent_count) * RECENT_USE_PENALTY, MAX_RECENT_USE_PENALTY)
            final_score = round(max(0.0, before_recency - recency_penalty), 4)
            scored.append(
                _ScoredProfile(
                    profile=profile,
                    asset=asset,
                    score=final_score,
                    score_before_recency=before_recency,
                    recency_penalty=recency_penalty,
                    breakdown=breakdown,
                )
            )

        if not scored:
            raise NoEligibleMusicError(
                "没有音乐同时满足可用、已审核、版权、时长和禁用特征条件"
            )
        selected = sorted(
            scored,
            key=lambda item: (
                -item.score,
                -CONFIDENCE_ORDER[item.profile["source_confidence"]],
                -item.profile["loop_suitability"],
                item.profile["identity"],
            ),
        )[0]
        return {
            "bgm_identity": selected.profile["identity"],
            "name": selected.profile["name"],
            "score": selected.score,
            "score_before_recency": selected.score_before_recency,
            "recency_penalty": selected.recency_penalty,
            "score_breakdown": selected.breakdown,
            "selection_source": "ai",
            "matcher_version": snapshot["matcher_version"],
            "taxonomy_version": snapshot["taxonomy_version"],
            "profile_version": snapshot["profile_version"],
            "profile_hash": snapshot["profile_hash"],
            "eligible_count": len(scored),
            "filtered_counts": filter_counts,
        }
