from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


LEGACY_DRAFT_APP_VERSION = "5.9.0"
LEGACY_DRAFT_NEW_VERSION = "110.0.0"
LEGACY_DRAFT_SCHEMA_VERSION = 360000


@dataclass(frozen=True)
class DraftCompatibilityResult:
    target_app_version: str
    target_new_version: str
    changed_contexts: int
    changed_fields: int
    source_platform_versions: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.changed_fields > 0


def normalize_draft_for_legacy_editor(
    data: dict[str, Any],
    *,
    target_app_version: str = LEGACY_DRAFT_APP_VERSION,
    target_new_version: str = LEGACY_DRAFT_NEW_VERSION,
    target_schema_version: int = LEGACY_DRAFT_SCHEMA_VERSION,
) -> DraftCompatibilityResult:
    """Normalize native newer-version draft envelopes for the fixed 5.9 renderer.

    A draft whose original ``platform.app_version`` is already at or below the
    target is left untouched. This deliberately preserves the known-good case
    where a draft originated in 5.9 and was later edited by a newer Jianying
    release (``last_modified_platform`` and ``new_version`` may be newer).

    Only output copies should be passed to this function. The source template
    remains the lossless, decrypted master stored in the template library.
    """

    changed_contexts = 0
    changed_fields = 0
    source_versions: list[str] = []

    for draft in iter_draft_contexts(data):
        platform = draft.get("platform")
        source_version = _platform_app_version(platform)
        if source_version:
            source_versions.append(source_version)
        if not source_version or not _is_newer_version(source_version, target_app_version):
            continue

        context_changes = 0
        context_changes += _set_platform_app_version(draft, "platform", target_app_version)
        context_changes += _set_platform_app_version(
            draft,
            "last_modified_platform",
            target_app_version,
        )

        current_new_version = str(draft.get("new_version") or "").strip()
        if current_new_version and _is_newer_version(current_new_version, target_new_version):
            draft["new_version"] = target_new_version
            context_changes += 1

        current_schema_version = draft.get("version")
        if isinstance(current_schema_version, (int, float)) and current_schema_version > target_schema_version:
            draft["version"] = target_schema_version
            context_changes += 1

        if context_changes:
            changed_contexts += 1
            changed_fields += context_changes

    return DraftCompatibilityResult(
        target_app_version=target_app_version,
        target_new_version=target_new_version,
        changed_contexts=changed_contexts,
        changed_fields=changed_fields,
        source_platform_versions=tuple(source_versions),
    )


def iter_draft_contexts(data: dict[str, Any], *, max_depth: int = 8) -> Iterator[dict[str, Any]]:
    """Yield the top-level draft and all embedded compound drafts."""

    stack: list[tuple[dict[str, Any], int]] = [(data, 0)]
    while stack:
        draft, depth = stack.pop()
        yield draft
        if depth >= max_depth:
            continue
        materials = draft.get("materials")
        nested_items = materials.get("drafts") if isinstance(materials, dict) else None
        if not isinstance(nested_items, list):
            continue
        for item in reversed(nested_items):
            nested = item.get("draft") if isinstance(item, dict) else None
            if isinstance(nested, dict):
                stack.append((nested, depth + 1))


def _set_platform_app_version(
    draft: dict[str, Any],
    key: str,
    target_app_version: str,
) -> int:
    platform = draft.get(key)
    if not isinstance(platform, dict):
        return 0
    current = str(platform.get("app_version") or "").strip()
    if current == target_app_version:
        return 0
    platform["app_version"] = target_app_version
    return 1


def _platform_app_version(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("app_version") or "").strip()


def _is_newer_version(value: str, target: str) -> bool:
    return _version_key(value) > _version_key(target)


def _version_key(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in str(value).replace("-", ".").split("."):
        digits = "".join(char for char in token if char.isdigit())
        parts.append(int(digits) if digits else 0)
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)
