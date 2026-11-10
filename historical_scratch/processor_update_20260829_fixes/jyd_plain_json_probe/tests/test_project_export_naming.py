from __future__ import annotations

from jyd_probe.project_export_naming import (
    available_draft_name,
    audio_export_filename,
    composition_draft_name,
    composition_export_filename,
    project_item_export_stem,
    segment_export_filename,
    variant_draft_name,
    variant_export_filename,
)


def _four_column_item() -> dict:
    return {
        "row_key": "2",
        "settings": {
            "source_metadata": {
                "article_type": "鸡汤文",
                "assigned_account": "5",
            }
        },
    }


def test_four_column_exports_use_account_type_task_order() -> None:
    item = _four_column_item()
    assert project_item_export_stem(item) == "账号5-鸡汤文-2"
    assert audio_export_filename(
        item, {"filename": "2_0.9倍速.mp3", "metadata": {"speed": 0.9}}
    ) == "账号5-鸡汤文-2_0.9倍速.mp3"
    assert composition_export_filename(
        item, {"filename": "2-composition.mp4"}
    ) == "账号5-鸡汤文-2-composition.mp4"
    assert composition_draft_name(item) == "账号5-鸡汤文-2-composition"
    assert variant_export_filename(
        item, {"filename": "任务-2-变体-007.mp4"}
    ) == "账号5-鸡汤文-2-变体-007.mp4"
    assert variant_draft_name(item, index=7) == "账号5-鸡汤文-2-变体-007"
    assert segment_export_filename(
        item, {"filename": "2-segment-001.mp4"}, index=1
    ) == "账号5-鸡汤文-2-segment-001.mp4"


def test_legacy_two_column_item_keeps_existing_asset_filename() -> None:
    item = {"row_key": "2", "settings": {}}
    assert audio_export_filename(item, {"filename": "2_0.9倍速.mp3"}) == "2_0.9倍速.mp3"
    assert composition_export_filename(
        item, {"filename": "2-composition.mp4"}
    ) == "2-composition.mp4"
    assert variant_export_filename(
        item, {"filename": "任务-2-变体-001.mp4"}
    ) == "任务-2-变体-001.mp4"


def test_existing_draft_gets_a_non_destructive_numeric_suffix(tmp_path) -> None:
    (tmp_path / "账号5-鸡汤文-2-composition").mkdir()
    (tmp_path / "账号5-鸡汤文-2-composition-02").mkdir()
    assert available_draft_name(
        tmp_path, "账号5-鸡汤文-2-composition"
    ) == "账号5-鸡汤文-2-composition-03"


def test_planned_batch_drafts_reserve_names_before_directories_exist(tmp_path) -> None:
    reserved = {"shared-source-composition"}
    assert available_draft_name(
        tmp_path,
        "shared-source-composition",
        reserved_names=reserved,
    ) == "shared-source-composition-02"

    reserved.add("shared-source-composition-02")
    assert available_draft_name(
        tmp_path,
        "shared-source-composition",
        reserved_names=reserved,
    ) == "shared-source-composition-03"
