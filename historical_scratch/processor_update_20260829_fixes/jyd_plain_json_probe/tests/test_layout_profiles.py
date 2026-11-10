from __future__ import annotations

from pathlib import Path

import pytest

from jyd_probe.layout_profiles import (
    DEFAULT_LAYOUT_PROFILE,
    LAYOUT_PROFILE_FONT_IDENTITY,
    layout_profile,
    nameplate_overlay,
    nameplate_texts,
    normalize_layout_profile,
    public_layout_profiles,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VISUAL_LIBRARY = PROJECT_ROOT / "data" / "libraries" / "semantic_visual_library"


def test_layout_profiles_preserve_the_two_manual_draft_standards() -> None:
    standing = layout_profile("standing")
    seated = layout_profile("seated")

    assert DEFAULT_LAYOUT_PROFILE == "standing"
    assert LAYOUT_PROFILE_FONT_IDENTITY == "resource_id:7086699209738424840"
    assert [row["id"] for row in public_layout_profiles()] == ["standing", "seated"]
    assert standing["caption"]["font_size"] == 11.0
    assert standing["caption"]["clip_scale"] == pytest.approx(1.32)
    assert standing["caption"]["transform_y"] == pytest.approx(-0.382336816305469)
    assert standing["caption"]["max_width_ratio"] == 0.8
    assert seated["caption"]["font_size"] == 15.0
    assert seated["caption"]["clip_scale"] == 1.0
    assert seated["caption"]["transform_y"] == pytest.approx(-0.32080308951309267)
    assert seated["caption"]["max_width_ratio"] == 0.8
    assert [row["text"] for row in standing["nameplate"]["texts"]] == [
        "张雒",
        "世界蹦床冠军",
        "专注35+女性身材管理",
    ]
    assert [row["text"] for row in seated["nameplate"]["texts"]] == [
        "张雒",
        "蹦床世界冠军",
        "专注35+女性身材管理",
    ]


def test_layout_profile_aliases_and_nameplate_assets() -> None:
    assert normalize_layout_profile("站") == "standing"
    assert normalize_layout_profile("坐姿") == "seated"
    with pytest.raises(ValueError):
        normalize_layout_profile("lying")

    for profile_id, expected_scale in (
        ("standing", 0.8941348042237189),
        ("seated", 1.08873624376896),
    ):
        overlay = nameplate_overlay(VISUAL_LIBRARY, profile_id)
        image = Path(overlay["bundle_path"]) / "resources" / "sticker" / "singleImage.png"
        assert image.is_file()
        assert overlay["rotation"] == -90.0
        assert overlay["scale"] == pytest.approx(expected_scale)
        assert overlay["renderer"] == "sticker"

    assert layout_profile("standing")["cover"]["text_scale"] == pytest.approx(
        1.1045453049181124
    )
    assert layout_profile("seated")["cover"]["text_scale"] == 1.0
    assert layout_profile("standing")["cover"]["auto_wrapping"] is False
    assert layout_profile("seated")["cover"]["auto_wrapping"] is False


def test_nameplate_texts_are_separate_editable_layers() -> None:
    fake_font = {
        "resource_id": "7086699209738424840",
        "path": "C:/font.ttf",
        "name": "金陵体",
    }
    standing = nameplate_texts("standing", font=fake_font)
    seated = nameplate_texts("seated", font=fake_font)

    assert len(standing) == len(seated) == 3
    assert all(row["scope"] == "top" for row in standing + seated)
    assert standing[0]["transform_x"] == pytest.approx(-0.6479077925336624)
    assert seated[0]["transform_x"] == pytest.approx(-0.689338395446377)
    assert all(row["font_id"] == "7086699209738424840" for row in standing + seated)
