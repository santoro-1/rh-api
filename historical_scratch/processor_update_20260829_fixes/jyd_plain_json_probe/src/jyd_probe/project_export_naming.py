"""Stable user-facing filenames for project audio and video exports."""

from __future__ import annotations

from pathlib import Path
import re
from typing import AbstractSet, Any, Mapping


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_SPEED_SUFFIX = re.compile(r"_([0-9]+(?:\.[0-9]+)?)倍速$", re.IGNORECASE)
_VARIANT_INDEX = re.compile(r"(?:变体|variant)[-_ ]*(\d+)", re.IGNORECASE)


def _safe_component(value: Any, *, fallback: str = "") -> str:
    text = _INVALID_FILENAME_CHARS.sub("_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text).strip(" .-_")
    return text or fallback


def _source_metadata(item: Mapping[str, Any]) -> tuple[str, str]:
    settings = item.get("settings")
    metadata = settings.get("source_metadata") if isinstance(settings, Mapping) else None
    if not isinstance(metadata, Mapping):
        return "", ""
    return (
        _safe_component(metadata.get("article_type")),
        _safe_component(metadata.get("assigned_account")),
    )


def project_item_export_stem(item: Mapping[str, Any]) -> str:
    """Return ``账号-类型-任务ID`` when four-column metadata is available."""

    row_key = _safe_component(
        item.get("row_key") or item.get("item_id"), fallback="未编号"
    )
    article_type, assigned_account = _source_metadata(item)
    if not article_type or not assigned_account:
        return row_key
    account_label = (
        assigned_account
        if assigned_account.startswith("账号")
        else f"账号{assigned_account}"
    )
    return f"{account_label}-{article_type}-{row_key}"


def _asset_suffix(asset: Mapping[str, Any] | None, fallback: str) -> str:
    if isinstance(asset, Mapping):
        filename = _safe_component(asset.get("filename"))
        suffix = Path(filename).suffix.lower()
        if suffix:
            return suffix
        managed_path = str(asset.get("managed_path") or "").strip()
        suffix = Path(managed_path).suffix.lower()
        if suffix:
            return suffix
    return fallback


def audio_export_filename(
    item: Mapping[str, Any], audio: Mapping[str, Any] | None = None
) -> str:
    """Build an audio download name while preserving the paid speed snapshot."""

    article_type, assigned_account = _source_metadata(item)
    if not article_type or not assigned_account:
        existing = _safe_component((audio or {}).get("filename"))
        if existing:
            return existing
    speed: float | None = None
    metadata = (audio or {}).get("metadata")
    if isinstance(metadata, Mapping):
        try:
            speed = float(metadata.get("speed"))
        except (TypeError, ValueError):
            speed = None
    if speed is None:
        existing_stem = Path(str((audio or {}).get("filename") or "")).stem
        match = _SPEED_SUFFIX.search(existing_stem)
        if match:
            speed = float(match.group(1))
    speed = speed if speed is not None else 1.0
    speed_text = f"{speed:.2f}".rstrip("0").rstrip(".")
    return (
        f"{project_item_export_stem(item)}_{speed_text}倍速"
        f"{_asset_suffix(audio, '.mp3')}"
    )


def composition_export_filename(
    item: Mapping[str, Any], video: Mapping[str, Any] | None = None
) -> str:
    article_type, assigned_account = _source_metadata(item)
    if not article_type or not assigned_account:
        existing = _safe_component((video or {}).get("filename"))
        if existing:
            return existing
    return (
        f"{project_item_export_stem(item)}-composition"
        f"{_asset_suffix(video, '.mp4')}"
    )


def composition_draft_name(item: Mapping[str, Any]) -> str:
    """Use the user-facing composition export stem as the Jianying draft name."""

    return Path(composition_export_filename(item)).stem


def variant_export_filename(
    item: Mapping[str, Any],
    asset: Mapping[str, Any] | None = None,
    *,
    index: int | None = None,
) -> str:
    article_type, assigned_account = _source_metadata(item)
    if not article_type or not assigned_account:
        existing = _safe_component((asset or {}).get("filename"))
        if existing:
            return existing
        return (
            f"任务-{project_item_export_stem(item)}-变体-{max(1, index or 1):03d}"
            f"{_asset_suffix(asset, '.mp4')}"
        )
    if index is None:
        existing = str((asset or {}).get("filename") or "")
        match = _VARIANT_INDEX.search(existing)
        if match:
            index = int(match.group(1))
    if index is None:
        try:
            index = int((asset or {}).get("version") or 1)
        except (TypeError, ValueError):
            index = 1
    return (
        f"{project_item_export_stem(item)}-变体-{max(1, index):03d}"
        f"{_asset_suffix(asset, '.mp4')}"
    )


def variant_draft_name(item: Mapping[str, Any], *, index: int) -> str:
    """Use the user-facing variant export stem as the Jianying draft name."""

    return Path(variant_export_filename(item, index=index)).stem


def available_draft_name(
    draft_root: str | Path,
    preferred_name: str,
    *,
    reserved_names: AbstractSet[str] | None = None,
) -> str:
    """Keep a readable export-based name without overwriting an existing draft."""

    root = Path(draft_root).expanduser().resolve()
    preferred = _safe_component(preferred_name, fallback="未命名草稿")
    reserved = reserved_names or frozenset()
    if preferred not in reserved and not (root / preferred).exists():
        return preferred
    for duplicate_index in range(2, 1000):
        candidate = f"{preferred}-{duplicate_index:02d}"
        if candidate not in reserved and not (root / candidate).exists():
            return candidate
    raise FileExistsError(f"剪映草稿同名副本过多: {preferred}")


def segment_export_filename(
    item: Mapping[str, Any],
    segment: Mapping[str, Any] | None,
    *,
    index: int,
) -> str:
    article_type, assigned_account = _source_metadata(item)
    if not article_type or not assigned_account:
        existing = _safe_component((segment or {}).get("filename"))
        if existing:
            return existing
    return (
        f"{project_item_export_stem(item)}-segment-{max(1, index):03d}"
        f"{_asset_suffix(segment, '.mp4')}"
    )
