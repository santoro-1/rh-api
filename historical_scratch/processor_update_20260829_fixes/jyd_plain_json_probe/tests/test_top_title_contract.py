from __future__ import annotations

import pytest

from jyd_probe.content_replace import _apply_text_material_overrides
from jyd_probe.project_postprocess import (
    CAPTION_REFERENCE_MAX_EM,
    bound_visual_overlays_to_video,
    build_project_cover,
    build_source_attribution_texts,
    build_top_title_texts,
    normalize_cover_title,
    normalize_top_title,
)
from jyd_probe.render_job import _build_text_replacements


def test_top_title_is_optional_and_normalizes_whitespace() -> None:
    assert normalize_top_title(None) == {"label": "", "headline": ""}
    assert normalize_top_title(
        {"label": "  \u51cf\u8102  \u771f\u76f8 ", "headline": "\u575a\u6301\n\u624d\u662f\u5173\u952e"}
    ) == {
        "label": "\u51cf\u8102 \u771f\u76f8",
        "headline": "\u575a\u6301 \u624d\u662f\u5173\u952e",
    }
    assert CAPTION_REFERENCE_MAX_EM == pytest.approx(10.382142857142858)


def test_video_uses_one_fixed_title_and_fixed_bottom_disclaimer() -> None:
    texts = build_top_title_texts(
        {"line_1": "\u51cf\u8102\u771f\u76f8", "line_2": "\u575a\u6301\u624d\u662f\u5173\u952e"},
        font={"resource_id": "font-id", "path": "D:/font.ttf", "name": "Fixed"},
    )
    assert [item["transform_y"] for item in texts] == pytest.approx(
        [0.8155959933996199, -1760 / 1920]
    )
    assert [item["size"] for item in texts] == [19.0, 6.0]
    assert [item["color"] for item in texts] == [
        "#FFF589",
        "#FFFFFF",
    ]
    assert texts[0]["text"] == "世界冠军带你自律"
    assert texts[0]["stroke_color"] == ""
    assert texts[0]["stroke_width"] == 0.0
    assert [item["opacity"] for item in texts] == [1.0, 0.5]
    assert texts[-1]["text"] == (
        "非医疗保健科普：仅供参考，个人经验分享，不代表普遍性\n"
        "如有不适请线下就医"
    )
    assert all(item["duration_us"] == 0 for item in texts)

    _replacements, additions, _styles, _nested_styles = _build_text_replacements(
        {"texts": texts}, timeline_duration_us=5_000_000
    )
    assert [item.duration_us for item in additions] == [
        5_000_000,
        5_000_000,
    ]
    assert [item.line_max_width for item in additions] == [0.92, 0.92]
    assert [item.font_id for item in additions] == ["font-id", "font-id"]
    assert [item.stroke_width for item in additions] == [0.0, 0.0]
    assert [item.opacity for item in additions] == [1.0, 0.5]

    material = {
        "content": '{"text":"免责声明","styles":[{"range":[0,5]}]}',
        "global_alpha": 1.0,
    }
    _apply_text_material_overrides(
        material,
        size=6.0,
        color="#FFFFFF",
        stroke_color="#000000",
        stroke_width=0.04,
        line_max_width=0.92,
        opacity=0.5,
    )
    assert material["global_alpha"] == 0.5


def test_added_text_clamps_a_single_frame_end_rounding_difference() -> None:
    _replacements, additions, _styles, _nested_styles = _build_text_replacements(
        {
            "texts": [
                {
                    "type": "add",
                    "text": "素材来源于网络",
                    "start_us": 6_000_825,
                    "duration_us": 8_810_894,
                }
            ]
        },
        timeline_duration_us=14_800_000,
    )

    assert additions[0].duration_us == 8_799_175


def test_added_text_still_rejects_material_end_beyond_one_frame() -> None:
    with pytest.raises(RuntimeError, match="新增文字时间范围超出视频时长"):
        _build_text_replacements(
            {
                "texts": [
                    {
                        "type": "add",
                        "text": "错误绑定的文字",
                        "start_us": 6_000_000,
                        "duration_us": 9_000_000,
                    }
                ]
            },
            timeline_duration_us=14_800_000,
        )


def test_added_text_starting_after_video_end_is_skipped() -> None:
    _replacements, additions, _styles, _nested_styles = _build_text_replacements(
        {
            "texts": [
                {
                    "type": "add",
                    "text": "素材来源于网络",
                    "start_us": 20_000_000,
                    "duration_us": 2_000_000,
                }
            ]
        },
        timeline_duration_us=19_467_000,
    )

    assert additions == []


def test_network_source_label_reuses_disclaimer_style_and_overlay_timing() -> None:
    texts = build_source_attribution_texts(
        [
            {
                "enabled": True,
                "attribution_text": "素材来源于网络",
                "start_us": 1_000_000,
                "duration_us": 2_000_000,
            },
            {
                "enabled": True,
                "attribution_text": "素材来源于网络",
                "start_us": 3_050_000,
                "duration_us": 1_000_000,
            },
            {
                "enabled": False,
                "attribution_text": "素材来源于网络",
                "start_us": 5_000_000,
                "duration_us": 1_000_000,
            },
        ],
        font={"resource_id": "font-id", "path": "D:/font.ttf", "name": "Fixed"},
    )

    assert len(texts) == 1
    source = texts[0]
    assert source["text"] == "素材来源于网络"
    assert source["start_us"] == 1_000_000
    assert source["duration_us"] == 3_050_000
    assert source["transform_x"] == pytest.approx(0.72)
    assert source["transform_y"] == pytest.approx(0.90)
    assert source["align"] == 2
    assert source["size"] == 6.0
    assert source["scale"] == 1.0
    assert source["color"] == "#FFFFFF"
    assert source["opacity"] == 0.5
    assert source["font_id"] == "font-id"


def test_out_of_range_visuals_are_dropped_and_partial_items_are_trimmed() -> None:
    overlays = bound_visual_overlays_to_video(
        [
            {"asset_id": "inside", "start_us": 8_000_000, "duration_us": 4_000_000},
            {"asset_id": "outside", "start_us": 12_000_000, "duration_us": 2_000_000},
        ],
        10_000_000,
    )
    assert overlays == [
        {"asset_id": "inside", "start_us": 8_000_000, "duration_us": 2_000_000}
    ]
    assert build_source_attribution_texts(
        [
            {
                "attribution_text": "素材来源于网络",
                "start_us": 12_000_000,
                "duration_us": 2_000_000,
            }
        ],
        video_duration_us=10_000_000,
    ) == []


def test_top_title_rejects_multiline_overflow() -> None:
    with pytest.raises(ValueError, match="\u9ec4\u8272\u5c0f\u6807\u9898"):
        normalize_top_title({"label": "123456", "headline": ""})


def test_cover_title_requires_two_compact_lines() -> None:
    assert normalize_cover_title(None) == {"line_1": "", "line_2": ""}
    assert normalize_cover_title({"topic": "健康真相", "hook": "别再踩坑"}) == {
        "line_1": "健康真相",
        "line_2": "别再踩坑",
    }
    with pytest.raises(ValueError, match="同时提供两行"):
        normalize_cover_title({"line_1": "健康真相", "line_2": ""})
    with pytest.raises(ValueError, match="不能包含空格"):
        normalize_cover_title({"line_1": "健康 真相", "line_2": "别再踩坑"})
    with pytest.raises(ValueError, match="最多 5 个字符"):
        normalize_cover_title({"line_1": "一二三四五六", "line_2": "别再踩坑"})
    with pytest.raises(ValueError, match="最多 14 个字符"):
        normalize_cover_title({"line_1": "健康真相", "line_2": "一二三四五六七八九十一二三四五"})
    assert normalize_cover_title(
        {"line_1": "健康真相", "line_2": "一二三四五六七八九十一二三四"}
    )["line_2"] == "一二三四五六七八九十一二三四"


def test_cover_title_naturally_rewrites_weight_management_risk_words() -> None:
    assert normalize_cover_title(
        {"line_1": "减肥最好", "line_2": "瘦了5斤"}
    ) == {
        "line_1": "控重更好",
        "line_2": "体重变化",
    }
    assert normalize_cover_title(
        {"line_1": "脂肪肚腩", "line_2": "掉秤真快"}
    ) == {
        "line_1": "体脂腰腹",
        "line_2": "变轻真快",
    }
    assert normalize_cover_title(
        {"line_1": "健康享瘦", "line_2": "健康瘦久"}
    ) == {
        "line_1": "健康享轻盈",
        "line_2": "体重稳定",
    }
    assert normalize_cover_title(
        {"line_1": "轻松变瘦", "line_2": "瘦得更快"}
    ) == {
        "line_1": "轻松变轻盈",
        "line_2": "体重变轻",
    }


@pytest.mark.parametrize(
    "title",
    [
        {"line_1": "祖传秘方", "line_2": "根治疾病"},
        {"line_1": "记得吃药", "line_2": "立刻见效"},
        {"line_1": "加微信", "line_2": "进群咨询"},
        {"line_1": "暴富秘笈", "line_2": "稳赚不赔"},
        {"line_1": "暴力血腥", "line_2": "未成年吸毒"},
        {"line_1": "私域引流", "line_2": "扫码进群"},
    ],
)
def test_cover_title_hard_risk_uses_neutral_fallback(title: dict[str, str]) -> None:
    assert normalize_cover_title(title) == {
        "line_1": "生活提醒",
        "line_2": "理性看待",
    }


def test_cover_title_does_not_rewrite_non_superlative_zui_words() -> None:
    assert normalize_cover_title(
        {"line_1": "最近状态", "line_2": "最后提醒"}
    ) == {
        "line_1": "最近状态",
        "line_2": "最后提醒",
    }


def test_project_cover_uses_input_image_and_fixed_visual_recipe(tmp_path) -> None:
    image = tmp_path / "person.png"
    image.write_bytes(b"image")
    font = tmp_path / "SourceHanSerifCN-Heavy.otf"
    font.write_bytes(b"font")
    cover = build_project_cover(
        {
            "row_key": "1",
            "inputs": {"image": {"managed_path": str(image)}},
            "settings": {
                "postprocess": {
                    "cover_title": {"line_1": "健康真相", "line_2": "别再踩坑"}
                }
            },
        },
        fonts={
            "resource_id:6807742980271641102": {
                "resource_id": "6807742980271641102",
                "name": "SourceHanSerifCN-Heavy",
                "path": str(font),
            }
        },
    )
    assert cover is not None
    assert cover["frame_source"] == "input_image"
    assert cover["image_path"] == str(image.resolve())
    assert cover["frame_count"] == 3
    assert cover["line_1_size"] == 30.0
    assert cover["line_2_size"] == 22.0
    assert cover["line_1_color"] == "#FADF4A"
    assert cover["line_2_color"] == "#F5F6F0"
    assert cover["line_1_y"] == pytest.approx(-160 / 1920)
    assert cover["line_2_y"] == pytest.approx(-655 / 1920)
    assert cover["overlay_y_ratio"] == pytest.approx(0.615)
    assert cover["overlay_height_ratio"] == pytest.approx(0.28)
    assert cover["overlay_top_ratio"] == pytest.approx(0.475)
    assert cover["overlay_bottom_ratio"] == pytest.approx(0.755)
    assert cover["text_scale"] == pytest.approx(1.1045453049181124)
    assert cover["auto_wrapping"] is False
    assert cover["line_1_shadow_alpha"] == pytest.approx(0.8999999761581421)
    assert cover["line_2_shadow_alpha"] == pytest.approx(0.8999999761581421)
    assert cover["line_1_shadow_smoothing"] == pytest.approx(0.45000001788139343)
    assert cover["line_2_shadow_smoothing"] == pytest.approx(0.45000001788139343)


def test_ltx_project_cover_uses_base_video_first_frame_without_input_image(tmp_path) -> None:
    video = tmp_path / "lip-sync.mp4"
    video.write_bytes(b"video")
    font = tmp_path / "SourceHanSerifCN-Heavy.otf"
    font.write_bytes(b"font")

    cover = build_project_cover(
        {
            "row_key": "1",
            "inputs": {"image": None},
            "outputs": {
                "base_video": {
                    "source_type": "ltx",
                    "managed_path": str(video),
                }
            },
            "settings": {
                "postprocess": {
                    "cover_title": {"line_1": "健康真相", "line_2": "别再踩坑"}
                }
            },
        },
        fonts={
            "resource_id:6807742980271641102": {
                "resource_id": "6807742980271641102",
                "name": "SourceHanSerifCN-Heavy",
                "path": str(font),
            }
        },
    )

    assert cover is not None
    assert cover["frame_source"] == "timeline"
    assert cover["frame_time_seconds"] == 0
    assert cover["image_path"] == ""


def test_project_cover_sanitizes_historical_saved_risk_title(tmp_path) -> None:
    image = tmp_path / "person.png"
    image.write_bytes(b"image")
    font = tmp_path / "SourceHanSerifCN-Heavy.otf"
    font.write_bytes(b"font")

    cover = build_project_cover(
        {
            "row_key": "1",
            "inputs": {"image": {"managed_path": str(image)}},
            "settings": {
                "postprocess": {
                    "cover_title": {"line_1": "祖传秘方", "line_2": "加微信咨询"}
                }
            },
        },
        fonts={
            "resource_id:6807742980271641102": {
                "resource_id": "6807742980271641102",
                "name": "SourceHanSerifCN-Heavy",
                "path": str(font),
            }
        },
    )

    assert cover is not None
    assert cover["text_line_1"] == "生活提醒"
    assert cover["text_line_2"] == "理性看待"
