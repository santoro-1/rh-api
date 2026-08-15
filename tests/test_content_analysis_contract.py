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
from app.services.content_analysis.contracts import (
    CONTENT_ANALYSIS_PROVIDER_SCHEMA_VERSION,
    boundary_indexed_script,
    content_analysis_provider_json_schema,
    parse_subtitle_break_plan_payload,
    subtitle_break_candidate_positions,
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


def test_provider_schema_returns_only_compact_three_branch_decisions() -> None:
    schema = content_analysis_provider_json_schema()

    assert schema["$id"].endswith("jyd.content-analysis.provider.v5.json")
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "music_intent",
        "subtitle_breaks",
        "visual_plan",
        "title",
    ]
    serialized = json.dumps(schema, ensure_ascii=False)
    assert "schema_version" not in schema["properties"]
    assert "subtitle_units" not in serialized
    assert '"text"' not in serialized
    assert "timestamp" not in serialized
    assert "asset" not in serialized
    assert "VA[0-9]+" in serialized


def test_boundary_indexed_script_withholds_punctuation_and_ascii_word_breaks() -> None:
    indexed = boundary_indexed_script("ADP提高，百分之八十四")

    assert "A⟦" not in indexed
    assert "D⟦" not in indexed
    assert "高⟦" not in indexed
    assert "，⟦" not in indexed
    assert "高，百" in indexed
    assert indexed.replace("⟦", "").count("B") > 0


def test_boundary_candidates_withhold_words_and_particle_starts_but_allow_completed_modifiers() -> None:
    script = "破罐子破摔，工作上的情绪，从原来的160斤"
    positions = set(subtitle_break_candidate_positions(script))

    idiom_start = script.index("破罐子破摔")
    emotion_start = script.index("情绪")
    particle = script.index("的", script.index("原来"))
    assert not positions.intersection(range(idiom_start + 1, idiom_start + len("破罐子破摔")))
    assert emotion_start + 1 not in positions
    assert particle not in positions
    assert particle + 1 in positions


def test_boundary_candidates_offer_task_30_completed_modifier() -> None:
    script = "世界上公认的十大免费最好的医生"
    positions = set(subtitle_break_candidate_positions(script))

    modifier_end = len("世界上公认的")
    assert modifier_end in positions
    assert script[:modifier_end] == "世界上公认的"


def test_boundary_candidates_keep_bound_relative_suffixes_intact() -> None:
    script = "早餐不要吃快餐类的，早餐不要吃蛋糕类的，疲惫生活中的一副重要解药"
    positions = set(subtitle_break_candidate_positions(script))

    first_category = script.index("类的")
    second_category = script.index("类的", first_category + 1)
    locative = script.index("中的")
    assert first_category not in positions
    assert second_category not in positions
    assert locative not in positions
    assert locative + len("中的") in positions


def test_break_plan_is_expanded_from_exact_source_slices() -> None:
    script = "百分之八十四是由呼吸的形式来离开我们身体的"

    units = parse_subtitle_break_plan_payload(
        {"prefer_after": [6], "allow_after": [13]},
        original_script=script,
    )

    assert [unit.text for unit in units] == [
        "百分之八十四",
        "是由呼吸的形式",
        "来离开我们身体的",
    ]
    assert [(unit.start, unit.end) for unit in units] == [(0, 6), (6, 13), (13, 21)]
    assert [unit.break_after.value for unit in units] == ["prefer", "allow", "prefer"]


def test_break_plan_drops_unoffered_positions_and_keeps_valid_boundaries() -> None:
    script = "那么通过八十四天"

    units = parse_subtitle_break_plan_payload(
        {"prefer_after": [4, 99], "allow_after": []},
        original_script=script,
    )

    assert [unit.text for unit in units] == ["那么通过", "八十四天"]
    assert [(unit.start, unit.end) for unit in units] == [(0, 4), (4, 8)]


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
