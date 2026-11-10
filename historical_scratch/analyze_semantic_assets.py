from __future__ import annotations

import collections
import hashlib
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def phash64(image: Image.Image) -> int:
    rgba = image.convert("RGBA")
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    white.alpha_composite(rgba)
    gray = white.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float64)
    n = 32
    x = np.arange(n)
    u = np.arange(n)[:, None]
    matrix = np.cos((math.pi / n) * (x + 0.5) * u)
    matrix[0] *= math.sqrt(1 / n)
    matrix[1:] *= math.sqrt(2 / n)
    coeff = matrix @ pixels @ matrix.T
    low = coeff[:8, :8].copy()
    values = low.flatten()
    median = float(np.median(values[1:]))
    bits = values > median
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def alpha_status(image: Image.Image) -> tuple[bool, bool]:
    has_alpha = "A" in image.getbands() or "transparency" in image.info
    if not has_alpha:
        return False, False
    alpha = image.convert("RGBA").getchannel("A")
    lo, _hi = alpha.getextrema()
    return True, lo < 255


def informative_name(path: Path) -> bool:
    stem = path.stem.strip().lower()
    generic = re.compile(
        r"^(img|image|pic|photo|screenshot|screen|wx|mmexport|微信图片|截图|截屏|"
        r"视频|素材|贴图|未命名|[a-f0-9_-]{12,}|\d+)[-_ ]*\d*$",
        re.I,
    )
    if not stem or generic.match(stem):
        return False
    cleaned = re.sub(r"[\W_\d]+", "", stem, flags=re.UNICODE)
    return len(cleaned) >= 2


def union_find_groups(items: list[tuple[Path, str, int, int, int]]) -> list[list[int]]:
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(items)):
        _, sha_i, hash_i, w_i, h_i = items[i]
        ratio_i = w_i / h_i
        for j in range(i + 1, len(items)):
            _, sha_j, hash_j, w_j, h_j = items[j]
            if sha_i == sha_j:
                continue
            ratio_j = w_j / h_j
            if abs(ratio_i - ratio_j) / max(ratio_i, ratio_j) > 0.04:
                continue
            if (hash_i ^ hash_j).bit_count() <= 5:
                union(i, j)
    groups: dict[int, list[int]] = collections.defaultdict(list)
    for i in range(len(items)):
        groups[find(i)].append(i)
    return [indices for indices in groups.values() if len(indices) > 1]


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    files = [path for path in root.rglob("*") if path.is_file()]
    images = [path for path in files if path.suffix.lower() in IMAGE_SUFFIXES]
    ext_counts = collections.Counter(path.suffix.lower() for path in files)
    format_counts: collections.Counter[str] = collections.Counter()
    mode_counts: collections.Counter[str] = collections.Counter()
    dimension_counts: collections.Counter[str] = collections.Counter()
    top_counts: collections.Counter[str] = collections.Counter()
    alpha_channel_count = 0
    transparent_count = 0
    transparent_files: list[dict[str, object]] = []
    extension_format_mismatches: list[dict[str, str]] = []
    orientation_counts: collections.Counter[str] = collections.Counter()
    invalid: list[dict[str, str]] = []
    exact: dict[str, list[Path]] = collections.defaultdict(list)
    phash_items: list[tuple[Path, str, int, int, int]] = []
    informative = 0

    for path in images:
        rel = path.relative_to(root)
        top_counts[rel.parts[0]] += 1
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        exact[sha].append(path)
        try:
            with Image.open(path) as image:
                image.load()
                width, height = image.size
                format_counts[str(image.format or "UNKNOWN")] += 1
                mode_counts[image.mode] += 1
                dimension_counts[f"{width}x{height}"] += 1
                orientation_counts[
                    "square" if width == height else ("landscape" if width > height else "portrait")
                ] += 1
                has_alpha, has_transparency = alpha_status(image)
                alpha_channel_count += int(has_alpha)
                transparent_count += int(has_transparency)
                if has_transparency:
                    transparent_files.append(
                        {
                            "path": str(rel),
                            "format": str(image.format or "UNKNOWN"),
                            "mode": image.mode,
                            "dimensions": f"{width}x{height}",
                        }
                    )
                expected = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}[path.suffix.lower()]
                actual = str(image.format or "UNKNOWN")
                if actual not in {expected, "MPO" if expected == "JPEG" else expected}:
                    extension_format_mismatches.append(
                        {"path": str(rel), "extension": path.suffix.lower(), "format": actual}
                    )
                phash_items.append((path, sha, phash64(image), width, height))
                informative += int(informative_name(path))
        except Exception as exc:  # noqa: BLE001
            invalid.append({"path": str(rel), "error": type(exc).__name__})

    exact_groups = [group for group in exact.values() if len(group) > 1]
    duplicate_files = sum(len(group) - 1 for group in exact_groups)
    duplicate_bytes = sum((len(group) - 1) * group[0].stat().st_size for group in exact_groups)
    near_groups = union_find_groups(phash_items)

    result = {
        "root": str(root),
        "total_files": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "extension_counts": dict(ext_counts.most_common()),
        "image_count": len(images),
        "decoded_image_count": len(phash_items),
        "invalid_images": invalid,
        "format_counts": dict(format_counts.most_common()),
        "mode_counts": dict(mode_counts.most_common()),
        "alpha_channel_count": alpha_channel_count,
        "transparent_pixel_count": transparent_count,
        "transparent_files": transparent_files,
        "opaque_or_no_alpha_count": len(phash_items) - transparent_count,
        "orientation_counts": dict(orientation_counts),
        "extension_format_mismatch_count": len(extension_format_mismatches),
        "extension_format_mismatches": extension_format_mismatches[:100],
        "unique_dimensions": len(dimension_counts),
        "top_dimensions": dimension_counts.most_common(30),
        "min_width": min((item[3] for item in phash_items), default=None),
        "max_width": max((item[3] for item in phash_items), default=None),
        "min_height": min((item[4] for item in phash_items), default=None),
        "max_height": max((item[4] for item in phash_items), default=None),
        "top_level_image_counts": dict(top_counts.most_common()),
        "informative_filename_count": informative,
        "generic_filename_count": len(phash_items) - informative,
        "exact_duplicate_group_count": len(exact_groups),
        "exact_duplicate_extra_file_count": duplicate_files,
        "exact_duplicate_extra_bytes": duplicate_bytes,
        "exact_duplicate_samples": [
            [str(path.relative_to(root)) for path in group[:8]] for group in exact_groups[:30]
        ],
        "near_duplicate_group_count": len(near_groups),
        "near_duplicate_member_count": sum(len(group) for group in near_groups),
        "near_duplicate_samples": [
            [str(phash_items[index][0].relative_to(root)) for index in group[:8]]
            for group in sorted(near_groups, key=len, reverse=True)[:30]
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
