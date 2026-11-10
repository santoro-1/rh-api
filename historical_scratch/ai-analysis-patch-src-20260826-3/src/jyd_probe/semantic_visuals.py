"""Local semantic-visual catalog, deterministic recall and timing policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .caption_alignment import CaptionAlignmentError, script_tokens
from .semantic_subtitles import SemanticSubtitleMappingError
from .layout_profiles import nameplate_overlay


CATALOG_SCHEMA_V1 = "jyd.semantic-visual-catalog.v1"
CATALOG_SCHEMA_V2 = "jyd.semantic-visual-catalog.v2"
CATALOG_SCHEMA_V3 = "jyd.semantic-visual-catalog.v3"
CATALOG_SCHEMAS = frozenset(
    {CATALOG_SCHEMA_V1, CATALOG_SCHEMA_V2, CATALOG_SCHEMA_V3}
)
# Import-compatible alias for callers that still mean the legacy schema.
CATALOG_SCHEMA = CATALOG_SCHEMA_V1
CANDIDATE_SCHEMA = "jyd.semantic-visual-candidates.v1"
RECIPE_SCHEMA_V1 = "jyd.semantic-visual-recipe.v1"
RECIPE_SCHEMA_V2 = "jyd.semantic-visual-recipe.v2"
RECIPE_SCHEMAS = frozenset({RECIPE_SCHEMA_V1, RECIPE_SCHEMA_V2})
RECIPE_SCHEMA = RECIPE_SCHEMA_V2
DEFAULT_LIBRARY_ID = "jyd.semantic-visual-library.default"
FIXED_NAMEPLATE_BUNDLE = Path("fixed") / "nameplate_zhangluo"
FIXED_NAMEPLATE_PREVIEW_URL = "/api/new/fixed-visuals/nameplate/preview"
# Keep the fixed nameplate clear of uniforms/logos on the upper right chest.
# At scale 0.60, transform_x=-0.40 aligns its left edge with the video frame.
FIXED_NAMEPLATE_SCALE = 0.60
FIXED_NAMEPLATE_TRANSFORM_X = -0.40
FIXED_NAMEPLATE_TRANSFORM_Y = -0.26
MEDIA_POLICIES = frozenset(
    {"image_only", "video_only", "prefer_image", "prefer_video", "mixed"}
)
VISUAL_CORNERS = frozenset(
    {
        "top_left",
        "top_right",
        "bottom_left",
        "bottom_right",
        "bottom_center",
        "center",
    }
)
VISUAL_PHRASE_BOUNDARIES = frozenset("，,。！？!?；;：:\n\r")
VISUAL_MAX_AUTO_PER_MINUTE = 24
VISUAL_ENRICHMENT_MIN_GAP_US = 6_000_000
VISUAL_MINOR_OVERLAP_TOLERANCE_US = 500_000
VISUAL_SEAM_BROLL_DURATION_US = 1_000_000
# Semantic foreground videos and full-screen B-roll always play once.  Keep
# this independent from legacy catalog loop metadata and generic render APIs.
VISUAL_VIDEO_LOOP_TO_TARGET = False
# Edit this one value to tune how often ordinary B-roll is attempted. It is a
# target cadence, not a quota: a slot is omitted when its context has no
# relevant, locally available concept.
VISUAL_BROLL_TARGET_INTERVAL_SECONDS = 10
VISUAL_MAX_CONCEPTS_PER_ANCHOR = 8
EDITORIAL_BROLL_POOL_IDS = (
    "editorial.home_daily",
    "editorial.meal_daily",
    "editorial.leisure_daily",
    "editorial.family_life",
    "editorial.mood_atmosphere",
)
_EDITORIAL_BROLL_POOLS_BY_ARTICLE_TYPE = {
    "鸡汤文": (
        "editorial.home_daily",
        "editorial.leisure_daily",
        "editorial.family_life",
        "editorial.mood_atmosphere",
    ),
    "干货类": (
        "editorial.home_daily",
        "editorial.meal_daily",
        "editorial.leisure_daily",
        "editorial.mood_atmosphere",
    ),
    "带人设介绍的干货类": EDITORIAL_BROLL_POOL_IDS,
}
_VISUAL_ESTIMATED_SPEECH_CHARS_PER_SECOND = 3.5
VISUAL_TIMING_POLICY_VERSION = "sentence-v1"
VISUAL_ENRICHMENT_TAGS = frozenset({"空镜", "相关素材", "b-roll", "broll", "enrichment"})
VISUAL_USAGE_MODES = frozenset(
    {
        "semantic_overlay",
        "list_quick_cut",
        "full_screen_broll",
        "seam_broll",
        "action_demo",
        "knowledge_card",
        "manual_only",
    }
)
VISUAL_TRIGGER_BASES = frozenset(
    {
        "exact_subject",
        "co_dominant_subject",
        "complete_scene",
        "category_collection",
        "infographic",
        "approved_exemplar",
    }
)
VISUAL_ACTIONS = frozenset(
    {
        "pouring",
        "drinking",
        "washing",
        "peeling",
        "cutting",
        "cooking",
        "plating",
        "mixing",
        "serving",
        "eating",
        "walking",
        "running",
        "stretching",
        "training",
        "weighing",
        "brewing",
        "commuting",
        "harvesting",
        "reading",
        "resting",
        "soaking",
        "working",
    }
)
VISUAL_RIGHTS_STATUSES = frozenset(
    {"cleared", "internal", "attributed", "unknown", "restricted"}
)
VISUAL_NETWORK_ATTRIBUTION_TEXT = "素材来源于网络"
VISUAL_PERSON_STATUSES = frozenset(
    {"none", "unidentifiable", "identifiable", "public_figure", "unknown"}
)
VISUAL_BRAND_STATUSES = frozenset({"none", "incidental", "prominent", "unknown"})
VISUAL_HEALTH_CLAIM_STATUSES = frozenset(
    {"none", "general_wellness", "specific_claim", "review_required", "unknown"}
)
VISUAL_PLATFORM_UI_STATUSES = frozenset({"none", "crop_edge", "embedded", "unknown"})
VISUAL_IMAGE_DEFAULT_CORNER = "bottom_center"
VISUAL_IMAGE_DEFAULT_SCALE = 0.56
VISUAL_ACTION_VIDEO_DEFAULT_CORNER = "bottom_center"
VISUAL_ACTION_VIDEO_DEFAULT_SCALE = 0.615
VISUAL_BROLL_DEFAULT_CORNER = "center"
VISUAL_BROLL_DEFAULT_SCALE = 1.0
VISUAL_ALIAS_EXCLUDED_COMPOUNDS = {
    "鸡蛋": ("鸡蛋糕",),
    "蔬菜": ("蔬菜沙拉", "蔬菜色拉"),
    "青菜": ("青菜沙拉", "青菜色拉"),
    "三文鱼": ("植物三文鱼",),
}


class SemanticVisualCatalogError(ValueError):
    pass


def fixed_nameplate_overlay(
    library_root: str | Path, layout_profile: Any = "standing"
) -> dict[str, Any]:
    """Return the portable, non-editable nameplate recipe used by every project video."""

    return nameplate_overlay(library_root, layout_profile)


@dataclass(frozen=True)
class SemanticVisualCatalog:
    root: Path
    schema: str
    library_id: str | None
    catalog_version: str
    concepts: tuple[dict[str, Any], ...]
    assets: tuple[dict[str, Any], ...]

    def concept(self, concept_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.concepts if item["concept_id"] == concept_id),
            None,
        )

    def asset(self, asset_id: str) -> dict[str, Any] | None:
        return next((item for item in self.assets if item["asset_id"] == asset_id), None)

    def public_payload(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "catalog_version": self.catalog_version,
            "concepts": [dict(item) for item in self.concepts],
            "assets": [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    not in {
                        "bundle_path",
                        "image_path",
                        "resource_path",
                        "preview_path",
                    }
                }
                for item in self.assets
            ],
        }
        if self.library_id is not None:
            payload["library_id"] = self.library_id
        return payload


def _safe_child(root: Path, raw: str, *, kind: str) -> Path:
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SemanticVisualCatalogError(f"{kind} path escapes catalog root") from exc
    if not candidate.is_file() and not candidate.is_dir():
        raise SemanticVisualCatalogError(f"{kind} path is missing")
    return candidate


def _safe_relative_child(root: Path, raw: object, *, kind: str) -> tuple[str, Path]:
    relative = str(raw).strip()
    if not relative or Path(relative).is_absolute():
        raise SemanticVisualCatalogError(f"{kind} path must be relative")
    return relative, _safe_child(root, relative, kind=kind)


def _update_file_hash(digest: Any, path: Path) -> None:
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)


def _load_semantic_visual_catalog_v1(root: str | Path) -> SemanticVisualCatalog:
    catalog_root = Path(root).expanduser().resolve()
    manifest_path = catalog_root / "catalog.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticVisualCatalogError("semantic visual catalog is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema") != CATALOG_SCHEMA_V1:
        raise SemanticVisualCatalogError("unsupported semantic visual catalog schema")
    concepts = payload.get("concepts")
    assets = payload.get("assets")
    if not isinstance(concepts, list) or not isinstance(assets, list):
        raise SemanticVisualCatalogError("catalog concepts/assets must be arrays")

    normalized_concepts: list[dict[str, Any]] = []
    concept_ids: set[str] = set()
    for raw in concepts:
        if not isinstance(raw, dict) or set(raw) != {
            "concept_id",
            "label",
            "description",
            "aliases",
        }:
            raise SemanticVisualCatalogError("invalid catalog concept")
        concept_id = str(raw["concept_id"]).strip()
        label = str(raw["label"]).strip()
        description = str(raw["description"]).strip()
        aliases = raw["aliases"]
        if (
            not concept_id
            or not label
            or not description
            or concept_id in concept_ids
            or not isinstance(aliases, list)
            or not aliases
            or any(
                not isinstance(alias, str) or not alias or alias != alias.strip()
                for alias in aliases
            )
        ):
            raise SemanticVisualCatalogError("invalid concept id or aliases")
        concept_ids.add(concept_id)
        normalized_concepts.append(
            {
                "concept_id": concept_id,
                "label": label,
                "description": description,
                "aliases": list(dict.fromkeys(aliases)),
            }
        )

    normalized_assets: list[dict[str, Any]] = []
    asset_ids: set[str] = set()
    for raw in assets:
        required = {
            "asset_id",
            "concept_id",
            "name",
            "bundle",
            "image",
            "default_corner",
            "default_scale",
            "default_opacity",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise SemanticVisualCatalogError("invalid catalog asset")
        asset_id = str(raw["asset_id"]).strip()
        concept_id = str(raw["concept_id"]).strip()
        name = str(raw["name"]).strip()
        corner = str(raw["default_corner"]).strip()
        try:
            scale = float(raw["default_scale"])
            opacity = float(raw["default_opacity"])
        except (TypeError, ValueError) as exc:
            raise SemanticVisualCatalogError("invalid asset defaults") from exc
        if (
            not asset_id
            or not name
            or asset_id in asset_ids
            or concept_id not in concept_ids
            or corner not in VISUAL_CORNERS
            or not 0.05 <= scale <= 2.0
            or not 0.0 <= opacity <= 1.0
        ):
            raise SemanticVisualCatalogError("invalid asset id or concept")
        bundle_path = _safe_child(catalog_root, str(raw["bundle"]), kind="bundle")
        image_path = _safe_child(catalog_root, str(raw["image"]), kind="image")
        if not bundle_path.is_dir() or not image_path.is_file():
            raise SemanticVisualCatalogError("asset bundle/image type is invalid")
        if not (bundle_path / "sticker.json").is_file():
            raise SemanticVisualCatalogError("asset bundle has no sticker.json")
        asset_ids.add(asset_id)
        normalized_assets.append(
            {
                "asset_id": asset_id,
                "concept_id": concept_id,
                "name": name,
                "bundle": str(raw["bundle"]),
                "image": str(raw["image"]),
                "preview_url": f"/api/new/semantic-visuals/{asset_id}/preview",
                "default_corner": corner,
                "default_scale": scale,
                "default_opacity": opacity,
                "bundle_path": str(bundle_path),
                "image_path": str(image_path),
            }
        )
    available_concepts = {item["concept_id"] for item in normalized_assets}
    normalized_concepts = [
        item for item in normalized_concepts if item["concept_id"] in available_concepts
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    version_digest = hashlib.sha256(canonical.encode("utf-8"))
    for asset in sorted(normalized_assets, key=lambda item: item["asset_id"]):
        _update_file_hash(version_digest, Path(asset["bundle_path"]) / "sticker.json")
        _update_file_hash(version_digest, Path(asset["image_path"]))
    catalog_version = "sha256:" + version_digest.hexdigest()
    return SemanticVisualCatalog(
        root=catalog_root,
        schema=CATALOG_SCHEMA_V1,
        library_id=None,
        catalog_version=catalog_version,
        concepts=tuple(normalized_concepts),
        assets=tuple(normalized_assets),
    )


def _with_unified_v1_assets(catalog: SemanticVisualCatalog) -> SemanticVisualCatalog:
    assets: list[dict[str, Any]] = []
    for raw in catalog.assets:
        asset = dict(raw)
        asset.update(
            {
                "concept_ids": [asset["concept_id"]],
                "description": asset["name"],
                "media_type": "image",
                "renderer": "jyd_sticker_bundle",
                "tags": [],
                "resource": {
                    "bundle": asset["bundle"],
                    "preview": asset["image"],
                },
                "defaults": {
                    "corner": asset["default_corner"],
                    "scale": asset["default_scale"],
                    "opacity": asset["default_opacity"],
                    "duration_us": 1_800_000,
                },
                "resource_path": asset["bundle_path"],
                "preview_path": asset["image_path"],
            }
        )
        assets.append(asset)
    return SemanticVisualCatalog(
        root=catalog.root,
        schema=catalog.schema,
        library_id=catalog.library_id,
        catalog_version=catalog.catalog_version,
        concepts=catalog.concepts,
        assets=tuple(assets),
    )


def _normalize_v2_concepts(raw_concepts: object) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(raw_concepts, list):
        raise SemanticVisualCatalogError("catalog concepts must be an array")
    concepts: list[dict[str, Any]] = []
    concept_ids: set[str] = set()
    for raw in raw_concepts:
        if not isinstance(raw, dict) or set(raw) != {
            "concept_id",
            "label",
            "description",
            "aliases",
        }:
            raise SemanticVisualCatalogError("invalid catalog concept")
        concept_id = str(raw["concept_id"]).strip()
        label = str(raw["label"]).strip()
        description = str(raw["description"]).strip()
        aliases = raw["aliases"]
        if (
            not concept_id
            or not label
            or not description
            or concept_id in concept_ids
            or not isinstance(aliases, list)
            or not aliases
            or any(
                not isinstance(alias, str) or not alias or alias != alias.strip()
                for alias in aliases
            )
        ):
            raise SemanticVisualCatalogError("invalid concept id or aliases")
        concept_ids.add(concept_id)
        concepts.append(
            {
                "concept_id": concept_id,
                "label": label,
                "description": description,
                "aliases": list(dict.fromkeys(aliases)),
            }
        )
    return concepts, concept_ids


def _normalize_v2_identity(
    raw: Mapping[str, Any],
    *,
    asset_ids: set[str],
    concept_ids: set[str],
    allow_empty_concepts: bool = False,
) -> tuple[str, list[str], str, str, list[str]]:
    asset_id = str(raw["asset_id"]).strip()
    name = str(raw["name"]).strip()
    description = str(raw["description"]).strip()
    raw_concept_ids = raw["concept_ids"]
    tags = raw["tags"]
    if not isinstance(raw_concept_ids, list) or not isinstance(tags, list):
        raise SemanticVisualCatalogError("asset concept_ids/tags must be arrays")
    normalized_concept_ids = list(
        dict.fromkeys(str(value).strip() for value in raw_concept_ids)
    )
    if (
        not asset_id
        or asset_id in asset_ids
        or not name
        or not description
        or (not normalized_concept_ids and not allow_empty_concepts)
        or any(not value or value not in concept_ids for value in normalized_concept_ids)
        or any(
            not isinstance(tag, str) or not tag or tag != tag.strip()
            for tag in tags
        )
    ):
        raise SemanticVisualCatalogError("invalid asset identity")
    asset_ids.add(asset_id)
    return asset_id, normalized_concept_ids, name, description, list(dict.fromkeys(tags))


def _normalized_visual_defaults(
    raw: Mapping[str, Any], *, video: bool
) -> dict[str, Any]:
    expected = {"corner", "scale", "opacity", "duration_us"}
    if video:
        expected |= {"source_start_us", "mute", "loop", "fit"}
    if set(raw) != expected:
        raise SemanticVisualCatalogError("invalid asset defaults")
    try:
        corner = str(raw["corner"]).strip()
        scale = float(raw["scale"])
        opacity = float(raw["opacity"])
        duration_us = int(raw["duration_us"])
    except (TypeError, ValueError) as exc:
        raise SemanticVisualCatalogError("invalid asset defaults") from exc
    normalized: dict[str, Any] = {
        "corner": corner,
        "scale": scale,
        "opacity": opacity,
        "duration_us": duration_us,
    }
    if (
        corner not in VISUAL_CORNERS
        or not 0.05 <= scale <= 2.0
        or not 0.0 <= opacity <= 1.0
        or duration_us <= 0
    ):
        raise SemanticVisualCatalogError("invalid asset defaults")
    if not video:
        return normalized
    try:
        source_start_us = int(raw["source_start_us"])
    except (TypeError, ValueError) as exc:
        raise SemanticVisualCatalogError("invalid video defaults") from exc
    mute = raw["mute"]
    loop = raw["loop"]
    fit = str(raw["fit"]).strip()
    if (
        source_start_us < 0
        or not isinstance(mute, bool)
        or not isinstance(loop, bool)
        or fit not in {"cover", "contain"}
    ):
        raise SemanticVisualCatalogError("invalid video defaults")
    normalized.update(
        {
            "source_start_us": source_start_us,
            "mute": mute,
            "loop": loop,
            "fit": fit,
        }
    )
    return normalized


def _apply_product_visual_layout(
    defaults: Mapping[str, Any],
    *,
    media_type: str,
    tags: Iterable[str],
    usage_modes: Iterable[str] = (),
) -> dict[str, Any]:
    """Keep automatic talking-head visuals on the accepted product layout.

    Catalog manifests retain asset provenance and duration defaults, but placement is
    a product-level rule.  Centralizing it here also refreshes untouched recipes from
    older catalogs without overwriting manual or locked project edits.
    """

    normalized = dict(defaults)
    if media_type == "image":
        normalized.update(
            corner=VISUAL_IMAGE_DEFAULT_CORNER,
            scale=VISUAL_IMAGE_DEFAULT_SCALE,
        )
        return normalized
    enrichment = _is_enrichment_metadata(tags=tags, usage_modes=usage_modes)
    normalized.update(
        corner=(VISUAL_BROLL_DEFAULT_CORNER if enrichment else VISUAL_ACTION_VIDEO_DEFAULT_CORNER),
        scale=(VISUAL_BROLL_DEFAULT_SCALE if enrichment else VISUAL_ACTION_VIDEO_DEFAULT_SCALE),
    )
    return normalized


def _is_enrichment_metadata(
    *, tags: Iterable[str], usage_modes: Iterable[str] = ()
) -> bool:
    normalized_modes = {str(value).strip() for value in usage_modes}
    if normalized_modes:
        return bool(normalized_modes & {"full_screen_broll", "seam_broll"})
    return any(str(tag).strip().lower() in VISUAL_ENRICHMENT_TAGS for tag in tags)


def _is_enrichment_asset(asset: Mapping[str, Any]) -> bool:
    return _is_enrichment_metadata(
        tags=asset.get("tags", ()), usage_modes=asset.get("usage_modes", ())
    )


def _compatibility_concept_id(common: Mapping[str, Any]) -> str:
    concept_ids = list(common.get("concept_ids", ()))
    if concept_ids:
        return str(concept_ids[0])
    semantic_roles = common.get("semantic_roles", {})
    if isinstance(semantic_roles, Mapping):
        for role in ("depicts", "expresses", "related"):
            values = semantic_roles.get(role, ())
            if isinstance(values, Iterable) and not isinstance(values, (str, bytes)):
                for value in values:
                    return str(value)
    raise SemanticVisualCatalogError("asset must retain at least one semantic relation")


def _normalize_v2_image(
    raw: Mapping[str, Any],
    *,
    catalog_root: Path,
    common: dict[str, Any],
) -> tuple[dict[str, Any], list[Path]]:
    resource = raw["resource"]
    defaults = raw["defaults"]
    if (
        not isinstance(resource, Mapping)
        or set(resource) != {"bundle", "preview"}
        or not isinstance(defaults, Mapping)
    ):
        raise SemanticVisualCatalogError("invalid image asset resource")
    bundle, bundle_path = _safe_relative_child(
        catalog_root, resource["bundle"], kind="bundle"
    )
    preview, preview_path = _safe_relative_child(
        catalog_root, resource["preview"], kind="preview"
    )
    sticker_path = bundle_path / "sticker.json"
    if not bundle_path.is_dir() or not preview_path.is_file() or not sticker_path.is_file():
        raise SemanticVisualCatalogError("invalid image asset files")
    normalized_defaults = _apply_product_visual_layout(
        _normalized_visual_defaults(defaults, video=False),
        media_type="image",
        tags=common["tags"],
        usage_modes=common.get("usage_modes", ()),
    )
    asset_id = common["asset_id"]
    normalized = {
        **common,
        "media_type": "image",
        "renderer": "jyd_sticker_bundle",
        "resource": {"bundle": bundle, "preview": preview},
        "defaults": normalized_defaults,
        # Compatibility aliases used by the current v1 recipe/web handlers.
        "concept_id": _compatibility_concept_id(common),
        "bundle": bundle,
        "image": preview,
        "preview_url": f"/api/new/semantic-visuals/{asset_id}/preview",
        "default_corner": normalized_defaults["corner"],
        "default_scale": normalized_defaults["scale"],
        "default_opacity": normalized_defaults["opacity"],
        "bundle_path": str(bundle_path),
        "image_path": str(preview_path),
        "resource_path": str(bundle_path),
        "preview_path": str(preview_path),
    }
    return normalized, [sticker_path, preview_path]


def _normalize_v2_video(
    raw: Mapping[str, Any],
    *,
    catalog_root: Path,
    common: dict[str, Any],
) -> tuple[dict[str, Any], list[Path]]:
    resource = raw["resource"]
    defaults = raw["defaults"]
    required = {"video", "preview", "duration_us", "width", "height", "has_audio"}
    if (
        not isinstance(resource, Mapping)
        or frozenset(resource) not in {frozenset(required), frozenset(required | {"metadata"})}
        or not isinstance(defaults, Mapping)
    ):
        raise SemanticVisualCatalogError("invalid video asset resource")
    video, video_path = _safe_relative_child(
        catalog_root, resource["video"], kind="video"
    )
    preview, preview_path = _safe_relative_child(
        catalog_root, resource["preview"], kind="preview"
    )
    if not video_path.is_file() or not preview_path.is_file():
        raise SemanticVisualCatalogError("invalid video asset files")
    try:
        source_duration_us = int(resource["duration_us"])
        width = int(resource["width"])
        height = int(resource["height"])
    except (TypeError, ValueError) as exc:
        raise SemanticVisualCatalogError("invalid video asset metadata") from exc
    has_audio = resource["has_audio"]
    if (
        source_duration_us <= 0
        or width <= 0
        or height <= 0
        or not isinstance(has_audio, bool)
    ):
        raise SemanticVisualCatalogError("invalid video asset metadata")
    normalized_defaults = _apply_product_visual_layout(
        _normalized_visual_defaults(defaults, video=True),
        media_type="video",
        tags=common["tags"],
        usage_modes=common.get("usage_modes", ()),
    )
    if normalized_defaults["source_start_us"] >= source_duration_us:
        raise SemanticVisualCatalogError("video default source start exceeds source duration")
    normalized_resource: dict[str, Any] = {
        "video": video,
        "preview": preview,
        "duration_us": source_duration_us,
        "width": width,
        "height": height,
        "has_audio": has_audio,
    }
    version_paths = [video_path, preview_path]
    if "metadata" in resource:
        metadata, metadata_path = _safe_relative_child(
            catalog_root, resource["metadata"], kind="metadata"
        )
        if not metadata_path.is_file():
            raise SemanticVisualCatalogError("invalid video metadata file")
        normalized_resource["metadata"] = metadata
        version_paths.append(metadata_path)
    asset_id = common["asset_id"]
    normalized = {
        **common,
        "media_type": "video",
        "renderer": "video_overlay",
        "resource": normalized_resource,
        "defaults": normalized_defaults,
        "concept_id": _compatibility_concept_id(common),
        "preview_url": f"/api/new/semantic-visuals/{asset_id}/preview",
        "resource_path": str(video_path),
        "preview_path": str(preview_path),
        # Preview endpoint compatibility; a video intentionally has no bundle_path.
        "image_path": str(preview_path),
        "default_corner": normalized_defaults["corner"],
        "default_scale": normalized_defaults["scale"],
        "default_opacity": normalized_defaults["opacity"],
    }
    return normalized, version_paths


def _load_semantic_visual_catalog_v2(
    catalog_root: Path, payload: Mapping[str, Any]
) -> SemanticVisualCatalog:
    if set(payload) != {"schema", "library_id", "concepts", "assets"}:
        raise SemanticVisualCatalogError("invalid semantic visual catalog v2 root")
    library_id = str(payload["library_id"]).strip()
    if not library_id:
        raise SemanticVisualCatalogError("catalog library_id is required")
    concepts, concept_ids = _normalize_v2_concepts(payload["concepts"])
    raw_assets = payload["assets"]
    if not isinstance(raw_assets, list):
        raise SemanticVisualCatalogError("catalog assets must be an array")
    expected_asset_fields = {
        "asset_id",
        "concept_ids",
        "name",
        "description",
        "media_type",
        "renderer",
        "tags",
        "resource",
        "defaults",
    }
    assets: list[dict[str, Any]] = []
    asset_ids: set[str] = set()
    version_paths: list[Path] = []
    for raw in raw_assets:
        if not isinstance(raw, dict) or set(raw) != expected_asset_fields:
            raise SemanticVisualCatalogError("invalid catalog v2 asset")
        asset_id, linked_concepts, name, description, tags = _normalize_v2_identity(
            raw, asset_ids=asset_ids, concept_ids=concept_ids
        )
        common = {
            "asset_id": asset_id,
            "concept_ids": linked_concepts,
            "name": name,
            "description": description,
            "tags": tags,
        }
        pair = (str(raw["media_type"]).strip(), str(raw["renderer"]).strip())
        if pair == ("image", "jyd_sticker_bundle"):
            asset, paths = _normalize_v2_image(
                raw, catalog_root=catalog_root, common=common
            )
        elif pair == ("video", "video_overlay"):
            asset, paths = _normalize_v2_video(
                raw, catalog_root=catalog_root, common=common
            )
        else:
            raise SemanticVisualCatalogError("unsupported media_type/renderer pair")
        assets.append(asset)
        version_paths.extend(paths)
    available_concepts = {
        concept_id for asset in assets for concept_id in asset["concept_ids"]
    }
    concepts = [item for item in concepts if item["concept_id"] in available_concepts]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    version_digest = hashlib.sha256(canonical.encode("utf-8"))
    for path in sorted(version_paths, key=lambda item: str(item).casefold()):
        _update_file_hash(version_digest, path)
    return SemanticVisualCatalog(
        root=catalog_root,
        schema=CATALOG_SCHEMA_V2,
        library_id=library_id,
        catalog_version="sha256:" + version_digest.hexdigest(),
        concepts=tuple(concepts),
        assets=tuple(assets),
    )


def _normalized_v3_string_list(
    raw: object,
    *,
    field: str,
    allowed: set[str] | frozenset[str] | None = None,
    allow_empty: bool = True,
) -> list[str]:
    if not isinstance(raw, list) or any(
        not isinstance(value, str) or not value or value != value.strip()
        for value in raw
    ):
        raise SemanticVisualCatalogError(f"invalid catalog v3 {field}")
    values = list(dict.fromkeys(raw))
    if len(values) != len(raw) or (not allow_empty and not values):
        raise SemanticVisualCatalogError(f"invalid catalog v3 {field}")
    if allowed is not None and any(value not in allowed for value in values):
        raise SemanticVisualCatalogError(f"invalid catalog v3 {field}")
    return values


def _normalize_v3_metadata(
    raw: Mapping[str, Any], *, concept_ids: set[str]
) -> dict[str, Any]:
    semantic_roles = raw["semantic_roles"]
    if not isinstance(semantic_roles, dict) or set(semantic_roles) != {
        "depicts",
        "expresses",
        "related",
    }:
        raise SemanticVisualCatalogError("invalid catalog v3 semantic_roles")
    roles = {
        role: _normalized_v3_string_list(
            semantic_roles[role],
            field=f"semantic_roles.{role}",
            allowed=concept_ids,
        )
        for role in ("depicts", "expresses", "related")
    }
    role_sets = [set(roles[role]) for role in ("depicts", "expresses", "related")]
    if any(role_sets[left] & role_sets[right] for left in range(3) for right in range(left + 1, 3)):
        raise SemanticVisualCatalogError("catalog v3 semantic roles must be disjoint")
    auto_trigger_ids = _normalized_v3_string_list(
        raw["auto_trigger_concept_ids"],
        field="auto_trigger_concept_ids",
        allowed=concept_ids,
    )
    if auto_trigger_ids != list(raw["concept_ids"]):
        raise SemanticVisualCatalogError(
            "catalog v3 concept_ids must equal auto_trigger_concept_ids"
        )
    direct_ids = set(roles["depicts"]) | set(roles["expresses"])
    if not set(auto_trigger_ids).issubset(direct_ids):
        raise SemanticVisualCatalogError(
            "catalog v3 auto triggers must come from depicts or expresses"
        )
    if set(auto_trigger_ids) & set(roles["related"]):
        raise SemanticVisualCatalogError("catalog v3 related concepts cannot auto trigger")

    trigger_basis = raw["trigger_basis"]
    if (
        not isinstance(trigger_basis, dict)
        or set(trigger_basis) != set(auto_trigger_ids)
        or any(
            not isinstance(value, str) or value not in VISUAL_TRIGGER_BASES
            for value in trigger_basis.values()
        )
    ):
        raise SemanticVisualCatalogError("invalid catalog v3 trigger_basis")
    actions = _normalized_v3_string_list(
        raw["visual_actions"], field="visual_actions", allowed=VISUAL_ACTIONS
    )
    usage_modes = _normalized_v3_string_list(
        raw["usage_modes"],
        field="usage_modes",
        allowed=VISUAL_USAGE_MODES,
        allow_empty=False,
    )
    if "manual_only" in usage_modes and usage_modes != ["manual_only"]:
        raise SemanticVisualCatalogError("catalog v3 manual_only must be exclusive")

    cleanliness_grade = str(raw["cleanliness_grade"]).strip()
    rights_status = str(raw["rights_status"]).strip()
    person_status = str(raw["person_status"]).strip()
    brand_status = str(raw["brand_status"]).strip()
    health_claim_status = str(raw["health_claim_status"]).strip()
    platform_ui_status = str(raw["platform_ui_status"]).strip()
    if cleanliness_grade not in {"A", "B", "C", "D"}:
        raise SemanticVisualCatalogError("invalid catalog v3 cleanliness_grade")
    if rights_status not in VISUAL_RIGHTS_STATUSES:
        raise SemanticVisualCatalogError("invalid catalog v3 rights_status")
    if person_status not in VISUAL_PERSON_STATUSES:
        raise SemanticVisualCatalogError("invalid catalog v3 person_status")
    if brand_status not in VISUAL_BRAND_STATUSES:
        raise SemanticVisualCatalogError("invalid catalog v3 brand_status")
    if health_claim_status not in VISUAL_HEALTH_CLAIM_STATUSES:
        raise SemanticVisualCatalogError("invalid catalog v3 health_claim_status")
    if platform_ui_status not in VISUAL_PLATFORM_UI_STATUSES:
        raise SemanticVisualCatalogError("invalid catalog v3 platform_ui_status")

    auto_eligible = raw["auto_eligible"]
    requires_clip = raw["requires_clip"]
    loop_allowed = raw["loop_allowed"]
    if not all(isinstance(value, bool) for value in (auto_eligible, requires_clip, loop_allowed)):
        raise SemanticVisualCatalogError("invalid catalog v3 eligibility flags")
    if auto_eligible and (
        not auto_trigger_ids
        or requires_clip
        or cleanliness_grade in {"C", "D"}
        or "manual_only" in usage_modes
    ):
        raise SemanticVisualCatalogError("invalid catalog v3 auto eligibility")
    if (
        auto_eligible
        and rights_status in {"unknown", "restricted"}
        and set(usage_modes) & {"full_screen_broll", "seam_broll"}
    ):
        raise SemanticVisualCatalogError(
            "catalog v3 unknown or restricted rights cannot auto full-screen"
        )
    if str(raw["media_type"]).strip() == "image" and (
        loop_allowed or set(usage_modes) & {"full_screen_broll", "seam_broll"}
    ):
        raise SemanticVisualCatalogError("invalid catalog v3 image usage")
    if "seam_broll" in usage_modes and str(raw["media_type"]).strip() != "video":
        raise SemanticVisualCatalogError("catalog v3 seam_broll must be video")

    return {
        "semantic_roles": roles,
        "auto_trigger_concept_ids": auto_trigger_ids,
        "trigger_basis": dict(trigger_basis),
        "visual_actions": actions,
        "usage_modes": usage_modes,
        "cleanliness_grade": cleanliness_grade,
        "auto_eligible": auto_eligible,
        "requires_clip": requires_clip,
        "loop_allowed": loop_allowed,
        "rights_status": rights_status,
        "person_status": person_status,
        "brand_status": brand_status,
        "health_claim_status": health_claim_status,
        "platform_ui_status": platform_ui_status,
    }


def _normalize_v3_video_taxonomy(
    raw: object,
    *,
    linked_concepts: list[str],
    concept_ids: set[str],
    usage_modes: Iterable[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {
        "l1_domain_ids",
        "l2_category_ids",
        "l3_exact_concept_ids",
        "action_ids",
        "scene_ids",
        "fallback_concept_ids",
        "fallback_policy",
        "review_status",
    }:
        raise SemanticVisualCatalogError("invalid catalog v3 video_taxonomy")

    def taxonomy_ids(field: str, prefix: str) -> list[str]:
        values = _normalized_v3_string_list(raw[field], field=f"video_taxonomy.{field}")
        if any(not value.startswith(prefix) for value in values):
            raise SemanticVisualCatalogError(
                f"invalid catalog v3 video_taxonomy.{field}"
            )
        return values

    l1_ids = taxonomy_ids("l1_domain_ids", "l1.")
    l2_ids = taxonomy_ids("l2_category_ids", "l2.")
    exact_ids = _normalized_v3_string_list(
        raw["l3_exact_concept_ids"],
        field="video_taxonomy.l3_exact_concept_ids",
        allowed=concept_ids,
    )
    action_ids = _normalized_v3_string_list(
        raw["action_ids"], field="video_taxonomy.action_ids", allowed=VISUAL_ACTIONS
    )
    scene_ids = taxonomy_ids("scene_ids", "l2.scene.")
    fallback_ids = _normalized_v3_string_list(
        raw["fallback_concept_ids"],
        field="video_taxonomy.fallback_concept_ids",
        allowed=concept_ids,
    )
    if not l1_ids or not l2_ids or not exact_ids:
        raise SemanticVisualCatalogError("catalog v3 video taxonomy levels are required")
    if exact_ids != linked_concepts:
        raise SemanticVisualCatalogError(
            "catalog v3 video taxonomy exact concepts must equal concept_ids"
        )
    if not set(scene_ids).issubset(l2_ids):
        raise SemanticVisualCatalogError(
            "catalog v3 video taxonomy facets must be declared L2 categories"
        )
    if any(
        value.startswith(("food.", "dish.", "drink.", "nutrition."))
        or value == "meal.breakfast"
        for value in fallback_ids
    ):
        raise SemanticVisualCatalogError(
            "catalog v3 food/drink/nutrition category fallback is forbidden"
        )
    if "nutrition.protein" in fallback_ids:
        raise SemanticVisualCatalogError("abstract nutrition fallback is forbidden")
    if fallback_ids and not (
        set(usage_modes) & {"full_screen_broll", "seam_broll"}
    ):
        raise SemanticVisualCatalogError(
            "catalog v3 video fallback requires approved b-roll usage"
        )
    fallback_policy = str(raw["fallback_policy"]).strip()
    review_status = str(raw["review_status"]).strip()
    if fallback_policy != "video_only_explicit_whitelist" or not review_status:
        raise SemanticVisualCatalogError("invalid catalog v3 video fallback approval")
    return {
        "l1_domain_ids": l1_ids,
        "l2_category_ids": l2_ids,
        "l3_exact_concept_ids": exact_ids,
        "action_ids": action_ids,
        "scene_ids": scene_ids,
        "fallback_concept_ids": fallback_ids,
        "fallback_policy": fallback_policy,
        "review_status": review_status,
    }


def _load_semantic_visual_catalog_v3(
    catalog_root: Path, payload: Mapping[str, Any]
) -> SemanticVisualCatalog:
    if set(payload) != {"schema", "library_id", "concepts", "assets"}:
        raise SemanticVisualCatalogError("invalid semantic visual catalog v3 root")
    library_id = str(payload["library_id"]).strip()
    if not library_id:
        raise SemanticVisualCatalogError("catalog library_id is required")
    concepts, concept_ids = _normalize_v2_concepts(payload["concepts"])
    raw_assets = payload["assets"]
    if not isinstance(raw_assets, list):
        raise SemanticVisualCatalogError("catalog assets must be an array")
    expected_asset_fields = {
        "asset_id",
        "concept_ids",
        "name",
        "description",
        "media_type",
        "renderer",
        "tags",
        "resource",
        "defaults",
        "semantic_roles",
        "auto_trigger_concept_ids",
        "trigger_basis",
        "visual_actions",
        "usage_modes",
        "cleanliness_grade",
        "auto_eligible",
        "requires_clip",
        "loop_allowed",
        "rights_status",
        "person_status",
        "brand_status",
        "health_claim_status",
        "platform_ui_status",
    }
    assets: list[dict[str, Any]] = []
    asset_ids: set[str] = set()
    version_paths: list[Path] = []
    for raw in raw_assets:
        if not isinstance(raw, dict) or frozenset(raw) not in {
            frozenset(expected_asset_fields),
            frozenset({*expected_asset_fields, "video_taxonomy"}),
        }:
            raise SemanticVisualCatalogError("invalid catalog v3 asset")
        asset_id, linked_concepts, name, description, tags = _normalize_v2_identity(
            raw,
            asset_ids=asset_ids,
            concept_ids=concept_ids,
            allow_empty_concepts=True,
        )
        metadata = _normalize_v3_metadata(raw, concept_ids=concept_ids)
        media_type = str(raw["media_type"]).strip()
        video_taxonomy = None
        if "video_taxonomy" in raw:
            if media_type != "video":
                raise SemanticVisualCatalogError(
                    "catalog v3 image cannot declare video taxonomy"
                )
            video_taxonomy = _normalize_v3_video_taxonomy(
                raw["video_taxonomy"],
                linked_concepts=linked_concepts,
                concept_ids=concept_ids,
                usage_modes=metadata["usage_modes"],
            )
            if video_taxonomy["action_ids"] != metadata["visual_actions"]:
                raise SemanticVisualCatalogError(
                    "catalog v3 video taxonomy actions must equal visual_actions"
                )
        common = {
            "asset_id": asset_id,
            "concept_ids": linked_concepts,
            "name": name,
            "description": description,
            "tags": tags,
            **metadata,
        }
        if video_taxonomy is not None:
            common["video_taxonomy"] = video_taxonomy
        pair = (str(raw["media_type"]).strip(), str(raw["renderer"]).strip())
        if pair == ("image", "jyd_sticker_bundle"):
            asset, paths = _normalize_v2_image(
                raw, catalog_root=catalog_root, common=common
            )
        elif pair == ("video", "video_overlay"):
            asset, paths = _normalize_v2_video(
                raw, catalog_root=catalog_root, common=common
            )
        else:
            raise SemanticVisualCatalogError("unsupported media_type/renderer pair")
        assets.append(asset)
        version_paths.extend(paths)
    quarantine_path = catalog_root / "quarantine" / "platform_ui_pending.json"
    if quarantine_path.exists():
        try:
            quarantine = json.loads(quarantine_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SemanticVisualCatalogError("semantic visual quarantine is unreadable") from exc
        if (
            not isinstance(quarantine, dict)
            or set(quarantine) != {"schema", "status", "reason", "assets"}
            or quarantine.get("schema") != "jyd.semantic-visual-quarantine.v1"
            or quarantine.get("status") != "DISABLED_PENDING_MANUAL_REVIEW"
            or not isinstance(quarantine.get("assets"), list)
        ):
            raise SemanticVisualCatalogError("invalid semantic visual quarantine")
        quarantined_ids: set[str] = set()
        for entry in quarantine["assets"]:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"asset_id", "name", "reason"}
                or not str(entry.get("asset_id") or "").strip()
                or not str(entry.get("reason") or "").strip()
            ):
                raise SemanticVisualCatalogError("invalid semantic visual quarantine asset")
            asset_id = str(entry["asset_id"]).strip()
            if asset_id in quarantined_ids:
                raise SemanticVisualCatalogError("duplicate semantic visual quarantine asset")
            quarantined_ids.add(asset_id)
        if quarantined_ids.difference(asset_ids):
            raise SemanticVisualCatalogError("semantic visual quarantine references unknown asset")
        assets = [
            ({**asset, "auto_eligible": False} if asset["asset_id"] in quarantined_ids else asset)
            for asset in assets
        ]
        version_paths.append(quarantine_path)
    available_concepts = {
        concept_id
        for asset in assets
        for role_ids in asset["semantic_roles"].values()
        for concept_id in role_ids
    }
    available_concepts.update(
        concept_id
        for asset in assets
        for concept_id in asset.get("video_taxonomy", {}).get(
            "fallback_concept_ids", ()
        )
    )
    concepts = [item for item in concepts if item["concept_id"] in available_concepts]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    version_digest = hashlib.sha256(canonical.encode("utf-8"))
    for path in sorted(version_paths, key=lambda item: str(item).casefold()):
        _update_file_hash(version_digest, path)
    return SemanticVisualCatalog(
        root=catalog_root,
        schema=CATALOG_SCHEMA_V3,
        library_id=library_id,
        catalog_version="sha256:" + version_digest.hexdigest(),
        concepts=tuple(concepts),
        assets=tuple(assets),
    )


def load_semantic_visual_catalog(root: str | Path) -> SemanticVisualCatalog:
    catalog_root = Path(root).expanduser().resolve()
    manifest_path = catalog_root / "catalog.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticVisualCatalogError("semantic visual catalog is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema") not in CATALOG_SCHEMAS:
        raise SemanticVisualCatalogError("unsupported semantic visual catalog schema")
    if payload["schema"] == CATALOG_SCHEMA_V1:
        if set(payload) != {"schema", "concepts", "assets"}:
            raise SemanticVisualCatalogError("invalid semantic visual catalog v1 root")
        return _with_unified_v1_assets(_load_semantic_visual_catalog_v1(catalog_root))
    if payload["schema"] == CATALOG_SCHEMA_V2:
        return _load_semantic_visual_catalog_v2(catalog_root, payload)
    return _load_semantic_visual_catalog_v3(catalog_root, payload)


def _alias_is_excluded_compound(
    script: str, *, start: int, end: int, alias: str
) -> bool:
    for compound in VISUAL_ALIAS_EXCLUDED_COMPOUNDS.get(alias, ()):
        first = max(0, start - len(compound) + len(alias))
        for compound_start in range(first, start + 1):
            compound_end = compound_start + len(compound)
            if (
                compound_start <= start
                and end <= compound_end
                and script[compound_start:compound_end] == compound
            ):
                return True
    return False


def visual_candidate_context(
    script: str, *, start: int, end: int, maximum_length: int = 60
) -> str:
    left = start
    while left > 0 and script[left - 1] not in VISUAL_PHRASE_BOUNDARIES:
        left -= 1
    right = end
    while right < len(script) and script[right] not in VISUAL_PHRASE_BOUNDARIES:
        right += 1
    context = script[left:right].strip()
    if len(context) <= maximum_length:
        return context
    padding = max(0, (maximum_length - (end - start)) // 2)
    return script[max(left, start - padding) : min(right, end + padding)].strip()


def visual_phrase_char_range(script: str, *, start: int, end: int) -> tuple[int, int]:
    """Expand a matched keyword to its punctuation-delimited spoken phrase."""

    left = start
    while left > 0 and script[left - 1] not in VISUAL_PHRASE_BOUNDARIES:
        left -= 1
    right = end
    while right < len(script) and script[right] not in VISUAL_PHRASE_BOUNDARIES:
        right += 1
    while left < right and script[left].isspace():
        left += 1
    while right > left and script[right - 1].isspace():
        right -= 1
    return left, right


def _broll_concept_ids(
    catalog: SemanticVisualCatalog, *, usage: str
) -> set[str]:
    """Return concepts backed by an automatic asset for one B-roll usage."""

    concept_ids: set[str] = set()
    required_mode = "seam_broll" if usage == "seam_broll" else "full_screen_broll"
    for asset in catalog.assets:
        if not asset.get("auto_eligible", True) or asset.get("media_type") != "video":
            continue
        if catalog.schema == CATALOG_SCHEMA_V3:
            if required_mode not in set(asset.get("usage_modes", ())):
                continue
        elif not _is_enrichment_asset(asset):
            continue
        concept_ids.update(str(value) for value in asset.get("concept_ids", ()))
        concept_ids.update(
            str(value)
            for value in asset.get("video_taxonomy", {}).get(
                "fallback_concept_ids", ()
            )
        )
    return concept_ids


def _contextual_broll_concepts(
    matches: Iterable[tuple[int, int, str, list[dict[str, Any]]]],
    *,
    start: int,
    end: int,
    eligible_concept_ids: set[str],
) -> list[dict[str, Any]]:
    """Keep only locally recalled concepts that genuinely occur in the context."""

    allowed: dict[str, dict[str, Any]] = {}
    for match_start, match_end, _alias, match_allowed in matches:
        if match_start >= end or match_end <= start:
            continue
        for concept in match_allowed:
            concept_id = str(concept.get("concept_id") or "")
            if concept_id in eligible_concept_ids:
                allowed.setdefault(concept_id, dict(concept))
    return [
        allowed[concept_id]
        for concept_id in sorted(allowed)[:VISUAL_MAX_CONCEPTS_PER_ANCHOR]
    ]


def editorial_broll_pool_ids(article_type: str | None) -> tuple[str, ...]:
    """Return the editorial B-roll pools allowed for one script category."""

    normalized = str(article_type or "").strip()
    if "带人设" in normalized and "干货" in normalized:
        return _EDITORIAL_BROLL_POOLS_BY_ARTICLE_TYPE["带人设介绍的干货类"]
    if "鸡汤" in normalized:
        return _EDITORIAL_BROLL_POOLS_BY_ARTICLE_TYPE["鸡汤文"]
    if "干货" in normalized:
        return _EDITORIAL_BROLL_POOLS_BY_ARTICLE_TYPE["干货类"]
    return EDITORIAL_BROLL_POOL_IDS


def _editorial_broll_concepts(
    catalog: SemanticVisualCatalog,
    *,
    usage: str,
    article_type: str | None,
) -> list[dict[str, Any]]:
    # Editorial atmosphere pools are reserved for short digital-human seams.
    # Ordinary sentence enrichment must be backed by a directly recalled
    # object, action, or scene instead of a generic mood/lifestyle pool.
    if usage != "seam_broll":
        return []
    backed = _broll_concept_ids(catalog, usage=usage)
    allowed_ids = set(editorial_broll_pool_ids(article_type)) & backed
    return [
        {
            "concept_id": str(concept["concept_id"]),
            "description": str(concept["description"]),
        }
        for concept in catalog.concepts
        if str(concept.get("concept_id") or "") in allowed_ids
    ]


def _merge_broll_concepts(
    contextual: Iterable[Mapping[str, Any]],
    editorial: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for concept in (*tuple(contextual), *tuple(editorial)):
        concept_id = str(concept.get("concept_id") or "")
        if concept_id:
            merged.setdefault(concept_id, dict(concept))
    return list(merged.values())[:VISUAL_MAX_CONCEPTS_PER_ANCHOR]


def _phrase_range_near_char(script: str, target: int) -> tuple[int, int] | None:
    if not script:
        return None
    target = min(len(script) - 1, max(0, target))
    probes = [target]
    for offset in range(1, len(script)):
        if target + offset < len(script):
            probes.append(target + offset)
        if target - offset >= 0:
            probes.append(target - offset)
    for probe in probes:
        if script[probe].isspace() or script[probe] in VISUAL_PHRASE_BOUNDARIES:
            continue
        return visual_phrase_char_range(script, start=probe, end=probe + 1)
    return None


def _available_anchor_start(
    script: str, *, start: int, end: int, occupied_starts: set[int]
) -> int | None:
    for index in range(max(0, start), min(len(script), end)):
        if (
            index not in occupied_starts
            and not script[index].isspace()
            and script[index] not in VISUAL_PHRASE_BOUNDARIES
        ):
            return index
    return None


def _append_broll_candidate(
    candidates: list[dict[str, Any]],
    *,
    original_script: str,
    script_sha256: str,
    prefix: str,
    usage: str,
    phrase_start: int,
    phrase_end: int,
    allowed_concepts: list[dict[str, Any]],
    occupied_starts: set[int],
    metadata: Mapping[str, Any],
) -> bool:
    anchor_start = _available_anchor_start(
        original_script,
        start=phrase_start,
        end=phrase_end,
        occupied_starts=occupied_starts,
    )
    if anchor_start is None or not allowed_concepts:
        return False
    identity = json.dumps(
        [
            script_sha256,
            usage,
            anchor_start,
            phrase_end,
            [item["concept_id"] for item in allowed_concepts],
            dict(metadata),
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    candidates.append(
        {
            "candidate_id": prefix
            + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
            "text": original_script[anchor_start:phrase_end],
            "char_start": anchor_start,
            "char_end": phrase_end,
            "allowed_concepts": allowed_concepts,
            "usage": usage,
            **dict(metadata),
        }
    )
    occupied_starts.add(anchor_start)
    return True


def recall_semantic_visual_candidates(
    original_script: str,
    catalog: SemanticVisualCatalog,
    *,
    video_duration_us: int | None = None,
    segment_boundaries: Iterable[Mapping[str, Any]] = (),
    article_type: str | None = None,
) -> dict[str, Any]:
    """Recall explicit anchors plus relevant periodic and seam B-roll attempts."""

    segment_boundaries = tuple(segment_boundaries)
    eligible_concept_ids = {
        concept_id
        for asset in catalog.assets
        if asset.get("auto_eligible", True)
        for concept_id in asset.get("concept_ids", ())
    }
    eligible_concept_ids.update(
        concept_id
        for asset in catalog.assets
        if asset.get("auto_eligible", True) and asset.get("media_type") == "video"
        for concept_id in asset.get("video_taxonomy", {}).get(
            "fallback_concept_ids", ()
        )
    )
    alias_concepts: dict[str, list[dict[str, Any]]] = {}
    for concept in catalog.concepts:
        if concept["concept_id"] not in eligible_concept_ids:
            continue
        compact = {
            "concept_id": concept["concept_id"],
            "description": concept["description"],
        }
        for alias in concept["aliases"]:
            alias_concepts.setdefault(alias, []).append(compact)

    matches: list[tuple[int, int, str, list[dict[str, Any]]]] = []
    for alias, allowed in alias_concepts.items():
        cursor = 0
        while True:
            start = original_script.find(alias, cursor)
            if start < 0:
                break
            end = start + len(alias)
            if not _alias_is_excluded_compound(
                original_script, start=start, end=end, alias=alias
            ):
                matches.append((start, end, alias, allowed))
            cursor = start + max(1, len(alias))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))

    selected: list[tuple[int, int, str, list[dict[str, Any]]]] = []
    for match in matches:
        start, end, _alias, _allowed = match
        if any(start < kept_end and end > kept_start for kept_start, kept_end, *_ in selected):
            continue
        selected.append(match)
    selected.sort(key=lambda item: (item[0], item[1]))

    script_sha256 = hashlib.sha256(original_script.encode("utf-8")).hexdigest()
    candidates: list[dict[str, Any]] = []
    for start, end, text, allowed in selected:
        allowed_sorted = sorted(allowed, key=lambda item: item["concept_id"])
        identity = json.dumps(
            [script_sha256, start, end, text, [x["concept_id"] for x in allowed_sorted]],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        candidates.append(
            {
                "candidate_id": "vc_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                "text": text,
                "char_start": start,
                "char_end": end,
                "allowed_concepts": allowed_sorted,
            }
        )

    occupied_starts = {int(item["char_start"]) for item in candidates}
    seam_reserved_starts: set[int] = set()
    for boundary in segment_boundaries:
        if not isinstance(boundary, Mapping):
            continue
        segment_script = str(boundary.get("script_text") or "").strip()
        if not segment_script:
            continue
        cursor = 0
        while True:
            found = original_script.find(segment_script, cursor)
            if found < 0:
                break
            if found not in occupied_starts:
                seam_reserved_starts.add(found)
            cursor = found + 1
    occupied_starts.update(seam_reserved_starts)
    periodic_concept_ids = _broll_concept_ids(catalog, usage="enrichment")
    periodic_editorial = _editorial_broll_concepts(
        catalog, usage="enrichment", article_type=article_type
    )
    interval_us = VISUAL_BROLL_TARGET_INTERVAL_SECONDS * 1_000_000
    planning_duration_us = (
        video_duration_us
        if isinstance(video_duration_us, int) and video_duration_us > 0
        else round(
            len(original_script)
            / _VISUAL_ESTIMATED_SPEECH_CHARS_PER_SECOND
            * 1_000_000
        )
    )
    if (
        original_script
        and planning_duration_us > interval_us
        and periodic_concept_ids
    ):
        periodic_phrase_ranges: set[tuple[int, int]] = set()
        target_window_chars = max(
            20,
            round(
                len(original_script)
                * interval_us
                / planning_duration_us
                / 2
            ),
        )
        for target_us in range(interval_us, planning_duration_us, interval_us):
            target_char = min(
                len(original_script) - 1,
                max(0, round(len(original_script) * target_us / planning_duration_us)),
            )
            nearby_matches = [
                match
                for match in matches
                if abs(((match[0] + match[1]) // 2) - target_char)
                <= target_window_chars
                and any(
                    str(concept.get("concept_id") or "") in periodic_concept_ids
                    for concept in match[3]
                )
            ]
            nearest = (
                min(
                    nearby_matches,
                    key=lambda match: (
                        abs(((match[0] + match[1]) // 2) - target_char),
                        match[0],
                    ),
                )
                if nearby_matches
                else None
            )
            phrase_range = (
                visual_phrase_char_range(
                    original_script, start=nearest[0], end=nearest[1]
                )
                if nearest is not None
                else _phrase_range_near_char(original_script, target_char)
            )
            if phrase_range is None:
                continue
            phrase_start, phrase_end = phrase_range
            if (phrase_start, phrase_end) in periodic_phrase_ranges:
                continue
            contextual = _contextual_broll_concepts(
                matches,
                start=phrase_start,
                end=phrase_end,
                eligible_concept_ids=periodic_concept_ids,
            )
            allowed = _merge_broll_concepts(contextual, periodic_editorial)
            if _append_broll_candidate(
                candidates,
                original_script=original_script,
                script_sha256=script_sha256,
                prefix="ve_",
                usage="enrichment",
                phrase_start=phrase_start,
                phrase_end=phrase_end,
                allowed_concepts=allowed,
                occupied_starts=occupied_starts,
                metadata={
                    "target_start_us": target_us,
                    "direct_concept_ids": [
                        str(item["concept_id"]) for item in contextual
                    ],
                },
            ):
                periodic_phrase_ranges.add((phrase_start, phrase_end))

    occupied_starts.difference_update(seam_reserved_starts)
    # Segment seams have a dedicated reviewed pool. Ordinary enrichment clips
    # must not become seam candidates unless they also carry ``seam_broll``.
    seam_concept_ids = _broll_concept_ids(catalog, usage="seam_broll")
    seam_editorial = _editorial_broll_concepts(
        catalog, usage="seam_broll", article_type=article_type
    )
    expected_denominator = max(1, planning_duration_us)
    for boundary in sorted(
        (item for item in segment_boundaries if isinstance(item, Mapping)),
        key=lambda item: int(item.get("boundary_us") or 0),
    ):
        boundary_us = int(boundary.get("boundary_us") or 0)
        segment_script = str(boundary.get("script_text") or "").strip()
        if boundary_us <= 0 or not segment_script or not seam_concept_ids:
            continue
        occurrences: list[int] = []
        cursor = 0
        while True:
            found = original_script.find(segment_script, cursor)
            if found < 0:
                break
            occurrences.append(found)
            cursor = found + 1
        if not occurrences:
            continue
        expected_char = round(len(original_script) * boundary_us / expected_denominator)
        segment_start = min(occurrences, key=lambda value: abs(value - expected_char))
        next_probe = segment_start
        while (
            next_probe < len(original_script)
            and (
                original_script[next_probe].isspace()
                or original_script[next_probe] in VISUAL_PHRASE_BOUNDARIES
            )
        ):
            next_probe += 1
        previous_probe = segment_start - 1
        while (
            previous_probe >= 0
            and (
                original_script[previous_probe].isspace()
                or original_script[previous_probe] in VISUAL_PHRASE_BOUNDARIES
            )
        ):
            previous_probe -= 1
        next_phrase_range = (
            visual_phrase_char_range(
                original_script, start=next_probe, end=next_probe + 1
            )
            if next_probe < len(original_script)
            else None
        )
        previous_phrase_range = (
            visual_phrase_char_range(
                original_script, start=previous_probe, end=previous_probe + 1
            )
            if previous_probe >= 0
            else None
        )
        if previous_phrase_range is not None and next_phrase_range is not None:
            phrase_range = (previous_phrase_range[0], next_phrase_range[1])
        else:
            phrase_range = next_phrase_range
        if phrase_range is None:
            continue
        phrase_start, phrase_end = phrase_range
        contextual = _contextual_broll_concepts(
            matches,
            start=phrase_start,
            end=phrase_end,
            eligible_concept_ids=seam_concept_ids,
        )
        allowed = _merge_broll_concepts(contextual, seam_editorial)
        _append_broll_candidate(
            candidates,
            original_script=original_script,
            script_sha256=script_sha256,
            prefix="vs_",
            usage="seam_broll",
            phrase_start=phrase_start,
            phrase_end=phrase_end,
            allowed_concepts=allowed,
            occupied_starts=occupied_starts,
            metadata={
                "segment_boundary_us": boundary_us,
                "direct_concept_ids": [
                    str(item["concept_id"]) for item in contextual
                ],
            },
        )
    candidates.sort(key=lambda item: (int(item["char_start"]), int(item["char_end"])))
    return {
        "schema_version": "jyd.visual-analysis.request.v1",
        "original_script": original_script,
        "script_sha256": script_sha256,
        "catalog_version": catalog.catalog_version,
        "candidates": candidates,
    }


def _character_time_ranges(
    original_script: str, raw_cues: Iterable[object]
) -> list[tuple[int, int]]:
    # Reuse the thoroughly tested MiniMax alignment implementation. One-character
    # units expose its private range calculation without inventing a second mapper.
    from .semantic_subtitles import map_subtitle_units_to_raw_cues

    units = [
        {
            "start": index,
            "end": index + 1,
            "text": character,
            "kind": "whitespace" if character.isspace() else "phrase",
            "bind": "none",
            "break_after": "allow",
        }
        for index, character in enumerate(original_script)
    ]
    timed = map_subtitle_units_to_raw_cues(original_script, units, raw_cues)
    return [(int(item["start_us"]), int(item["end_us"])) for item in timed]


def _asr_candidate_time_range(
    original_script: str,
    raw_cues: Iterable[object],
    asr_alignment: Mapping[str, Any] | None,
    *,
    start: int,
    end: int,
) -> tuple[int, int] | None:
    if not isinstance(asr_alignment, Mapping) or asr_alignment.get("status") != "SUCCESS":
        return None
    raw_ranges = asr_alignment.get("ranges")
    if not isinstance(raw_ranges, list):
        return None
    try:
        _cues, tokens = script_tokens(original_script, raw_cues)
        aligned = {
            int(item["token_index"]): item
            for item in raw_ranges
            if isinstance(item, Mapping) and "token_index" in item
        }
        overlapping = [
            token for token in tokens if token.start < end and token.end > start
        ]
        if not overlapping or any(token.index not in aligned for token in overlapping):
            return None
        first = aligned[overlapping[0].index]
        last = aligned[overlapping[-1].index]
        start_us = int(first["start_us"])
        end_us = int(last["end_us"])
        if end_us <= start_us:
            return None
        return start_us, end_us
    except (CaptionAlignmentError, KeyError, TypeError, ValueError):
        return None


def map_visual_candidates_to_raw_cues(
    original_script: str,
    candidates: Iterable[Mapping[str, Any]],
    raw_cues: Iterable[object],
    *,
    video_duration_us: int | None = None,
    asr_alignment: Mapping[str, Any] | None = None,
    cover_offset_us: int = 0,
) -> list[dict[str, Any]]:
    ranges = _character_time_ranges(original_script, raw_cues)
    mapped: list[dict[str, Any]] = []
    for candidate in candidates:
        start = int(candidate["char_start"])
        end = int(candidate["char_end"])
        if start < 0 or end <= start or end > len(ranges):
            raise SemanticSubtitleMappingError(
                "VISUAL_CANDIDATE_RANGE_INVALID", "语义图片候选字符范围无效"
            )
        phrase_start, phrase_end = visual_phrase_char_range(
            original_script, start=start, end=end
        )
        keyword_precise_range = _asr_candidate_time_range(
            original_script,
            raw_cues,
            asr_alignment,
            start=start,
            end=end,
        )
        phrase_precise_range = _asr_candidate_time_range(
            original_script,
            raw_cues,
            asr_alignment,
            start=phrase_start,
            end=phrase_end,
        )
        phrase_start_us = (
            phrase_precise_range[0]
            if phrase_precise_range is not None
            else ranges[phrase_start][0]
        )
        phrase_end_us = (
            phrase_precise_range[1]
            if phrase_precise_range is not None
            else ranges[phrase_end - 1][1]
        )
        offset_us = max(0, cover_offset_us)
        start_us = max(0, phrase_start_us) + offset_us
        phrase_end_us += offset_us
        matched_end_us = (
            keyword_precise_range[1]
            if keyword_precise_range is not None
            else ranges[end - 1][1]
        ) + offset_us
        matched_start_us = (
            keyword_precise_range[0]
            if keyword_precise_range is not None
            else ranges[start][0]
        ) + offset_us
        duration_us = max(0, phrase_end_us - start_us)
        if video_duration_us is not None:
            duration_us = min(duration_us, max(0, int(video_duration_us) - start_us))
        if duration_us <= 0:
            raise SemanticSubtitleMappingError(
                "VISUAL_TIME_OUT_OF_RANGE", "语义图片映射时间超出视频范围"
            )
        mapped.append(
            {
                **dict(candidate),
                "start_us": start_us,
                "duration_us": duration_us,
                "matched_end_us": matched_end_us,
                "matched_start_us": matched_start_us,
                "keyword_start_us": matched_start_us,
                "keyword_end_us": matched_end_us,
                "video_duration_us": video_duration_us,
                "phrase_char_start": phrase_start,
                "phrase_char_end": phrase_end,
                "phrase_text": original_script[phrase_start:phrase_end],
                "timing_source": (
                    "funasr_phrase_timestamps"
                    if phrase_precise_range is not None
                    else "minimax_raw_cue_phrase_span"
                ),
            }
        )
    return mapped


def _assets_for_media_policy(
    catalog: SemanticVisualCatalog,
    concept_id: str,
    media_policy: str,
    *,
    usage: str = "explicit",
) -> list[dict[str, Any]]:
    if media_policy not in MEDIA_POLICIES:
        raise ValueError("未知的语义视觉媒体策略")
    available = [
        item
        for item in catalog.assets
        if item.get("auto_eligible", True) and concept_id in item["concept_ids"]
    ]
    if catalog.schema == CATALOG_SCHEMA_V3:
        allowed_modes = {
            "explicit": {"semantic_overlay", "action_demo", "knowledge_card"},
            "rapid_list": {"list_quick_cut"},
            "enrichment": {"full_screen_broll"},
            "seam_broll": {"seam_broll"},
        }.get(usage, set())
        available = [
            item
            for item in available
            if set(item.get("usage_modes", ())) & allowed_modes
        ]
        if (
            usage in {"explicit", "enrichment", "seam_broll"}
            and media_policy != "image_only"
            and (usage != "explicit" or not available)
        ):
            fallback_modes = (
                {"seam_broll"}
                if usage == "seam_broll"
                else {"full_screen_broll"}
            )
            fallback = [
                item
                for item in catalog.assets
                if item.get("auto_eligible", True)
                and item.get("media_type") == "video"
                and concept_id
                in item.get("video_taxonomy", {}).get("fallback_concept_ids", ())
                and set(item.get("usage_modes", ())) & fallback_modes
            ]
            available.extend(
                item
                for item in fallback
                if str(item.get("asset_id") or "")
                not in {str(value.get("asset_id") or "") for value in available}
            )
    elif usage == "seam_broll":
        available = [
            item
            for item in available
            if item["media_type"] == "video" and _is_enrichment_asset(item)
        ]
    elif usage == "enrichment":
        available = [item for item in available if _is_enrichment_asset(item)]
    else:
        enrichment_assets = [item for item in available if _is_enrichment_asset(item)]
        explicit_assets = [item for item in available if item not in enrichment_assets]
        available = explicit_assets or available
    images = [item for item in available if item["media_type"] == "image"]
    videos = [item for item in available if item["media_type"] == "video"]
    if usage == "seam_broll":
        return [item for item in available if item["media_type"] == "video"]
    if media_policy == "image_only":
        return images
    if media_policy == "video_only":
        return videos
    if media_policy == "prefer_image":
        return images or videos
    if media_policy == "prefer_video":
        return videos or images
    # Motion concepts benefit from actual movement when both media types are
    # available. Static objects remain image-first. Either branch falls back
    # locally without adding fields to the model response.
    return videos + images if concept_id.startswith("activity.") else images + videos


def visual_overlay_conflicts(
    candidate: Mapping[str, Any], selected: Iterable[Mapping[str, Any]]
) -> bool:
    """Apply the shared automatic visual-occupancy and density rules."""

    start_us = int(candidate.get("start_us") or 0)
    duration_us = int(candidate.get("duration_us") or 0)
    concept_id = str(candidate.get("concept_id") or "")
    asset_id = str(candidate.get("asset_id") or "")
    display_role = _visual_display_role(candidate)
    active = [item for item in selected if item.get("enabled") is not False]
    if any(
        start_us < int(item.get("start_us") or 0) + int(item.get("duration_us") or 0)
        and int(item.get("start_us") or 0) < start_us + duration_us
        for item in active
    ):
        return True
    if (
        sum(
            abs(start_us - int(item.get("start_us") or 0)) < 60_000_000
            for item in active
        )
        >= VISUAL_MAX_AUTO_PER_MINUTE
    ):
        return True
    if concept_id and any(
        str(item.get("concept_id") or "") == concept_id
        and _visual_display_role(item) == display_role
        and abs(start_us - int(item.get("start_us") or 0)) < 20_000_000
        for item in active
    ):
        return True
    return bool(
        asset_id
        and any(str(item.get("asset_id") or "") == asset_id for item in active)
    )


def _visual_display_role(item: Mapping[str, Any]) -> str:
    explicit = str(item.get("display_role") or "").strip()
    if explicit:
        return explicit
    usage = str(item.get("usage") or "").strip()
    timing_mode = str(item.get("timing_mode") or "").strip()
    if usage in {"enrichment", "seam_broll"} or timing_mode == "seam_broll":
        return "full_screen_broll"
    return "semantic_overlay"


def validate_visual_occupancy(overlays: Iterable[Mapping[str, Any]]) -> None:
    """Reject a frozen recipe that would render two semantic visuals at once."""

    enabled = sorted(
        (item for item in overlays if item.get("enabled") is not False),
        key=lambda item: int(item.get("start_us") or 0),
    )
    for previous, current in zip(enabled, enabled[1:]):
        if int(current.get("start_us") or 0) < int(previous.get("start_us") or 0) + int(
            previous.get("duration_us") or 0
        ):
            raise ValueError("同一时间只能显示一个语义视觉素材")


def _asset_runtime_available(asset: Mapping[str, Any]) -> bool:
    path = Path(str(asset.get("resource_path") or ""))
    if asset.get("media_type") == "image":
        return path.is_dir() and (path / "sticker.json").is_file()
    return path.is_file()


def _sentence_key(candidate: Mapping[str, Any]) -> tuple[int, int]:
    return (
        int(candidate.get("phrase_char_start", candidate.get("char_start", 0)) or 0),
        int(candidate.get("phrase_char_end", candidate.get("char_end", 0)) or 0),
    )


def _entry_priority(entry: Mapping[str, Any]) -> tuple[float, float, int]:
    decision = entry["decision"]
    candidate = entry["candidate"]
    return (
        -float(decision.get("importance", 0.0) or 0.0),
        -float(decision.get("confidence", 0.0) or 0.0),
        int(candidate.get("char_start", 0) or 0),
    )


def _is_rapid_list(entries: list[dict[str, Any]]) -> bool:
    if len(entries) < 2:
        return False
    ordered = sorted(entries, key=lambda item: int(item["candidate"].get("char_start", 0)))
    first = ordered[0]["candidate"]
    phrase_start = int(first.get("phrase_char_start", first.get("char_start", 0)) or 0)
    phrase_text = str(first.get("phrase_text") or "")
    if not phrase_text:
        return False
    for previous, current in zip(ordered, ordered[1:]):
        previous_end = int(previous["candidate"].get("char_end", 0)) - phrase_start
        current_start = int(current["candidate"].get("char_start", 0)) - phrase_start
        if "、" not in phrase_text[max(0, previous_end) : max(0, current_start)]:
            return False
    return True


def _candidate_occurrence(
    mapped: Mapping[str, Mapping[str, Any]], candidate: Mapping[str, Any], concept_id: str
) -> int:
    return sum(
        1
        for item in mapped.values()
        if int(item.get("start_us", 0)) < int(candidate.get("start_us", 0))
        and any(
            concept.get("concept_id") == concept_id
            for concept in item.get("allowed_concepts", [])
            if isinstance(concept, Mapping)
        )
    )


def _choose_unused_asset(
    *,
    catalog: SemanticVisualCatalog,
    mapped: Mapping[str, Mapping[str, Any]],
    candidate: Mapping[str, Any],
    concept_id: str,
    media_policy: str,
    usage: str,
    used_asset_ids: set[str],
    match_tier: str = "any",
) -> dict[str, Any] | None:
    assets = _assets_for_media_policy(catalog, concept_id, media_policy, usage=usage)
    if not assets:
        return None
    occurrence = _candidate_occurrence(mapped, candidate, concept_id)
    exact_assets = [asset for asset in assets if concept_id in asset.get("concept_ids", ())]
    fallback_assets = [asset for asset in assets if asset not in exact_assets]
    pools = {
        "any": (exact_assets, fallback_assets),
        "exact": (exact_assets,),
        "fallback": (fallback_assets,),
    }.get(match_tier)
    if pools is None:
        raise ValueError("未知的语义视觉匹配层级")
    for pool in pools:
        if not pool:
            continue
        offset = occurrence % len(pool)
        ordered = [*pool[offset:], *pool[:offset]]
        chosen = next(
            (
                asset
                for asset in ordered
                if str(asset["asset_id"]) not in used_asset_ids
                and _asset_runtime_available(asset)
            ),
            None,
        )
        if chosen is not None:
            return chosen
    return None


def _choose_ranked_broll_asset(
    *,
    catalog: SemanticVisualCatalog,
    mapped: Mapping[str, Mapping[str, Any]],
    candidate: Mapping[str, Any],
    decision: Mapping[str, Any],
    usage: str,
    used_asset_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Resolve only the model-selected concept to an exact automatic asset."""

    allowed_ids = [
        str(item.get("concept_id") or "")
        for item in candidate.get("allowed_concepts", ())
        if isinstance(item, Mapping) and str(item.get("concept_id") or "")
    ]
    selected_id = str(decision.get("concept_id") or "")
    direct_ids = {
        str(value)
        for value in candidate.get("direct_concept_ids", ())
        if str(value) in allowed_ids and not str(value).startswith("editorial.")
    }
    if selected_id not in direct_ids:
        return None
    asset = _choose_unused_asset(
        catalog=catalog,
        mapped=mapped,
        candidate=candidate,
        concept_id=selected_id,
        media_policy="video_only",
        usage=usage,
        used_asset_ids=used_asset_ids,
        match_tier="exact",
    )
    if asset is None:
        return None
    return dict(decision), asset


def _target_sentence_range(
    candidate: Mapping[str, Any],
    *,
    final_video_duration_us: int | None,
    start_override_us: int | None = None,
    minimum_duration_us: int = 0,
) -> tuple[int, int]:
    start_us = int(
        candidate.get("start_us", 0) if start_override_us is None else start_override_us
    )
    sentence_end_us = int(candidate.get("start_us", 0)) + int(
        candidate.get("duration_us", 0)
    )
    end_us = max(sentence_end_us, start_us + max(0, minimum_duration_us))
    if final_video_duration_us is not None:
        end_us = min(end_us, int(final_video_duration_us))
    return start_us, max(start_us, end_us)


def _overlay_from_asset(
    *,
    candidate: Mapping[str, Any],
    decision: Mapping[str, Any],
    asset: Mapping[str, Any],
    start_us: int,
    end_us: int,
    timing_mode: str,
    usage: str,
    overlay_id: str | None = None,
    list_index: int | None = None,
    list_size: int | None = None,
    segment_boundary_us: int | None = None,
) -> dict[str, Any]:
    defaults = asset["defaults"]
    resource = asset["resource"]
    media_type = str(asset["media_type"])
    sentence_start, sentence_end = _sentence_key(candidate)
    overlay = {
        "overlay_id": overlay_id
        or "vo_" + str(candidate["candidate_id"]).removeprefix("vc_"),
        "candidate_id": candidate["candidate_id"],
        "concept_id": str(decision.get("concept_id") or ""),
        "asset_id": asset["asset_id"],
        "asset_name": asset["name"],
        "rights_status": str(asset.get("rights_status") or ""),
        "attribution_text": (
            VISUAL_NETWORK_ATTRIBUTION_TEXT
            if asset.get("rights_status") == "attributed"
            else ""
        ),
        "preview_url": asset["preview_url"],
        "media_type": media_type,
        "renderer": asset["renderer"],
        "resource_path": resource["bundle"] if media_type == "image" else resource["video"],
        "enabled": True,
        "selection_mode": "auto",
        "manual": False,
        "locked": False,
        "corner": defaults["corner"],
        "scale": defaults["scale"],
        "opacity": defaults["opacity"],
        "start_us": int(start_us),
        "duration_us": int(end_us - start_us),
        "confidence": float(decision.get("confidence", 0.0) or 0.0),
        "importance": float(decision.get("importance", 0.0) or 0.0),
        "usage": usage,
        "display_role": (
            "full_screen_broll"
            if usage in {"enrichment", "seam_broll"}
            else "semantic_overlay"
        ),
        "reason_code": decision.get("reason_code"),
        "timing_source": str(
            candidate.get("timing_source") or "minimax_raw_cue_phrase_span"
        ),
        "timing_mode": timing_mode,
        "sentence_text": str(candidate.get("phrase_text") or candidate.get("text") or ""),
        "sentence_char_start": sentence_start,
        "sentence_char_end": sentence_end,
        "phrase_text": str(candidate.get("phrase_text") or candidate.get("text") or ""),
        "phrase_char_start": sentence_start,
        "phrase_char_end": sentence_end,
        "list_index": list_index,
        "list_size": list_size,
        "segment_boundary_us": segment_boundary_us,
    }
    if media_type == "video":
        source_start_us = int(defaults["source_start_us"])
        available_us = max(0, int(resource.get("duration_us") or 0) - source_start_us)
        source_duration_us = min(int(defaults["duration_us"]), available_us)
        overlay["duration_us"] = min(int(overlay["duration_us"]), source_duration_us)
        overlay.update(
            {
                "source_start_us": source_start_us,
                "source_duration_us": source_duration_us,
                "loop_to_target": VISUAL_VIDEO_LOOP_TO_TARGET,
                "mute": defaults["mute"],
                "fit": defaults["fit"],
            }
        )
    return overlay


def _rapid_ranges(
    entries: list[dict[str, Any]], start_us: int, end_us: int
) -> list[tuple[int, int]]:
    if not entries or end_us <= start_us:
        return []
    if end_us - start_us < len(entries):
        return [
            (
                start_us + (end_us - start_us) * index // len(entries),
                start_us + (end_us - start_us) * (index + 1) // len(entries),
            )
            for index in range(len(entries))
        ]
    centers = [
        (
            int(entry["candidate"].get("keyword_start_us", entry["candidate"].get("start_us", 0)))
            + int(entry["candidate"].get("keyword_end_us", entry["candidate"].get("matched_end_us", 0)))
        )
        // 2
        for entry in entries
    ]
    cuts = [start_us]
    for index, (left, right) in enumerate(zip(centers, centers[1:]), start=1):
        earliest = start_us + index
        latest = end_us - (len(entries) - index)
        cuts.append(max(earliest, cuts[-1] + 1, min(latest, (left + right) // 2)))
    cuts.append(end_us)
    return [(cuts[index], cuts[index + 1]) for index in range(len(entries))]


def _group_conflicts(
    overlays: list[dict[str, Any]], selected: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    staged: list[dict[str, Any]] = []
    for overlay in overlays:
        fitted = _fit_minor_overlap(overlay, [*selected, *staged])
        if fitted is None or visual_overlay_conflicts(fitted, [*selected, *staged]):
            return None
        staged.append(fitted)
    return staged


def _fit_minor_overlap(
    overlay: Mapping[str, Any], selected: Iterable[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Trim an edge overlap of at most 0.5s without moving frozen selections."""

    fitted = dict(overlay)
    start_us = int(fitted.get("start_us") or 0)
    end_us = start_us + int(fitted.get("duration_us") or 0)
    if end_us <= start_us:
        return None
    active = sorted(
        (item for item in selected if item.get("enabled") is not False),
        key=lambda item: int(item.get("start_us") or 0),
    )
    for item in active:
        item_start = int(item.get("start_us") or 0)
        item_end = item_start + int(item.get("duration_us") or 0)
        if start_us >= item_end or item_start >= end_us:
            continue
        if item_start <= start_us < item_end < end_us:
            overlap_us = item_end - start_us
            if overlap_us > VISUAL_MINOR_OVERLAP_TOLERANCE_US:
                return None
            start_us = item_end
            continue
        if start_us < item_start < end_us <= item_end:
            overlap_us = end_us - item_start
            if overlap_us > VISUAL_MINOR_OVERLAP_TOLERANCE_US:
                return None
            end_us = item_start
            continue
        return None
    if end_us <= start_us:
        return None
    fitted["start_us"] = start_us
    fitted["duration_us"] = end_us - start_us
    if fitted.get("media_type") == "video" and not fitted.get("loop_to_target"):
        fitted["source_duration_us"] = min(
            int(fitted.get("source_duration_us") or fitted["duration_us"]),
            fitted["duration_us"],
        )
    return fitted


def _fit_enrichment_around_seams(
    overlay: Mapping[str, Any], selected: Iterable[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Keep an ordinary B-roll attempt inside its sentence while seams win."""

    fitted = dict(overlay)
    start_us = int(fitted.get("start_us") or 0)
    end_us = start_us + int(fitted.get("duration_us") or 0)
    intervals = [(start_us, end_us)]
    for item in sorted(
        (
            value
            for value in selected
            if value.get("enabled") is not False
            and (
                str(value.get("usage") or "") == "seam_broll"
                or str(value.get("timing_mode") or "") == "seam_broll"
            )
        ),
        key=lambda value: int(value.get("start_us") or 0),
    ):
        seam_start = int(item.get("start_us") or 0)
        seam_end = seam_start + int(item.get("duration_us") or 0)
        next_intervals: list[tuple[int, int]] = []
        for interval_start, interval_end in intervals:
            if seam_start >= interval_end or seam_end <= interval_start:
                next_intervals.append((interval_start, interval_end))
                continue
            if seam_start > interval_start:
                next_intervals.append((interval_start, seam_start))
            if interval_end > seam_end:
                next_intervals.append((seam_end, interval_end))
        intervals = next_intervals
        if not intervals:
            return None
    chosen_start, chosen_end = min(
        intervals,
        key=lambda value: (
            abs(value[0] - start_us),
            -(value[1] - value[0]),
        ),
    )
    fitted["start_us"] = chosen_start
    fitted["duration_us"] = chosen_end - chosen_start
    if fitted.get("media_type") == "video" and not fitted.get("loop_to_target"):
        fitted["source_duration_us"] = min(
            int(fitted.get("source_duration_us") or fitted["duration_us"]),
            fitted["duration_us"],
        )
    return fitted


def build_visual_recipe(
    *,
    catalog: SemanticVisualCatalog,
    mapped_candidates: Iterable[Mapping[str, Any]],
    decisions: Iterable[Mapping[str, Any]],
    media_policy: str = "image_only",
    locked_overlays: Iterable[Mapping[str, Any]] = (),
    segment_boundaries: Iterable[Mapping[str, Any]] = (),
    final_video_duration_us: int | None = None,
) -> dict[str, Any]:
    """Build one priority-ordered, sentence-timed, video-level deduplicated recipe."""

    mapped = {str(item["candidate_id"]): dict(item) for item in mapped_candidates}
    if final_video_duration_us is None:
        durations = [
            int(item["video_duration_us"])
            for item in mapped.values()
            if isinstance(item.get("video_duration_us"), int)
            and int(item["video_duration_us"]) > 0
        ]
        final_video_duration_us = max(durations, default=None)
    locked = [dict(item) for item in locked_overlays]
    selected: list[dict[str, Any]] = [
        item for item in locked if item.get("enabled") is not False
    ]
    used_asset_ids = {
        str(item.get("asset_id") or "")
        for item in selected
        if str(item.get("asset_id") or "")
    }
    entries: list[dict[str, Any]] = []
    for decision in decisions:
        candidate = mapped.get(str(decision.get("candidate_id") or ""))
        confidence = float(decision.get("confidence", 0.0) or 0.0)
        if candidate is None or decision.get("decision") != "SHOW" or confidence < 0.85:
            continue
        usage = str(
            decision.get("usage")
            or candidate.get("usage")
            or (
                "enrichment"
                if str(candidate.get("candidate_id") or "").startswith("ve_")
                else "explicit"
            )
        )
        entries.append({"candidate": candidate, "decision": dict(decision), "usage": usage})

    explicit_groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    enrichment_entries: list[dict[str, Any]] = []
    seam_entries: list[dict[str, Any]] = []
    for entry in entries:
        if entry["usage"] == "enrichment":
            enrichment_entries.append(entry)
        elif entry["usage"] == "seam_broll":
            seam_entries.append(entry)
        else:
            explicit_groups.setdefault(_sentence_key(entry["candidate"]), []).append(entry)
    for group in explicit_groups.values():
        group.sort(key=lambda item: int(item["candidate"].get("char_start", 0)))

    automatic: list[dict[str, Any]] = []
    for raw_boundary in sorted(
        (item for item in segment_boundaries if isinstance(item, Mapping)),
        key=lambda item: int(item.get("boundary_us") or 0),
    ):
        boundary_us = int(raw_boundary.get("boundary_us") or 0)
        if boundary_us <= 0:
            continue
        matching_seam = [
            entry
            for entry in seam_entries
            if int(entry["candidate"].get("segment_boundary_us") or 0)
            == boundary_us
        ]
        if matching_seam:
            group = matching_seam
        else:
            # A seam without a model-approved strong match stays as the original
            # digital-human transition. Never invent an editorial fallback.
            continue
        # One short full-screen insert is enough to cover a digital-human seam.
        # Never turn a one-second seam into a rapid multi-asset list.
        rapid = False
        source_entries = [min(group, key=_entry_priority)]
        provisional_used = set(used_asset_ids)
        chosen: list[
            tuple[dict[str, Any], dict[str, Any], dict[str, Any]]
        ] = []
        for entry in source_entries:
            resolved = _choose_ranked_broll_asset(
                catalog=catalog,
                mapped=mapped,
                candidate=entry["candidate"],
                decision=entry["decision"],
                usage="seam_broll",
                used_asset_ids=provisional_used,
            )
            if resolved is None:
                continue
            resolved_decision, asset = resolved
            provisional_used.add(str(asset["asset_id"]))
            chosen.append((entry, resolved_decision, asset))
        if not chosen:
            continue
        half_duration_us = VISUAL_SEAM_BROLL_DURATION_US // 2
        start_us = max(0, boundary_us - half_duration_us)
        end_us = boundary_us + half_duration_us
        if isinstance(final_video_duration_us, int) and final_video_duration_us > 0:
            end_us = min(end_us, final_video_duration_us)
        if end_us <= start_us:
            continue
        ranges = (
            _rapid_ranges([entry for entry, _decision, _asset in chosen], start_us, end_us)
            if rapid and len(chosen) > 1
            else [(start_us, end_us)]
        )
        overlays = [
            _overlay_from_asset(
                candidate=entry["candidate"],
                decision=resolved_decision,
                asset=asset,
                start_us=interval[0],
                end_us=interval[1],
                timing_mode="seam_broll",
                usage="seam_broll",
                overlay_id=(
                    f"vo_seam_{boundary_us}_{entry['candidate']['candidate_id']}"
                ),
                list_index=(index if len(chosen) > 1 else None),
                list_size=(len(chosen) if len(chosen) > 1 else None),
                segment_boundary_us=boundary_us,
            )
            for index, ((entry, resolved_decision, asset), interval) in enumerate(
                zip(chosen, ranges)
            )
        ]
        fitted_overlays = _group_conflicts(overlays, selected)
        if fitted_overlays is None:
            continue
        automatic.extend(fitted_overlays)
        selected.extend(fitted_overlays)
        used_asset_ids.update(
            str(asset["asset_id"]) for _entry, _decision, asset in chosen
        )
    # Reserve full-screen B-roll before scheduling small semantic images/videos.
    # This prevents dense explicit overlays from consuming every eligible slot.
    for entry in sorted(enrichment_entries, key=_entry_priority):
        candidate = entry["candidate"]
        target_start_us = int(
            candidate.get("target_start_us", candidate.get("start_us", 0)) or 0
        )
        start_us, end_us = _target_sentence_range(
            candidate,
            final_video_duration_us=final_video_duration_us,
            start_override_us=max(
                int(candidate.get("start_us") or 0), target_start_us
            ),
        )
        previous_end_us = max(
            (
                int(item.get("start_us") or 0) + int(item.get("duration_us") or 0)
                for item in selected
                if int(item.get("start_us") or 0) < start_us
                and _visual_display_role(item) == "full_screen_broll"
                and str(item.get("usage") or "") != "seam_broll"
                and str(item.get("timing_mode") or "") != "seam_broll"
            ),
            default=0,
        )
        if start_us - previous_end_us < VISUAL_ENRICHMENT_MIN_GAP_US:
            continue
        resolved = _choose_ranked_broll_asset(
            catalog=catalog,
            mapped=mapped,
            candidate=candidate,
            decision=entry["decision"],
            usage="enrichment",
            used_asset_ids=used_asset_ids,
        )
        if resolved is None:
            continue
        resolved_decision, asset = resolved
        overlay = _overlay_from_asset(
            candidate=candidate,
            decision=resolved_decision,
            asset=asset,
            start_us=start_us,
            end_us=end_us,
            timing_mode="sentence",
            usage="enrichment",
        )
        overlay = _fit_enrichment_around_seams(overlay, selected)
        if overlay is None:
            continue
        fitted_overlay = _fit_minor_overlap(overlay, selected)
        if fitted_overlay is None or visual_overlay_conflicts(fitted_overlay, selected):
            continue
        automatic.append(fitted_overlay)
        selected.append(fitted_overlay)
        used_asset_ids.add(str(asset["asset_id"]))

    ordered_groups = sorted(
        explicit_groups.items(),
        key=lambda pair: (
            min(_entry_priority(entry) for entry in pair[1]),
            int(pair[1][0]["candidate"].get("start_us", 0)),
        ),
    )
    for _key, group in ordered_groups:
        rapid = _is_rapid_list(group)
        source_entries = group if rapid else [min(group, key=_entry_priority)]
        provisional_used = set(used_asset_ids)
        chosen: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for entry in source_entries:
            concept_id = str(entry["decision"].get("concept_id") or "")
            asset = _choose_unused_asset(
                catalog=catalog,
                mapped=mapped,
                candidate=entry["candidate"],
                concept_id=concept_id,
                media_policy=media_policy,
                usage="rapid_list" if rapid else "explicit",
                used_asset_ids=provisional_used,
            )
            if asset is None:
                continue
            provisional_used.add(str(asset["asset_id"]))
            chosen.append((entry, asset))
        if not chosen:
            continue
        first_candidate = chosen[0][0]["candidate"]
        rapid = rapid and len(chosen) > 1
        start_us, end_us = _target_sentence_range(
            first_candidate,
            final_video_duration_us=final_video_duration_us,
            minimum_duration_us=0,
        )
        ranges = (
            _rapid_ranges([entry for entry, _asset in chosen], start_us, end_us)
            if rapid
            else [(start_us, end_us)]
        )
        if not rapid:
            chosen = chosen[:1]
        overlays = [
            _overlay_from_asset(
                candidate=entry["candidate"],
                decision=entry["decision"],
                asset=asset,
                start_us=interval[0],
                end_us=interval[1],
                timing_mode="rapid_list" if rapid else "sentence",
                usage=entry["usage"],
                list_index=(index if rapid else None),
                list_size=(len(chosen) if rapid else None),
            )
            for index, ((entry, asset), interval) in enumerate(zip(chosen, ranges))
        ]
        fitted_overlays = _group_conflicts(overlays, selected)
        if fitted_overlays is None:
            continue
        automatic.extend(fitted_overlays)
        selected.extend(fitted_overlays)
        used_asset_ids.update(str(asset["asset_id"]) for _entry, asset in chosen)

    overlays = sorted(
        [*locked, *automatic],
        key=lambda item: (int(item.get("start_us") or 0), str(item.get("overlay_id") or "")),
    )
    return {
        "schema": RECIPE_SCHEMA,
        "library_id": catalog.library_id or DEFAULT_LIBRARY_ID,
        "catalog_version": catalog.catalog_version,
        "media_policy": media_policy,
        "timing_policy_version": VISUAL_TIMING_POLICY_VERSION,
        "used_asset_ids": sorted(used_asset_ids),
        "overlays": overlays,
    }


def frozen_visual_overlays(
    item: Mapping[str, Any],
    *,
    library_root: str | Path | None = None,
    catalog: SemanticVisualCatalog | None = None,
) -> list[dict[str, Any]]:
    """Return enabled overlays and resolve v2 paths only at render-job time."""

    analysis = item.get("visual_analysis")
    recipe = analysis.get("recipe") if isinstance(analysis, Mapping) else None
    if not isinstance(recipe, Mapping) or recipe.get("schema") not in RECIPE_SCHEMAS:
        return []
    schema = str(recipe["schema"])
    resolved_root = Path(library_root).expanduser().resolve() if library_root else None
    current_catalog = catalog
    if current_catalog is None and resolved_root is not None:
        try:
            current_catalog = load_semantic_visual_catalog(resolved_root)
        except SemanticVisualCatalogError:
            current_catalog = None
    result: list[dict[str, Any]] = []
    for raw in recipe.get("overlays", []):
        if (
            not isinstance(raw, Mapping)
            or raw.get("enabled") is False
            or raw.get("requires_review") is True
        ):
            continue
        overlay = dict(raw)
        # Product defaults may evolve while a project is still under review.
        # Refresh only untouched automatic selections; manual/locked recipes remain frozen.
        if (
            current_catalog is not None
            and overlay.get("selection_mode") == "auto"
            and overlay.get("manual") is not True
            and overlay.get("locked") is not True
        ):
            current_asset = current_catalog.asset(str(overlay.get("asset_id") or ""))
            if current_asset is not None:
                if (
                    current_catalog.schema == CATALOG_SCHEMA_V3
                    and current_asset.get("auto_eligible") is not True
                ):
                    continue
                defaults = current_asset["defaults"]
                resource = current_asset["resource"]
                media_type = current_asset["media_type"]
                overlay.update(
                    {
                        "asset_name": current_asset["name"],
                        "rights_status": str(current_asset.get("rights_status") or ""),
                        "attribution_text": (
                            VISUAL_NETWORK_ATTRIBUTION_TEXT
                            if current_asset.get("rights_status") == "attributed"
                            else ""
                        ),
                        "preview_url": current_asset["preview_url"],
                        "media_type": current_asset["media_type"],
                        "renderer": current_asset["renderer"],
                        "resource_path": (
                            resource["bundle"]
                            if current_asset["media_type"] == "image"
                            else resource["video"]
                        ),
                        "corner": defaults["corner"],
                        "scale": defaults["scale"],
                        "opacity": defaults["opacity"],
                    }
                )
                if media_type == "video":
                    source_start_us = int(defaults["source_start_us"])
                    available_us = max(
                        0, int(resource.get("duration_us") or 0) - source_start_us
                    )
                    source_duration_us = min(int(defaults["duration_us"]), available_us)
                    overlay.update(
                        {
                            "source_start_us": source_start_us,
                            "source_duration_us": source_duration_us,
                            "loop_to_target": VISUAL_VIDEO_LOOP_TO_TARGET,
                            "mute": defaults["mute"],
                            "fit": defaults["fit"],
                        }
                    )
                    overlay["duration_us"] = min(
                        int(overlay.get("duration_us") or 0), source_duration_us
                    )
        if schema == RECIPE_SCHEMA_V2 and resolved_root is not None:
            try:
                _relative, resource_path = _safe_relative_child(
                    resolved_root, overlay.get("resource_path"), kind="recipe resource"
                )
            except SemanticVisualCatalogError:
                # A semantic overlay is optional enhancement; one missing or unsafe
                # resource must not break the base video, voice, captions or BGM.
                continue
            renderer = str(overlay.get("renderer") or "")
            if renderer == "jyd_sticker_bundle":
                if not resource_path.is_dir() or not (resource_path / "sticker.json").is_file():
                    continue
                overlay["bundle_path"] = str(resource_path)
            elif renderer == "video_overlay":
                if not resource_path.is_file():
                    continue
                overlay["video_path"] = str(resource_path)
            else:
                continue
        if overlay.get("media_type") == "video":
            # Old manual/locked recipes may still contain loop_to_target=true.
            # Enforce the current one-shot policy at consumption time too.
            overlay["loop_to_target"] = VISUAL_VIDEO_LOOP_TO_TARGET
            source_duration_us = int(overlay.get("source_duration_us") or 0)
            if source_duration_us > 0:
                overlay["duration_us"] = min(
                    int(overlay.get("duration_us") or 0), source_duration_us
                )
        result.append(overlay)
    validate_visual_occupancy(result)
    return result
