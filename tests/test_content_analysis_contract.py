from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.content_analysis import (
    CONTENT_ANALYSIS_SCHEMA_VERSION,
    MUSIC_MATCHER_HARD_FILTERS_V1,
    MUSIC_MATCHER_VERSION,
    MUSIC_MATCHER_WEIGHTS_V1,
    ContentAnalysisContractError,
    ContentAnalysisResult,
    content_analysis_json_schema,
    parse_content_analysis_payload,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"
ORIGINAL_SCRIPT = "那么通过八十四天"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_valid_fixture_round_trips_without_changing_script() -> None:
    payload = load_fixture("content_analysis_v1_valid.json")

    result = parse_content_analysis_payload(payload, original_script=ORIGINAL_SCRIPT)

    assert result.schema_version == CONTENT_ANALYSIS_SCHEMA_VERSION
    assert "".join(unit.text for unit in result.subtitle_units) == ORIGINAL_SCRIPT
    assert result.model_dump(mode="json") == payload


def test_json_string_uses_the_same_contract() -> None:
    payload = load_fixture("content_analysis_v1_valid.json")

    result = parse_content_analysis_payload(
        json.dumps(payload, ensure_ascii=False),
        original_script=ORIGINAL_SCRIPT,
    )

    assert isinstance(result, ContentAnalysisResult)


@pytest.mark.parametrize(
    ("fixture_name", "expected_code"),
    [
        ("content_analysis_v1_changed_text.json", "text_mismatch"),
        ("content_analysis_v1_gap.json", "gap"),
    ],
)
def test_source_script_mismatch_is_rejected(
    fixture_name: str,
    expected_code: str,
) -> None:
    with pytest.raises(ContentAnalysisContractError) as error:
        parse_content_analysis_payload(
            load_fixture(fixture_name),
            original_script=ORIGINAL_SCRIPT,
        )

    assert error.value.code == expected_code


def test_overlap_or_reordering_is_rejected() -> None:
    payload = load_fixture("content_analysis_v1_valid.json")
    payload["subtitle_units"][1]["start"] = 1
    payload["subtitle_units"][1]["text"] = ORIGINAL_SCRIPT[1:4]

    with pytest.raises(ContentAnalysisContractError) as error:
        parse_content_analysis_payload(payload, original_script=ORIGINAL_SCRIPT)

    assert error.value.code == "overlap_or_reorder"


def test_repeated_unit_is_rejected_as_overlap() -> None:
    payload = load_fixture("content_analysis_v1_valid.json")
    payload["subtitle_units"].insert(1, deepcopy(payload["subtitle_units"][0]))

    with pytest.raises(ContentAnalysisContractError) as error:
        parse_content_analysis_payload(payload, original_script=ORIGINAL_SCRIPT)

    assert error.value.code == "overlap_or_reorder"


def test_out_of_bounds_unit_is_rejected() -> None:
    payload = load_fixture("content_analysis_v1_valid.json")
    final_unit = payload["subtitle_units"][-1]
    final_unit["end"] = len(ORIGINAL_SCRIPT) + 1
    final_unit["text"] = "天外"

    with pytest.raises(ContentAnalysisContractError) as error:
        parse_content_analysis_payload(payload, original_script=ORIGINAL_SCRIPT)

    assert error.value.code == "out_of_bounds"


def test_empty_unit_is_rejected_by_the_json_contract() -> None:
    payload = load_fixture("content_analysis_v1_valid.json")
    payload["subtitle_units"][1]["end"] = payload["subtitle_units"][1]["start"]
    payload["subtitle_units"][1]["text"] = ""

    with pytest.raises(ValidationError):
        parse_content_analysis_payload(payload, original_script=ORIGINAL_SCRIPT)


def test_incomplete_final_coverage_is_rejected() -> None:
    payload = load_fixture("content_analysis_v1_valid.json")
    payload["subtitle_units"] = payload["subtitle_units"][:-1]
    payload["subtitle_units"][-1]["bind"] = "none"
    payload["subtitle_units"][-1]["break_after"] = "prefer"

    with pytest.raises(ContentAnalysisContractError) as error:
        parse_content_analysis_payload(payload, original_script=ORIGINAL_SCRIPT)

    assert error.value.code == "incomplete_coverage"


def test_binding_rules_prevent_connector_from_hanging_on_previous_caption() -> None:
    payload = load_fixture("content_analysis_v1_valid.json")
    payload["subtitle_units"][0]["break_after"] = "allow"

    with pytest.raises(ValidationError, match="right-bound units"):
        parse_content_analysis_payload(payload, original_script=ORIGINAL_SCRIPT)


def test_left_binding_requires_previous_break_to_be_avoided() -> None:
    payload = load_fixture("content_analysis_v1_valid.json")
    payload["subtitle_units"][2]["bind"] = "left"
    payload["subtitle_units"][2]["break_after"] = "allow"

    with pytest.raises(ContentAnalysisContractError) as error:
        parse_content_analysis_payload(payload, original_script=ORIGINAL_SCRIPT)

    assert error.value.code == "binding_break_conflict"


def test_python_unicode_code_point_offsets_are_explicit() -> None:
    payload = load_fixture("content_analysis_v1_valid.json")
    payload["subtitle_units"] = [
        {
            "start": 0,
            "end": 1,
            "text": "🍎",
            "kind": "word",
            "bind": "none",
            "break_after": "allow",
        },
        {
            "start": 1,
            "end": 3,
            "text": "很好",
            "kind": "phrase",
            "bind": "none",
            "break_after": "prefer",
        },
    ]

    result = parse_content_analysis_payload(payload, original_script="🍎很好")

    assert result.subtitle_units[1].start == 1


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("music_intent", "energy"), 6),
        (("music_intent", "energy"), "3"),
        (("music_intent", "confidence"), 1.1),
        (("subtitle_units", 0, "start"), "0"),
    ],
)
def test_numeric_ranges_and_types_are_strict(path: tuple, value: object) -> None:
    payload = load_fixture("content_analysis_v1_valid.json")
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        parse_content_analysis_payload(payload, original_script=ORIGINAL_SCRIPT)


def test_unknown_fields_reject_timestamps_visual_cues_and_music_file_selection() -> None:
    base = load_fixture("content_analysis_v1_valid.json")
    invalid_payloads = []
    for key, value in (
        ("visual_cues", []),
        ("start_us", 0),
        ("bgm_identity", "music_id:should-not-be-selected-by-model"),
    ):
        payload = deepcopy(base)
        payload[key] = value
        invalid_payloads.append(payload)

    for payload in invalid_payloads:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            parse_content_analysis_payload(payload, original_script=ORIGINAL_SCRIPT)

    nested_timestamp = deepcopy(base)
    nested_timestamp["subtitle_units"][0]["start_us"] = 0
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_content_analysis_payload(
            nested_timestamp,
            original_script=ORIGINAL_SCRIPT,
        )


def test_music_lists_are_unique_and_do_not_repeat_primary_values() -> None:
    duplicate_list = load_fixture("content_analysis_v1_valid.json")
    duplicate_list["music_intent"]["topics"] = ["nutrition", "nutrition"]
    duplicate_primary = load_fixture("content_analysis_v1_valid.json")
    duplicate_primary["music_intent"]["secondary_moods"] = ["rational"]

    for payload in (duplicate_list, duplicate_primary):
        with pytest.raises(ValidationError):
            parse_content_analysis_payload(payload, original_script=ORIGINAL_SCRIPT)


def test_schema_is_versioned_strict_and_provider_neutral() -> None:
    schema = content_analysis_json_schema()

    assert schema["$id"].endswith("jyd.content-analysis.v1.json")
    assert schema["title"] == "JYD Content Analysis v1"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version",
        "music_intent",
        "subtitle_units",
    }
    serialized = json.dumps(schema, ensure_ascii=False)
    assert "visual_cues" not in serialized
    assert "start_us" not in serialized
    assert "bgm_identity" not in serialized


def test_music_matcher_v1_weights_and_hard_filters_are_frozen() -> None:
    assert MUSIC_MATCHER_VERSION == "music-matcher.v1"
    assert sum(MUSIC_MATCHER_WEIGHTS_V1.values()) == 100
    assert MUSIC_MATCHER_WEIGHTS_V1 == {
        "scene": 25,
        "content_format": 20,
        "mood_valence": 20,
        "energy_pace": 15,
        "expression_axes": 10,
        "speech_vocal": 10,
    }
    assert "duration_covers_video" in MUSIC_MATCHER_HARD_FILTERS_V1
