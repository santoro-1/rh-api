from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.project_postprocess import derive_project_render_cues  # noqa: E402
from jyd_probe.semantic_subtitles import (  # noqa: E402
    SemanticSubtitleMappingError,
    map_subtitle_units_to_raw_cues,
    semantic_break_groups,
)


FONT_PATH = (
    PROJECT_ROOT
    / "data"
    / "libraries"
    / "font_library"
    / "files"
    / "DouyinSansBold_7244518590332801592.otf"
)
PRODUCTION_CAPTION_FONT_PATH = (
    PROJECT_ROOT
    / "data"
    / "libraries"
    / "font_library"
    / "files"
    / "FZCuJinLJW_7086699209738424840.ttf"
)
PRODUCTION_CAPTION_FONT_SIZE = 11.0 * 1.32


def _units(parts: list[tuple[str, str, str, str]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    cursor = 0
    for text, kind, bind, break_after in parts:
        result.append(
            {
                "start": cursor,
                "end": cursor + len(text),
                "text": text,
                "kind": kind,
                "bind": bind,
                "break_after": break_after,
            }
        )
        cursor += len(text)
    return result


def _item(
    script: str,
    units: list[dict[str, object]],
    raw_cues: list[dict[str, object]],
    *,
    audio_script_hash: str | None = None,
    prompt_version: str = "jyd.content-analysis.prompt.v1",
) -> dict[str, object]:
    script_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
    return {
        "script_text": script,
        "content_analysis": {
            "subtitle_analysis_status": "SUCCESS",
            "subtitle_units": units,
            "script_sha256": script_hash,
            "schema_version": "jyd.content-analysis.v1",
            "prompt_version": prompt_version,
        },
        "outputs": {
            "audio": {
                "asset_id": "audio-current",
                "version": 2,
                "metadata": {
                    "script_sha256": audio_script_hash or script_hash,
                    "script_length": len(script),
                },
            }
        },
        "subtitles": {
            "bound_audio_asset_id": "audio-current",
            "raw_cues": raw_cues,
        },
    }


def test_v20_boundaries_are_hard_and_ten_full_width_characters_fit() -> None:
    script = "肚子饿了第一个想吃的就是鸡蛋"
    units = _units(
        [
            ("肚子饿了第一个想吃的", "phrase", "none", "prefer"),
            ("就是鸡蛋", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 4_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(
            script,
            units,
            raw_cues,
            prompt_version="jyd.content-analysis.prompt.v20",
        ),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
    )

    assert mapping["status"] == "SUCCESS"
    assert mapping["analysis_prompt_version"] == "jyd.content-analysis.prompt.v20"
    assert len(mapping["analysis_subtitle_sha256"]) == 64
    assert [cue["text"] for cue in render_cues] == [
        "肚子饿了第一个想吃的",
        "就是鸡蛋",
    ]


def test_semantic_layout_keeps_connectors_numbers_words_and_tilde_intact() -> None:
    script = "那么通过八十四天糖原呼吸MiniMax~模型说明"
    units = _units(
        [
            ("那么", "connector", "right", "avoid"),
            ("通过", "word", "none", "prefer"),
            ("八十四", "number", "right", "avoid"),
            ("天", "word", "none", "prefer"),
            ("糖原", "word", "none", "prefer"),
            ("呼吸", "word", "none", "prefer"),
            ("MiniMax~", "proper_noun", "none", "prefer"),
            ("模型说明", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [
        {"start_us": 0, "end_us": 2_400_000, "text": "那么通过八十四天糖原"},
        {"start_us": 2_500_000, "end_us": 4_800_000, "text": "呼吸MiniMax~模型说明"},
    ]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    assert mapping["status"] == "SUCCESS"
    assert mapping["mapped_unit_count"] == len(units)
    assert render_cues
    texts = [str(cue["text"]) for cue in render_cues]
    assert any("那么通过" in text for text in texts)
    assert any("八十四天" in text for text in texts)
    assert any("糖原" in text for text in texts)
    assert any("呼吸" in text for text in texts)
    assert any("MiniMax~" in text for text in texts)
    assert all(int(cue["end_us"]) > int(cue["start_us"]) for cue in render_cues)


def test_spaces_and_newlines_may_be_absent_from_provider_cues_without_losing_version_safety() -> None:
    script = "那么 通过八十四天\n糖原~呼吸"
    units = _units(
        [
            ("那么", "connector", "right", "avoid"),
            (" ", "whitespace", "none", "allow"),
            ("通过", "word", "none", "prefer"),
            ("八十四", "number", "right", "avoid"),
            ("天", "word", "none", "prefer"),
            ("\n", "whitespace", "none", "allow"),
            ("糖原", "word", "none", "prefer"),
            ("~", "punctuation", "none", "allow"),
            ("呼吸", "word", "none", "prefer"),
        ]
    )
    raw_cues = [
        {"start_us": 0, "end_us": 2_000_000, "text": "那么通过八十四天"},
        {"start_us": 2_100_000, "end_us": 3_600_000, "text": "糖原~呼吸"},
    ]

    timed = map_subtitle_units_to_raw_cues(script, units, raw_cues)
    groups = semantic_break_groups(timed)
    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    assert mapping["status"] == "SUCCESS"
    assert len(timed) == len(units)
    assert "".join(str(group["text"]) for group in groups) == script.replace("\n", "")
    assert all("\n" not in str(group["text"]) for group in groups)
    assert "~" in "".join(str(cue["text"]) for cue in render_cues)
    assert all("\n" not in str(cue["text"]) for cue in render_cues)
    assert not any(
        "八十四天" in str(cue["text"]) and "糖原" in str(cue["text"])
        for cue in render_cues
    )


def test_contiguous_raw_cues_remain_hard_timing_boundaries() -> None:
    script = "少走十年弯路觉得我说的对你有用"
    units = _units([(script, "phrase", "none", "avoid")])
    raw_cues = [
        {"start_us": 0, "end_us": 1_600_000, "text": "少走十年弯路"},
        {"start_us": 1_600_000, "end_us": 3_600_000, "text": "觉得我说的对你有用"},
    ]

    timed = map_subtitle_units_to_raw_cues(script, units, raw_cues)
    groups = semantic_break_groups(timed)
    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    assert mapping["status"] == "SUCCESS"
    assert len(timed) == 2
    assert [int(unit["raw_cue_index"]) for unit in timed] == [0, 1]
    assert len(groups) == 2
    assert int(groups[0]["end_us"]) == int(groups[1]["start_us"])
    assert not any(
        "弯路" in str(cue["text"]) and "觉得我说" in str(cue["text"])
        for cue in render_cues
    )


def test_consecutive_omitted_whitespace_uses_one_monotonic_gap() -> None:
    script = "第一段\n\n第二段"
    units = _units(
        [
            ("第一段", "phrase", "none", "prefer"),
            ("\n", "whitespace", "none", "allow"),
            ("\n", "whitespace", "none", "allow"),
            ("第二段", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [
        {"start_us": 0, "end_us": 1_000_000, "text": "第一段"},
        {"start_us": 1_200_000, "end_us": 2_200_000, "text": "第二段"},
    ]

    timed = map_subtitle_units_to_raw_cues(script, units, raw_cues)

    assert [item["text"] for item in timed] == ["第一段", "\n", "\n", "第二段"]
    assert timed[1]["start_us"] == 1_000_000
    assert timed[1]["end_us"] == 1_200_000
    assert timed[2]["start_us"] == 1_200_000
    assert timed[2]["end_us"] == 1_200_000
    assert timed[3]["start_us"] == 1_200_000


def test_local_reflow_prefers_complete_subject_before_adverbial_predicate() -> None:
    script = "八十几岁很多人已经在坐轮椅，"
    units = _units(
        [
            ("八十几岁很多", "phrase", "none", "prefer"),
            ("人已经在坐轮椅，", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 3_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(
            script,
            units,
            raw_cues,
            prompt_version="jyd.subtitle-analysis.prompt.v23",
        ),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    assert mapping["status"] == "SUCCESS"
    assert [cue["text"] for cue in render_cues] == [
        "八十几岁很多人",
        "已经在坐轮椅",
    ]


def test_overwide_semantic_group_is_repaired_without_discarding_other_ai_breaks() -> None:
    script = "百分之八十四另外一部分就叫到肌肉和肝脏成为肌糖原和肝糖原呼吸排出"
    units = _units(
        [
            ("百分之八十四", "phrase", "none", "prefer"),
            (
                "另外一部分就叫到肌肉和肝脏成为肌糖原和肝糖原",
                "phrase",
                "none",
                "prefer",
            ),
            ("呼吸排出", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 6_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    assert mapping["status"] == "SUCCESS"
    assert mapping["mapped_unit_count"] == len(units)
    assert len(render_cues) >= 3
    assert any("百分之八十四" in text for text in texts)
    assert any("呼吸排出" in text for text in texts)


def test_bad_model_fragments_are_repaired_and_hard_sentence_breaks_are_preserved() -> None:
    script = (
        "脂肪是怎么离开我们身体的呢？有人说是出汗，"
        "出汗只是身体调节体温的表现。如果你真的这么想"
    )
    units = _units(
        [
            ("脂肪是怎么离开我们身体的", "phrase", "none", "prefer"),
            ("呢？", "phrase", "none", "prefer"),
            ("有人说是出汗，", "phrase", "none", "prefer"),
            ("出汗只是身体调节体温的表", "phrase", "none", "prefer"),
            ("现。", "phrase", "none", "prefer"),
            ("如果你真的这么想", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 8_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    assert mapping["status"] == "SUCCESS"
    assert "呢有人" not in "|".join(texts)
    assert "现如果" not in "|".join(texts)
    assert any(text.endswith("呢") for text in texts)
    assert any(text.endswith("表现") for text in texts)
    assert any(text.startswith("如果") for text in texts)


def test_soft_comma_does_not_force_an_orphan_short_caption() -> None:
    script = "第一，脂肪是怎么储存在我们身体的。"
    units = _units(
        [
            ("第一，", "phrase", "none", "prefer"),
            ("脂肪是怎么储存在我们身体的。", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 4_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    assert mapping["status"] == "SUCCESS"
    assert texts[0] != "第一"
    assert texts[0].startswith("第一脂肪")
    assert not any(
        left.endswith("储") and right.startswith("存")
        for left, right in zip(texts, texts[1:])
    )


def test_legacy_model_preference_remains_soft_when_ten_characters_fit() -> None:
    script = "只是让你多上点心坚持，"
    units = _units(
        [
            ("只是让你多上点心", "phrase", "none", "prefer"),
            ("坚持，", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 4_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    assert mapping["status"] == "SUCCESS"
    assert [str(cue["text"]) for cue in render_cues] == ["只是让你多上点心坚持"]


def test_ten_full_width_characters_fit_without_a_model_boundary() -> None:
    script = "只是让你多上点心坚持，"
    units = _units([(script, "phrase", "none", "prefer")])
    raw_cues = [{"start_us": 0, "end_us": 4_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    assert mapping["status"] == "SUCCESS"
    assert [str(cue["text"]) for cue in render_cues] == ["只是让你多上点心坚持"]


def test_task_20_semantic_answer_boundary_is_kept_even_when_text_fits() -> None:
    script = "最好的保护神洋葱，"
    units = _units(
        [
            ("最好的保护神", "phrase", "none", "prefer"),
            ("洋葱，", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 2_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    assert mapping["status"] == "SUCCESS"
    assert [str(cue["text"]) for cue in render_cues] == ["最好的保护神", "洋葱"]


@pytest.mark.parametrize(
    ("script", "parts", "expected"),
    [
        (
            "最简单的补钙方式晒太阳，",
            [
                ("最简单的补钙方式", "phrase", "none", "prefer"),
                ("晒太阳，", "phrase", "none", "prefer"),
            ],
            ["最简单的补钙方式", "晒太阳"],
        ),
        (
            "最简单的排毒法揉肚子，",
            [("最简单的排毒法揉肚子，", "phrase", "none", "prefer")],
            ["最简单的排毒法", "揉肚子"],
        ),
        (
            "最重要的方法是睡眠，",
            [("最重要的方法是睡眠，", "phrase", "none", "prefer")],
            ["最重要的方法是", "睡眠"],
        ),
    ],
)
def test_task_20_answer_phrases_follow_semantics_not_short_tail_balance(
    script: str,
    parts: list[tuple[str, str, str, str]],
    expected: list[str],
) -> None:
    raw_cues = [{"start_us": 0, "end_us": 2_400_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, _units(parts), raw_cues),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    assert mapping["status"] == "SUCCESS"
    assert [str(cue["text"]) for cue in render_cues] == expected


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (
            "世界上公认的十大免费最好的医生，",
            ["世界上公认的", "十大免费最好的医生"],
        ),
        ("第十个睁一只眼闭一只眼，", ["第十个", "睁一只眼闭一只眼"]),
    ],
)
def test_task_30_uses_complete_modifier_and_numbered_item_beats(
    script: str,
    expected: list[str],
) -> None:
    raw_cues = [{"start_us": 0, "end_us": 3_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, _units([(script, "phrase", "none", "prefer")]), raw_cues),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    assert mapping["status"] == "SUCCESS"
    assert [str(cue["text"]) for cue in render_cues] == expected


def test_h3_bound_script_keeps_consecutive_numbered_foods_in_separate_cues() -> None:
    script = (
        "记住你每天一定要吃的食物，第一苹果，第二鸡蛋，第三牛奶，"
        "第四西红柿，第五瘦猪肉，第六巴旦木，第七橄榄油。"
    )
    parts = [
        ("记住你每天一定要吃的食物，", "phrase", "none", "prefer"),
        ("第一苹果，", "phrase", "none", "prefer"),
        ("第二鸡蛋，", "phrase", "none", "prefer"),
        ("第三牛奶，", "phrase", "none", "prefer"),
        ("第四西红柿，", "phrase", "none", "prefer"),
        ("第五瘦猪肉，", "phrase", "none", "prefer"),
        ("第六巴旦木，", "phrase", "none", "prefer"),
        ("第七橄榄油。", "phrase", "none", "prefer"),
    ]
    raw_cues = [
        {"start_us": 0, "end_us": 4_000_000, "text": script[: len(script) // 2]},
        {"start_us": 4_000_000, "end_us": 8_000_000, "text": script[len(script) // 2 :]},
    ]

    render_cues, mapping = derive_project_render_cues(
        _item(
            script,
            _units(parts),
            raw_cues,
            prompt_version="jyd.content-analysis.prompt.v23",
        ),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    assert mapping["status"] == "SUCCESS"
    assert mapping["reason_code"] is None
    for numbered_food in (
        "第一苹果",
        "第二鸡蛋",
        "第三牛奶",
        "第四西红柿",
        "第五瘦猪肉",
        "第六巴旦木",
        "第七橄榄油",
    ):
        assert numbered_food in texts
    assert not any("第一苹果" in text and "第二鸡蛋" in text for text in texts)


def test_local_reflow_overrides_model_break_inside_locative_relative() -> None:
    script = "存款和好看才是你疲惫生活中的一副重要的解药，"
    units = _units(
        [
            ("存款和好看才是你疲惫生活", "phrase", "none", "prefer"),
            ("中的一副重要的解药，", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 4_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    joined = "|".join(texts)
    assert mapping["status"] == "SUCCESS"
    assert "疲惫生活|中的" not in joined
    assert "疲惫生活中的|一副重要的解药" in joined


def test_local_reflow_overrides_model_break_before_category_suffix() -> None:
    script = (
        "第二，早餐不要吃快餐类的，盐多油多。"
        "第三，早餐不要吃蛋糕类的，饼干类的，糖多油多。"
    )
    units = _units(
        [
            ("第二，", "phrase", "none", "prefer"),
            ("早餐不要吃快餐", "phrase", "none", "prefer"),
            ("类的，", "phrase", "none", "prefer"),
            ("盐多油多。", "phrase", "none", "prefer"),
            ("第三，", "phrase", "none", "prefer"),
            ("早餐不要吃蛋糕", "phrase", "none", "prefer"),
            ("类的，", "phrase", "none", "prefer"),
            ("饼干类的，", "phrase", "none", "prefer"),
            ("糖多油多。", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 8_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    joined = "|".join(str(cue["text"]) for cue in render_cues)
    assert mapping["status"] == "SUCCESS"
    assert "快餐|类的" not in joined
    assert "蛋糕|类的" not in joined


def test_local_reflow_keeps_predicate_object_dependency_without_term_lists() -> None:
    script = "坚持吃一个带壳煮鸡蛋，"
    units = _units([(script, "phrase", "none", "prefer")])
    raw_cues = [{"start_us": 0, "end_us": 3_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    assert mapping["status"] == "SUCCESS"
    assert "带|壳" not in "|".join(texts)
    assert "".join(texts) == "坚持吃一个带壳煮鸡蛋"


def test_model_preference_cannot_create_one_character_particle_tail() -> None:
    script = "找几个你喜欢的就OK了，"
    units = _units(
        [
            ("找几个你喜欢的就OK", "phrase", "none", "prefer"),
            ("了，", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 3_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    assert mapping["status"] == "SUCCESS"
    assert "了" not in texts
    assert texts[-1].endswith("了")
    assert all(len(text) >= 2 for text in texts)


def test_model_preference_cannot_isolate_a_pronoun() -> None:
    script = "你今天不愿意为未来的健康做一点投资，"
    units = _units(
        [
            ("你", "phrase", "none", "prefer"),
            ("今天不愿意为未来的健康做一点投资，", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 4_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    assert mapping["status"] == "SUCCESS"
    assert "你" not in texts
    assert texts[0].startswith("你")


@pytest.mark.parametrize(
    ("script", "parts", "forbidden_boundary"),
    [
        (
            "希望你好好做体重管理给你带来帮助，",
            [("希望你好好做体重", "phrase", "none", "prefer"),
             ("管理给你带来帮助，", "phrase", "none", "prefer")],
            "体重|管理",
        ),
        (
            "你会越来越喜欢你自己，",
            [("你会越来越喜欢你", "phrase", "none", "prefer"),
             ("自己，", "phrase", "none", "prefer")],
            "你|自己",
        ),
        (
            "你的体重如果增加五斤，",
            [("你的体重如果增加", "phrase", "none", "prefer"),
             ("五斤，", "phrase", "none", "prefer")],
            "增加|五斤",
        ),
    ],
)
def test_model_preference_cannot_split_generic_syntax_dependencies(
    script: str,
    parts: list[tuple[str, str, str, str]],
    forbidden_boundary: str,
) -> None:
    raw_cues = [{"start_us": 0, "end_us": 4_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, _units(parts), raw_cues),
        font_path=PRODUCTION_CAPTION_FONT_PATH,
        font_size=PRODUCTION_CAPTION_FONT_SIZE,
        max_width_ratio=0.8,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    assert mapping["status"] == "SUCCESS"
    assert forbidden_boundary not in "|".join(texts)
    assert "".join(texts) == script.rstrip("，")


def test_enumeration_commas_remain_legal_breaks_after_punctuation_is_hidden() -> None:
    script = "当你掉秤慢、嘴馋、减不动的时候啊，你就安排吃这十种蔬菜"
    units = _units(
        [
            ("当你掉秤慢、", "phrase", "none", "prefer"),
            ("嘴馋、", "phrase", "none", "prefer"),
            ("减不动的时候啊，", "phrase", "none", "prefer"),
            ("你就安排吃这十种蔬菜", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 6_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    assert mapping["status"] == "SUCCESS"
    assert all(not text.endswith("减") for text in texts)
    assert "嘴馋减" not in "|".join(texts)
    assert any(text.endswith("嘴馋") for text in texts)
    assert any(text.startswith("减不动") for text in texts)


def test_real_draft_keeps_comma_clauses_and_number_units_intact() -> None:
    script = (
        "我是蹦床世界冠军张雒，退役之后做了十年的健康体重管理，"
        "跟着我，吃对一日三餐健康瘦，我带着近5万名女性成功瘦了下来，"
        "我还带着我姐从原来的160斤减到现在110斤，"
    )
    units = _units(
        [
            ("我是蹦床世界冠军", "phrase", "none", "prefer"),
            ("张雒，", "phrase", "none", "prefer"),
            ("退役之后做了十年的健康体重", "phrase", "none", "prefer"),
            ("管理，", "phrase", "none", "prefer"),
            ("跟着我，", "phrase", "none", "prefer"),
            ("吃对一日三餐", "phrase", "none", "allow"),
            ("健康瘦，", "phrase", "none", "prefer"),
            ("我带着近5万名女性成功", "phrase", "none", "prefer"),
            ("瘦了下来，", "phrase", "none", "prefer"),
            ("我还带着我姐从原", "phrase", "none", "allow"),
            ("来的160斤减到现在", "phrase", "none", "prefer"),
            ("110斤，", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 10_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    joined = "|".join(texts)
    assert mapping["status"] == "SUCCESS"
    assert "我是蹦床世界冠军张雒" in texts
    assert "张雒退役之后做了十" not in texts
    assert not any(text.startswith("张雒退役") for text in texts)
    assert "十|年" not in joined
    assert "近|5万" not in joined
    assert "5万|名" not in joined
    assert "原|来的" not in joined
    assert "一日三|餐" not in joined
    assert any("十年" in text for text in texts)
    assert any("近5万名" in text for text in texts)


def test_decimal_range_survives_a_bad_model_boundary() -> None:
    script = "每周掉秤0.5到1公斤是最健康的速度。"
    units = _units(
        [
            ("每周掉秤0.", "phrase", "none", "prefer"),
            ("5到1公斤", "number", "none", "prefer"),
            ("是最健康的速度。", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 3_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    joined = "".join(texts)
    assert mapping["status"] == "SUCCESS"
    assert "0.5到1公斤" in joined
    assert "0|.5" not in "|".join(texts)
    assert "0.|5" not in "|".join(texts)


def test_structural_particle_boundary_repair_is_not_tied_to_one_script() -> None:
    script = "这是团队从现场带来的关键经验，可以帮助更多普通人稳定地完成长期改变。"
    units = _units(
        [
            ("这是团队从现场带", "phrase", "none", "allow"),
            ("来的关键经验，", "phrase", "none", "prefer"),
            ("可以帮助更多普通人稳定", "phrase", "none", "allow"),
            ("地完成长期改变。", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 6_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    joined = "|".join(str(cue["text"]) for cue in render_cues)
    assert mapping["status"] == "SUCCESS"
    assert "带|来的" not in joined
    assert "稳定|地" not in joined


def test_model_boundary_cannot_split_adverb_quantity_phrases() -> None:
    script = "而要把更多的营养留给身体，至少三个方法都要记住。"
    units = _units(
        [
            ("而要把更", "phrase", "none", "prefer"),
            ("多的营养留给身体，至少", "phrase", "none", "prefer"),
            ("三个方法都要记住。", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 4_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    joined = "|".join(str(cue["text"]) for cue in render_cues)
    assert mapping["status"] == "SUCCESS"
    assert "更|多" not in joined
    assert "至少|三个" not in joined
    assert "更多" in "".join(str(cue["text"]) for cue in render_cues)


def test_leading_particle_is_rebalanced_with_its_phrase() -> None:
    script = "答案是让你呼吸急促心跳加速的轻活动。"
    units = _units(
        [
            ("答案是让你呼吸急促心跳加速", "phrase", "none", "prefer"),
            ("的轻活动。", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 4_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    assert mapping["status"] == "SUCCESS"
    assert not any(text.startswith(tuple("的地得呢啊了吧吗")) for text in texts[1:])
    assert any("呼吸急促" in text for text in texts)
    assert any(text.endswith("的轻活动") for text in texts)


def test_model_and_local_fallback_cannot_split_protected_phrases() -> None:
    script = (
        "现在专注新中年女性健康体重管理，今天给大家讲清核心逻辑。"
        "而且优先胖肚子以及成为内脏脂肪。"
        "也能够通过呼吸的形式帮助分解脂肪。"
    )
    units = _units(
        [
            ("现在专注新中年女性健康体重管理，", "phrase", "none", "prefer"),
            ("今天给大家讲清核心", "phrase", "none", "prefer"),
            ("逻", "phrase", "none", "prefer"),
            ("辑。", "phrase", "none", "prefer"),
            ("而且优先胖肚子以及成为内脏脂", "phrase", "none", "prefer"),
            ("肪。", "phrase", "none", "prefer"),
            ("也能够通过呼吸的形", "phrase", "none", "prefer"),
            ("式帮助分解脂肪。", "phrase", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 9_000_000, "text": script}]

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    joined = "|".join(texts)
    assert mapping["status"] == "SUCCESS"
    assert "女|性" not in joined
    assert "核心|逻辑" not in joined
    assert "以|及" not in joined
    assert "内脏脂|肪" not in joined
    assert "形|式" not in joined
    assert "现在专注新中年女性" in texts
    assert "而且优先胖肚子" in texts
    assert "以及成为内脏脂肪" in texts
    assert "也能够通过呼吸的形式" in texts


def test_real_model_mistakes_are_reflowed_without_crossing_clauses_or_raw_cues() -> None:
    script = (
        "原因是，你一直都在用减重的思维去减脂。"
        "你会不会干脆破罐子破摔，觉得自己就是易胖体质。"
        "结果饿到头晕眼花，掉的都是水分。"
        "最后，每天抽十几分钟做点简单的活动就够了。"
        "你控制得了工作上的情绪，管得了家里的大小事。"
        "我见过太多四十多五十多的姐姐。"
        "少走十年弯路。"
        "觉得我说的对你有用，我每天都会分享一个能落地的体重管理小方法。"
    )
    units = _units(
        [
            ("原因是，", "phrase", "none", "prefer"),
            ("你一直都在用减重的思维", "phrase", "none", "prefer"),
            ("去减脂。", "phrase", "none", "prefer"),
            ("你会不会干脆破罐子", "phrase", "none", "prefer"),
            ("破摔，", "phrase", "none", "prefer"),
            ("觉得自己就是易胖体质。", "phrase", "none", "prefer"),
            ("结果饿到头晕眼", "phrase", "none", "allow"),
            ("花，", "phrase", "none", "prefer"),
            ("掉的都是水分。", "phrase", "none", "prefer"),
            ("最后，", "phrase", "none", "prefer"),
            ("每天抽十几分钟做点简单的活动就够", "phrase", "none", "prefer"),
            ("了。", "phrase", "none", "prefer"),
            ("你控制得了工作上的情绪，", "phrase", "none", "prefer"),
            ("管得了家里的大小", "phrase", "none", "allow"),
            ("事。", "phrase", "none", "prefer"),
            ("我见过太多四十多五十多", "phrase", "none", "prefer"),
            ("的姐姐。", "phrase", "none", "prefer"),
            ("少走十年弯路。", "phrase", "none", "prefer"),
            ("觉得我说的对你有", "phrase", "none", "allow"),
            ("用，", "phrase", "none", "prefer"),
            ("我每天都会分享一个能落地的体重管理小方", "phrase", "none", "prefer"),
            ("法。", "phrase", "none", "prefer"),
        ]
    )
    raw_texts = [
        "原因是，你一直都在用减重的思维去减脂。",
        "你会不会干脆破罐子破摔，觉得自己就是易胖体质。",
        "结果饿到头晕眼花，掉的都是水分。",
        "最后，每天抽十几分钟做点简单的活动就够了。",
        "你控制得了工作上的情绪，管得了家里的大小事。",
        "我见过太多四十多五十多的姐姐。",
        "少走十年弯路。",
        "觉得我说的对你有用，我每天都会分享一个能落地的体重管理小方法。",
    ]
    raw_cues: list[dict[str, object]] = []
    cursor = 0
    for text in raw_texts:
        duration = max(1_000_000, len(text) * 100_000)
        raw_cues.append({"start_us": cursor, "end_us": cursor + duration, "text": text})
        cursor += duration + 200_000

    render_cues, mapping = derive_project_render_cues(
        _item(script, units, raw_cues),
        font_path=FONT_PATH,
    )

    texts = [str(cue["text"]) for cue in render_cues]
    joined = "|".join(texts)
    assert mapping["status"] == "SUCCESS"
    assert "破罐子|破摔" not in joined
    assert "头晕眼|花" not in joined
    assert "做|点" not in joined
    assert "情|绪" not in joined
    assert "四十|多" not in joined
    assert "弯|路" not in joined
    assert "一个|能" not in joined
    assert any("破罐子破摔" in text for text in texts)
    assert any("头晕眼花" in text for text in texts)
    # The 14pt layout is intentionally narrower than the former 11pt layout;
    # the phrase may wrap before the word, but the word itself must stay intact.
    assert any("情绪" in text for text in texts)
    assert any("少走十年弯路" in text for text in texts)
    previous_raw = raw_cues[-2]
    following_raw = raw_cues[-1]
    assert max(int(cue["end_us"]) for cue in render_cues if "弯路" in str(cue["text"])) <= int(
        previous_raw["end_us"]
    )
    assert min(int(cue["start_us"]) for cue in render_cues if "觉得我说" in str(cue["text"])) >= int(
        following_raw["start_us"]
    )


def test_tilde_is_an_exact_character_not_a_wildcard() -> None:
    script = "糖原~呼吸"
    units = _units([("糖原~呼吸", "phrase", "none", "prefer")])
    raw_cues = [{"start_us": 0, "end_us": 1_000_000, "text": "糖原呼吸"}]

    with pytest.raises(SemanticSubtitleMappingError) as error:
        map_subtitle_units_to_raw_cues(script, units, raw_cues)

    assert error.value.code == "RAW_CUES_TEXT_MISMATCH"


def test_model_generated_timestamps_are_rejected() -> None:
    script = "八十四天"
    units = _units([("八十四天", "number", "none", "prefer")])
    units[0]["start_us"] = 123

    with pytest.raises(SemanticSubtitleMappingError) as error:
        map_subtitle_units_to_raw_cues(
            script,
            units,
            [{"start_us": 0, "end_us": 1_000_000, "text": script}],
        )

    assert error.value.code == "MODEL_TIMESTAMP_FORBIDDEN"


def test_audio_script_version_mismatch_falls_back_without_mutating_raw_cues() -> None:
    script = "那么通过八十四天"
    units = _units(
        [
            ("那么", "connector", "right", "avoid"),
            ("通过", "word", "none", "prefer"),
            ("八十四天", "number", "none", "prefer"),
        ]
    )
    raw_cues = [{"start_us": 0, "end_us": 2_000_000, "text": script}]
    original_raw_cues = [dict(cue) for cue in raw_cues]

    render_cues, mapping = derive_project_render_cues(
        _item(
            script,
            units,
            raw_cues,
            audio_script_hash=hashlib.sha256("旧脚本".encode("utf-8")).hexdigest(),
        ),
        font_path=FONT_PATH,
    )

    assert mapping["status"] == "FALLBACK"
    assert mapping["reason_code"] == "AUDIO_SCRIPT_VERSION_MISMATCH"
    assert render_cues
    assert raw_cues == original_raw_cues
