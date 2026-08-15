"""Strict contract for semantic foreground visual-context analysis."""

from __future__ import annotations

import hashlib
import json
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
from app.services.content_analysis.contracts import _inline_local_schema_refs


VISUAL_ANALYSIS_SCHEMA_VERSION = "jyd.visual-analysis.v1"
VISUAL_ANALYSIS_REQUEST_SCHEMA_VERSION = "jyd.visual-analysis.request.v1"
VISUAL_ANALYSIS_SCHEMA_ID = (
    "https://video.lanyingjk01.com/schemas/jyd.visual-analysis.v1.json"
)
ConfidenceScore = confloat(strict=True, ge=0.0, le=1.0)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class VisualDecisionValue(str, Enum):
    SHOW = "SHOW"
    REVIEW = "REVIEW"
    SKIP = "SKIP"


class VisualUsage(str, Enum):
    LITERAL = "literal"
    INGREDIENT = "ingredient"
    MEAL_EXAMPLE = "meal_example"
    IDIOM = "idiom"
    METAPHOR = "metaphor"
    NEGATED = "negated"
    META_MENTION = "meta_mention"
    PASSING_MENTION = "passing_mention"
    UNCERTAIN = "uncertain"
    NO_ASSET = "no_asset"
    ACTION = "action"
    SCENE = "scene"
    EDITORIAL_CONTEXT = "editorial_context"


class VisualReasonCode(str, Enum):
    LITERAL_CONCRETE_OBJECT = "LITERAL_CONCRETE_OBJECT"
    MATCH_EXACT_OBJECT = "MATCH_EXACT_OBJECT"
    MATCH_SAME_ACTION = "MATCH_SAME_ACTION"
    MATCH_SAME_SCENE = "MATCH_SAME_SCENE"
    MATCH_EDITORIAL_CONTEXT = "MATCH_EDITORIAL_CONTEXT"
    SKIP_IDIOM = "SKIP_IDIOM"
    SKIP_METAPHOR = "SKIP_METAPHOR"
    SKIP_NEGATED = "SKIP_NEGATED"
    SKIP_META_MENTION = "SKIP_META_MENTION"
    SKIP_PASSING_MENTION = "SKIP_PASSING_MENTION"
    SKIP_UNCERTAIN = "SKIP_UNCERTAIN"
    SKIP_NO_ASSET = "SKIP_NO_ASSET"
    SKIP_UNRELATED = "SKIP_UNRELATED"


class AllowedConcept(ContractModel):
    concept_id: StrictStr = Field(min_length=1, max_length=100)
    description: StrictStr = Field(min_length=1, max_length=500)


class VisualCandidate(ContractModel):
    candidate_id: StrictStr = Field(min_length=1, max_length=100)
    text: StrictStr = Field(min_length=1)
    char_start: StrictInt = Field(ge=0)
    char_end: StrictInt = Field(gt=0)
    allowed_concepts: list[AllowedConcept] = Field(min_length=1, max_length=8)
    usage: Literal["explicit", "enrichment", "seam_broll"] = "explicit"
    direct_concept_ids: list[StrictStr] = Field(default_factory=list, max_length=8)
    segment_boundary_us: StrictInt | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_span_and_concepts(self) -> "VisualCandidate":
        if self.char_end <= self.char_start:
            raise ValueError("candidate char_end must be greater than char_start")
        concept_ids = [item.concept_id for item in self.allowed_concepts]
        if len(concept_ids) != len(set(concept_ids)):
            raise ValueError("allowed concept ids must be unique")
        if len(self.direct_concept_ids) != len(set(self.direct_concept_ids)):
            raise ValueError("direct concept ids must be unique")
        if not set(self.direct_concept_ids).issubset(concept_ids):
            raise ValueError("direct concept ids must belong to allowed concepts")
        if self.usage == "seam_broll" and self.segment_boundary_us is None:
            raise ValueError("seam_broll candidate requires segment_boundary_us")
        return self


class VisualAnalysisRequest(ContractModel):
    schema_version: Literal[VISUAL_ANALYSIS_REQUEST_SCHEMA_VERSION]
    original_script: StrictStr = Field(min_length=1)
    script_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: StrictStr = Field(min_length=1, max_length=128)
    candidates: list[VisualCandidate] = Field(max_length=200)

    @field_validator("candidates")
    @classmethod
    def reject_duplicate_candidate_ids(
        cls, candidates: list[VisualCandidate]
    ) -> list[VisualCandidate]:
        ids = [item.candidate_id for item in candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate ids must be unique")
        return candidates

    @model_validator(mode="after")
    def validate_exact_source(self) -> "VisualAnalysisRequest":
        actual_hash = hashlib.sha256(self.original_script.encode("utf-8")).hexdigest()
        if self.script_sha256 != actual_hash:
            raise ValueError("script_sha256 does not match original_script")
        previous_end = -1
        previous_usage = ""
        for candidate in self.candidates:
            if candidate.char_end > len(self.original_script):
                raise ValueError("candidate span exceeds original_script")
            if (
                self.original_script[candidate.char_start : candidate.char_end]
                != candidate.text
            ):
                raise ValueError("candidate text does not match original_script span")
            if candidate.char_start < previous_end and not (
                previous_usage == "seam_broll" and candidate.usage == "seam_broll"
            ):
                raise ValueError("candidates must be ordered and non-overlapping")
            previous_end = candidate.char_end
            previous_usage = candidate.usage
        return self


class VisualDecision(ContractModel):
    candidate_id: StrictStr = Field(min_length=1, max_length=100)
    decision: VisualDecisionValue
    concept_id: StrictStr = Field(min_length=1, max_length=100)
    usage: VisualUsage
    importance: ConfidenceScore
    confidence: ConfidenceScore
    reason_code: VisualReasonCode


class VisualAnalysisResult(ContractModel):
    schema_version: Literal[VISUAL_ANALYSIS_SCHEMA_VERSION]
    script_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: StrictStr = Field(min_length=1, max_length=128)
    decisions: list[VisualDecision]


class VisualAnalysisContractError(ValueError):
    pass


def parse_visual_analysis_request(
    payload: Mapping[str, Any] | str | bytes,
) -> VisualAnalysisRequest:
    try:
        if isinstance(payload, (str, bytes)):
            return VisualAnalysisRequest.model_validate_json(payload)
        return VisualAnalysisRequest.model_validate(payload)
    except ValidationError as exc:
        raise VisualAnalysisContractError(str(exc)) from exc


def parse_visual_analysis_result(
    payload: Mapping[str, Any] | str | bytes,
    *,
    request: VisualAnalysisRequest,
) -> VisualAnalysisResult:
    try:
        if isinstance(payload, (str, bytes)):
            result = VisualAnalysisResult.model_validate_json(payload)
        else:
            result = VisualAnalysisResult.model_validate(payload)
    except ValidationError as exc:
        raise VisualAnalysisContractError(str(exc)) from exc

    expected = {item.candidate_id: item for item in request.candidates}
    if result.script_sha256 != request.script_sha256:
        raise VisualAnalysisContractError("response script_sha256 does not match request")
    if result.catalog_version != request.catalog_version:
        raise VisualAnalysisContractError("response catalog_version does not match request")
    received_ids = [item.candidate_id for item in result.decisions]
    if len(received_ids) != len(set(received_ids)):
        raise VisualAnalysisContractError("duplicate decision candidate_id")
    if set(received_ids) != set(expected):
        raise VisualAnalysisContractError(
            "decisions must contain every input candidate exactly once"
        )
    for decision in result.decisions:
        allowed = {
            item.concept_id for item in expected[decision.candidate_id].allowed_concepts
        }
        if decision.concept_id not in allowed:
            raise VisualAnalysisContractError(
                f"unknown concept for candidate {decision.candidate_id}"
            )
    return result


def visual_analysis_json_schema() -> dict[str, Any]:
    schema = VisualAnalysisResult.model_json_schema(mode="validation")
    schema["$id"] = VISUAL_ANALYSIS_SCHEMA_ID
    schema["title"] = "JYD Visual Analysis v1"
    return _inline_local_schema_refs(schema)


def candidate_set_sha256(candidates: Sequence[VisualCandidate]) -> str:
    canonical = [item.model_dump(mode="json") for item in candidates]
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
