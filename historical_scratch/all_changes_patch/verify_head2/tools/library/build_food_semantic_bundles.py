from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid

import cv2
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE_BUNDLE = (
    PROJECT_ROOT
    / "data"
    / "libraries"
    / "semantic_visual_library"
    / "bundles"
    / "egg_boiled"
)
CANVAS_SIZE = 1254
PATCH_SCHEMA = "jyd.semantic-visual-catalog-patch.v1"
REPORT_SCHEMA = "jyd.semantic-visual-bundle-build-report.v1"


@dataclass(frozen=True)
class FoodAssetSpec:
    concept_id: str
    label: str
    description: str
    aliases: tuple[str, ...]
    asset_id: str
    name: str
    source: str
    bundle: str
    fit: str
    default_corner: str
    default_scale: float
    default_opacity: float = 1.0


FOOD_ASSETS = (
    FoodAssetSpec(
        concept_id="food.vegetable",
        label="蔬菜",
        description="作为食物、食材或明确餐食示例出现的蔬菜或青菜",
        aliases=("绿叶蔬菜", "蔬菜", "青菜"),
        asset_id="vegetable.market_photo.02",
        name="真实蔬菜摊",
        source="贴图1/食物/蔬菜4.jpg",
        bundle="vegetable_market_photo_02",
        fit="cover_top_fade",
        default_corner="bottom_left",
        default_scale=0.60,
    ),
    FoodAssetSpec(
        concept_id="food.whole_grain",
        label="粗粮杂粮",
        description="明确提到粗粮、杂粮、杂粮饭或用全谷杂粮替换部分精米白面",
        aliases=("杂粮粥", "杂粮饭", "全谷物", "粗粮", "杂粮"),
        asset_id="whole_grain.multigrain_rice.01",
        name="真实杂粮饭",
        source="贴图1/食物/杂粮饭.png",
        bundle="whole_grain_multigrain_rice_01",
        fit="cover_top_fade",
        default_corner="bottom_left",
        default_scale=0.60,
    ),
    FoodAssetSpec(
        concept_id="nutrition.protein",
        label="蛋白质食物",
        description="明确讨论蛋白质、优质蛋白或高蛋白食物集合",
        aliases=("蛋白质食物", "优质蛋白", "高蛋白", "蛋白质"),
        asset_id="protein.food_guide.01",
        name="蛋白质食物指南",
        source="贴图1/食物/蛋白质食物.jpg",
        bundle="protein_food_guide_01",
        fit="contain",
        default_corner="top_left",
        default_scale=0.23,
        default_opacity=0.98,
    ),
    FoodAssetSpec(
        concept_id="nutrition.carbohydrate",
        label="优质碳水",
        description="明确讨论碳水化合物或优质碳水；不因泛指主食而触发",
        aliases=("碳水化合物", "优质碳水", "碳水"),
        asset_id="carbohydrate.quality_guide.01",
        name="优质碳水指南",
        source="贴图1/食物/优质碳水.jpg",
        bundle="carbohydrate_quality_guide_01",
        fit="contain",
        default_corner="top_right",
        default_scale=0.23,
        default_opacity=0.98,
    ),
    FoodAssetSpec(
        concept_id="food.nuts",
        label="坚果",
        description="作为食物、加餐或明确营养搭配出现的坚果",
        aliases=("混合坚果", "巴旦木", "坚果", "核桃", "杏仁"),
        asset_id="nuts.mixed.01",
        name="混合坚果",
        source="蛋白质/坚果.webp",
        bundle="nuts_mixed_01",
        fit="cover",
        default_corner="top_right",
        default_scale=0.26,
    ),
    FoodAssetSpec(
        concept_id="meal.breakfast",
        label="早餐",
        description="明确泛指早餐、早饭或营养早餐搭配",
        aliases=("早餐搭配", "营养早餐", "早餐", "早饭"),
        asset_id="breakfast.balanced_plate.01",
        name="营养早餐餐盘",
        source="早餐/1040g3k031i0ni85pna0049vtvvkekjc7fvlfqh8!nc_n_webp_mw_1.webp",
        bundle="breakfast_balanced_plate_01",
        fit="cover",
        default_corner="top_left",
        default_scale=0.25,
    ),
    FoodAssetSpec(
        concept_id="food.milk",
        label="牛奶",
        description="作为饮品或明确餐食组成出现的牛奶",
        aliases=("低脂牛奶", "脱脂牛奶", "纯牛奶", "牛奶"),
        asset_id="milk.glass.01",
        name="一杯牛奶",
        source="水/牛奶.webp",
        bundle="milk_glass_01",
        fit="cover",
        default_corner="top_right",
        default_scale=0.26,
    ),
    FoodAssetSpec(
        concept_id="food.fish",
        label="鱼",
        description="作为食物、食材或明确餐食示例出现的鱼肉或鱼类菜品",
        aliases=("清蒸鱼", "鱼肉", "鱼类", "吃鱼"),
        asset_id="fish.steamed.01",
        name="清蒸鱼",
        source="蛋白质/清蒸鱼.webp",
        bundle="fish_steamed_01",
        fit="cover",
        default_corner="top_left",
        default_scale=0.26,
    ),
    FoodAssetSpec(
        concept_id="food.shrimp",
        label="虾",
        description="作为食物、食材或明确餐食示例出现的虾或虾仁",
        aliases=("水煮虾", "虾仁", "大虾", "虾"),
        asset_id="shrimp.cooked.01",
        name="熟虾",
        source="蛋白质/虾.webp",
        bundle="shrimp_cooked_01",
        fit="cover",
        default_corner="top_right",
        default_scale=0.26,
    ),
    FoodAssetSpec(
        concept_id="food.beef",
        label="牛肉",
        description="作为食物、食材或明确餐食示例出现的牛肉",
        aliases=("瘦牛肉", "牛肉片", "牛肉"),
        asset_id="beef.boiled.01",
        name="水煮牛肉片",
        source="蛋白质/牛肉.webp",
        bundle="beef_boiled_01",
        fit="cover",
        default_corner="top_left",
        default_scale=0.26,
    ),
    FoodAssetSpec(
        concept_id="food.chicken_breast",
        label="鸡胸肉",
        description="作为食物、食材或明确餐食示例出现的鸡胸肉",
        aliases=("鸡胸肉", "鸡脯肉", "鸡胸"),
        asset_id="chicken_breast.cooked.01",
        name="熟鸡胸肉",
        source="蛋白质/鸡胸肉.webp",
        bundle="chicken_breast_cooked_01",
        fit="cover",
        default_corner="top_right",
        default_scale=0.26,
    ),
    FoodAssetSpec(
        concept_id="food.tofu",
        label="豆腐",
        description="作为食物、食材或明确餐食示例出现的豆腐",
        aliases=("老豆腐", "嫩豆腐", "豆制品", "豆腐"),
        asset_id="tofu.plain.01",
        name="白豆腐",
        source="贴图1/食物/豆腐.jpg",
        bundle="tofu_plain_01",
        fit="cover",
        default_corner="top_left",
        default_scale=0.26,
    ),
    FoodAssetSpec(
        concept_id="food.cucumber",
        label="黄瓜",
        description="作为食物、食材或明确餐食示例出现的黄瓜",
        aliases=("生吃黄瓜", "黄瓜", "青瓜"),
        asset_id="cucumber.salad.01",
        name="凉拌黄瓜",
        source="贴图1/食物/黄瓜3.jpg",
        bundle="cucumber_salad_lower_fade_02",
        fit="cover_top_fade",
        default_corner="bottom_left",
        default_scale=0.60,
    ),
    FoodAssetSpec(
        concept_id="food.fruit",
        label="水果",
        description="明确泛指水果、新鲜水果或水果拼盘",
        aliases=("水果拼盘", "新鲜水果", "水果"),
        asset_id="fruit.platter.01",
        name="水果拼盘",
        source="贴图1/食物/水果拼盘.jpg",
        bundle="fruit_platter_01",
        fit="cover",
        default_corner="top_left",
        default_scale=0.25,
    ),
)


def _read_image(path: Path) -> np.ndarray:
    try:
        encoded = np.fromfile(path, dtype=np.uint8)
    except OSError as exc:
        raise ValueError(f"无法读取图片：{path}") from exc
    if encoded.size == 0:
        raise ValueError(f"图片为空：{path}")
    image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"无法解码图片：{path}")
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGRA)
    if image.ndim != 3:
        raise ValueError(f"不支持的图片维度：{path}")
    channels = image.shape[2]
    if channels == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    if channels == 4:
        return image
    raise ValueError(f"不支持的图片通道数 {channels}：{path}")


def normalize_image(image: np.ndarray, fit: str, size: int = CANVAS_SIZE) -> np.ndarray:
    if fit not in {"cover", "contain", "cover_top_fade"}:
        raise ValueError(f"未知图片适配方式：{fit}")
    if image.ndim != 3 or image.shape[2] != 4:
        raise ValueError("normalize_image 需要 BGRA 图片")
    height, width = image.shape[:2]
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸无效")

    cover = fit in {"cover", "cover_top_fade"}
    scale = max(size / width, size / height) if cover else min(
        size / width, size / height
    )
    resized_width = max(1, round(width * scale))
    resized_height = max(1, round(height * scale))
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LANCZOS4
    resized = cv2.resize(
        image,
        (resized_width, resized_height),
        interpolation=interpolation,
    )

    if cover:
        x = max(0, (resized_width - size) // 2)
        y = max(0, (resized_height - size) // 2)
        result = resized[y : y + size, x : x + size]
        if result.shape[:2] != (size, size):
            raise ValueError("cover 裁剪结果尺寸异常")
        if fit == "cover_top_fade":
            result = result.copy()
            fade_height = max(1, round(size * 0.32))
            ramp = np.linspace(0.0, 1.0, fade_height, dtype=np.float32)
            source_alpha = result[:fade_height, :, 3].astype(np.float32)
            result[:fade_height, :, 3] = (
                source_alpha * ramp[:, np.newaxis]
            ).astype(np.uint8)
        return result

    canvas = np.full((size, size, 4), 255, dtype=np.uint8)
    x = (size - resized_width) // 2
    y = (size - resized_height) // 2
    source_alpha = resized[:, :, 3:4].astype(np.float32) / 255.0
    destination = canvas[y : y + resized_height, x : x + resized_width]
    destination[:, :, :3] = (
        resized[:, :, :3].astype(np.float32) * source_alpha
        + destination[:, :, :3].astype(np.float32) * (1.0 - source_alpha)
    ).astype(np.uint8)
    destination[:, :, 3] = 255
    return canvas


def _write_png(path: Path, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError(f"PNG 编码失败：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        encoded.tofile(path)
    except OSError as exc:
        raise ValueError(f"PNG 写入失败：{path}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_template(template_bundle: Path) -> None:
    sticker = template_bundle / "sticker.json"
    preview = template_bundle / "resources" / "sticker" / "singleImage.png"
    if not sticker.is_file() or not preview.is_file():
        raise ValueError(f"模板 bundle 不完整：{template_bundle}")


def _sanitize_sticker_metadata(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取模板 sticker.json：{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"模板 sticker.json 不是对象：{path}")

    material = payload.get("material")
    resource = payload.get("resource")
    source = payload.get("source")
    if isinstance(material, dict):
        # The renderer resolves and overwrites this path from resource.library_path.
        material["path"] = ""
    if isinstance(resource, dict):
        # Keep only the portable relative library_path as the runtime source.
        resource["original_path"] = ""
    if isinstance(source, dict):
        # Collection-machine draft labels are audit hints, not runtime dependencies.
        source["label"] = ""

    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _concept_payload(spec: FoodAssetSpec) -> dict[str, object]:
    return {
        "concept_id": spec.concept_id,
        "label": spec.label,
        "description": spec.description,
        "aliases": list(spec.aliases),
    }


def _asset_payload(spec: FoodAssetSpec) -> dict[str, object]:
    bundle = f"bundles/{spec.bundle}"
    return {
        "asset_id": spec.asset_id,
        "concept_id": spec.concept_id,
        "name": spec.name,
        "bundle": bundle,
        "image": f"{bundle}/resources/sticker/singleImage.png",
        "default_corner": spec.default_corner,
        "default_scale": spec.default_scale,
        "default_opacity": spec.default_opacity,
    }


def build_food_semantic_bundles(
    *,
    source_root: Path,
    output_root: Path,
    template_bundle: Path = DEFAULT_TEMPLATE_BUNDLE,
    specs: tuple[FoodAssetSpec, ...] = FOOD_ASSETS,
) -> dict[str, object]:
    source_root = source_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    template_bundle = template_bundle.expanduser().resolve()
    _validate_template(template_bundle)
    output_root.mkdir(parents=True, exist_ok=True)
    bundles_root = output_root / "bundles"
    bundles_root.mkdir(exist_ok=True)

    succeeded: list[dict[str, object]] = []
    failed: list[dict[str, str]] = []
    successful_specs: list[FoodAssetSpec] = []

    for spec in specs:
        source_path = source_root / Path(spec.source)
        target_bundle = bundles_root / spec.bundle
        temporary_bundle = bundles_root / f".building-{spec.bundle}-{uuid.uuid4().hex}"
        try:
            if not source_path.is_file():
                raise ValueError(f"源图片不存在：{source_path}")
            if target_bundle.exists():
                raise ValueError(f"目标 bundle 已存在，拒绝覆盖：{target_bundle}")
            image = normalize_image(_read_image(source_path), spec.fit)
            shutil.copytree(template_bundle, temporary_bundle)
            _sanitize_sticker_metadata(temporary_bundle / "sticker.json")
            preview_path = (
                temporary_bundle / "resources" / "sticker" / "singleImage.png"
            )
            _write_png(preview_path, image)
            temporary_bundle.rename(target_bundle)
            final_preview = target_bundle / "resources" / "sticker" / "singleImage.png"
            successful_specs.append(spec)
            succeeded.append(
                {
                    "asset_id": spec.asset_id,
                    "concept_id": spec.concept_id,
                    "source": spec.source,
                    "bundle": f"bundles/{spec.bundle}",
                    "image_sha256": _sha256(final_preview),
                    "width": CANVAS_SIZE,
                    "height": CANVAS_SIZE,
                }
            )
        except Exception as exc:  # noqa: BLE001 - batch must continue per asset
            if temporary_bundle.exists():
                shutil.rmtree(temporary_bundle)
            failed.append({"asset_id": spec.asset_id, "error": str(exc)})

    concepts: list[dict[str, object]] = []
    seen_concepts: set[str] = set()
    for spec in successful_specs:
        if spec.concept_id not in seen_concepts:
            concepts.append(_concept_payload(spec))
            seen_concepts.add(spec.concept_id)

    patch = {
        "schema": PATCH_SCHEMA,
        "concepts": concepts,
        "assets": [_asset_payload(spec) for spec in successful_specs],
    }
    patch_path = output_root / "catalog.patch.json"
    patch_path.write_text(
        json.dumps(patch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    report = {
        "schema": REPORT_SCHEMA,
        "source_root": str(source_root),
        "output_root": str(output_root),
        "template_bundle": str(template_bundle),
        "requested": len(specs),
        "succeeded_count": len(succeeded),
        "failed_count": len(failed),
        "succeeded": succeeded,
        "failed": failed,
        "catalog_patch": str(patch_path),
    }
    report_path = output_root / "build_report.json"
    report["report_path"] = str(report_path)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="为首批高频食物素材生成语义贴图 sticker bundle（只写 staging）。"
    )
    parser.add_argument("--source-root", required=True, help="新食物素材根目录")
    parser.add_argument("--output-root", required=True, help="staging 输出目录")
    parser.add_argument(
        "--template-bundle",
        default=str(DEFAULT_TEMPLATE_BUNDLE),
        help="已验证的静态 sticker bundle 模板",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="ASSET_ID",
        help="只生成指定 asset_id；可重复传入",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    specs = FOOD_ASSETS
    if args.only:
        requested = set(args.only)
        known = {spec.asset_id for spec in FOOD_ASSETS}
        unknown = sorted(requested - known)
        if unknown:
            print(f"error: 未知 asset_id：{', '.join(unknown)}", file=sys.stderr)
            return 2
        specs = tuple(spec for spec in FOOD_ASSETS if spec.asset_id in requested)

    try:
        report = build_food_semantic_bundles(
            source_root=Path(args.source_root),
            output_root=Path(args.output_root),
            template_bundle=Path(args.template_bundle),
            specs=specs,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
