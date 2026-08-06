"""Strict provider-neutral contract for whole-script analysis v1."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    confloat,
    field_validator,
    model_validator,
)

from app.services.content_analysis.taxonomy import (
    ContentFormat,
    ContentTopic,
    MusicAvoidTrait,
    MusicMood,
    MusicScene,
    OpeningPreference,
    Pace,
    SpeechDensity,
    Valence,
    VocalPreference,
)


CONTENT_ANALYSIS_SCHEMA_VERSION = "jyd.content-analysis.v1"
CONTENT_ANALYSIS_SCHEMA_ID = (
    "https://video.lanyingjk01.com/schemas/jyd.content-analysis.v1.json"
)

LevelScore = Field(ge=1, le=5, description="Integer level from 1 (low) to 5 (high).")
ConfidenceScore = confloat(strict=True, ge=0.0, le=1.0)


class SubtitleUnitKind(str, Enum):
    CONNECTOR = "connector"
    WORD = "word"
    NUMBER = "number"
    PROPER_NOUN = "proper_noun"
    PHRASE = "phrase"
    PUNCTUATION = "punctuation"
    WHITESPACE = "whitespace"


class SubtitleBinding(str, Enum):
    NONE = "none"
    LEFT = "left"
    RIGHT = "right"
    BOTH = "both"


class BreakPreference(str, Enum):
    PREFER = "prefer"
    ALLOW = "allow"
    AVOID = "avoid"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class MusicIntent(ContractModel):
    primary_scene: MusicScene
    secondary_scenes: list[MusicScene] = Field(max_length=3)
    content_format: ContentFormat
    topics: list[ContentTopic] = Field(min_length=1, max_length=5)
    primary_mood: MusicMood
    secondary_moods: list[MusicMood] = Field(max_length=3)
    valence: Valence
    energy: StrictInt = LevelScore
    pace: Pace
    seriousness: StrictInt = LevelScore
    warmth: StrictInt = LevelScore
    tension: StrictInt = LevelScore
    speech_density: SpeechDensity
    vocal_preference: VocalPreference
    opening_preference: OpeningPreference
    avoid_traits: list[MusicAvoidTrait] = Field(max_length=5)
    confidence: ConfidenceScore

    @field_validator("secondary_scenes", "topics", "secondary_moods", "avoid_traits")
    @classmethod
    def reject_duplicate_values(cls, values: list[Enum]) -> list[Enum]:
        if len(values) != len(set(values)):
            raise ValueError("list values must be unique")
        return values

    @model_validator(mode="after")
    def reject_primary_duplicates(self) -> "MusicIntent":
        if self.primary_scene in self.secondary_scenes:
            raise ValueError("primary_scene cannot appear in secondary_scenes")
        if self.primary_mood in self.secondary_moods:
            raise ValueError("primary_mood cannot appear in secondary_moods")
        return self


class SubtitleUnit(ContractModel):
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)
    text: StrictStr = Field(min_length=1)
    kind: SubtitleUnitKind
    bind: SubtitleBinding
    break_after: BreakPreference

    @model_validator(mode="after")
    def validate_local_shape(self) -> "SubtitleUnit":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        if self.kind == SubtitleUnitKind.WHITESPACE and not self.text.isspace():
            raise ValueError("whitespace units must contain only whitespace")
        if self.kind != SubtitleUnitKind.WHITESPACE and self.text.isspace():
            raise ValueError("whitespace text must use kind=whitespace")
        if self.bind in {SubtitleBinding.RIGHT, SubtitleBinding.BOTH}:
            if self.break_after != BreakPreference.AVOID:
                raise ValueError("right-bound units must use break_after=avoid")
        return self


class ContentAnalysisResult(ContractModel):
    schema_version: Literal[CONTENT_ANALYSIS_SCHEMA_VERSION]
    music_intent: MusicIntent
    subtitle_units: list[SubtitleUnit] = Field(min_length=1)


class ContentAnalysisContractError(ValueError):
    """A structurally valid payload does not faithfully represent its script."""

    def __init__(self, code: str, message: str, *, unit_index: int | None = None):
        self.code = code
        self.unit_index = unit_index
        prefix = f"{code}"
        if unit_index is not None:
            prefix += f" at subtitle_units[{unit_index}]"
        super().__init__(f"{prefix}: {message}")


def validate_subtitle_units(
    original_script: str,
    units: Sequence[SubtitleUnit],
) -> None:
    """Verify exact, ordered, gap-free Python code-point coverage of a script."""

    if not isinstance(original_script, str):
        raise TypeError("original_script must be a string")
    if not original_script:
        raise ContentAnalysisContractError("empty_script", "script must not be empty")
    if not units:
        raise ContentAnalysisContractError(
            "empty_subtitle_units",
            "at least one subtitle unit is required",
        )

    expected_start = 0
    script_length = len(original_script)
    for index, unit in enumerate(units):
        if unit.start != expected_start:
            relationship = "gap" if unit.start > expected_start else "overlap_or_reorder"
            raise ContentAnalysisContractError(
                relationship,
                f"expected start {expected_start}, received {unit.start}",
                unit_index=index,
            )
        if unit.end > script_length:
            raise ContentAnalysisContractError(
                "out_of_bounds",
                f"end {unit.end} exceeds script length {script_length}",
                unit_index=index,
            )
        expected_text = original_script[unit.start : unit.end]
        if unit.text != expected_text:
            raise ContentAnalysisContractError(
                "text_mismatch",
                "unit text does not equal original_script[start:end]",
                unit_index=index,
            )
        if index == 0 and unit.bind in {
            SubtitleBinding.LEFT,
            SubtitleBinding.BOTH,
        }:
            raise ContentAnalysisContractError(
                "invalid_edge_binding",
                "the first unit cannot bind left",
                unit_index=index,
            )
        if index == len(units) - 1 and unit.bind in {
            SubtitleBinding.RIGHT,
            SubtitleBinding.BOTH,
        }:
            raise ContentAnalysisContractError(
                "invalid_edge_binding",
                "the final unit cannot bind right",
                unit_index=index,
            )
        if index > 0 and unit.bind in {
            SubtitleBinding.LEFT,
            SubtitleBinding.BOTH,
        }:
            if units[index - 1].break_after != BreakPreference.AVOID:
                raise ContentAnalysisContractError(
                    "binding_break_conflict",
                    "a left-bound unit requires the previous break_after=avoid",
                    unit_index=index,
                )
        expected_start = unit.end

    if expected_start != script_length:
        raise ContentAnalysisContractError(
            "incomplete_coverage",
            f"units end at {expected_start}, script length is {script_length}",
            unit_index=len(units) - 1,
        )


def parse_content_analysis_payload(
    payload: Mapping[str, Any] | str | bytes,
    *,
    original_script: str,
) -> ContentAnalysisResult:
    """Parse strict JSON/data and then verify it against the exact source script."""

    if isinstance(payload, (str, bytes)):
        result = ContentAnalysisResult.model_validate_json(payload)
    else:
        result = ContentAnalysisResult.model_validate(payload)
    validate_subtitle_units(original_script, result.subtitle_units)
    return result


def parse_music_intent_payload(payload: Mapping[str, Any]) -> MusicIntent:
    """Validate one music branch without coupling it to subtitle validity."""

    return MusicIntent.model_validate(payload)


def parse_subtitle_units_payload(
    payload: Any,
    *,
    original_script: str,
) -> list[SubtitleUnit]:
    """Validate one subtitle branch and deterministically repair only indexes.

    Ark must still return every required field with strict types. The supplied
    integer ``start``/``end`` values are treated as advisory: when unit texts in
    their original order concatenate exactly to the source script, code-point
    positions are rebuilt from cumulative text lengths before the final strict
    contract validation. No text, ordering, whitespace or semantics are fixed.
    """

    if not isinstance(payload, list) or not payload:
        raise ContentAnalysisContractError(
            "subtitle_schema_invalid",
            "subtitle_units must be a non-empty list",
        )
    required_keys = {"start", "end", "text", "kind", "bind", "break_after"}
    rebuilt: list[SubtitleUnit] = []
    cursor = 0
    texts: list[str] = []
    for index, raw_unit in enumerate(payload):
        if not isinstance(raw_unit, Mapping) or set(raw_unit) != required_keys:
            raise ContentAnalysisContractError(
                "subtitle_schema_invalid",
                "subtitle unit fields do not match the v1 contract",
                unit_index=index,
            )
        if type(raw_unit["start"]) is not int or type(raw_unit["end"]) is not int:
            raise ContentAnalysisContractError(
                "subtitle_schema_invalid",
                "subtitle indexes must be integers",
                unit_index=index,
            )
        text = raw_unit["text"]
        if not isinstance(text, str) or not text:
            raise ContentAnalysisContractError(
                "subtitle_schema_invalid",
                "subtitle text must be a non-empty string",
                unit_index=index,
            )
        texts.append(text)
        repaired = dict(raw_unit)
        repaired["start"] = cursor
        cursor += len(text)
        repaired["end"] = cursor
        try:
            rebuilt.append(SubtitleUnit.model_validate(repaired))
        except ValidationError as exc:
            raise ContentAnalysisContractError(
                "subtitle_schema_invalid",
                "subtitle unit values do not match the v1 contract",
                unit_index=index,
            ) from exc
    if "".join(texts) != original_script:
        raise ContentAnalysisContractError(
            "subtitle_text_mismatch",
            "subtitle unit texts do not exactly reconstruct the source script",
        )
    validate_subtitle_units(original_script, rebuilt)
    return rebuilt


def content_analysis_json_schema() -> dict[str, Any]:
    """Return the canonical schema later supplied to Ark structured output."""

    schema = ContentAnalysisResult.model_json_schema(mode="validation")
    schema["$id"] = CONTENT_ANALYSIS_SCHEMA_ID
    schema["title"] = "JYD Content Analysis v1"
    return schema
