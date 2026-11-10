from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from tools.library.build_food_semantic_bundles import (
    CANVAS_SIZE,
    FoodAssetSpec,
    build_food_semantic_bundles,
    normalize_image,
)


def _write_image(path: Path, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(path.suffix, image)
    assert ok
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded.tofile(path)


def _spec() -> FoodAssetSpec:
    return FoodAssetSpec(
        concept_id="food.test",
        label="测试食物",
        description="测试食物素材",
        aliases=("测试食物",),
        asset_id="test.food.01",
        name="测试图片",
        source="中文目录/测试图片.jpg",
        bundle="test_food_01",
        fit="cover",
        default_corner="top_right",
        default_scale=0.25,
    )


def _template(root: Path) -> Path:
    template = root / "template"
    (template / "resources" / "sticker").mkdir(parents=True)
    (template / "sticker.json").write_text(
        json.dumps(
            {
                "schema": "test.sticker.v1",
                "material": {"path": "C:/old-machine/cache"},
                "resource": {
                    "original_path": "C:/old-machine/cache",
                    "library_path": "resources/sticker",
                },
                "source": {"label": "D:\\old-machine\\draft"},
            }
        ),
        encoding="utf-8",
    )
    _write_image(
        template / "resources" / "sticker" / "singleImage.png",
        np.zeros((8, 8, 4), dtype=np.uint8),
    )
    return template


def test_normalize_image_supports_cover_and_contain() -> None:
    image = np.zeros((100, 200, 4), dtype=np.uint8)
    image[:, :, :3] = (10, 20, 30)
    image[:, :, 3] = 255

    cover = normalize_image(image, "cover")
    contain = normalize_image(image, "contain")

    assert cover.shape == (CANVAS_SIZE, CANVAS_SIZE, 4)
    assert contain.shape == (CANVAS_SIZE, CANVAS_SIZE, 4)
    assert tuple(cover[0, 0]) == (10, 20, 30, 255)
    assert tuple(contain[0, 0]) == (255, 255, 255, 255)


def test_normalize_image_supports_lower_half_top_fade() -> None:
    image = np.full((100, 200, 4), (10, 20, 30, 255), dtype=np.uint8)

    blended = normalize_image(image, "cover_top_fade")

    assert blended.shape == (CANVAS_SIZE, CANVAS_SIZE, 4)
    assert blended[0, CANVAS_SIZE // 2, 3] == 0
    assert blended[CANVAS_SIZE // 2, CANVAS_SIZE // 2, 3] == 255


def test_builder_generates_staging_bundle_and_catalog_patch(tmp_path: Path) -> None:
    source_root = tmp_path / "素材"
    source_path = source_root / "中文目录" / "测试图片.jpg"
    original = np.full((120, 80, 3), (30, 100, 220), dtype=np.uint8)
    _write_image(source_path, original)
    original_bytes = source_path.read_bytes()
    output_root = tmp_path / "staging"
    template = _template(tmp_path)

    report = build_food_semantic_bundles(
        source_root=source_root,
        output_root=output_root,
        template_bundle=template,
        specs=(_spec(),),
    )

    bundle = output_root / "bundles" / "test_food_01"
    preview = bundle / "resources" / "sticker" / "singleImage.png"
    generated = cv2.imdecode(np.fromfile(preview, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    patch = json.loads((output_root / "catalog.patch.json").read_text(encoding="utf-8"))

    assert report["succeeded_count"] == 1
    assert report["failed_count"] == 0
    assert generated.shape == (CANVAS_SIZE, CANVAS_SIZE, 4)
    sticker = json.loads((bundle / "sticker.json").read_text(encoding="utf-8"))
    assert sticker["schema"] == "test.sticker.v1"
    assert sticker["material"]["path"] == ""
    assert sticker["resource"]["original_path"] == ""
    assert sticker["resource"]["library_path"] == "resources/sticker"
    assert sticker["source"]["label"] == ""
    assert patch["concepts"][0]["concept_id"] == "food.test"
    assert patch["assets"][0]["bundle"] == "bundles/test_food_01"
    assert source_path.read_bytes() == original_bytes


def test_builder_refuses_to_overwrite_existing_bundle(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_path = source_root / "中文目录" / "测试图片.jpg"
    _write_image(source_path, np.zeros((20, 20, 3), dtype=np.uint8))
    output_root = tmp_path / "staging"
    existing = output_root / "bundles" / "test_food_01"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    report = build_food_semantic_bundles(
        source_root=source_root,
        output_root=output_root,
        template_bundle=_template(tmp_path),
        specs=(_spec(),),
    )

    assert report["succeeded_count"] == 0
    assert report["failed_count"] == 1
    assert "拒绝覆盖" in report["failed"][0]["error"]
    assert marker.read_text(encoding="utf-8") == "keep"
    patch = json.loads((output_root / "catalog.patch.json").read_text(encoding="utf-8"))
    assert patch["concepts"] == []
    assert patch["assets"] == []
