"""Controlled v1 taxonomy shared by analysis and local music matching."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType


MUSIC_MATCHER_VERSION = "music-matcher.v1"


class MusicScene(str, Enum):
    HEALTH_EDUCATION = "health_education"
    NUTRITION_FOOD = "nutrition_food"
    WEIGHT_MANAGEMENT = "weight_management"
    FITNESS_EXERCISE = "fitness_exercise"
    HABIT_LIFESTYLE = "habit_lifestyle"
    PERSONAL_GROWTH = "personal_growth"
    EMOTIONAL_STORY = "emotional_story"
    FAMILY_RELATIONSHIP = "family_relationship"
    PRODUCT_EXPLANATION = "product_explanation"
    INTERVIEW_CONVERSATION = "interview_conversation"
    GENERAL_KNOWLEDGE = "general_knowledge"


class ContentFormat(str, Enum):
    KNOWLEDGE_EXPLANATION = "knowledge_explanation"
    PRACTICAL_ADVICE = "practical_advice"
    MYTH_BUSTING = "myth_busting"
    RISK_WARNING = "risk_warning"
    MOTIVATIONAL_MESSAGE = "motivational_message"
    PERSONAL_STORY = "personal_story"
    PROGRESS_CHECKIN = "progress_checkin"
    INTERVIEW = "interview"
    PRODUCT_INTRODUCTION = "product_introduction"


class ContentTopic(str, Enum):
    GENERAL_HEALTH = "general_health"
    NUTRITION = "nutrition"
    WEIGHT_LOSS = "weight_loss"
    BLOOD_SUGAR = "blood_sugar"
    METABOLISM = "metabolism"
    FITNESS = "fitness"
    SLEEP = "sleep"
    HABITS = "habits"
    MEDICAL_RISK = "medical_risk"
    EMOTIONAL_WELLBEING = "emotional_wellbeing"
    FAMILY = "family"
    SELF_GROWTH = "self_growth"
    MOTIVATION = "motivation"
    SCIENCE_EDUCATION = "science_education"
    PRODUCT = "product"
    LIFESTYLE = "lifestyle"
    OTHER = "other"


class MusicMood(str, Enum):
    CALM = "calm"
    WARM = "warm"
    HEALING = "healing"
    HOPEFUL = "hopeful"
    ENCOURAGING = "encouraging"
    CHEERFUL = "cheerful"
    LIVELY = "lively"
    CONFIDENT = "confident"
    SERIOUS = "serious"
    RATIONAL = "rational"
    URGENT = "urgent"
    TENSE = "tense"
    EMOTIONAL = "emotional"
    NOSTALGIC = "nostalgic"
    INSPIRING = "inspiring"
    NEUTRAL = "neutral"


class Valence(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    MIXED = "mixed"


class Pace(str, Enum):
    SLOW = "slow"
    MEDIUM_SLOW = "medium_slow"
    MEDIUM = "medium"
    MEDIUM_FAST = "medium_fast"
    FAST = "fast"


class SpeechDensity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class VocalPreference(str, Enum):
    INSTRUMENTAL_ONLY = "instrumental_only"
    PREFER_INSTRUMENTAL = "prefer_instrumental"
    VOCAL_ALLOWED = "vocal_allowed"
    PREFER_VOCAL = "prefer_vocal"


class OpeningPreference(str, Enum):
    SOFT = "soft"
    IMMEDIATE = "immediate"
    GRADUAL = "gradual"
    NO_PREFERENCE = "no_preference"


class MusicAvoidTrait(str, Enum):
    STRONG_VOCALS = "strong_vocals"
    DENSE_ARRANGEMENT = "dense_arrangement"
    DRAMATIC_DROP = "dramatic_drop"
    EXCESSIVE_TENSION = "excessive_tension"
    EXCESSIVE_SADNESS = "excessive_sadness"
    PLAYFUL_COMEDY = "playful_comedy"
    CEREMONIAL_GRANDNESS = "ceremonial_grandness"
    FAST_PERCUSSION = "fast_percussion"
    SLOW_INTRO = "slow_intro"


# Module 2 will implement the scorer.  Freezing the dimensions here prevents the
# spreadsheet and prose requirements from becoming two competing algorithms.
MUSIC_MATCHER_WEIGHTS_V1 = MappingProxyType(
    {
        "scene": 25,
        "content_format": 20,
        "mood_valence": 20,
        "energy_pace": 15,
        "expression_axes": 10,
        "speech_vocal": 10,
    }
)

# These conditions must be checked before scoring.  They are deliberately not
# converted into soft points, because an unavailable or unusable track must never
# win merely through a strong semantic match.
MUSIC_MATCHER_HARD_FILTERS_V1 = (
    "enabled_and_available",
    "auto_eligible",
    "rights_allowed",
    "duration_covers_video",
    "forbidden_traits_absent",
)
