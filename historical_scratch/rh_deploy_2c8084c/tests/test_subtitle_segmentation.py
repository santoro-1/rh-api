from __future__ import annotations

from app.services.content_analysis.subtitle_segmentation import (
    SUBTITLE_ANALYSIS_PROMPT_VERSION,
    build_subtitle_messages,
    deterministic_subtitle_units,
    effective_character_count,
    subtitle_system_prompt,
    validate_subtitle_split,
)


def test_v23_prompt_keeps_hard_limits_and_adds_narrow_semantic_exceptions() -> None:
    prompt = subtitle_system_prompt()

    assert SUBTITLE_ANALYSIS_PROMPT_VERSION == "jyd.subtitle-analysis.prompt.v23"
    assert "唯一目的" in prompt
    assert "必须100%原样保留" in prompt
    assert "新增逗号数量默认必须最少" in prompt
    assert "10个有效字符是上限，不是建议长度" in prompt
    assert "原文完全不变 ＞ 每段不超过10个有效字符" in prompt
    assert "只要仍有任何一段超过10个有效字符" in prompt
    assert "每个阿拉伯数字计1个有效字符" in prompt
    assert "每个英文字母计1个有效字符" in prompt
    assert "连续并列结构例外" in prompt
    assert "到八十岁｜到九十岁｜到一百岁" in prompt
    assert "八十几岁很多人｜已经在坐轮椅" in prompt
    assert "只输出切分后的完整文案" in prompt

    messages = build_subtitle_messages("每天喝2000ml水")
    assert len(messages) == 2
    assert '"每天喝2000ml水"' in messages[1]["content"]


def test_effective_count_includes_han_digits_and_english() -> None:
    assert effective_character_count("每天喝2000ml水") == 10
    assert effective_character_count("3.5kg，OK！") == 6


def test_validator_rejects_overflow_and_source_edits() -> None:
    source = "减肥成功的人特别不想跟你分享的"

    overflow = validate_subtitle_split(source, source)
    changed = validate_subtitle_split(source, "控重成功的人特别，不想跟你分享的")

    assert overflow.valid is False
    assert "需要最少1个新增逗号" in overflow.error
    assert changed.valid is False
    assert "修改、删除、移动" in changed.error


def test_validator_accepts_minimal_safe_split_and_rejects_short_span_split() -> None:
    source = "减肥成功的人特别不想跟你分享的"
    accepted = validate_subtitle_split(source, "减肥成功的人，特别不想跟你分享的")
    unnecessary = validate_subtitle_split("每天喝2000ml水", "每天喝，2000ml水")

    assert accepted.valid is True
    assert accepted.inserted_positions == (6,)
    assert unnecessary.valid is False
    assert "不允许新增逗号" in unnecessary.error


def test_validator_rejects_breaks_after_degree_adverbs_and_before_dynamic_particles() -> None:
    broken_degree_phrase = validate_subtitle_split(
        "这实在是太难得的一件事儿了",
        "这实在是太，难得的一件事儿了",
    )
    broken_dynamic_particle = validate_subtitle_split(
        "它的蛋白质达到了百分之十七",
        "它的蛋白质达到，了百分之十七",
    )
    broken_attributive_phrase = validate_subtitle_split(
        "这实在是太难得的一件事儿了",
        "这实在是太难得的，一件事儿了",
    )

    assert broken_degree_phrase.valid is False
    assert "不安全断点" in broken_degree_phrase.error
    assert broken_dynamic_particle.valid is False
    assert "不安全断点" in broken_dynamic_particle.error
    assert broken_attributive_phrase.valid is False
    assert "不安全断点" in broken_attributive_phrase.error


def test_parallel_age_items_allow_one_semantic_extra_break() -> None:
    source = "到八十岁到九十岁到一百岁"

    accepted = validate_subtitle_split(source, "到八十岁，到九十岁，到一百岁")
    overly_compact = validate_subtitle_split(source, "到八十岁到九十岁，到一百岁")
    stranded_introducer = validate_subtitle_split(
        source, "到八十岁到，九十岁到一百岁"
    )
    units = deterministic_subtitle_units(source)

    assert accepted.valid is True
    assert accepted.inserted_positions == (4, 8)
    assert overly_compact.valid is False
    assert "同构并列项" in overly_compact.error
    assert stranded_introducer.valid is False
    assert "不安全断点" in stranded_introducer.error
    assert [unit.text for unit in units] == ["到八十岁", "到九十岁", "到一百岁"]


def test_subject_predicate_boundary_rejects_split_inside_many_people() -> None:
    source = "八十几岁很多人已经在坐轮椅"

    accepted = validate_subtitle_split(source, "八十几岁很多人，已经在坐轮椅")
    broken_subject = validate_subtitle_split(source, "八十几岁很多，人已经在坐轮椅")

    assert accepted.valid is True
    assert broken_subject.valid is False
    assert "不安全断点" in broken_subject.error


def test_deterministic_fallback_keeps_tight_grammar_units_intact() -> None:
    degree_units = deterministic_subtitle_units("这实在是太难得的一件事儿了")
    particle_units = deterministic_subtitle_units("它的蛋白质达到了百分之十七")

    assert [unit.text for unit in degree_units] == ["这实在是", "太难得的一件事儿了"]
    assert [unit.text for unit in particle_units] == ["它的蛋白质", "达到了百分之十七"]


def test_deterministic_fallback_uses_minimal_safe_boundaries() -> None:
    source = "但是一定要有三次轻松的活动，"

    units = deterministic_subtitle_units(source)

    assert "".join(unit.text for unit in units) == source
    assert [unit.text for unit in units] == ["但是一定要有", "三次轻松的活动，"]
    assert all(unit.break_after.value == "prefer" for unit in units)


def test_august_14_row_13_overflows_all_receive_a_safe_break() -> None:
    sources = [
        "减肥成功的人特别不想跟你分享的",
        "每天早晨必须吃一个鸡蛋，",
        "但是一定要有三次轻松的活动，",
        "记住每天喝水是两千毫升，",
        "想吃一点甜的就吃个苹果，",
    ]

    for source in sources:
        units = deterministic_subtitle_units(source)
        assert "".join(unit.text for unit in units) == source
        assert len(units) >= 2
        assert all(
            effective_character_count(unit.text) <= 10
            for unit in units
        )
