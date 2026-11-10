from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator
import uuid

from .logging_config import log_event
from .layout_profiles import normalize_layout_profile
from .semantic_visuals import (
    DEFAULT_LIBRARY_ID,
    MEDIA_POLICIES,
    RECIPE_SCHEMA,
    VISUAL_CORNERS,
)


PROJECT_SCHEMA_VERSION = 12
STORAGE_PATH_PREFIX = "storage://"
logger = logging.getLogger("jyd_probe.workbench")
MAX_PROJECT_ITEMS = 500
ANALYSIS_PENDING_TIMEOUT_SECONDS = 15 * 60

PROJECT_ITEM_STATUSES = {
    "DRAFT",
    "AUDIO_QUEUED",
    "AUDIO_RUNNING",
    "AUDIO_READY",
    "AUDIO_FAILED",
    "H3_COST_PENDING",
    "H3_QUEUED",
    "H3_RUNNING",
    "H3_FAILED",
    "COMPOSITION_QUEUED",
    "DIGITAL_HUMAN_RUNNING",
    "VIDEO_ENHANCING",
    "VIDEO_MERGING",
    "BASE_VIDEO_READY",
    "POSTPROCESS_RUNNING",
    "COMPOSITION_READY",
    "COMPOSITION_FAILED",
    "VARIANT_QUEUED",
    "VARIANT_RUNNING",
    "VARIANT_READY",
    "VARIANT_FAILED",
}

ACTIVE_ITEM_STATUSES = {
    "AUDIO_QUEUED",
    "AUDIO_RUNNING",
    "H3_QUEUED",
    "H3_RUNNING",
    "COMPOSITION_QUEUED",
    "DIGITAL_HUMAN_RUNNING",
    "VIDEO_ENHANCING",
    "VIDEO_MERGING",
    "POSTPROCESS_RUNNING",
    "VARIANT_QUEUED",
    "VARIANT_RUNNING",
}

# H3 status polling is allowed to advance rows into H3, but it must never
# overwrite a local stage that already consumed the H3 base video.  The H3
# metadata and failed segments are still refreshed separately.
H3_DOWNSTREAM_ITEM_STATUSES = {
    "BASE_VIDEO_READY",
    "POSTPROCESS_RUNNING",
    "COMPOSITION_READY",
    "COMPOSITION_FAILED",
    "VARIANT_QUEUED",
    "VARIANT_RUNNING",
    "VARIANT_READY",
    "VARIANT_FAILED",
}

EDITABLE_ITEM_STATUSES = PROJECT_ITEM_STATUSES - ACTIVE_ITEM_STATUSES
IMAGE_EDITABLE_ITEM_STATUSES = EDITABLE_ITEM_STATUSES

FAILED_ITEM_STATUSES = {
    "AUDIO_FAILED",
    "H3_FAILED",
    "COMPOSITION_FAILED",
    "VARIANT_FAILED",
}


class ProjectRevisionConflict(ValueError):
    """The caller edited an older project revision."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _timestamp_is_older_than(value: Any, cutoff: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=cutoff.tzinfo)
    return parsed <= cutoff


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _object(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _default_subtitles() -> dict[str, Any]:
    return {
        "source": None,
        "raw_cues": [],
        "render_cues": [],
        "bound_audio_asset_id": None,
        "bound_video_asset_id": None,
        "semantic_mapping": {
            "schema": "jyd.semantic-caption-mapping.v1",
            "status": "NOT_REQUESTED",
            "reason_code": None,
            "reason_summary": None,
        },
        "style": {
            "font_id": None,
            "font_size": 15,
            "max_width_ratio": 0.8,
            "max_lines": 1,
            "bottom_offset_ratio": 0.3,
            "transform_y": -0.4,
        },
        "status": "NOT_AVAILABLE",
        "overflow_risk": False,
    }


def _script_sha256(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def subtitle_analysis_sha256(snapshot: dict[str, Any] | None) -> str | None:
    """Identify the exact subtitle-analysis contract and unit boundaries."""

    if not isinstance(snapshot, dict):
        return None
    if str(snapshot.get("subtitle_analysis_status") or "").upper() != "SUCCESS":
        return None
    units = snapshot.get("subtitle_units")
    if not isinstance(units, list):
        return None
    payload = {
        "script_sha256": snapshot.get("script_sha256"),
        "schema_version": snapshot.get("schema_version"),
        "prompt_version": snapshot.get("prompt_version"),
        "subtitle_prompt_version": snapshot.get("subtitle_prompt_version"),
        "subtitle_units": units,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _default_content_analysis(
    script: str,
    *,
    invalidated_reason: str | None = None,
) -> dict[str, Any]:
    now = _now() if invalidated_reason else None
    return {
        "snapshot_schema": "jyd.project-content-analysis.v1",
        "script_sha256": _script_sha256(script),
        "script_length": len(script),
        "overall_status": "NOT_REQUESTED",
        "music_analysis_status": "NOT_REQUESTED",
        "subtitle_analysis_status": "NOT_REQUESTED",
        "title_analysis_status": "NOT_REQUESTED",
        "music_intent": None,
        "subtitle_units": None,
        "title": None,
        "errors": {"music": None, "subtitle": None, "title": None, "request": None},
        "schema_version": None,
        "prompt_version": None,
        "model": None,
        "provider_request_id": None,
        "provider_attempts": 0,
        "cache_hit": False,
        "cacheable": False,
        "request_count": 0,
        "requested_at": None,
        "analyzed_at": None,
        "invalidated_reason": invalidated_reason,
        "invalidated_at": now,
    }


def _content_analysis_snapshot(value: Any, script: str) -> dict[str, Any]:
    default = _default_content_analysis(script)
    if not isinstance(value, dict):
        return default
    if not value.get("script_sha256"):
        return default
    if value.get("script_sha256") != default["script_sha256"]:
        return _default_content_analysis(script, invalidated_reason="SCRIPT_CHANGED")
    return {**default, **value}


def _default_visual_analysis(
    script: str,
    *,
    invalidated_reason: str | None = None,
    retained_overlays: list[dict[str, Any]] | None = None,
    bound_audio_asset_id: str | None = None,
    raw_cues_sha256: str | None = None,
) -> dict[str, Any]:
    now = _now() if invalidated_reason else None
    overlays = [dict(item) for item in (retained_overlays or [])]
    return {
        "snapshot_schema": "jyd.project-visual-analysis.v1",
        "script_sha256": _script_sha256(script),
        "script_length": len(script),
        "bound_audio_asset_id": bound_audio_asset_id,
        "raw_cues_sha256": raw_cues_sha256,
        "analysis_status": "NOT_REQUESTED",
        "mapping_status": "NOT_REQUESTED",
        "catalog_version": None,
        "candidate_set_sha256": None,
        "candidate_request": None,
        "mapped_candidates": [],
        "visual_plan": [],
        "decisions": [],
        "seam_analysis": {
            "status": "NOT_REQUESTED",
            "candidate_set_sha256": None,
            "decisions": [],
            "mapped_candidates": [],
            "error": None,
            "analyzed_at": None,
        },
        "recipe": {
            "schema": RECIPE_SCHEMA,
            "library_id": DEFAULT_LIBRARY_ID,
            "catalog_version": None,
            "media_policy": "image_only",
            "overlays": overlays,
        },
        "error": None,
        "provider_request_id": None,
        "provider_attempts": 0,
        "cache_hit": False,
        "cacheable": False,
        "request_count": 0,
        "requested_at": None,
        "analyzed_at": None,
        "invalidated_reason": invalidated_reason,
        "invalidated_at": now,
        "revision": 1,
    }


def _raw_cues_sha256(raw_cues: Any) -> str:
    encoded = json.dumps(
        raw_cues if isinstance(raw_cues, list) else [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _visual_candidate_set_sha256(candidate_request: Any) -> str:
    candidates = (
        candidate_request.get("candidates", [])
        if isinstance(candidate_request, dict)
        else []
    )
    encoded = json.dumps(
        candidates,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _visual_analysis_snapshot(
    value: Any,
    script: str,
    *,
    current_audio_asset_id: str | None = None,
    current_raw_cues: Any = None,
    validate_media_binding: bool = False,
) -> dict[str, Any]:
    default = _default_visual_analysis(script)
    if not isinstance(value, dict):
        return default
    if value.get("script_sha256") != default["script_sha256"]:
        recipe = value.get("recipe") if isinstance(value.get("recipe"), dict) else {}
        retained = [
            {**dict(item), "requires_review": True}
            for item in recipe.get("overlays", [])
            if isinstance(item, dict)
            and item.get("manual") is True
            and item.get("locked") is True
        ]
        return _default_visual_analysis(
            script,
            invalidated_reason="SCRIPT_CHANGED",
            retained_overlays=retained,
        )
    snapshot = {**default, **value}
    if not isinstance(snapshot.get("recipe"), dict):
        snapshot["recipe"] = default["recipe"]
    if validate_media_binding and snapshot.get("analysis_status") != "NOT_REQUESTED":
        current_cues_hash = _raw_cues_sha256(current_raw_cues)
        if (
            snapshot.get("bound_audio_asset_id") != current_audio_asset_id
            or snapshot.get("raw_cues_sha256") != current_cues_hash
        ):
            recipe = snapshot.get("recipe") if isinstance(snapshot.get("recipe"), dict) else {}
            retained = [
                {**dict(item), "requires_review": True}
                for item in recipe.get("overlays", [])
                if isinstance(item, dict)
                and item.get("manual") is True
                and item.get("locked") is True
            ]
            if (
                snapshot.get("analysis_status") == "SUCCESS"
                and isinstance(snapshot.get("visual_plan"), list)
                and isinstance(snapshot.get("candidate_request"), dict)
            ):
                return {
                    **snapshot,
                    "mapping_status": "NOT_REQUESTED",
                    "mapped_candidates": [],
                    "recipe": {
                        **dict(snapshot.get("recipe") or {}),
                        "overlays": retained,
                    },
                    "error": None,
                    "bound_audio_asset_id": current_audio_asset_id,
                    "raw_cues_sha256": current_cues_hash,
                    "invalidated_reason": "AUDIO_OR_RAW_CUES_CHANGED",
                    "invalidated_at": _now(),
                }
            return _default_visual_analysis(
                script,
                invalidated_reason="AUDIO_OR_RAW_CUES_CHANGED",
                retained_overlays=retained,
                bound_audio_asset_id=current_audio_asset_id,
                raw_cues_sha256=current_cues_hash,
            )
    return snapshot


def _analysis_overall_status(
    music_status: str, subtitle_status: str, title_status: str
) -> str:
    success_count = sum(
        status == "SUCCESS" for status in (music_status, subtitle_status, title_status)
    )
    if success_count == 3:
        return "SUCCESS"
    if success_count:
        return "PARTIAL"
    return "FAILED"


def _invalidate_auto_music_selection(
    settings: dict[str, Any], reason_code: str
) -> dict[str, Any]:
    postprocess = settings.get("postprocess")
    if not isinstance(postprocess, dict) or postprocess.get("bgm_selection_mode") != "auto":
        return settings
    postprocess = dict(postprocess)
    previous_identity = str(postprocess.get("bgm_identity") or "").strip()
    previous_selection = postprocess.get("music_selection")
    if reason_code == "AUDIO_VERSION_CHANGED" and previous_identity:
        postprocess["music_selection"] = {
            **(
                dict(previous_selection)
                if isinstance(previous_selection, dict)
                else {}
            ),
            "schema": "jyd.project-music-selection.v1",
            "status": "STALE",
            "selection_source": "ai",
            "bgm_identity": previous_identity,
            "audio_asset_id": None,
            "video_duration_us": 0,
            "reason_code": reason_code,
            "reason_summary": "声音版本已变化，保留当前推荐并在成片前按真实时长复核",
        }
    else:
        postprocess["bgm_identity"] = ""
        postprocess["music_selection"] = {
            "schema": "jyd.project-music-selection.v1",
            "status": "NOT_REQUESTED",
            "selection_source": "ai",
            "bgm_identity": None,
            "reason_code": reason_code,
        }
    settings["postprocess"] = postprocess
    return settings


def _clean_name(value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError("项目名称不能为空")
    if len(result) > 120:
        raise ValueError("项目名称不能超过 120 个字符")
    return result


def _clean_row_key(value: Any, position: int) -> str:
    result = str(value or "").strip() or f"{position:03d}"
    if len(result) > 80:
        raise ValueError("脚本行编号不能超过 80 个字符")
    return result


def _clean_script(value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError("脚本内容不能为空")
    if len(result) > 50_000:
        raise ValueError("单条脚本不能超过 50000 个字符")
    return result


def _clean_source_metadata(article_type: Any, assigned_account: Any) -> dict[str, str]:
    clean_article_type = str(article_type or "").strip()
    clean_assigned_account = str(assigned_account or "").strip()
    if not clean_article_type:
        raise ValueError("文章类型不能为空")
    if not clean_assigned_account:
        raise ValueError("分配账号不能为空")
    if len(clean_article_type) > 120:
        raise ValueError("文章类型不能超过 120 个字符")
    if len(clean_assigned_account) > 120:
        raise ValueError("分配账号不能超过 120 个字符")
    return {
        "article_type": clean_article_type,
        "assigned_account": clean_assigned_account,
    }


def _clean_status(value: Any, *, allowed: set[str], label: str) -> str:
    result = str(value or "").strip().upper()
    if result not in allowed:
        raise ValueError(f"{label}无效: {result}")
    return result


class ProjectStore:
    """Unified project data stored beside the existing render queue.

    This store creates only `project_*` tables. It deliberately leaves the
    existing render queue's `schema_meta`, `batches`, `jobs`, and `agents`
    untouched so an existing installation can adopt the new workspace without
    migrating or rewriting old Jianying tasks.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._storage_root = self.path.parent
        self._schema_lock = threading.Lock()
        self._pending_recovery_lock = threading.Lock()
        self._last_pending_recovery_monotonic = 0.0
        self.startup_database_backup_path = self._backup_before_v12_path_migration()
        self.startup_storage_path_migration_count = 0
        self._initialize()
        self.startup_relocated_managed_path_count = (
            self.recover_relocated_managed_paths()
        )
        self.startup_recovered_analysis_count = self.recover_stale_analysis_pending()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _backup_before_v12_path_migration(self) -> str | None:
        """Create one consistent SQLite backup before rewriting stored paths."""

        if not self.path.is_file():
            return None
        source = sqlite3.connect(self.path, timeout=30)
        source.row_factory = sqlite3.Row
        try:
            schema_exists = source.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='project_schema_meta'"
            ).fetchone()
            if schema_exists is None:
                return None
            version_row = source.execute(
                "SELECT value FROM project_schema_meta WHERE key='version'"
            ).fetchone()
            previous_version = (
                int(version_row["value"])
                if version_row is not None and str(version_row["value"]).isdigit()
                else 0
            )
            if previous_version >= 12:
                return None
            backup_path = self.path.with_name(f"{self.path.name}.pre-project-v12.bak")
            if not backup_path.exists():
                destination = sqlite3.connect(backup_path)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
                logger.warning(
                    "Created project database backup before v12 path migration: %s",
                    backup_path,
                )
            return str(backup_path)
        finally:
            source.close()

    @property
    def storage_root(self) -> Path:
        return self._storage_root

    def encode_managed_path(self, managed_path: str | Path) -> str:
        """Encode a local managed file without coupling it to the install path."""

        raw = str(managed_path or "").strip()
        if not raw:
            return ""
        if raw.startswith(STORAGE_PATH_PREFIX):
            resolved = self.resolve_managed_path(raw)
            return self._storage_reference(resolved)

        path = Path(raw).expanduser()
        if not path.is_absolute():
            return self._storage_reference(self._safe_storage_candidate(raw))

        resolved = path.resolve()
        try:
            return self._storage_reference(resolved)
        except ValueError:
            relocated = self._relocated_managed_path(
                raw, current_root=self.storage_root
            )
            if relocated is not None and relocated.is_file():
                return self._storage_reference(relocated)
            # Deliberately preserve valid external paths. Some integrations can
            # hand off files outside web_storage and must remain compatible.
            return str(resolved)

    def resolve_managed_path(self, stored_path: str | Path) -> Path:
        """Resolve storage references while rejecting traversal outside the root."""

        raw = str(stored_path or "").strip()
        if not raw:
            raise ValueError("素材路径不能为空")
        if raw.startswith(STORAGE_PATH_PREFIX):
            return self._safe_storage_candidate(raw[len(STORAGE_PATH_PREFIX) :])
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path.resolve()
        # Accept pre-v12 relative rows defensively and normalize them on the
        # next startup migration pass.
        return self._safe_storage_candidate(raw)

    def _safe_storage_candidate(self, relative_value: str) -> Path:
        normalized = str(relative_value or "").strip().replace("\\", "/")
        parts = normalized.split("/")
        if (
            not parts
            or any(part in {"", ".", ".."} for part in parts)
            or Path(normalized).is_absolute()
        ):
            raise ValueError("素材相对路径无效")
        # Every part has already been constrained to a plain child component,
        # so lexical joining is sufficient and avoids a filesystem resolve for
        # every asset on high-frequency project status reads.
        return self.storage_root.joinpath(*parts)

    def _storage_reference(self, path: Path) -> str:
        resolved = Path(path).expanduser().resolve()
        try:
            relative = resolved.relative_to(self.storage_root)
        except ValueError as exc:
            raise ValueError("素材路径不属于本地数据目录") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("素材相对路径无效")
        return STORAGE_PATH_PREFIX + "/".join(relative.parts)

    def _payload_managed_path(self, stored_path: object) -> tuple[str, str, str | None]:
        reference = str(stored_path or "").strip()
        if not reference:
            return "", "", None
        try:
            resolved = str(self.resolve_managed_path(reference))
        except (OSError, ValueError) as exc:
            return "", reference, str(exc)
        return resolved, reference, None

    def recover_relocated_managed_paths(self) -> int:
        """Normalize legacy path rows and rebind copied project files.

        Older releases stored absolute paths in the project database.  When an
        installation was copied to a new directory, the database and files
        moved together but those absolute paths still pointed at the previous
        ``data/web_storage`` directory.  Only rebind a row when the same
        relative file already exists below the current storage root; this
        never copies, downloads, overwrites, or recreates user media.
        """

        with self._transaction() as connection:
            relocated = self._migrate_managed_path_rows(connection)
        if relocated:
            logger.warning(
                "Normalized %s project media paths for current storage root: %s",
                relocated,
                self.storage_root,
            )
        return relocated

    def _migrate_managed_path_rows(self, connection: sqlite3.Connection) -> int:
        targets = (
            ("project_assets", "asset_id"),
            ("project_input_images", "image_id"),
            ("project_script_sources", "source_id"),
        )
        migrated = 0
        for table_name, identity_column in targets:
            rows = connection.execute(
                f"SELECT {identity_column}, managed_path FROM {table_name} "
                "WHERE managed_path IS NOT NULL AND TRIM(managed_path)<>''"
            ).fetchall()
            for row in rows:
                old_value = str(row["managed_path"] or "").strip()
                try:
                    encoded = self.encode_managed_path(old_value)
                except (OSError, ValueError):
                    continue
                if not encoded or encoded == old_value:
                    continue
                connection.execute(
                    f"UPDATE {table_name} SET managed_path=? "
                    f"WHERE {identity_column}=?",
                    (encoded, row[identity_column]),
                )
                migrated += 1
        return migrated

    @staticmethod
    def _relocated_managed_path(
        managed_path: str, *, current_root: Path
    ) -> Path | None:
        normalized = str(managed_path or "").strip().replace("\\", "/")
        parts = normalized.split("/")
        storage_indexes = [
            index
            for index, part in enumerate(parts)
            if part.casefold() == "web_storage"
        ]
        if not storage_indexes:
            return None
        relative_parts = parts[storage_indexes[-1] + 1 :]
        if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
            return None
        candidate = current_root.joinpath(*relative_parts).resolve()
        try:
            candidate.relative_to(current_root)
        except ValueError:
            return None
        return candidate

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._schema_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS project_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_counters (
                    day_key TEXT PRIMARY KEY,
                    last_value INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    project_no TEXT NOT NULL UNIQUE,
                    owner_user_id TEXT NOT NULL,
                    owner_username TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    settings_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_projects_owner_updated
                    ON projects(owner_user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS project_items (
                    item_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    row_key TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    script_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_image_asset_id TEXT,
                    current_audio_asset_id TEXT,
                    current_base_video_asset_id TEXT,
                    current_video_asset_id TEXT,
                    subtitles_json TEXT NOT NULL DEFAULT '{}',
                    content_analysis_json TEXT NOT NULL DEFAULT '{}',
                    visual_analysis_json TEXT NOT NULL DEFAULT '{}',
                    settings_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    UNIQUE(project_id, row_key),
                    UNIQUE(project_id, position)
                );

                CREATE INDEX IF NOT EXISTS idx_project_items_project
                    ON project_items(project_id, position);

                CREATE TABLE IF NOT EXISTS project_assets (
                    asset_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    filename TEXT NOT NULL DEFAULT '',
                    managed_path TEXT,
                    external_ref_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(item_id) REFERENCES project_items(item_id)
                        ON DELETE CASCADE,
                    UNIQUE(item_id, asset_type, version)
                );

                CREATE INDEX IF NOT EXISTS idx_project_assets_item_type
                    ON project_assets(item_id, asset_type, version);

                CREATE TABLE IF NOT EXISTS project_input_images (
                    image_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    managed_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    UNIQUE(project_id, position)
                );

                CREATE INDEX IF NOT EXISTS idx_project_input_images_project
                    ON project_input_images(project_id, position);

                CREATE TABLE IF NOT EXISTS project_script_sources (
                    source_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    managed_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    UNIQUE(project_id, version)
                );

                CREATE INDEX IF NOT EXISTS idx_project_script_sources_project
                    ON project_script_sources(project_id, version DESC);

                CREATE TABLE IF NOT EXISTS project_result_batch_counters (
                    day_key TEXT PRIMARY KEY,
                    last_value INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_result_batches (
                    result_batch_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    date_key TEXT NOT NULL,
                    date_label TEXT NOT NULL,
                    batch_no INTEGER NOT NULL,
                    export_path TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    jianying_batch_id TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    UNIQUE(date_key, batch_no)
                );

                CREATE INDEX IF NOT EXISTS idx_project_result_batches_owner
                    ON project_result_batches(owner_user_id, date_key DESC, batch_no DESC);

                CREATE TABLE IF NOT EXISTS project_operations (
                    operation_id TEXT PRIMARY KEY,
                    correlation_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    item_id TEXT,
                    operation_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    error_message TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(item_id) REFERENCES project_items(item_id)
                        ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_project_operations_idempotency
                    ON project_operations(
                        project_id,
                        IFNULL(item_id, ''),
                        operation_type,
                        idempotency_key
                    );

                CREATE INDEX IF NOT EXISTS idx_project_operations_status
                    ON project_operations(status, created_at);

                CREATE TABLE IF NOT EXISTS project_links (
                    link_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    item_id TEXT,
                    system TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(item_id) REFERENCES project_items(item_id)
                        ON DELETE CASCADE,
                    UNIQUE(project_id, system, relation, external_id)
                );

                CREATE INDEX IF NOT EXISTS idx_project_links_project
                    ON project_links(project_id, system, relation);

                CREATE TABLE IF NOT EXISTS project_user_preferences (
                    owner_user_id TEXT PRIMARY KEY,
                    default_voice_asset_id TEXT,
                    voice_settings_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                """
            )
            item_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(project_items)").fetchall()
            }
            if "current_image_asset_id" not in item_columns:
                connection.execute(
                    "ALTER TABLE project_items ADD COLUMN current_image_asset_id TEXT"
                )
            if "current_base_video_asset_id" not in item_columns:
                connection.execute(
                    "ALTER TABLE project_items ADD COLUMN current_base_video_asset_id TEXT"
                )
            if "content_analysis_json" not in item_columns:
                connection.execute(
                    "ALTER TABLE project_items ADD COLUMN content_analysis_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "visual_analysis_json" not in item_columns:
                connection.execute(
                    "ALTER TABLE project_items ADD COLUMN visual_analysis_json TEXT NOT NULL DEFAULT '{}'"
                )
            operation_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(project_operations)"
                ).fetchall()
            }
            if "correlation_id" not in operation_columns:
                connection.execute(
                    "ALTER TABLE project_operations ADD COLUMN correlation_id TEXT"
                )
                connection.execute(
                    "UPDATE project_operations SET correlation_id=operation_id "
                    "WHERE correlation_id IS NULL OR correlation_id=''"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_operations_correlation "
                "ON project_operations(correlation_id, created_at)"
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                version_row = connection.execute(
                    "SELECT value FROM project_schema_meta WHERE key='version'"
                ).fetchone()
                previous_schema_version = (
                    int(version_row["value"])
                    if version_row is not None and str(version_row["value"]).isdigit()
                    else 0
                )
                if previous_schema_version < 11:
                    self._invalidate_legacy_subtitle_bindings(connection)
                if previous_schema_version < 12:
                    self.startup_storage_path_migration_count = (
                        self._migrate_managed_path_rows(connection)
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO project_schema_meta(key, value) "
                    "VALUES('version', ?)",
                    (str(PROJECT_SCHEMA_VERSION),),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _invalidate_legacy_subtitle_bindings(
        self, connection: sqlite3.Connection
    ) -> None:
        """Repair previews saved before subtitle-analysis identities were persisted."""

        rows = connection.execute(
            """
            SELECT item_id, project_id, status, current_audio_asset_id,
                   current_base_video_asset_id, subtitles_json,
                   content_analysis_json
            FROM project_items
            """
        ).fetchall()
        changed_projects: set[str] = set()
        now = _now()
        for item in rows:
            if str(item["status"]) in ACTIVE_ITEM_STATUSES:
                continue
            subtitles = _object(item["subtitles_json"], _default_subtitles())
            if not subtitles.get("render_cues"):
                continue
            analysis = _object(item["content_analysis_json"], {})
            current_identity = subtitle_analysis_sha256(analysis)
            if current_identity is None:
                continue
            mapping = (
                dict(subtitles.get("semantic_mapping") or {})
                if isinstance(subtitles.get("semantic_mapping"), dict)
                else {}
            )
            mapped_identity = str(
                mapping.get("analysis_subtitle_sha256") or ""
            ).strip()
            if mapped_identity:
                binding_is_stale = mapped_identity != current_identity
            else:
                mapped_prompt_version = str(
                    mapping.get("analysis_prompt_version") or ""
                ).strip()
                current_prompt_version = str(
                    analysis.get("prompt_version") or ""
                ).strip()
                mapped_subtitle_version = str(
                    mapping.get("analysis_subtitle_prompt_version") or ""
                ).strip()
                current_subtitle_version = str(
                    analysis.get("subtitle_prompt_version") or ""
                ).strip()
                binding_is_stale = bool(
                    mapped_prompt_version
                    and current_prompt_version
                    and mapped_prompt_version != current_prompt_version
                ) or bool(
                    mapped_subtitle_version
                    and current_subtitle_version
                    and mapped_subtitle_version != current_subtitle_version
                )
            if not binding_is_stale:
                continue
            subtitles["render_cues"] = []
            subtitles["bound_video_asset_id"] = None
            subtitles["overflow_risk"] = False
            subtitles["review_reason"] = None
            subtitles["status"] = (
                "READY" if subtitles.get("raw_cues") else "NOT_AVAILABLE"
            )
            subtitles["semantic_mapping"] = {
                "schema": "jyd.semantic-caption-mapping.v1",
                "status": "NOT_REQUESTED",
                "reason_code": "SUBTITLE_ANALYSIS_VERSION_CHANGED",
                "reason_summary": "字幕分析版本或断句结果已更新，需重新生成预览",
                "analysis_prompt_version": analysis.get("prompt_version"),
                "analysis_subtitle_prompt_version": analysis.get(
                    "subtitle_prompt_version"
                ),
            }
            next_status = (
                "BASE_VIDEO_READY"
                if item["current_base_video_asset_id"]
                else (
                    "AUDIO_READY" if item["current_audio_asset_id"] else "DRAFT"
                )
            )
            connection.execute(
                """
                UPDATE project_items
                SET subtitles_json=?, current_video_asset_id=NULL,
                    status=?, updated_at=?
                WHERE item_id=?
                """,
                (_json(subtitles), next_status, now, item["item_id"]),
            )
            changed_projects.add(str(item["project_id"]))
        for project_id in changed_projects:
            connection.execute(
                """
                UPDATE projects
                SET revision=revision+1, updated_at=?
                WHERE project_id=?
                """,
                (now, project_id),
            )
            self._refresh_project_status(connection, project_id, now=now)

    def recover_stale_analysis_pending(
        self,
        *,
        max_age_seconds: int = ANALYSIS_PENDING_TIMEOUT_SECONDS,
        force: bool = True,
    ) -> int:
        """Make interrupted model-analysis snapshots retryable without a new request."""

        if not force:
            elapsed = time.monotonic() - self._last_pending_recovery_monotonic
            if elapsed < 60:
                return 0
        with self._pending_recovery_lock:
            if not force:
                elapsed = time.monotonic() - self._last_pending_recovery_monotonic
                if elapsed < 60:
                    return 0
            now_value = datetime.now().astimezone()
            cutoff = now_value - timedelta(seconds=max(1, int(max_age_seconds)))
            recovered = 0
            affected_projects: set[str] = set()
            with self._transaction() as connection:
                rows = connection.execute(
                    """
                    SELECT item_id, project_id, script_text,
                           content_analysis_json, visual_analysis_json
                    FROM project_items
                    """
                ).fetchall()
                for row in rows:
                    content = _content_analysis_snapshot(
                        _object(row["content_analysis_json"], {}),
                        str(row["script_text"]),
                    )
                    visual = _visual_analysis_snapshot(
                        _object(row["visual_analysis_json"], {}),
                        str(row["script_text"]),
                    )
                    content_stale = (
                        content.get("overall_status") == "PENDING"
                        and _timestamp_is_older_than(content.get("requested_at"), cutoff)
                    )
                    visual_stale = (
                        visual.get("analysis_status") == "PENDING"
                        and _timestamp_is_older_than(visual.get("requested_at"), cutoff)
                    )
                    if not content_stale and not visual_stale:
                        continue
                    error = {
                        "code": "ANALYSIS_INTERRUPTED",
                        "summary": "上次分析已中断或超时，可直接重试",
                    }
                    now = now_value.isoformat(timespec="seconds")
                    if content_stale:
                        completed_branches = sum(
                            str(content.get(key) or "").upper() == "SUCCESS"
                            for key in (
                                "music_analysis_status",
                                "subtitle_analysis_status",
                                "title_analysis_status",
                            )
                        )
                        content["overall_status"] = (
                            "PARTIAL" if completed_branches else "FAILED"
                        )
                        content["errors"] = {
                            **dict(content.get("errors") or {}),
                            "request": error,
                        }
                        content["analyzed_at"] = now
                    if visual_stale:
                        visual.update(
                            {
                                "analysis_status": "FAILED",
                                "mapping_status": "FAILED",
                                "error": error,
                                "analyzed_at": now,
                                "cache_hit": False,
                                "cacheable": False,
                                "revision": int(visual.get("revision") or 0) + 1,
                            }
                        )
                    connection.execute(
                        """
                        UPDATE project_items
                        SET content_analysis_json=?, visual_analysis_json=?, updated_at=?
                        WHERE item_id=?
                        """,
                        (_json(content), _json(visual), now, row["item_id"]),
                    )
                    affected_projects.add(str(row["project_id"]))
                    recovered += 1
                for project_id in affected_projects:
                    connection.execute(
                        "UPDATE projects SET updated_at=? WHERE project_id=?",
                        (now_value.isoformat(timespec="seconds"), project_id),
                    )
            self._last_pending_recovery_monotonic = time.monotonic()
            return recovered

    def create_project(
        self,
        *,
        owner_user_id: str,
        owner_username: str,
        name: str,
        items: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner_id = str(owner_user_id or "").strip()
        if not owner_id:
            raise ValueError("项目必须绑定有效账号")
        project_name = _clean_name(name)
        if not isinstance(items, list) or not items:
            raise ValueError("项目至少需要一条脚本")
        if len(items) > MAX_PROJECT_ITEMS:
            raise ValueError(f"单个项目最多包含 {MAX_PROJECT_ITEMS} 条脚本")

        normalized_items: list[dict[str, Any]] = []
        row_keys: set[str] = set()
        for position, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"第 {position} 条脚本格式无效")
            row_key = _clean_row_key(raw.get("row_key"), position)
            if row_key in row_keys:
                raise ValueError(f"脚本行编号重复: {row_key}")
            row_keys.add(row_key)
            normalized_items.append(
                {
                    "item_id": uuid.uuid4().hex,
                    "row_key": row_key,
                    "position": position,
                    "script_text": _clean_script(raw.get("script_text")),
                    "settings": raw.get("settings")
                    if isinstance(raw.get("settings"), dict)
                    else {},
                }
            )

        project_id = uuid.uuid4().hex
        now = _now()
        with self._transaction() as connection:
            project_no = self._next_project_no(connection)
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, project_no, owner_user_id, owner_username,
                    name, status, revision, settings_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'DRAFT', 1, ?, ?, ?)
                """,
                (
                    project_id,
                    project_no,
                    owner_id,
                    str(owner_username or "").strip()[:120],
                    project_name,
                    _json(settings or {}),
                    now,
                    now,
                ),
            )
            for item in normalized_items:
                connection.execute(
                    """
                    INSERT INTO project_items(
                        item_id, project_id, row_key, position, script_text,
                        status, subtitles_json, content_analysis_json, settings_json,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?)
                    """,
                    (
                        item["item_id"],
                        project_id,
                        item["row_key"],
                        item["position"],
                        item["script_text"],
                        _json(_default_subtitles()),
                        _json(_default_content_analysis(item["script_text"])),
                        _json(item["settings"]),
                        now,
                        now,
                    ),
                )
        return self.get_project(owner_id, project_id)

    def import_h3_handoff_project(
        self,
        *,
        owner_user_id: str,
        owner_username: str,
        project_name: str,
        row_key: str,
        script_text: str,
        handoff_id: str,
        audio_filename: str,
        audio_managed_path: str,
        audio_metadata: dict[str, Any],
        base_video_filename: str,
        base_video_managed_path: str,
        base_video_metadata: dict[str, Any],
        subtitles: dict[str, Any],
        link_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically import one immutable H3 handoff and deduplicate it per owner."""

        owner_id = str(owner_user_id or "").strip()
        if not owner_id:
            raise ValueError("项目必须绑定有效账号")
        clean_handoff_id = str(handoff_id or "").strip()
        if not clean_handoff_id:
            raise ValueError("H3 交接编号不能为空")
        clean_row_key = _clean_row_key(row_key, 1)
        clean_script = _clean_script(script_text)
        clean_name = _clean_name(project_name)
        clean_audio_filename = Path(str(audio_filename or "")).name.strip()
        clean_audio_path = self.encode_managed_path(audio_managed_path)
        clean_video_filename = Path(str(base_video_filename or "")).name.strip()
        clean_video_path = self.encode_managed_path(base_video_managed_path)
        if not all(
            (
                clean_audio_filename,
                clean_audio_path,
                clean_video_filename,
                clean_video_path,
            )
        ):
            raise ValueError("H3 交接音视频素材信息不完整")
        if not all(
            isinstance(value, dict)
            for value in (
                audio_metadata,
                base_video_metadata,
                subtitles,
                link_metadata,
            )
        ):
            raise ValueError("H3 交接元数据格式错误")

        system = "h3_workbench"
        relation = "imported_handoff"
        project_id = uuid.uuid4().hex
        item_id = uuid.uuid4().hex
        audio_asset_id = uuid.uuid4().hex
        base_video_asset_id = uuid.uuid4().hex
        now = _now()
        settings = {"source_workbench": "minimax_h3_ref2va"}
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT p.project_id
                FROM project_links AS link
                JOIN projects AS p ON p.project_id = link.project_id
                WHERE p.owner_user_id=? AND link.system=?
                  AND link.relation=? AND link.external_id=?
                ORDER BY link.rowid DESC
                LIMIT 1
                """,
                (owner_id, system, relation, clean_handoff_id),
            ).fetchone()
            if existing is not None:
                return self._project_payload(connection, existing["project_id"])

            project_no = self._next_project_no(connection)
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, project_no, owner_user_id, owner_username,
                    name, status, revision, settings_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'DRAFT', 1, ?, ?, ?)
                """,
                (
                    project_id,
                    project_no,
                    owner_id,
                    str(owner_username or "").strip()[:120],
                    clean_name,
                    _json(settings),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO project_items(
                    item_id, project_id, row_key, position, script_text,
                    status, subtitles_json, content_analysis_json, settings_json,
                    created_at, updated_at
                ) VALUES(?, ?, ?, 1, ?, 'DRAFT', ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    project_id,
                    clean_row_key,
                    clean_script,
                    _json(_default_subtitles()),
                    _json(_default_content_analysis(clean_script)),
                    _json(settings),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO project_assets(
                    asset_id, project_id, item_id, asset_type, version,
                    status, source_type, filename, managed_path,
                    external_ref_json, metadata_json, created_at, updated_at
                ) VALUES(?, ?, ?, 'audio', 1, 'READY', 'h3_handoff', ?, ?, ?, ?, ?, ?)
                """,
                (
                    audio_asset_id,
                    project_id,
                    item_id,
                    clean_audio_filename,
                    clean_audio_path,
                    _json({}),
                    _json(audio_metadata),
                    now,
                    now,
                ),
            )
            item = self._owned_item(connection, project_id, item_id)
            self._set_current_asset(
                connection,
                item,
                asset_id=audio_asset_id,
                asset_type="audio",
                source_type="h3_handoff",
                asset_status="READY",
                now=now,
            )
            bound_subtitles = dict(subtitles)
            bound_subtitles["bound_audio_asset_id"] = audio_asset_id
            connection.execute(
                "UPDATE project_items SET subtitles_json=?, updated_at=? WHERE item_id=?",
                (_json(bound_subtitles), now, item_id),
            )
            connection.execute(
                """
                INSERT INTO project_assets(
                    asset_id, project_id, item_id, asset_type, version,
                    status, source_type, filename, managed_path,
                    external_ref_json, metadata_json, created_at, updated_at
                ) VALUES(?, ?, ?, 'base_video', 1, 'READY', 'h3_handoff', ?, ?, ?, ?, ?, ?)
                """,
                (
                    base_video_asset_id,
                    project_id,
                    item_id,
                    clean_video_filename,
                    clean_video_path,
                    _json({}),
                    _json(base_video_metadata),
                    now,
                    now,
                ),
            )
            item = self._owned_item(connection, project_id, item_id)
            self._set_current_asset(
                connection,
                item,
                asset_id=base_video_asset_id,
                asset_type="base_video",
                source_type="h3_handoff",
                asset_status="READY",
                now=now,
            )
            connection.execute(
                """
                INSERT INTO project_links(
                    link_id, project_id, item_id, system, relation,
                    external_id, metadata_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    project_id,
                    item_id,
                    system,
                    relation,
                    clean_handoff_id,
                    _json(link_metadata),
                    now,
                ),
            )
            self._refresh_project_status(connection, project_id, now=now)
            return self._project_payload(connection, project_id)

    def list_projects(
        self,
        owner_user_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.recover_stale_analysis_pending(force=False)
        owner_id = str(owner_user_id or "").strip()
        safe_limit = min(max(int(limit), 1), 100)
        safe_offset = max(int(offset), 0)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM projects WHERE owner_user_id=?",
                    (owner_id,),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT project_id FROM projects
                WHERE owner_user_id=?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ? OFFSET ?
                """,
                (owner_id, safe_limit, safe_offset),
            ).fetchall()
            now = _now()
            for row in rows:
                self._reconcile_durable_project_state(
                    connection, str(row["project_id"]), now=now
                )
            projects = [
                self._project_payload(connection, row["project_id"])
                for row in rows
            ]
        return {
            "schema": "jyd.project-list.v1",
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
            "projects": projects,
        }

    def get_project(self, owner_user_id: str, project_id: str) -> dict[str, Any]:
        self.recover_stale_analysis_pending(force=False)
        with self._connect() as connection:
            row = self._owned_project(connection, owner_user_id, project_id)
            self._reconcile_durable_project_state(
                connection, str(row["project_id"]), now=_now()
            )
            return self._project_payload(connection, row["project_id"])

    def visual_analysis_recovery_projects(self) -> list[dict[str, Any]]:
        """Return projects that have a locally reusable visual-analysis snapshot."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT p.project_id
                FROM projects p
                JOIN project_items i ON i.project_id=p.project_id
                WHERE json_extract(i.visual_analysis_json, '$.analysis_status')='SUCCESS'
                ORDER BY p.updated_at DESC
                """
            ).fetchall()
            return [
                self._project_payload(connection, str(row["project_id"]))
                for row in rows
            ]

    def update_project(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        name: str | None = None,
        settings: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            if expected_revision is not None and int(expected_revision) != int(
                project["revision"]
            ):
                raise ProjectRevisionConflict("项目已被其他操作更新，请刷新后重试")
            updates: list[str] = []
            values: list[Any] = []
            if name is not None:
                updates.append("name=?")
                values.append(_clean_name(name))
            if settings is not None:
                if not isinstance(settings, dict):
                    raise ValueError("项目设置必须是对象")
                updates.append("settings_json=?")
                values.append(_json(settings))
            if updates:
                updates.extend(["revision=revision+1", "updated_at=?"])
                values.extend([_now(), project_id])
                connection.execute(
                    f"UPDATE projects SET {', '.join(updates)} WHERE project_id=?",
                    values,
                )
        return self.get_project(owner_user_id, project_id)

    def update_item(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        row_key: str | None = None,
        script_text: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            if item["status"] in ACTIVE_ITEM_STATUSES:
                raise ValueError("当前脚本行正在生成，请等待完成后再修改")
            updates: list[str] = []
            values: list[Any] = []
            if row_key is not None:
                clean_row_key = _clean_row_key(row_key, int(item["position"]))
                if clean_row_key != str(item["row_key"]):
                    updates.append("row_key=?")
                    values.append(clean_row_key)
            content_changed = False
            script_changed = False
            if script_text is not None:
                clean_script = _clean_script(script_text)
                if clean_script != str(item["script_text"]):
                    updates.append("script_text=?")
                    values.append(clean_script)
                    content_changed = True
                    script_changed = True
            if settings is not None:
                if not isinstance(settings, dict):
                    raise ValueError("脚本行设置必须是对象")
                if settings != _object(item["settings_json"], {}):
                    updates.append("settings_json=?")
                    values.append(_json(settings))
                    content_changed = True
            if updates:
                now = _now()
                if content_changed:
                    updates.extend(
                        [
                            "current_audio_asset_id=NULL",
                            "current_base_video_asset_id=NULL",
                            "current_video_asset_id=NULL",
                            "subtitles_json=?",
                            "status='DRAFT'",
                        ]
                    )
                    values.append(_json(_default_subtitles()))
                if script_changed:
                    updates.append("content_analysis_json=?")
                    values.append(
                        _json(
                            _default_content_analysis(
                                clean_script,
                                invalidated_reason="SCRIPT_CHANGED",
                            )
                        )
                    )
                    current_visual = _visual_analysis_snapshot(
                        _object(item["visual_analysis_json"], {}),
                        str(item["script_text"]),
                    )
                    retained_overlays = [
                        {**dict(overlay), "requires_review": True}
                        for overlay in current_visual.get("recipe", {}).get(
                            "overlays", []
                        )
                        if isinstance(overlay, dict)
                        and overlay.get("manual") is True
                        and overlay.get("locked") is True
                    ]
                    updates.append("visual_analysis_json=?")
                    values.append(
                        _json(
                            _default_visual_analysis(
                                clean_script,
                                invalidated_reason="SCRIPT_CHANGED",
                                retained_overlays=retained_overlays,
                            )
                        )
                    )
                    if settings is None:
                        invalidated_settings = _invalidate_auto_music_selection(
                            _object(item["settings_json"], {}), "SCRIPT_CHANGED"
                        )
                        updates.append("settings_json=?")
                        values.append(_json(invalidated_settings))
                updates.append("updated_at=?")
                values.extend([now, item_id])
                try:
                    connection.execute(
                        f"UPDATE project_items SET {', '.join(updates)} WHERE item_id=?",
                        values,
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError("脚本行编号不能重复") from exc
                connection.execute(
                    """
                    UPDATE projects
                    SET revision=revision+1, updated_at=?
                    WHERE project_id=?
                    """,
                    (now, project["project_id"]),
                )
                self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def import_source_metadata(
        self,
        owner_user_id: str,
        project_id: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Backfill article/account metadata without invalidating generated assets."""

        if not isinstance(rows, list) or not rows:
            raise ValueError("分类信息不能为空")
        normalized: list[dict[str, Any]] = []
        row_keys: set[str] = set()
        for position, raw in enumerate(rows, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"第 {position} 条分类信息格式无效")
            row_key = _clean_row_key(raw.get("row_key"), position)
            if row_key in row_keys:
                raise ValueError(f"脚本行编号重复: {row_key}")
            row_keys.add(row_key)
            normalized.append(
                {
                    "row_key": row_key,
                    "script_text": _clean_script(raw.get("script_text")),
                    "source_metadata": _clean_source_metadata(
                        raw.get("article_type"), raw.get("assigned_account")
                    ),
                }
            )

        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            existing_rows = connection.execute(
                "SELECT * FROM project_items WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            existing_by_key = {str(row["row_key"]): row for row in existing_rows}
            existing_keys = set(existing_by_key)
            if row_keys != existing_keys:
                missing = sorted(existing_keys.difference(row_keys))
                unknown = sorted(row_keys.difference(existing_keys))
                details: list[str] = []
                if missing:
                    details.append(f"缺少任务ID: {', '.join(missing[:10])}")
                if unknown:
                    details.append(f"未知任务ID: {', '.join(unknown[:10])}")
                raise ValueError("分类表必须完整对应当前项目；" + "；".join(details))

            prepared: list[tuple[str, str]] = []
            for row in normalized:
                existing = existing_by_key[row["row_key"]]
                settings = _object(existing["settings_json"], {})
                updated_settings = {
                    **settings,
                    "source_metadata": row["source_metadata"],
                }
                if updated_settings != settings:
                    prepared.append((_json(updated_settings), str(existing["item_id"])))

            if prepared:
                now = _now()
                connection.executemany(
                    "UPDATE project_items SET settings_json=?, updated_at=? WHERE item_id=?",
                    [(settings_json, now, item_id) for settings_json, item_id in prepared],
                )
                connection.execute(
                    "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                    (now, project["project_id"]),
                )
        return self.get_project(owner_user_id, project_id)

    def delete_item(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
    ) -> tuple[dict[str, Any], list[str]]:
        """Remove one inactive project row and its item-owned local assets."""

        cleanup_candidates: set[str] = set()
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            if str(item["status"]) in ACTIVE_ITEM_STATUSES:
                raise ValueError("当前任务正在生成，请等待完成后再删除")
            analysis = _content_analysis_snapshot(
                _object(item["content_analysis_json"], {}),
                str(item["script_text"]),
            )
            if str(analysis.get("overall_status") or "") == "PENDING":
                raise ValueError("当前任务正在进行内容分析，请等待完成后再删除")
            visual_analysis = _visual_analysis_snapshot(
                _object(item["visual_analysis_json"], {}),
                str(item["script_text"]),
            )
            if str(visual_analysis.get("analysis_status") or "") == "PENDING":
                raise ValueError("当前任务正在进行语义视觉分析，请等待完成后再删除")
            active_operation = connection.execute(
                """
                SELECT 1 FROM project_operations
                WHERE item_id=? AND status IN ('PENDING', 'RUNNING')
                LIMIT 1
                """,
                (item_id,),
            ).fetchone()
            if active_operation is not None:
                raise ValueError("当前任务仍有异步操作，请等待完成后再删除")

            cleanup_candidates.update(
                str(row["managed_path"])
                for row in connection.execute(
                    """
                    SELECT managed_path FROM project_assets
                    WHERE item_id=? AND asset_type!='input_image'
                      AND managed_path IS NOT NULL AND managed_path!=''
                    """,
                    (item_id,),
                ).fetchall()
            )
            connection.execute("DELETE FROM project_items WHERE item_id=?", (item_id,))

            remaining = connection.execute(
                "SELECT item_id FROM project_items WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            for offset, row in enumerate(remaining, start=1):
                connection.execute(
                    "UPDATE project_items SET position=? WHERE item_id=?",
                    (-offset, row["item_id"]),
                )
            for position, row in enumerate(remaining, start=1):
                connection.execute(
                    "UPDATE project_items SET position=? WHERE item_id=?",
                    (position, row["item_id"]),
                )

            cleanup_paths = [
                path
                for path in sorted(cleanup_candidates)
                if connection.execute(
                    "SELECT 1 FROM project_assets WHERE managed_path=? LIMIT 1",
                    (path,),
                ).fetchone()
                is None
            ]
            now = _now()
            connection.execute(
                """
                UPDATE projects
                SET revision=revision+1, updated_at=?
                WHERE project_id=?
                """,
                (now, project["project_id"]),
            )
            self._refresh_project_status(connection, project_id, now=now)
        resolved_cleanup_paths = [
            resolved
            for path in cleanup_paths
            if (resolved := self._payload_managed_path(path)[0])
        ]
        return self.get_project(owner_user_id, project_id), resolved_cleanup_paths

    def get_voice_preferences(self, owner_user_id: str) -> dict[str, Any]:
        owner_id = str(owner_user_id or "").strip()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_user_preferences WHERE owner_user_id=?",
                (owner_id,),
            ).fetchone()
        if row is None:
            return {
                "default_voice_asset_id": None,
                "voice_settings": {
                    "model": "speech-2.8-hd",
                    "speed": 1.0,
                    "volume": 1.0,
                    "pitch": 0,
                    "language_boost": "Chinese",
                    "output_format": "mp3",
                },
            }
        return {
            "default_voice_asset_id": row["default_voice_asset_id"],
            "voice_settings": _object(row["voice_settings_json"], {}),
        }

    def set_voice_preferences(
        self,
        owner_user_id: str,
        *,
        default_voice_asset_id: str,
        voice_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner_id = str(owner_user_id or "").strip()
        voice_id = str(default_voice_asset_id or "").strip()
        if not owner_id or not voice_id:
            raise ValueError("默认声音编号不能为空")
        if voice_settings is not None and not isinstance(voice_settings, dict):
            raise ValueError("声音参数必须是对象")
        current = self.get_voice_preferences(owner_id)
        resolved_settings = (
            voice_settings
            if voice_settings is not None
            else current["voice_settings"]
        )
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO project_user_preferences(
                    owner_user_id, default_voice_asset_id,
                    voice_settings_json, updated_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(owner_user_id) DO UPDATE SET
                    default_voice_asset_id=excluded.default_voice_asset_id,
                    voice_settings_json=excluded.voice_settings_json,
                    updated_at=excluded.updated_at
                """,
                (owner_id, voice_id, _json(resolved_settings), _now()),
            )
        return self.get_voice_preferences(owner_id)

    def configure_item_voice(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        voice_asset_id: str,
    ) -> dict[str, Any]:
        voice_id = str(voice_asset_id or "").strip()
        if not voice_id:
            raise ValueError("声音原型不能为空")
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            settings = _object(item["settings_json"], {})
            if settings.get("voice_asset_id") == voice_id:
                changed = False
            else:
                changed = True
            if changed and item["status"] in ACTIVE_ITEM_STATUSES:
                raise ValueError("当前脚本行正在生成，请等待完成后再更换声音")
            now = _now()
            if changed:
                settings["voice_asset_id"] = voice_id
                settings = _invalidate_auto_music_selection(
                    settings, "AUDIO_VERSION_CHANGED"
                )
            if changed:
                connection.execute(
                    """
                    UPDATE project_items
                    SET settings_json=?, current_audio_asset_id=NULL,
                        current_base_video_asset_id=NULL,
                        current_video_asset_id=NULL, subtitles_json=?,
                        status='DRAFT', updated_at=?
                    WHERE item_id=?
                    """,
                    (_json(settings), _json(_default_subtitles()), now, item_id),
                )
            if changed:
                connection.execute(
                    "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                    (now, project["project_id"]),
                )
                self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def configure_project_voice(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        voice_asset_id: str,
        item_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Atomically apply a saved voice to the requested items and user default.

        Omitting ``item_ids`` preserves the historical whole-project behavior.  A
        supplied list is an explicit scope from the table selection or article-type
        filter; rows outside that scope must keep their current audio and downstream
        bindings.
        """

        voice_id = str(voice_asset_id or "").strip()
        if not voice_id:
            raise ValueError("声音原型不能为空")
        owner_id = str(owner_user_id or "").strip()
        clean_item_ids: list[str] | None = None
        if item_ids is not None:
            clean_item_ids = list(
                dict.fromkeys(str(value or "").strip() for value in item_ids)
            )
            if not clean_item_ids or any(not value for value in clean_item_ids):
                raise ValueError("至少选择一条需要更换声音的脚本行")
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_id, project_id)
            items = connection.execute(
                "SELECT * FROM project_items WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            existing_ids = {str(item["item_id"]) for item in items}
            if clean_item_ids is not None and any(
                item_id not in existing_ids for item_id in clean_item_ids
            ):
                raise KeyError("项目脚本行不存在")
            target_ids = (
                existing_ids if clean_item_ids is None else set(clean_item_ids)
            )
            changed_items = []
            for item in items:
                if str(item["item_id"]) not in target_ids:
                    continue
                settings = _object(item["settings_json"], {})
                if settings.get("voice_asset_id") != voice_id:
                    changed_items.append((item, settings))
            blocked = [
                item["row_key"]
                for item, _settings in changed_items
                if item["status"] in ACTIVE_ITEM_STATUSES
            ]
            if blocked:
                raise ValueError(
                    "以下脚本行已进入声音生成，不能批量更换声音："
                    + "、".join(str(value) for value in blocked[:10])
                )

            now = _now()
            for item, settings in changed_items:
                settings["voice_asset_id"] = voice_id
                settings = _invalidate_auto_music_selection(
                    settings, "AUDIO_VERSION_CHANGED"
                )
                connection.execute(
                    """
                    UPDATE project_items
                    SET settings_json=?, current_audio_asset_id=NULL,
                        current_base_video_asset_id=NULL,
                        current_video_asset_id=NULL, subtitles_json=?,
                        status='DRAFT', updated_at=?
                    WHERE item_id=?
                    """,
                    (_json(settings), _json(_default_subtitles()), now, item["item_id"]),
                )

            project_settings = _object(project["settings_json"], {})
            project_default_changed = (
                project_settings.get("default_voice_asset_id") != voice_id
            )
            project_settings["default_voice_asset_id"] = voice_id
            connection.execute(
                """
                INSERT INTO project_user_preferences(
                    owner_user_id, default_voice_asset_id,
                    voice_settings_json, updated_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(owner_user_id) DO UPDATE SET
                    default_voice_asset_id=excluded.default_voice_asset_id,
                    updated_at=excluded.updated_at
                """,
                (
                    owner_id,
                    voice_id,
                    _json(
                        {
                            "model": "speech-2.8-hd",
                            "speed": 1.0,
                            "volume": 1.0,
                            "pitch": 0,
                            "language_boost": "Chinese",
                            "output_format": "mp3",
                        }
                    ),
                    now,
                ),
            )
            if changed_items or project_default_changed:
                connection.execute(
                    """
                    UPDATE projects
                    SET settings_json=?, revision=revision+1, updated_at=?
                    WHERE project_id=?
                    """,
                    (_json(project_settings), now, project_id),
                )
                self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_id, project_id)

    def projects_using_voice(
        self, owner_user_id: str, voice_asset_id: str
    ) -> list[dict[str, str]]:
        """Return current projects whose default or item settings select a voice."""

        owner_id = str(owner_user_id or "").strip()
        voice_id = str(voice_asset_id or "").strip()
        if not owner_id or not voice_id:
            return []
        used: dict[str, dict[str, str]] = {}
        with self._connect() as connection:
            projects = connection.execute(
                "SELECT project_id, project_no, name, settings_json "
                "FROM projects WHERE owner_user_id=?",
                (owner_id,),
            ).fetchall()
            for project in projects:
                settings = _object(project["settings_json"], {})
                if settings.get("default_voice_asset_id") == voice_id:
                    used[project["project_id"]] = {
                        "project_id": project["project_id"],
                        "project_no": project["project_no"],
                        "name": project["name"],
                    }
            rows = connection.execute(
                """
                SELECT p.project_id, p.project_no, p.name, i.settings_json
                FROM projects p
                JOIN project_items i ON i.project_id=p.project_id
                WHERE p.owner_user_id=?
                """,
                (owner_id,),
            ).fetchall()
            for row in rows:
                settings = _object(row["settings_json"], {})
                if settings.get("voice_asset_id") == voice_id:
                    used[row["project_id"]] = {
                        "project_id": row["project_id"],
                        "project_no": row["project_no"],
                        "name": row["name"],
                    }
        return list(used.values())

    def prepare_item_audio_generation(
        self, owner_user_id: str, project_id: str, item_id: str
    ) -> dict[str, Any]:
        """Start a new audio version without deleting any historical assets."""

        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            if item["status"] in ACTIVE_ITEM_STATUSES:
                raise ValueError("当前脚本行正在生成，不能创建新的声音任务")
            settings = _invalidate_auto_music_selection(
                _object(item["settings_json"], {}), "AUDIO_VERSION_CHANGED"
            )
            now = _now()
            connection.execute(
                """
                UPDATE project_items
                SET settings_json=?, current_audio_asset_id=NULL,
                    current_base_video_asset_id=NULL,
                    current_video_asset_id=NULL,
                    subtitles_json=?, status='DRAFT', updated_at=?
                WHERE item_id=?
                """,
                (_json(settings), _json(_default_subtitles()), now, item_id),
            )
            connection.execute(
                """
                UPDATE projects
                SET revision=revision+1, updated_at=?
                WHERE project_id=?
                """,
                (now, project["project_id"]),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def configure_item_postprocess(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        font_identity: str,
        bgm_identity: str,
        text_color: str,
        bgm_selection_mode: str = "manual",
        music_selection: dict[str, Any] | None = None,
        top_title: dict[str, str] | None = None,
        cover_title: dict[str, str] | None = None,
        layout_profile: str | None = None,
        automatic_bgm_volume: float | None = None,
        bgm_loudness: dict[str, Any] | None = None,
        jianying_template: dict[str, Any] | None = None,
        force_invalidate: bool = False,
        preserve_auto_bgm: bool = False,
    ) -> dict[str, Any]:
        """Save editable subtitle/BGM settings and invalidate only final rendering."""

        clean_font = str(font_identity or "").strip()
        clean_bgm = str(bgm_identity or "").strip()
        clean_bgm_mode = str(bgm_selection_mode or "manual").strip().lower()
        clean_color = str(text_color or "#FFFFFF").strip().upper()
        if not clean_font:
            raise ValueError("字幕字体不能为空")
        if len(clean_color) != 7 or not clean_color.startswith("#"):
            raise ValueError("字幕颜色格式不正确")
        if clean_bgm_mode not in {"auto", "manual"}:
            raise ValueError("BGM 选择模式只能是 auto 或 manual")
        if music_selection is not None and not isinstance(music_selection, dict):
            raise ValueError("音乐选择快照必须是对象")
        if top_title is not None and not isinstance(top_title, dict):
            raise ValueError("顶部固定标题必须是对象")
        if cover_title is not None and not isinstance(cover_title, dict):
            raise ValueError("封面标题必须是对象")
        if automatic_bgm_volume is not None and not (
            0.0 <= float(automatic_bgm_volume) <= 1.0
        ):
            raise ValueError("自动 BGM 音量必须在 0 到 1 之间")
        if bgm_loudness is not None and not isinstance(bgm_loudness, dict):
            raise ValueError("BGM 响度快照必须是对象")
        if jianying_template is not None and not isinstance(jianying_template, dict):
            raise ValueError("剪映模板绑定必须是对象")
        clean_layout_profile = (
            normalize_layout_profile(layout_profile)
            if layout_profile is not None
            else None
        )
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            if item["status"] in ACTIVE_ITEM_STATUSES:
                raise ValueError("当前脚本行正在生成，请等待完成后再修改字幕或 BGM")
            settings = _object(item["settings_json"], {})
            requested = dict(settings.get("postprocess") or {})
            previous_bgm = str(requested.get("bgm_identity") or "")
            preserve_saved_auto_bgm = bool(
                preserve_auto_bgm
                and clean_bgm_mode == "auto"
                and str(requested.get("bgm_selection_mode") or "").strip().lower()
                == "auto"
            )
            effective_bgm = previous_bgm if preserve_saved_auto_bgm else clean_bgm
            requested.update(
                {
                    "font_identity": clean_font,
                    "bgm_identity": effective_bgm,
                    "bgm_selection_mode": clean_bgm_mode,
                    "text_color": clean_color,
                }
            )
            if not effective_bgm:
                requested.pop("bgm_volume", None)
                requested.pop("bgm_loudness", None)
            elif automatic_bgm_volume is not None:
                requested["bgm_volume"] = round(float(automatic_bgm_volume), 4)
                requested["bgm_loudness"] = dict(bgm_loudness or {})
            elif effective_bgm != previous_bgm:
                requested.pop("bgm_volume", None)
                requested.pop("bgm_loudness", None)
            if clean_layout_profile is not None:
                requested["layout_profile"] = clean_layout_profile
            if top_title is not None:
                requested["top_title"] = {
                    "label": str(top_title.get("label") or "").strip(),
                    "headline": str(top_title.get("headline") or "").strip(),
                }
            if cover_title is not None:
                requested["cover_title"] = {
                    "line_1": str(cover_title.get("line_1") or "").strip(),
                    "line_2": str(cover_title.get("line_2") or "").strip(),
                }
            if music_selection is not None:
                requested["music_selection"] = music_selection
            elif clean_bgm_mode == "manual":
                requested["music_selection"] = {
                    "schema": "jyd.project-music-selection.v1",
                    "status": "MANUAL",
                    "selection_source": "manual",
                    "bgm_identity": effective_bgm or None,
                    "reason_code": (
                        "USER_SELECTED" if effective_bgm else "USER_SELECTED_NONE"
                    ),
                }
            elif preserve_saved_auto_bgm and isinstance(
                requested.get("music_selection"), dict
            ):
                pass
            else:
                requested["music_selection"] = {
                    "schema": "jyd.project-music-selection.v1",
                    "status": "NOT_REQUESTED",
                    "selection_source": "ai",
                    "bgm_identity": None,
                    "reason_code": "WAITING_FOR_4B",
                }
            if jianying_template is not None:
                if jianying_template:
                    requested["jianying_template"] = dict(jianying_template)
                else:
                    requested.pop("jianying_template", None)
            if settings.get("postprocess") == requested and not force_invalidate:
                return self.get_project(owner_user_id, project_id)
            settings["postprocess"] = requested
            subtitles = _object(item["subtitles_json"], _default_subtitles())
            subtitles["render_cues"] = []
            subtitles["bound_video_asset_id"] = None
            subtitles["overflow_risk"] = False
            subtitles["review_reason"] = None
            subtitles["status"] = (
                "READY" if subtitles.get("raw_cues") else "NOT_AVAILABLE"
            )
            next_status = "DRAFT"
            if item["current_base_video_asset_id"]:
                next_status = "BASE_VIDEO_READY"
            elif item["current_audio_asset_id"]:
                next_status = "AUDIO_READY"
            now = _now()
            connection.execute(
                """
                UPDATE project_items
                SET settings_json=?, current_video_asset_id=NULL,
                    subtitles_json=?, status=?, updated_at=?
                WHERE item_id=?
                """,
                (_json(settings), _json(subtitles), next_status, now, item_id),
            )
            connection.execute(
                """
                UPDATE projects
                SET revision=revision+1, updated_at=?
                WHERE project_id=?
                """,
                (now, project["project_id"]),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def save_item_auto_music_selection(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        expected_script_sha256: str,
        bgm_identity: str,
        music_selection: dict[str, Any],
    ) -> bool:
        """Persist an AI Top1 choice without overwriting an explicit manual choice."""

        clean_bgm = str(bgm_identity or "").strip()
        if not isinstance(music_selection, dict):
            raise ValueError("音乐选择快照必须是对象")
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            script = str(item["script_text"])
            if _script_sha256(script) != expected_script_sha256:
                return False
            settings = _object(item["settings_json"], {})
            current = settings.get("postprocess")
            postprocess = dict(current) if isinstance(current, dict) else {}
            if postprocess.get("bgm_selection_mode") == "manual":
                return False
            previous_identity = str(postprocess.get("bgm_identity") or "").strip()
            postprocess.update(
                {
                    "bgm_identity": clean_bgm,
                    "bgm_selection_mode": "auto",
                    "music_selection": dict(music_selection),
                }
            )
            if current == postprocess:
                return True
            settings["postprocess"] = postprocess
            now = _now()
            if (
                previous_identity != clean_bgm
                and item["current_video_asset_id"]
                and item["status"] not in ACTIVE_ITEM_STATUSES
            ):
                subtitles = _object(item["subtitles_json"], _default_subtitles())
                subtitles["render_cues"] = []
                subtitles["bound_video_asset_id"] = None
                subtitles["overflow_risk"] = False
                subtitles["review_reason"] = None
                subtitles["status"] = (
                    "READY" if subtitles.get("raw_cues") else "NOT_AVAILABLE"
                )
                next_status = (
                    "BASE_VIDEO_READY"
                    if item["current_base_video_asset_id"]
                    else ("AUDIO_READY" if item["current_audio_asset_id"] else "DRAFT")
                )
                connection.execute(
                    """
                    UPDATE project_items
                    SET settings_json=?, current_video_asset_id=NULL,
                        subtitles_json=?, status=?, updated_at=?
                    WHERE item_id=?
                    """,
                    (_json(settings), _json(subtitles), next_status, now, item_id),
                )
            else:
                connection.execute(
                    "UPDATE project_items SET settings_json=?, updated_at=? WHERE item_id=?",
                    (_json(settings), now, item_id),
                )
            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project["project_id"]),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return True

    def configure_variant_settings(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        settings: dict[str, Any] | None,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist module-6 counts without duplicating the project cover recipe."""

        if settings is not None and not isinstance(settings, dict):
            raise ValueError("变体设置必须是对象")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("逐行变体设置格式不正确")
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            active = connection.execute(
                "SELECT COUNT(*) FROM project_items WHERE project_id=? AND status IN (?, ?)",
                (project_id, "VARIANT_QUEUED", "VARIANT_RUNNING"),
            ).fetchone()[0]
            if active:
                raise ValueError("当前变体任务正在生成，不能修改设置")
            project_settings = _object(project["settings_json"], {})
            if settings is not None:
                project_settings["variants"] = settings
            for raw in items:
                item_id = str(raw.get("item_id") or "").strip()
                item = self._owned_item(connection, project_id, item_id)
                count = int(raw.get("count") or 0)
                if not 1 <= count <= MAX_PROJECT_ITEMS:
                    raise ValueError("每行变体数量必须在 1 到 500 之间")
                item_settings = _object(item["settings_json"], {})
                item_settings["variants"] = {"count": count}
                connection.execute(
                    "UPDATE project_items SET settings_json=?, updated_at=? WHERE item_id=?",
                    (_json(item_settings), _now(), item_id),
                )
            now = _now()
            connection.execute(
                "UPDATE projects SET settings_json=?, revision=revision+1, updated_at=? WHERE project_id=?",
                (_json(project_settings), now, project_id),
            )
        return self.get_project(owner_user_id, project_id)

    def add_asset(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        asset_type: str,
        source_type: str,
        status: str,
        filename: str = "",
        managed_path: str | None = None,
        external_ref: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        make_current: bool = False,
    ) -> dict[str, Any]:
        clean_type = str(asset_type or "").strip().lower()
        if not clean_type:
            raise ValueError("素材类型不能为空")
        clean_source = str(source_type or "").strip().lower()
        if not clean_source:
            raise ValueError("素材来源不能为空")
        clean_status = str(status or "").strip().upper()
        if not clean_status:
            raise ValueError("素材状态不能为空")

        asset_id = uuid.uuid4().hex
        now = _now()
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            version = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1
                    FROM project_assets
                    WHERE item_id=? AND asset_type=?
                    """,
                    (item_id, clean_type),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO project_assets(
                    asset_id, project_id, item_id, asset_type, version,
                    status, source_type, filename, managed_path,
                    external_ref_json, metadata_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    project_id,
                    item_id,
                    clean_type,
                    version,
                    clean_status,
                    clean_source,
                    str(filename or "").strip()[:255],
                    self.encode_managed_path(managed_path) if managed_path else None,
                    _json(external_ref or {}),
                    _json(metadata or {}),
                    now,
                    now,
                ),
            )
            if make_current:
                self._set_current_asset(
                    connection,
                    item,
                    asset_id=asset_id,
                    asset_type=clean_type,
                    source_type=clean_source,
                    asset_status=clean_status,
                    now=now,
                )
            self._refresh_project_status(connection, project_id, now=now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
            return self._asset_payload(row)

    def replace_inputs(
        self,
        owner_user_id: str,
        project_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(items, list) or not items:
            raise ValueError("项目至少需要一条脚本")
        if len(items) > MAX_PROJECT_ITEMS:
            raise ValueError(f"单个项目最多包含 {MAX_PROJECT_ITEMS} 条脚本")
        normalized: list[dict[str, Any]] = []
        row_keys: set[str] = set()
        supplied_ids: set[str] = set()
        for position, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"第 {position} 条脚本格式无效")
            item_id = str(raw.get("item_id") or "").strip()
            if item_id and item_id in supplied_ids:
                raise ValueError("同一脚本行不能重复提交")
            if item_id:
                supplied_ids.add(item_id)
            row_key = _clean_row_key(raw.get("row_key"), position)
            if row_key in row_keys:
                raise ValueError(f"脚本行编号重复: {row_key}")
            row_keys.add(row_key)
            normalized.append(
                {
                    "item_id": item_id or uuid.uuid4().hex,
                    "is_new": not bool(item_id),
                    "row_key": row_key,
                    "position": position,
                    "script_text": _clean_script(raw.get("script_text")),
                    "settings": (
                        dict(raw.get("settings"))
                        if isinstance(raw.get("settings"), dict)
                        else {}
                    ),
                }
            )

        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            existing_rows = connection.execute(
                "SELECT * FROM project_items WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            if any(str(row["status"]) != "DRAFT" for row in existing_rows):
                raise ValueError("项目已进入生成流程，不能替换脚本输入")
            existing = {str(row["item_id"]): row for row in existing_rows}
            unknown = supplied_ids.difference(existing)
            if unknown:
                raise KeyError("项目脚本行不存在")

            now = _now()
            for offset, row in enumerate(existing_rows, start=1):
                connection.execute(
                    "UPDATE project_items SET row_key=?, position=? WHERE item_id=?",
                    (f"__updating__{row['item_id']}", -offset, row["item_id"]),
                )
            retained = {item["item_id"] for item in normalized if not item["is_new"]}
            for item_id in set(existing).difference(retained):
                connection.execute("DELETE FROM project_items WHERE item_id=?", (item_id,))

            for item in normalized:
                if item["is_new"]:
                    connection.execute(
                        """
                        INSERT INTO project_items(
                            item_id, project_id, row_key, position, script_text,
                            status, subtitles_json, content_analysis_json, settings_json,
                            created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?)
                        """,
                        (
                            item["item_id"],
                            project_id,
                            item["row_key"],
                            item["position"],
                            item["script_text"],
                            _json(_default_subtitles()),
                            _json(_default_content_analysis(item["script_text"])),
                            _json(item["settings"]),
                            now,
                            now,
                        ),
                    )
                else:
                    previous = existing[item["item_id"]]
                    script_changed = str(previous["script_text"]) != item["script_text"]
                    previous_settings = _object(previous["settings_json"], {})
                    incoming_source_metadata = item["settings"].get("source_metadata")
                    updated_settings = (
                        {**previous_settings, "source_metadata": incoming_source_metadata}
                        if isinstance(incoming_source_metadata, dict)
                        else previous_settings
                    )
                    connection.execute(
                        """
                        UPDATE project_items
                        SET row_key=?, position=?, script_text=?,
                            content_analysis_json=?, settings_json=?, updated_at=?
                        WHERE item_id=?
                        """,
                        (
                            item["row_key"],
                            item["position"],
                            item["script_text"],
                            (
                                _json(
                                    _default_content_analysis(
                                        item["script_text"],
                                        invalidated_reason="SCRIPT_CHANGED",
                                    )
                                )
                                if script_changed
                                else str(previous["content_analysis_json"] or "{}")
                            ),
                            _json(updated_settings),
                            now,
                            item["item_id"],
                        ),
                    )
            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project["project_id"]),
            )
        return self.get_project(owner_user_id, project_id)

    def append_item(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        row_key: str,
        script_text: str,
    ) -> dict[str, Any]:
        """Append one draft row without rewriting or invalidating existing rows."""

        return self.append_items(
            owner_user_id,
            project_id,
            items=[{"row_key": row_key, "script_text": script_text}],
        )

    def append_items(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically append draft rows without rewriting existing project items."""

        if not isinstance(items, list) or not items:
            raise ValueError("追加脚本不能为空")

        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            existing_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM project_items WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
            )
            if existing_count + len(items) > MAX_PROJECT_ITEMS:
                raise ValueError(f"单个项目最多包含 {MAX_PROJECT_ITEMS} 条脚本")

            existing_row_keys = {
                str(row["row_key"])
                for row in connection.execute(
                    "SELECT row_key FROM project_items WHERE project_id=?",
                    (project_id,),
                ).fetchall()
            }
            normalized: list[dict[str, Any]] = []
            incoming_row_keys: set[str] = set()
            for offset, raw in enumerate(items, start=1):
                if not isinstance(raw, dict):
                    raise ValueError(f"第 {offset} 条追加脚本格式无效")
                position = existing_count + offset
                clean_row_key = _clean_row_key(raw.get("row_key"), position)
                if clean_row_key in existing_row_keys or clean_row_key in incoming_row_keys:
                    raise ValueError(f"脚本行编号重复: {clean_row_key}")
                incoming_row_keys.add(clean_row_key)
                item_settings = (
                    dict(raw.get("settings"))
                    if isinstance(raw.get("settings"), dict)
                    else {}
                )
                normalized.append(
                    {
                        "item_id": uuid.uuid4().hex,
                        "row_key": clean_row_key,
                        "position": position,
                        "script_text": _clean_script(raw.get("script_text")),
                        "settings": item_settings,
                    }
                )

            now = _now()
            images = connection.execute(
                "SELECT * FROM project_input_images WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            mapping = _object(project["settings_json"], {}).get("image_mapping", {})
            strategy = str(mapping.get("strategy") or "loop")
            reuse_count = max(1, int(mapping.get("reuse_count") or 1))
            for item in normalized:
                connection.execute(
                    """
                    INSERT INTO project_items(
                        item_id, project_id, row_key, position, script_text,
                        status, subtitles_json, content_analysis_json, settings_json,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?)
                    """,
                    (
                        item["item_id"],
                        project_id,
                        item["row_key"],
                        item["position"],
                        item["script_text"],
                        _json(_default_subtitles()),
                        _json(_default_content_analysis(item["script_text"])),
                        _json(item["settings"]),
                        now,
                        now,
                    ),
                )
                if images:
                    image_index = (item["position"] - 1) % len(images)
                    if strategy == "count":
                        image_index = (
                            (item["position"] - 1) // reuse_count
                        ) % len(images)
                    stored_item = connection.execute(
                        "SELECT * FROM project_items WHERE item_id=?",
                        (item["item_id"],),
                    ).fetchone()
                    self._assign_input_image(
                        connection,
                        stored_item,
                        images[image_index],
                        mapping_source="append",
                        now=now,
                    )

            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project_id),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def register_input_image(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        managed_path: str,
    ) -> dict[str, Any]:
        image_id = uuid.uuid4().hex
        now = _now()
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            position = int(
                connection.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM project_input_images WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO project_input_images(
                    image_id, project_id, position, filename, content_type,
                    size_bytes, sha256, managed_path, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_id,
                    project_id,
                    position,
                    str(filename or "").strip()[:255],
                    str(content_type or "").strip(),
                    max(0, int(size_bytes)),
                    str(sha256 or "").strip(),
                    self.encode_managed_path(managed_path),
                    now,
                ),
            )
            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project["project_id"]),
            )
            row = connection.execute(
                "SELECT * FROM project_input_images WHERE image_id=?", (image_id,)
            ).fetchone()
            return self._input_image_payload(row)

    def get_input_image(
        self, owner_user_id: str, project_id: str, image_id: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            row = connection.execute(
                "SELECT * FROM project_input_images WHERE image_id=? AND project_id=?",
                (str(image_id or "").strip(), project_id),
            ).fetchone()
            if row is None:
                raise KeyError("项目图片不存在")
            return self._input_image_payload(row)

    def remove_input_image(
        self, owner_user_id: str, project_id: str, image_id: str
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            row = connection.execute(
                "SELECT * FROM project_input_images WHERE image_id=? AND project_id=?",
                (str(image_id or "").strip(), project_id),
            ).fetchone()
            if row is None:
                raise KeyError("项目图片不存在")
            referenced_assets = connection.execute(
                """
                SELECT asset_id, external_ref_json
                FROM project_assets
                WHERE project_id=? AND asset_type='input_image'
                """,
                (project_id,),
            ).fetchall()
            matching_asset_ids = [
                str(asset["asset_id"])
                for asset in referenced_assets
                if _object(asset["external_ref_json"], {}).get("input_image_id")
                == image_id
            ]
            items = connection.execute(
                "SELECT * FROM project_items WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            matching_asset_id_set = set(matching_asset_ids)
            frozen_asset_ids: set[str] = set()
            frozen_image_sha256s: set[str] = set()
            operation_rows = connection.execute(
                """
                SELECT payload_json
                FROM project_operations
                WHERE project_id=? AND operation_type='COMPOSITION_GENERATE'
                """,
                (project_id,),
            ).fetchall()
            for operation_row in operation_rows:
                frozen_asset_id = str(
                    _object(operation_row["payload_json"], {}).get(
                        "input_image_asset_id"
                    )
                    or ""
                )
                if frozen_asset_id:
                    frozen_asset_ids.add(frozen_asset_id)
                frozen_image_sha256 = str(
                    _object(operation_row["payload_json"], {}).get(
                        "input_image_sha256"
                    )
                    or ""
                ).strip().lower()
                if frozen_image_sha256:
                    frozen_image_sha256s.add(frozen_image_sha256)
            if (
                matching_asset_id_set & frozen_asset_ids
                or str(row["sha256"] or "").strip().lower()
                in frozen_image_sha256s
            ):
                raise ValueError("图片已被付费画面任务冻结，不能删除")
            affected_items = [
                item
                for item in items
                if str(item["current_image_asset_id"] or "") in matching_asset_id_set
            ]
            mapping_scope_ids = {
                str(item["item_id"])
                for item in items
                if _object(item["settings_json"], {}).get("image_mapping_target")
                is True
            }
            if mapping_scope_ids and any(
                str(item["item_id"]) not in mapping_scope_ids
                for item in affected_items
            ):
                raise ValueError("图片仍被换图范围外的脚本使用，不能删除")
            if any(str(item["status"]) in ACTIVE_ITEM_STATUSES for item in affected_items):
                raise ValueError("图片正被生成中的脚本使用，请等待该行完成后再删除")

            remaining_images = connection.execute(
                """
                SELECT * FROM project_input_images
                WHERE project_id=? AND image_id<>?
                ORDER BY position
                """,
                (project_id, image_id),
            ).fetchall()
            now = _now()
            mapping = _object(project["settings_json"], {}).get("image_mapping", {})
            strategy = str(mapping.get("strategy") or "loop")
            reuse_count = max(1, int(mapping.get("reuse_count") or 1))
            for item in affected_items:
                if remaining_images:
                    item_position = max(1, int(item["position"]))
                    image_index = (item_position - 1) % len(remaining_images)
                    if strategy == "count":
                        image_index = ((item_position - 1) // reuse_count) % len(
                            remaining_images
                        )
                    self._assign_input_image(
                        connection,
                        item,
                        remaining_images[image_index],
                        mapping_source="delete_fallback",
                        now=now,
                    )
                else:
                    subtitles = _object(item["subtitles_json"], _default_subtitles())
                    subtitles["bound_video_asset_id"] = None
                    if subtitles.get("raw_cues"):
                        subtitles["status"] = "READY"
                    next_status = (
                        "AUDIO_READY" if item["current_audio_asset_id"] else "DRAFT"
                    )
                    connection.execute(
                        """
                        UPDATE project_items
                        SET current_image_asset_id=NULL,
                            current_base_video_asset_id=NULL,
                            current_video_asset_id=NULL,
                            subtitles_json=?, status=?, updated_at=?
                        WHERE item_id=?
                        """,
                        (_json(subtitles), next_status, now, item["item_id"]),
                    )
            for asset_id in matching_asset_ids:
                connection.execute(
                    "DELETE FROM project_assets WHERE asset_id=?", (asset_id,)
                )
            connection.execute("DELETE FROM project_input_images WHERE image_id=?", (image_id,))
            remaining = connection.execute(
                "SELECT image_id FROM project_input_images WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            for offset, remaining_row in enumerate(remaining, start=1):
                connection.execute(
                    "UPDATE project_input_images SET position=? WHERE image_id=?",
                    (-offset, remaining_row["image_id"]),
                )
            for position, remaining_row in enumerate(remaining, start=1):
                connection.execute(
                    "UPDATE project_input_images SET position=? WHERE image_id=?",
                    (position, remaining_row["image_id"]),
                )
            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project["project_id"]),
            )
            self._refresh_project_status(connection, project_id, now=now)
            return self._input_image_payload(row)

    def apply_image_strategy(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        strategy: str,
        reuse_count: int = 1,
        image_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        clean_strategy = str(strategy or "").strip().lower()
        if clean_strategy not in {"count", "loop"}:
            raise ValueError("图片分配策略必须是 count 或 loop")
        safe_count = int(reuse_count)
        if safe_count < 1 or safe_count > 100:
            raise ValueError("每张图片复用次数必须在 1 到 100 之间")
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            all_images = connection.execute(
                "SELECT * FROM project_input_images WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            if not all_images:
                raise ValueError("请先上传至少一张图片")
            if image_ids is None:
                images = all_images
            else:
                clean_image_ids = list(
                    dict.fromkeys(str(value or "").strip() for value in image_ids)
                )
                if not clean_image_ids or any(not value for value in clean_image_ids):
                    raise ValueError("本次重新分配至少需要一张有效图片")
                images_by_id = {
                    str(row["image_id"]): row for row in all_images
                }
                if any(image_id not in images_by_id for image_id in clean_image_ids):
                    raise ValueError("本次重新分配包含不属于当前项目的图片")
                images = [images_by_id[image_id] for image_id in clean_image_ids]
            items = connection.execute(
                "SELECT * FROM project_items WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            scoped_items = [
                item
                for item in items
                if _object(item["settings_json"], {}).get("image_mapping_target")
                is True
            ]
            mapping_items = scoped_items or items
            if any(
                str(item["status"]) not in IMAGE_EDITABLE_ITEM_STATUSES
                for item in mapping_items
            ):
                raise ValueError("换图范围内有画面任务正在生成，请等待完成后再重新分配")
            now = _now()
            for index, item in enumerate(mapping_items):
                image_index = index % len(images)
                if clean_strategy == "count":
                    image_index = (index // safe_count) % len(images)
                self._assign_input_image(
                    connection,
                    item,
                    images[image_index],
                    mapping_source="strategy",
                    now=now,
                )
            settings = _object(project["settings_json"], {})
            settings["image_mapping"] = {
                "strategy": clean_strategy,
                "reuse_count": safe_count,
                "image_ids": [str(image["image_id"]) for image in images],
            }
            connection.execute(
                """
                UPDATE projects
                SET settings_json=?, revision=revision+1, updated_at=?
                WHERE project_id=?
                """,
                (_json(settings), now, project_id),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def set_image_mapping_scope(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        item_ids: list[str],
    ) -> dict[str, Any]:
        clean_item_ids = list(
            dict.fromkeys(str(value or "").strip() for value in item_ids)
        )
        if any(not value for value in clean_item_ids):
            raise ValueError("换图范围包含无效脚本行 ID")
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            rows = connection.execute(
                "SELECT * FROM project_items WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            existing_ids = {str(row["item_id"]) for row in rows}
            if any(item_id not in existing_ids for item_id in clean_item_ids):
                raise KeyError("项目脚本行不存在")
            target_ids = set(clean_item_ids)
            now = _now()
            for row in rows:
                settings = _object(row["settings_json"], {})
                if str(row["item_id"]) in target_ids:
                    settings["image_mapping_target"] = True
                else:
                    settings.pop("image_mapping_target", None)
                connection.execute(
                    "UPDATE project_items SET settings_json=?, updated_at=? WHERE item_id=?",
                    (_json(settings), now, row["item_id"]),
                )
            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project["project_id"]),
            )
        return self.get_project(owner_user_id, project_id)

    def replace_item_image(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        image_id: str,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            if item["status"] in ACTIVE_ITEM_STATUSES:
                raise ValueError("当前脚本行正在生成，请等待完成后再替换图片")
            image = connection.execute(
                "SELECT * FROM project_input_images WHERE image_id=? AND project_id=?",
                (str(image_id or "").strip(), project_id),
            ).fetchone()
            if image is None:
                raise KeyError("项目图片不存在")
            now = _now()
            self._assign_input_image(
                connection, item, image, mapping_source="manual", now=now
            )
            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project["project_id"]),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def invalidate_item_composition(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Detach a known-stale current video without deleting its history or file."""

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("基础视频失效原因不能为空")
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            if item["status"] in ACTIVE_ITEM_STATUSES:
                raise ValueError("当前脚本行正在生成，不能撤回基础视频")
            subtitles = _object(item["subtitles_json"], _default_subtitles())
            subtitles["render_cues"] = []
            subtitles["bound_video_asset_id"] = None
            subtitles["overflow_risk"] = False
            subtitles["review_reason"] = None
            subtitles["status"] = (
                "READY" if subtitles.get("raw_cues") else "NOT_AVAILABLE"
            )
            settings = _object(item["settings_json"], {})
            settings["composition_invalidated_reason"] = clean_reason
            next_status = "AUDIO_READY" if item["current_audio_asset_id"] else "DRAFT"
            now = _now()
            connection.execute(
                """
                UPDATE project_items
                SET current_base_video_asset_id=NULL, current_video_asset_id=NULL,
                    subtitles_json=?, settings_json=?, status=?, updated_at=?
                WHERE item_id=?
                """,
                (
                    _json(subtitles),
                    _json(settings),
                    next_status,
                    now,
                    item_id,
                ),
            )
            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project["project_id"]),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def set_digital_human_resolution(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        resolution: str,
    ) -> dict[str, Any]:
        """Persist the project default without hiding already completed videos."""

        clean_resolution = str(resolution or "").strip()
        try:
            if int(clean_resolution) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("数字人最长边分辨率必须是正整数") from exc
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            active_item = connection.execute(
                "SELECT row_key FROM project_items WHERE project_id=? "
                f"AND status IN ({','.join('?' for _ in ACTIVE_ITEM_STATUSES)}) LIMIT 1",
                (project_id, *sorted(ACTIVE_ITEM_STATUSES)),
            ).fetchone()
            if active_item is not None:
                raise ValueError(
                    f"任务 {active_item['row_key']} 正在生成，请完成后再修改分辨率"
                )
            project_settings = _object(project["settings_json"], {})
            digital_human = project_settings.get("digital_human")
            if not isinstance(digital_human, dict):
                digital_human = {}
            current_resolution = str(digital_human.get("resolution") or "1024")
            if current_resolution == clean_resolution:
                return self._project_payload(connection, project_id)
            project_settings["digital_human"] = {
                **digital_human,
                "resolution": clean_resolution,
            }
            now = _now()
            connection.execute(
                """
                UPDATE projects
                SET settings_json=?, revision=revision+1, updated_at=?
                WHERE project_id=?
                """,
                (_json(project_settings), now, project_id),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def set_h3_configuration(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        identity_image_ids: list[str],
        defaults: dict[str, Any],
    ) -> dict[str, Any]:
        """Freeze project-level H3 inputs while retaining paid history."""

        clean_ids = list(
            dict.fromkeys(str(value or "").strip() for value in identity_image_ids)
        )
        clean_ids = [value for value in clean_ids if value]
        if len(clean_ids) > 4:
            raise ValueError("H3 人物参考图最多选择 4 张")
        if not isinstance(defaults, dict):
            raise ValueError("H3 默认参数必须是对象")
        continuity = str(
            defaults.get("continuity_mode") or "loop_anchor"
        ).strip()
        if continuity not in {"loop_anchor", "fast", "soft_chain"}:
            raise ValueError("H3 衔接模式无效")
        aspect = str(
            defaults.get("aspect_ratio") or "9:16 (Portrait Widescreen)"
        ).strip()
        if aspect not in {
            "9:16 (Portrait Widescreen)",
            "16:9 (Widescreen)",
        }:
            raise ValueError("H3 画面比例无效")
        try:
            megapixels = float(defaults.get("megapixels", 1.0))
            tail = float(defaults.get("generation_tail_seconds", 0.1))
        except (TypeError, ValueError) as exc:
            raise ValueError("H3 清晰度或时长余量格式错误") from exc
        if not 0.2 <= megapixels <= 2.0:
            raise ValueError("H3 清晰度必须在 0.2–2.0 MP")
        if not 0 <= tail <= 1:
            raise ValueError("H3 时长余量必须在 0–1 秒")
        normalized_defaults = {
            "continuity_mode": continuity,
            "aspect_ratio": aspect,
            "megapixels": round(megapixels, 2),
            "multiple": 32,
            "generation_tail_seconds": round(tail, 3),
        }
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            if clean_ids:
                placeholders = ",".join("?" for _ in clean_ids)
                found = {
                    str(row["image_id"])
                    for row in connection.execute(
                        f"SELECT image_id FROM project_input_images "
                        f"WHERE project_id=? AND image_id IN ({placeholders})",
                        (project_id, *clean_ids),
                    ).fetchall()
                }
                if found != set(clean_ids):
                    raise KeyError("H3 人物参考图不存在")
            settings = _object(project["settings_json"], {})
            previous = (
                settings.get("h3") if isinstance(settings.get("h3"), dict) else {}
            )
            current_contract = {
                "identity_image_ids": list(previous.get("identity_image_ids") or []),
                "defaults": dict(previous.get("defaults") or {}),
            }
            next_contract = {
                "identity_image_ids": clean_ids,
                "defaults": normalized_defaults,
            }
            if (
                settings.get("generation_mode") == "minimax_h3_ref2va"
                and current_contract == next_contract
            ):
                return self._project_payload(connection, project_id)
            rows = connection.execute(
                "SELECT * FROM project_items WHERE project_id=?", (project_id,)
            ).fetchall()
            if any(str(row["status"]) in ACTIVE_ITEM_STATUSES for row in rows):
                raise ValueError("H3 批次正在生成，完成后才能修改人物图或批次参数")
            if current_contract == next_contract:
                previous_mode = str(settings.get("generation_mode") or "")
                settings["generation_mode"] = "minimax_h3_ref2va"
                now = _now()
                connection.execute(
                    "UPDATE projects SET settings_json=?, revision=revision+1, "
                    "updated_at=? WHERE project_id=?",
                    (_json(settings), now, project_id),
                )
                for item in rows:
                    item_settings = _object(item["settings_json"], {})
                    mode_views = item_settings.get("generation_mode_views")
                    if not isinstance(mode_views, dict):
                        mode_views = {}
                    if previous_mode and (
                        item["current_audio_asset_id"]
                        or item["current_base_video_asset_id"]
                    ):
                        mode_views[previous_mode] = {
                            "audio_asset_id": item["current_audio_asset_id"],
                            "base_video_asset_id": item["current_base_video_asset_id"],
                            "subtitles": _object(
                                item["subtitles_json"], _default_subtitles()
                            ),
                        }
                    saved_h3 = mode_views.get("minimax_h3_ref2va")
                    if not isinstance(saved_h3, dict):
                        saved_h3 = {}

                    def h3_asset(asset_id: Any, asset_type: str):
                        clean_asset_id = str(asset_id or "").strip()
                        if not clean_asset_id:
                            return None
                        return connection.execute(
                            "SELECT * FROM project_assets WHERE asset_id=? "
                            "AND item_id=? AND asset_type=? AND status='READY' "
                            "AND source_type IN ('h3', 'h3_handoff')",
                            (clean_asset_id, item["item_id"], asset_type),
                        ).fetchone()

                    h3_audio = h3_asset(saved_h3.get("audio_asset_id"), "audio")
                    if h3_audio is None:
                        h3_audio = connection.execute(
                            "SELECT * FROM project_assets WHERE item_id=? "
                            "AND asset_type='audio' AND status='READY' "
                            "AND source_type IN ('h3', 'h3_handoff') "
                            "ORDER BY version DESC LIMIT 1",
                            (item["item_id"],),
                        ).fetchone()
                    h3_base = h3_asset(
                        saved_h3.get("base_video_asset_id"), "base_video"
                    )
                    if h3_base is None:
                        h3_base = connection.execute(
                            "SELECT * FROM project_assets WHERE item_id=? "
                            "AND asset_type='base_video' AND status='READY' "
                            "AND source_type IN ('h3', 'h3_handoff') "
                            "ORDER BY version DESC LIMIT 1",
                            (item["item_id"],),
                        ).fetchone()
                    selected_audio_id = h3_audio["asset_id"] if h3_audio else None
                    selected_base_id = h3_base["asset_id"] if h3_base else None
                    saved_subtitles = saved_h3.get("subtitles")
                    if isinstance(saved_subtitles, dict):
                        subtitles = dict(saved_subtitles)
                    else:
                        audio_metadata = (
                            _object(h3_audio["metadata_json"], {}) if h3_audio else {}
                        )
                        saved_cues = audio_metadata.get("subtitle_cues")
                        subtitles = _default_subtitles()
                        subtitles.update(
                            {
                                "source": "h3_generated_audio",
                                "raw_cues": (
                                    [
                                        dict(value)
                                        for value in saved_cues
                                        if isinstance(value, dict)
                                    ]
                                    if isinstance(saved_cues, list)
                                    else []
                                ),
                                "bound_audio_asset_id": selected_audio_id,
                                "status": (
                                    "READY"
                                    if isinstance(saved_cues, list)
                                    else (
                                        "PENDING_TIMESTAMPS"
                                        if selected_audio_id
                                        else "NOT_AVAILABLE"
                                    )
                                ),
                            }
                        )
                    subtitles["bound_audio_asset_id"] = selected_audio_id
                    subtitles["bound_video_asset_id"] = selected_base_id
                    item_settings["generation_mode_views"] = mode_views
                    next_status = (
                        "BASE_VIDEO_READY"
                        if selected_base_id
                        else ("AUDIO_READY" if selected_audio_id else "DRAFT")
                    )
                    connection.execute(
                        "UPDATE project_items SET current_audio_asset_id=?, "
                        "current_base_video_asset_id=?, current_video_asset_id=NULL, "
                        "subtitles_json=?, settings_json=?, status=?, updated_at=? "
                        "WHERE item_id=?",
                        (
                            selected_audio_id,
                            selected_base_id,
                            _json(subtitles),
                            _json(item_settings),
                            next_status,
                            now,
                            item["item_id"],
                        ),
                    )
                self._refresh_project_status(connection, project_id, now=now)
                return self._project_payload(connection, project_id)
            settings["generation_mode"] = "minimax_h3_ref2va"
            settings["h3"] = {
                "schema": "jyd.project-h3.v1",
                **next_contract,
                "config_version": int(previous.get("config_version") or 0) + 1,
                "remote_batch_id": None,
                "remote_status": None,
                "fee_snapshot": None,
                "prepare_key": None,
            }
            now = _now()
            connection.execute(
                "UPDATE projects SET settings_json=?, revision=revision+1, "
                "updated_at=? WHERE project_id=?",
                (_json(settings), now, project_id),
            )
            for item in rows:
                item_settings = _object(item["settings_json"], {})
                h3 = (
                    item_settings.get("h3")
                    if isinstance(item_settings.get("h3"), dict)
                    else {}
                )
                item_settings["h3"] = {
                    **h3,
                    "remote_item_id": None,
                    "remote_status": None,
                    "invalidated_reason": "H3_PROJECT_INPUT_CHANGED",
                }
                subtitles = _object(item["subtitles_json"], _default_subtitles())
                subtitles["render_cues"] = []
                subtitles["bound_video_asset_id"] = None
                subtitles["status"] = (
                    "READY" if subtitles.get("raw_cues") else "NOT_AVAILABLE"
                )
                next_status = (
                    "AUDIO_READY" if item["current_audio_asset_id"] else "DRAFT"
                )
                connection.execute(
                    """
                    UPDATE project_items
                    SET current_base_video_asset_id=NULL, current_video_asset_id=NULL,
                        subtitles_json=?, settings_json=?, status=?, updated_at=?
                    WHERE item_id=?
                    """,
                    (
                        _json(subtitles),
                        _json(item_settings),
                        next_status,
                        now,
                        item["item_id"],
                    ),
                )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def set_h3_item_overrides(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(overrides, dict):
            raise ValueError("H3 行级覆盖必须是对象")
        normalized: dict[str, Any] = {}
        direction = str(overrides.get("user_direction") or "").strip()
        if len(direction) > 1000:
            raise ValueError("H3 本行补充方向不能超过 1000 字")
        if direction:
            normalized["user_direction"] = direction
        continuity = str(overrides.get("continuity_mode") or "").strip()
        if continuity:
            if continuity not in {"loop_anchor", "fast", "soft_chain"}:
                raise ValueError("H3 本行衔接模式无效")
            normalized["continuity_mode"] = continuity
        aspect = str(overrides.get("aspect_ratio") or "").strip()
        if aspect:
            if aspect not in {
                "9:16 (Portrait Widescreen)",
                "16:9 (Widescreen)",
            }:
                raise ValueError("H3 本行画面比例无效")
            normalized["aspect_ratio"] = aspect
        raw_mp = overrides.get("megapixels")
        if raw_mp not in {None, ""}:
            try:
                megapixels = float(raw_mp)
            except (TypeError, ValueError) as exc:
                raise ValueError("H3 本行清晰度格式错误") from exc
            if not 0.2 <= megapixels <= 2.0:
                raise ValueError("H3 本行清晰度必须在 0.2–2.0 MP")
            normalized["megapixels"] = round(megapixels, 2)

        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            item_settings = _object(item["settings_json"], {})
            h3 = item_settings.get("h3") if isinstance(item_settings.get("h3"), dict) else {}
            if dict(h3.get("overrides") or {}) == normalized:
                return self._project_payload(connection, project_id)
            project_settings = _object(project["settings_json"], {})
            project_h3 = project_settings.get("h3") if isinstance(project_settings.get("h3"), dict) else {}
            if str(item["status"]) in ACTIVE_ITEM_STATUSES:
                raise ValueError("当前脚本行正在生成，不能修改 H3 行级参数")
            if str(h3.get("remote_status") or "").upper() in {
                "ACTIVE", "QUEUED", "RUNNING", "PENDING", "UPLOADING", "SUBMITTED",
                "WAITING_DEPENDENCY", "WAITING_REGENERATION_DEPENDENCY",
            }:
                raise ValueError("当前脚本行正在生成，不能修改 H3 行级参数")
            item_settings["h3"] = {
                **h3,
                "overrides": normalized,
                "remote_item_id": None,
                "remote_status": None,
                "invalidated_reason": "H3_ITEM_OVERRIDE_CHANGED",
            }
            subtitles = _object(item["subtitles_json"], _default_subtitles())
            subtitles["render_cues"] = []
            subtitles["bound_video_asset_id"] = None
            subtitles["status"] = "READY" if subtitles.get("raw_cues") else "NOT_AVAILABLE"
            now = _now()
            next_status = "AUDIO_READY" if item["current_audio_asset_id"] else "DRAFT"
            connection.execute(
                """
                UPDATE project_items
                SET current_base_video_asset_id=NULL, current_video_asset_id=NULL,
                    subtitles_json=?, settings_json=?, status=?, updated_at=?
                WHERE item_id=?
                """,
                (_json(subtitles), _json(item_settings), next_status, now, item_id),
            )
            project_settings["generation_mode"] = "minimax_h3_ref2va"
            # Other rows may belong to active H3 batches. Row-level edits must
            # not erase their project-level batch registry or polling state.
            project_settings["h3"] = {
                "schema": "jyd.project-h3.v1",
                **project_h3,
            }
            connection.execute(
                "UPDATE projects SET settings_json=?, revision=revision+1, updated_at=? WHERE project_id=?",
                (_json(project_settings), now, project_id),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def set_generation_mode(
        self, owner_user_id: str, project_id: str, mode: str
    ) -> dict[str, Any]:
        clean_mode = str(mode or "").strip()
        if clean_mode not in {
            "runninghub_digital_human",
            "minimax_h3_ref2va",
            "ltx_lip_sync",
        }:
            raise ValueError("画面生成方式无效")
        if clean_mode == "minimax_h3_ref2va":
            raise ValueError("切换 H3 时请同时保存 H3 参数")
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            rows = connection.execute(
                "SELECT * FROM project_items WHERE project_id=?", (project_id,)
            ).fetchall()
            settings = _object(project["settings_json"], {})
            previous_mode = str(settings.get("generation_mode") or "")
            active_statuses = {
                str(row["status"])
                for row in rows
                if str(row["status"]) in ACTIVE_ITEM_STATUSES
            }
            h3_can_continue_in_background = (
                previous_mode == "minimax_h3_ref2va"
                and active_statuses
                and active_statuses <= {"H3_QUEUED", "H3_RUNNING"}
            )
            if active_statuses and not h3_can_continue_in_background:
                raise ValueError("当前有任务正在生成，完成后才能切换画面生成方式")
            if settings.get("generation_mode") == clean_mode:
                return self._project_payload(connection, project_id)
            settings["generation_mode"] = clean_mode
            now = _now()
            connection.execute(
                "UPDATE projects SET settings_json=?, revision=revision+1, updated_at=? "
                "WHERE project_id=?",
                (_json(settings), now, project_id),
            )
            for item in rows:
                subtitles = _object(item["subtitles_json"], _default_subtitles())
                item_settings = _object(item["settings_json"], {})
                mode_views = item_settings.get("generation_mode_views")
                if not isinstance(mode_views, dict):
                    mode_views = {}
                if previous_mode and (
                    item["current_audio_asset_id"]
                    or item["current_base_video_asset_id"]
                ):
                    mode_views[previous_mode] = {
                        "audio_asset_id": item["current_audio_asset_id"],
                        "base_video_asset_id": item["current_base_video_asset_id"],
                        "subtitles": dict(subtitles),
                    }
                item_settings["generation_mode_views"] = mode_views
                selected_audio_id = item["current_audio_asset_id"]
                if clean_mode in {
                    "runninghub_digital_human",
                    "ltx_lip_sync",
                }:
                    minimax_audio = connection.execute(
                        "SELECT * FROM project_assets WHERE item_id=? "
                        "AND asset_type='audio' AND source_type='minimax' "
                        "AND status='READY' ORDER BY version DESC LIMIT 1",
                        (item["item_id"],),
                    ).fetchone()
                    selected_audio_id = (
                        minimax_audio["asset_id"] if minimax_audio is not None else None
                    )
                    if subtitles.get("bound_audio_asset_id") != selected_audio_id:
                        audio_metadata = (
                            _object(minimax_audio["metadata_json"], {})
                            if minimax_audio is not None
                            else {}
                        )
                        saved_cues = audio_metadata.get("subtitle_cues")
                        subtitles["source"] = (
                            "minimax_timestamps" if isinstance(saved_cues, list) else None
                        )
                        subtitles["raw_cues"] = (
                            [dict(value) for value in saved_cues if isinstance(value, dict)]
                            if isinstance(saved_cues, list)
                            else []
                        )
                        subtitles["bound_audio_asset_id"] = selected_audio_id
                subtitles["render_cues"] = []
                subtitles["bound_video_asset_id"] = None
                subtitles["status"] = (
                    "READY"
                    if subtitles.get("raw_cues")
                    else (
                        "PENDING_TIMESTAMPS"
                        if selected_audio_id
                        else "NOT_AVAILABLE"
                    )
                )
                next_status = (
                    "AUDIO_READY" if selected_audio_id else "DRAFT"
                )
                connection.execute(
                    "UPDATE project_items SET current_audio_asset_id=?, "
                    "current_base_video_asset_id=NULL, "
                    "current_video_asset_id=NULL, subtitles_json=?, settings_json=?, "
                    "status=?, updated_at=? "
                    "WHERE item_id=?",
                    (
                        selected_audio_id,
                        _json(subtitles),
                        _json(item_settings),
                        next_status,
                        now,
                        item["item_id"],
                    ),
                )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def add_h3_reference_video(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        filename: str,
        managed_path: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Version one row reference video and invalidate H3/downstream only."""

        clean_name = Path(str(filename or "")).name.strip()
        clean_path = self.encode_managed_path(managed_path)
        if not clean_name or not clean_path:
            raise ValueError("H3 参考视频信息不完整")
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            project_settings = _object(project["settings_json"], {})
            project_h3 = project_settings.get("h3") if isinstance(project_settings.get("h3"), dict) else {}
            if str(item["status"]) in ACTIVE_ITEM_STATUSES:
                raise ValueError("当前脚本行正在生成，不能替换 H3 参考视频")
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM project_assets "
                    "WHERE item_id=? AND asset_type='h3_reference_video'",
                    (item_id,),
                ).fetchone()[0]
            )
            asset_id = uuid.uuid4().hex
            now = _now()
            connection.execute(
                """
                INSERT INTO project_assets(
                    asset_id, project_id, item_id, asset_type, version,
                    status, source_type, filename, managed_path,
                    external_ref_json, metadata_json, created_at, updated_at
                ) VALUES(?, ?, ?, 'h3_reference_video', ?, 'READY',
                         'project_upload', ?, ?, '{}', ?, ?, ?)
                """,
                (
                    asset_id,
                    project_id,
                    item_id,
                    version,
                    clean_name,
                    clean_path,
                    _json(metadata),
                    now,
                    now,
                ),
            )
            item_settings = _object(item["settings_json"], {})
            h3 = (
                item_settings.get("h3")
                if isinstance(item_settings.get("h3"), dict)
                else {}
            )
            if str(h3.get("remote_status") or "").upper() in {
                "ACTIVE", "QUEUED", "RUNNING", "PENDING", "UPLOADING", "SUBMITTED",
                "WAITING_DEPENDENCY", "WAITING_REGENERATION_DEPENDENCY",
            }:
                raise ValueError("当前脚本行正在生成，不能替换 H3 参考视频")
            item_settings["h3"] = {
                **h3,
                "reference_video_asset_id": asset_id,
                "reference_video_version": version,
                "remote_item_id": None,
                "remote_status": None,
                "invalidated_reason": "H3_REFERENCE_VIDEO_CHANGED",
            }
            subtitles = _object(item["subtitles_json"], _default_subtitles())
            subtitles["render_cues"] = []
            subtitles["bound_video_asset_id"] = None
            subtitles["status"] = (
                "READY" if subtitles.get("raw_cues") else "NOT_AVAILABLE"
            )
            next_status = "AUDIO_READY" if item["current_audio_asset_id"] else "DRAFT"
            connection.execute(
                """
                UPDATE project_items
                SET current_base_video_asset_id=NULL, current_video_asset_id=NULL,
                    subtitles_json=?, settings_json=?, status=?, updated_at=?
                WHERE item_id=?
                """,
                (_json(subtitles), _json(item_settings), next_status, now, item_id),
            )
            settings = project_settings
            h3_project = project_h3
            settings["generation_mode"] = "minimax_h3_ref2va"
            # Preserve batches owned by other rows while invalidating only the
            # edited row's H3 input/output binding.
            settings["h3"] = {
                "schema": "jyd.project-h3.v1",
                **h3_project,
            }
            connection.execute(
                "UPDATE projects SET settings_json=?, revision=revision+1, "
                "updated_at=? WHERE project_id=?",
                (_json(settings), now, project_id),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def mark_h3_audio_reviewed(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        asset_id: str,
        reviewed_at: str,
    ) -> dict[str, Any]:
        """Persist the explicit cloud review on the exact MiniMax audio asset."""

        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            self._owned_item(connection, project_id, item_id)
            row = connection.execute(
                """
                SELECT metadata_json FROM project_assets
                WHERE asset_id=? AND project_id=? AND item_id=?
                  AND asset_type='audio' AND source_type='minimax'
                """,
                (asset_id, project_id, item_id),
            ).fetchone()
            if row is None:
                raise KeyError("MiniMax 声音素材不存在或已失效")
            metadata = _object(row["metadata_json"], {})
            metadata["provider_status"] = "SUCCESS"
            metadata["h3_reviewed_at"] = str(reviewed_at or _now())
            now = _now()
            connection.execute(
                "UPDATE project_assets SET metadata_json=?, updated_at=? WHERE asset_id=?",
                (_json(metadata), now, asset_id),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def repair_legacy_h3_script_binding(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        segment_signature: str,
    ) -> bool:
        """Bind legacy current H3 assets to the immutable item script.

        Early H3 handoff versions did not persist the script fingerprint on the
        authoritative audio. Subtitle post-processing therefore had to discard
        semantic script units and fall back to coarse H3 segment cues. This
        metadata-only repair is intentionally limited to the current matching
        H3 audio/base pair. Any derived preview is detached but kept in history.
        """

        clean_signature = str(segment_signature or "").strip()
        if not clean_signature:
            raise ValueError("H3 分段签名不能为空")
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            if str(item["status"] or "") in ACTIVE_ITEM_STATUSES:
                return False
            current_ids = {
                "audio": str(item["current_audio_asset_id"] or ""),
                "base_video": str(item["current_base_video_asset_id"] or ""),
            }
            if not all(current_ids.values()):
                return False
            rows = connection.execute(
                """
                SELECT * FROM project_assets
                WHERE project_id=? AND item_id=? AND asset_id IN (?, ?)
                """,
                (
                    project_id,
                    item_id,
                    current_ids["audio"],
                    current_ids["base_video"],
                ),
            ).fetchall()
            assets = {str(row["asset_type"]): row for row in rows}
            if set(assets) != {"audio", "base_video"}:
                return False

            script_text = str(item["script_text"] or "")
            script_sha256 = hashlib.sha256(script_text.encode("utf-8")).hexdigest()
            script_length = len(script_text)
            metadata_by_type: dict[str, dict[str, Any]] = {}
            repair_required = False
            for asset_type, row in assets.items():
                metadata = _object(row["metadata_json"], {})
                if (
                    str(row["source_type"] or "").lower() != "h3"
                    or str(row["status"] or "").upper() != "READY"
                    or metadata.get("h3_segment_signature") != clean_signature
                ):
                    return False
                existing_sha256 = str(metadata.get("script_sha256") or "").strip()
                existing_length = metadata.get("script_length")
                if existing_sha256 and existing_sha256 != script_sha256:
                    return False
                if existing_length not in (None, "", script_length):
                    return False
                if existing_sha256 != script_sha256 or existing_length != script_length:
                    repair_required = True
                metadata_by_type[asset_type] = metadata
            if not repair_required:
                return False

            now = _now()
            for asset_type, row in assets.items():
                metadata = metadata_by_type[asset_type]
                metadata["script_sha256"] = script_sha256
                metadata["script_length"] = script_length
                metadata["script_binding_repaired_at"] = now
                connection.execute(
                    "UPDATE project_assets SET metadata_json=?, updated_at=? WHERE asset_id=?",
                    (_json(metadata), now, row["asset_id"]),
                )

            subtitles = _object(item["subtitles_json"], _default_subtitles())
            subtitles["render_cues"] = []
            subtitles["bound_video_asset_id"] = current_ids["base_video"]
            subtitles["overflow_risk"] = False
            subtitles["review_reason"] = None
            subtitles.pop("semantic_mapping", None)
            subtitles["status"] = (
                "READY" if subtitles.get("raw_cues") else "PENDING_TIMESTAMPS"
            )
            settings = _object(item["settings_json"], {})
            settings["composition_invalidated_reason"] = "H3_SCRIPT_BINDING_REPAIRED"
            connection.execute(
                """
                UPDATE project_items
                SET current_video_asset_id=NULL, subtitles_json=?, settings_json=?,
                    status='BASE_VIDEO_READY', updated_at=?
                WHERE item_id=?
                """,
                (_json(subtitles), _json(settings), now, item_id),
            )
            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project["project_id"]),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return True

    def set_h3_batch_snapshot(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        prepare_key: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            raise ValueError("H3 批次快照格式错误")
        remote_batch_id = str(snapshot.get("batch_id") or "").strip()
        if not remote_batch_id:
            raise ValueError("H3 批次编号缺失")
        remote_status = str(snapshot.get("status") or "").strip().upper()
        remote_items = {
            str(value.get("row_id") or ""): value
            for value in snapshot.get("items", [])
            if isinstance(value, dict)
        }
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            settings = _object(project["settings_json"], {})
            h3 = settings.get("h3") if isinstance(settings.get("h3"), dict) else {}
            batches = [
                dict(value)
                for value in h3.get("batches", [])
                if isinstance(value, dict)
                and str(value.get("batch_id") or "").strip()
            ]
            legacy_batch_id = str(h3.get("remote_batch_id") or "").strip()
            if legacy_batch_id and not any(
                str(value.get("batch_id") or "") == legacy_batch_id
                for value in batches
            ):
                batches.append(
                    {
                        "batch_id": legacy_batch_id,
                        "prepare_key": str(h3.get("prepare_key") or ""),
                        "status": str(h3.get("remote_status") or "").upper(),
                        "fee_snapshot": h3.get("fee_snapshot"),
                        "last_synced_at": h3.get("last_synced_at"),
                    }
                )
            existing = next(
                (
                    value
                    for value in batches
                    if str(value.get("batch_id") or "") == remote_batch_id
                ),
                {},
            )
            resolved_prepare_key = str(
                prepare_key
                or existing.get("prepare_key")
                or (
                    h3.get("prepare_key")
                    if legacy_batch_id == remote_batch_id
                    else ""
                )
                or ""
            )
            now = _now()
            batch_record = {
                **existing,
                "batch_id": remote_batch_id,
                "prepare_key": resolved_prepare_key,
                "status": remote_status,
                "fee_snapshot": snapshot.get("fee_snapshot"),
                "row_ids": list(remote_items),
                "last_synced_at": now,
            }
            batches = [
                value
                for value in batches
                if str(value.get("batch_id") or "") != remote_batch_id
            ]
            batches.append(batch_record)
            # Keep enough terminal history for diagnosis while preventing an
            # unbounded settings payload. Open batches are never discarded.
            if len(batches) > 100:
                open_batches = [
                    value
                    for value in batches
                    if str(value.get("status") or "").upper()
                    in {"AWAITING_COST_CONFIRMATION", "ACTIVE", "QUEUED", "RUNNING"}
                ]
                open_ids = {
                    str(value.get("batch_id") or "") for value in open_batches
                }
                terminal_candidates = [
                    value
                    for value in batches
                    if str(value.get("batch_id") or "") not in open_ids
                ]
                terminal_limit = max(0, 100 - len(open_batches))
                terminal_batches = (
                    terminal_candidates[-terminal_limit:]
                    if terminal_limit
                    else []
                )
                batches = terminal_batches + open_batches
            settings["generation_mode"] = "minimax_h3_ref2va"
            settings["h3"] = {
                "schema": "jyd.project-h3.v1",
                **h3,
                "remote_batch_id": remote_batch_id,
                "remote_status": remote_status,
                "fee_snapshot": snapshot.get("fee_snapshot"),
                "prepare_key": resolved_prepare_key,
                "last_synced_at": now,
                "batches": batches,
            }
            connection.execute(
                "UPDATE projects SET settings_json=?, updated_at=? WHERE project_id=?",
                (_json(settings), now, project_id),
            )
            rows = connection.execute(
                "SELECT * FROM project_items WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            for item in rows:
                remote = remote_items.get(str(item["row_key"]))
                # A partial H3 batch deliberately leaves unselected project rows
                # untouched. Never inherit the project-level remote status here.
                if remote is None:
                    continue
                remote_item_status = str(remote.get("status") or remote_status).upper()
                current_item_status = str(item["status"])
                if current_item_status in H3_DOWNSTREAM_ITEM_STATUSES:
                    item_status = current_item_status
                elif item["current_base_video_asset_id"]:
                    subtitles = _object(item["subtitles_json"], _default_subtitles())
                    active_postprocess = connection.execute(
                        """
                        SELECT 1 FROM project_operations
                        WHERE item_id=?
                          AND operation_type IN ('POSTPROCESS_GENERATE', 'POSTPROCESS_EXPORT')
                          AND status IN ('PENDING', 'RUNNING')
                        LIMIT 1
                        """,
                        (item["item_id"],),
                    ).fetchone()
                    if active_postprocess is not None:
                        item_status = "POSTPROCESS_RUNNING"
                    elif (
                        item["current_video_asset_id"]
                        or str(subtitles.get("status") or "") == "PREVIEW_READY"
                    ):
                        item_status = "COMPOSITION_READY"
                    else:
                        item_status = "BASE_VIDEO_READY"
                elif remote_status == "AWAITING_COST_CONFIRMATION":
                    item_status = "H3_COST_PENDING"
                elif remote_item_status in {"FAILED", "PARTIAL_FAILED"}:
                    item_status = "H3_FAILED"
                elif remote_status in {"ACTIVE", "QUEUED", "RUNNING"}:
                    item_status = "H3_RUNNING"
                else:
                    item_status = str(item["status"])
                item_settings = _object(item["settings_json"], {})
                item_h3 = (
                    item_settings.get("h3")
                    if isinstance(item_settings.get("h3"), dict)
                    else {}
                )
                item_settings["h3"] = {
                    **item_h3,
                    "remote_item_id": remote.get("item_id"),
                    "remote_batch_id": remote_batch_id,
                    "remote_status": remote_item_status,
                    "segments": (
                        remote.get("segments")
                        if isinstance(remote.get("segments"), list)
                        else []
                    ),
                    "invalidated_reason": None,
                }
                connection.execute(
                    "UPDATE project_items SET settings_json=?, status=?, "
                    "updated_at=? WHERE item_id=?",
                    (_json(item_settings), item_status, now, item["item_id"]),
                )
            self._refresh_project_status(connection, project_id, now=now)
        return self.get_project(owner_user_id, project_id)

    def delete_project(
        self, owner_user_id: str, project_id: str
    ) -> dict[str, list[str]]:
        """Delete one inactive batch and return its unreferenced local artifacts."""

        cleanup_files: set[str] = set()
        cleanup_directories: set[str] = set()
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            item_rows = connection.execute(
                """
                SELECT status, content_analysis_json, visual_analysis_json
                FROM project_items WHERE project_id=?
                """,
                (project_id,),
            ).fetchall()
            for item in item_rows:
                if str(item["status"]) in ACTIVE_ITEM_STATUSES:
                    raise ValueError("当前批次仍有任务正在生成，请等待完成后再删除")
                content_analysis = _object(item["content_analysis_json"], {})
                visual_analysis = _object(item["visual_analysis_json"], {})
                if str(content_analysis.get("overall_status") or "") == "PENDING":
                    raise ValueError("当前批次仍在进行内容分析，请等待完成后再删除")
                if str(visual_analysis.get("analysis_status") or "") == "PENDING":
                    raise ValueError("当前批次仍在进行语义视觉分析，请等待完成后再删除")

            active_operation = connection.execute(
                """
                SELECT 1 FROM project_operations
                WHERE project_id=? AND status IN ('PENDING', 'RUNNING')
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if active_operation is not None:
                raise ValueError("当前批次仍有异步操作，请等待完成后再删除")
            active_result_batch = connection.execute(
                """
                SELECT 1 FROM project_result_batches
                WHERE project_id=? AND status IN ('ALLOCATED', 'RUNNING')
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if active_result_batch is not None:
                raise ValueError("当前批次仍在导出成果，请等待完成后再删除")

            cleanup_files.update(
                str(row["managed_path"])
                for row in connection.execute(
                    """
                    SELECT managed_path FROM project_assets
                    WHERE project_id=? AND managed_path IS NOT NULL AND managed_path!=''
                    """,
                    (project_id,),
                ).fetchall()
            )
            cleanup_files.update(
                str(row["managed_path"])
                for row in connection.execute(
                    "SELECT managed_path FROM project_input_images WHERE project_id=?",
                    (project_id,),
                ).fetchall()
                if row["managed_path"]
            )
            cleanup_files.update(
                str(row["managed_path"])
                for row in connection.execute(
                    "SELECT managed_path FROM project_script_sources WHERE project_id=?",
                    (project_id,),
                ).fetchall()
                if row["managed_path"]
            )
            cleanup_directories.update(
                str(row["export_path"])
                for row in connection.execute(
                    "SELECT export_path FROM project_result_batches WHERE project_id=?",
                    (project_id,),
                ).fetchall()
                if row["export_path"]
            )
            connection.execute("DELETE FROM projects WHERE project_id=?", (project_id,))

            referenced_files: set[str] = set()
            for path in cleanup_files:
                if connection.execute(
                    "SELECT 1 FROM project_assets WHERE managed_path=? LIMIT 1", (path,)
                ).fetchone() is not None:
                    referenced_files.add(path)
                    continue
                if connection.execute(
                    "SELECT 1 FROM project_input_images WHERE managed_path=? LIMIT 1",
                    (path,),
                ).fetchone() is not None:
                    referenced_files.add(path)
                    continue
                if connection.execute(
                    "SELECT 1 FROM project_script_sources WHERE managed_path=? LIMIT 1",
                    (path,),
                ).fetchone() is not None:
                    referenced_files.add(path)
            cleanup_files.difference_update(referenced_files)

            cleanup_directories = {
                path
                for path in cleanup_directories
                if connection.execute(
                    "SELECT 1 FROM project_result_batches WHERE export_path=? LIMIT 1",
                    (path,),
                ).fetchone()
                is None
            }
        resolved_cleanup_files = {
            resolved
            for path in cleanup_files
            if (resolved := self._payload_managed_path(path)[0])
        }
        return {
            "files": sorted(resolved_cleanup_files),
            "directories": sorted(cleanup_directories),
        }

    def add_script_source(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        managed_path: str,
        allow_active: bool = False,
    ) -> dict[str, Any]:
        clean_filename = Path(str(filename or "")).name.strip()
        clean_path = self.encode_managed_path(managed_path)
        if not clean_filename or not clean_path:
            raise ValueError("脚本源文件名称和保存路径不能为空")
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            if not allow_active:
                self._require_editable_inputs(connection, project_id)
            version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM project_script_sources WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
            )
            source_id = uuid.uuid4().hex
            now = _now()
            connection.execute(
                """
                INSERT INTO project_script_sources(
                    source_id, project_id, version, filename, content_type,
                    size_bytes, sha256, managed_path, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    project_id,
                    version,
                    clean_filename,
                    str(content_type or "application/octet-stream"),
                    max(0, int(size_bytes)),
                    str(sha256 or ""),
                    clean_path,
                    now,
                ),
            )
            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project_id),
            )
            row = connection.execute(
                "SELECT * FROM project_script_sources WHERE source_id=?", (source_id,)
            ).fetchone()
        return self._script_source_payload(row)

    def allocate_result_batch(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        export_root: str | Path,
        operation_type: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        created = (now or datetime.now().astimezone()).astimezone()
        date_key = created.strftime("%Y%m%d")
        date_label = f"{created.month}.{created.day}"
        root = Path(export_root).expanduser().resolve()
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            row = connection.execute(
                "SELECT last_value FROM project_result_batch_counters WHERE day_key=?",
                (date_key,),
            ).fetchone()
            sequence = int(row["last_value"]) + 1 if row is not None else 1
            while (root / date_label / str(sequence)).exists():
                sequence += 1
            connection.execute(
                """
                INSERT INTO project_result_batch_counters(day_key, last_value)
                VALUES(?, ?)
                ON CONFLICT(day_key) DO UPDATE SET last_value=excluded.last_value
                """,
                (date_key, sequence),
            )
            result_batch_id = uuid.uuid4().hex
            timestamp = created.isoformat(timespec="seconds")
            export_path = str((root / date_label / str(sequence)).resolve())
            connection.execute(
                """
                INSERT INTO project_result_batches(
                    result_batch_id, project_id, owner_user_id, date_key,
                    date_label, batch_no, export_path, operation_type,
                    status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'ALLOCATED', ?, ?)
                """,
                (
                    result_batch_id,
                    project_id,
                    str(owner_user_id),
                    date_key,
                    date_label,
                    sequence,
                    export_path,
                    str(operation_type or "").strip().upper(),
                    timestamp,
                    timestamp,
                ),
            )
            batch = connection.execute(
                "SELECT * FROM project_result_batches WHERE result_batch_id=?",
                (result_batch_id,),
            ).fetchone()
        return self._result_batch_payload(batch)

    def update_result_batch(
        self,
        owner_user_id: str,
        result_batch_id: str,
        *,
        status: str,
        jianying_batch_id: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        clean_status = str(status or "").strip().upper()
        if clean_status not in {
            "ALLOCATED", "RUNNING", "SUCCEEDED", "PARTIAL_FAILED", "FAILED"
        }:
            raise ValueError(f"成果批次状态无效: {clean_status}")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM project_result_batches WHERE result_batch_id=? AND owner_user_id=?",
                (str(result_batch_id), str(owner_user_id)),
            ).fetchone()
            if row is None:
                raise KeyError("成果批次不存在")
            connection.execute(
                """
                UPDATE project_result_batches
                SET status=?, jianying_batch_id=?, error_message=?, updated_at=?
                WHERE result_batch_id=?
                """,
                (
                    clean_status,
                    str(jianying_batch_id if jianying_batch_id is not None else row["jianying_batch_id"]),
                    str(error_message if error_message is not None else row["error_message"]),
                    _now(),
                    str(result_batch_id),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM project_result_batches WHERE result_batch_id=?",
                (str(result_batch_id),),
            ).fetchone()
        return self._result_batch_payload(updated)

    def list_gallery_records(self, owner_user_id: str) -> dict[str, Any]:
        owner_id = str(owner_user_id or "").strip()
        with self._connect() as connection:
            batch_rows = connection.execute(
                """
                SELECT rb.*, p.project_no, p.name AS project_name
                FROM project_result_batches rb
                JOIN projects p ON p.project_id=rb.project_id
                WHERE rb.owner_user_id=?
                ORDER BY rb.date_key DESC, rb.batch_no DESC
                """,
                (owner_id,),
            ).fetchall()
            asset_rows = connection.execute(
                """
                SELECT a.*, i.row_key, i.script_text, p.project_no, p.name AS project_name
                FROM project_assets a
                JOIN project_items i ON i.item_id=a.item_id
                JOIN projects p ON p.project_id=a.project_id
                WHERE p.owner_user_id=? AND a.asset_type='variant_video'
                ORDER BY a.created_at DESC
                """,
                (owner_id,),
            ).fetchall()
        videos = []
        for row in asset_rows:
            asset = self._asset_payload(row)
            videos.append(
                {
                    **asset,
                    "project_id": row["project_id"],
                    "item_id": row["item_id"],
                    "project_no": row["project_no"],
                    "project_name": row["project_name"],
                    "row_key": row["row_key"],
                    "script_text": row["script_text"],
                }
            )
        return {
            "batches": [
                {
                    **self._result_batch_payload(row),
                    "project_no": row["project_no"],
                    "project_name": row["project_name"],
                }
                for row in batch_rows
            ],
            "videos": videos,
        }

    def create_operation(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        operation_type: str,
        idempotency_key: str,
        item_id: str | None = None,
        payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        clean_type = str(operation_type or "").strip().upper()
        clean_key = str(idempotency_key or "").strip()
        if not clean_type or not clean_key:
            raise ValueError("操作类型和幂等键不能为空")
        now = _now()
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            if item_id:
                self._owned_item(connection, project_id, item_id)
            existing = connection.execute(
                """
                SELECT * FROM project_operations
                WHERE project_id=? AND IFNULL(item_id, '')=?
                  AND operation_type=? AND idempotency_key=?
                """,
                (project_id, item_id or "", clean_type, clean_key),
            ).fetchone()
            if existing is not None:
                return self._operation_payload(existing)
            operation_id = uuid.uuid4().hex
            clean_correlation_id = str(correlation_id or "").strip()
            if clean_correlation_id and (
                len(clean_correlation_id) > 64
                or any(
                    not (character.isalnum() or character in "._:-")
                    for character in clean_correlation_id
                )
            ):
                raise ValueError("日志关联标识不合法")
            if not clean_correlation_id:
                clean_correlation_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO project_operations(
                    operation_id, correlation_id, project_id, item_id, operation_type,
                    status, idempotency_key, payload_json, result_json,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'PENDING', ?, ?, '{}', ?, ?)
                """,
                (
                    operation_id,
                    clean_correlation_id,
                    project_id,
                    item_id,
                    clean_type,
                    clean_key,
                    _json(payload or {}),
                    now,
                    now,
                ),
            )
            if item_id:
                queued_status = {
                    "AUDIO_GENERATE": "AUDIO_QUEUED",
                    "COMPOSITION_GENERATE": "COMPOSITION_QUEUED",
                    "POSTPROCESS_GENERATE": "POSTPROCESS_RUNNING",
                    "VARIANT_GENERATE": "VARIANT_QUEUED",
                    "VARIANT_SUPPLEMENT": "VARIANT_QUEUED",
                    "VARIANT_RETRY": "VARIANT_QUEUED",
                }.get(clean_type)
                if queued_status:
                    connection.execute(
                        "UPDATE project_items SET status=?, updated_at=? WHERE item_id=?",
                        (queued_status, now, item_id),
                    )
            self._refresh_project_status(connection, project_id, now=now)
            row = connection.execute(
                "SELECT * FROM project_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            operation_payload = self._operation_payload(row)
        log_event(
            logger,
            "workbench.operation_created",
            "工作台异步操作已创建",
            component="workbench",
            user_id=owner_user_id,
            project_id=project_id,
            item_id=item_id,
            operation_id=operation_id,
            operation_type=clean_type,
            status="PENDING",
            correlation_id=clean_correlation_id,
        )
        return operation_payload

    def claim_pending_operation(
        self,
        owner_user_id: str,
        project_id: str,
        operation_id: str,
        *,
        operation_type: str,
    ) -> dict[str, Any] | None:
        """Atomically reserve one persisted operation for local handoff work.

        ``STARTING`` is intentionally distinct from ``RUNNING``: the latter
        means that the cloud accepted the idempotent paid request and status
        polling may begin.  A process restart can therefore safely put only
        interrupted local handoffs back into ``PENDING``.
        """

        clean_type = str(operation_type or "").strip().upper()
        now = _now()
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            updated = connection.execute(
                """
                UPDATE project_operations
                SET status='STARTING', attempt_count=attempt_count+1,
                    started_at=COALESCE(started_at, ?), updated_at=?
                WHERE operation_id=? AND project_id=?
                  AND operation_type=? AND status='PENDING'
                """,
                (now, now, operation_id, project_id, clean_type),
            )
            if updated.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM project_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            operation = self._operation_payload(row)
        log_event(
            logger,
            "workbench.operation_start_claimed",
            "后台协调器已认领待启动操作",
            component="workbench",
            user_id=owner_user_id,
            project_id=project_id,
            item_id=operation.get("item_id"),
            operation_id=operation_id,
            operation_type=clean_type,
            status="STARTING",
            correlation_id=operation.get("correlation_id"),
        )
        return operation

    def retire_unstarted_legacy_composition_operations(self) -> int:
        """Fail local 4A starts that never reached a running cloud handoff.

        The current workbench is H3-only.  PENDING/STARTING operations have not
        entered a paid remote stage yet, so they are safe to retire at startup.
        RUNNING operations are deliberately left untouched so historical paid
        jobs can still be reconciled through the read-only status endpoint.
        """

        now = _now()
        error_code = "NEW_WORKBENCH_H3_ONLY"
        error_message = "新版工作台只支持多参考生成；普通数字人启动操作已停止"
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT operation_id, project_id, item_id
                FROM project_operations
                WHERE operation_type='COMPOSITION_GENERATE'
                  AND status IN ('PENDING', 'STARTING')
                """
            ).fetchall()
            if not rows:
                return 0
            operation_ids = [str(row["operation_id"]) for row in rows]
            connection.executemany(
                """
                UPDATE project_operations
                SET status='FAILED', error_code=?, error_message=?,
                    updated_at=?, finished_at=?
                WHERE operation_id=?
                """,
                [
                    (error_code, error_message, now, now, operation_id)
                    for operation_id in operation_ids
                ],
            )
            item_ids = {
                str(row["item_id"])
                for row in rows
                if row["item_id"] is not None
            }
            for item_id in item_ids:
                connection.execute(
                    """
                    UPDATE project_items
                    SET status=CASE
                            WHEN current_base_video_asset_id IS NOT NULL
                                THEN 'BASE_VIDEO_READY'
                            WHEN current_audio_asset_id IS NOT NULL
                                THEN 'AUDIO_READY'
                            ELSE 'DRAFT'
                        END,
                        updated_at=?
                    WHERE item_id=? AND status='COMPOSITION_QUEUED'
                    """,
                    (now, item_id),
                )
            for project_id in {str(row["project_id"]) for row in rows}:
                self._refresh_project_status(connection, project_id, now=now)
            return len(operation_ids)

    def recover_interrupted_composition_starts(self) -> int:
        """Restore STARTING 4A operations for explicitly enabled legacy mode."""

        now = _now()
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE project_operations
                SET status='PENDING', updated_at=?
                WHERE operation_type='COMPOSITION_GENERATE'
                  AND status='STARTING'
                """,
                (now,),
            )
            return int(updated.rowcount)

    def transition_audio_operation(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        status: str,
        item_status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        return self.transition_operation(
            owner_user_id,
            project_id,
            item_id,
            operation_type="AUDIO_GENERATE",
            status=status,
            item_status=item_status,
            result=result,
            error_code=error_code,
            error_message=error_message,
        )

    def transition_operation(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        operation_id: str | None = None,
        operation_type: str,
        status: str,
        item_status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        operation_status = str(status or "").strip().upper()
        clean_type = str(operation_type or "").strip().upper()
        resolved_item_status = _clean_status(
            item_status, allowed=PROJECT_ITEM_STATUSES, label="项目行状态"
        )
        if operation_status not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED"}:
            raise ValueError("异步操作状态无效")
        now = _now()
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            self._owned_item(connection, project_id, item_id)
            if operation_id:
                operation = connection.execute(
                    """
                    SELECT * FROM project_operations
                    WHERE operation_id=? AND project_id=? AND item_id=?
                      AND operation_type=?
                    """,
                    (operation_id, project_id, item_id, clean_type),
                ).fetchone()
            else:
                operation = connection.execute(
                    """
                    SELECT * FROM project_operations
                    WHERE project_id=? AND item_id=? AND operation_type=?
                    ORDER BY rowid DESC LIMIT 1
                    """,
                    (project_id, item_id, clean_type),
                ).fetchone()
            if operation is None:
                raise KeyError("异步操作不存在")
            previous_status = str(operation["status"])
            started_at = operation["started_at"] or (
                now if operation_status == "RUNNING" else None
            )
            finished_at = (
                now if operation_status in {"SUCCEEDED", "FAILED"} else None
            )
            connection.execute(
                """
                UPDATE project_operations
                SET status=?, attempt_count=CASE
                        WHEN ?='RUNNING' AND status!='RUNNING'
                        THEN attempt_count+1 ELSE attempt_count END,
                    error_code=?, error_message=?, result_json=?,
                    updated_at=?, started_at=?, finished_at=?
                WHERE operation_id=?
                """,
                (
                    operation_status,
                    operation_status,
                    error_code,
                    error_message,
                    _json(result or {}),
                    now,
                    started_at,
                    finished_at,
                    operation["operation_id"],
                ),
            )
            connection.execute(
                "UPDATE project_items SET status=?, updated_at=? WHERE item_id=?",
                (resolved_item_status, now, item_id),
            )
            self._refresh_project_status(connection, project_id, now=now)
        if previous_status != operation_status:
            log_event(
                logger,
                "workbench.operation_status_changed",
                "工作台异步操作状态已变化",
                component="workbench",
                user_id=owner_user_id,
                project_id=project_id,
                item_id=item_id,
                operation_id=operation["operation_id"],
                operation_type=clean_type,
                previous_status=previous_status,
                status=operation_status,
                item_status=resolved_item_status,
                error_code=error_code,
                correlation_id=operation["correlation_id"],
            )
        return self.get_project(owner_user_id, project_id)

    def delete_variant_asset(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        asset_id: str,
    ) -> str | None:
        """Delete one generated variant record without affecting sibling variants."""

        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            self._owned_item(connection, project_id, item_id)
            asset = connection.execute(
                """
                SELECT * FROM project_assets
                WHERE project_id=? AND item_id=? AND asset_id=?
                  AND asset_type='variant_video'
                """,
                (project_id, item_id, asset_id),
            ).fetchone()
            if asset is None:
                raise KeyError("变体不存在")
            managed_path = str(asset["managed_path"] or "").strip() or None
            connection.execute("DELETE FROM project_assets WHERE asset_id=?", (asset_id,))
            remaining = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM project_assets
                    WHERE item_id=? AND asset_type='variant_video' AND status='READY'
                    """,
                    (item_id,),
                ).fetchone()[0]
            )
            now = _now()
            latest_variant_operation = connection.execute(
                """
                SELECT status FROM project_operations
                WHERE item_id=? AND operation_type IN (
                    'VARIANT_GENERATE', 'VARIANT_SUPPLEMENT', 'VARIANT_RETRY'
                )
                ORDER BY rowid DESC LIMIT 1
                """,
                (item_id,),
            ).fetchone()
            item_status = (
                "VARIANT_FAILED"
                if latest_variant_operation is not None
                and latest_variant_operation["status"] == "FAILED"
                else ("VARIANT_READY" if remaining else "COMPOSITION_READY")
            )
            connection.execute(
                "UPDATE project_items SET status=?, updated_at=? WHERE item_id=?",
                (item_status, now, item_id),
            )
            self._refresh_project_status(connection, project_id, now=now)
        return self._payload_managed_path(managed_path)[0] or None

    def delete_variant_assets(
        self,
        owner_user_id: str,
        asset_ids: list[str],
    ) -> list[dict[str, str | None]]:
        """Atomically delete account-owned generated variants from the gallery."""

        clean_ids = list(
            dict.fromkeys(str(asset_id or "").strip() for asset_id in asset_ids)
        )
        clean_ids = [asset_id for asset_id in clean_ids if asset_id]
        if not clean_ids:
            raise ValueError("请选择需要删除的成果视频")
        placeholders = ", ".join("?" for _ in clean_ids)
        with self._transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT a.asset_id, a.project_id, a.item_id, a.managed_path
                FROM project_assets a
                JOIN projects p ON p.project_id=a.project_id
                WHERE p.owner_user_id=? AND a.asset_type='variant_video'
                  AND a.asset_id IN ({placeholders})
                """,
                (str(owner_user_id or "").strip(), *clean_ids),
            ).fetchall()
            by_id = {str(row["asset_id"]): row for row in rows}
            if any(asset_id not in by_id for asset_id in clean_ids):
                raise KeyError("成果视频不存在或无权访问")

            connection.execute(
                f"DELETE FROM project_assets WHERE asset_id IN ({placeholders})",
                clean_ids,
            )
            now = _now()
            affected_items = {
                (str(row["project_id"]), str(row["item_id"])) for row in rows
            }
            affected_projects: set[str] = set()
            for project_id, item_id in affected_items:
                remaining = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM project_assets
                        WHERE item_id=? AND asset_type='variant_video' AND status='READY'
                        """,
                        (item_id,),
                    ).fetchone()[0]
                )
                latest_variant_operation = connection.execute(
                    """
                    SELECT status FROM project_operations
                    WHERE item_id=? AND operation_type IN (
                        'VARIANT_GENERATE', 'VARIANT_SUPPLEMENT', 'VARIANT_RETRY'
                    )
                    ORDER BY rowid DESC LIMIT 1
                    """,
                    (item_id,),
                ).fetchone()
                item_status = (
                    "VARIANT_FAILED"
                    if latest_variant_operation is not None
                    and latest_variant_operation["status"] == "FAILED"
                    else ("VARIANT_READY" if remaining else "COMPOSITION_READY")
                )
                connection.execute(
                    "UPDATE project_items SET status=?, updated_at=? WHERE item_id=?",
                    (item_status, now, item_id),
                )
                affected_projects.add(project_id)
            for project_id in affected_projects:
                self._refresh_project_status(connection, project_id, now=now)

        return [
            {
                "asset_id": asset_id,
                "managed_path": (
                    self._payload_managed_path(by_id[asset_id]["managed_path"])[0]
                    or None
                ),
            }
            for asset_id in clean_ids
        ]

    def mark_item_content_analysis_pending(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        expected_script_sha256: str,
    ) -> bool:
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            script = str(item["script_text"])
            if _script_sha256(script) != expected_script_sha256:
                return False
            snapshot = _content_analysis_snapshot(
                _object(item["content_analysis_json"], {}), script
            )
            snapshot.update(
                {
                    "overall_status": "PENDING",
                    "request_count": int(snapshot.get("request_count") or 0) + 1,
                    "requested_at": _now(),
                    "invalidated_reason": None,
                    "invalidated_at": None,
                }
            )
            now = _now()
            connection.execute(
                "UPDATE project_items SET content_analysis_json=?, updated_at=? WHERE item_id=?",
                (_json(snapshot), now, item_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE project_id=?", (now, project_id)
            )
        return True

    def complete_item_content_analysis(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        expected_script_sha256: str,
        result: dict[str, Any],
        previous: dict[str, Any] | None = None,
    ) -> bool:
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            script = str(item["script_text"])
            if _script_sha256(script) != expected_script_sha256:
                return False
            current = _content_analysis_snapshot(
                _object(item["content_analysis_json"], {}), script
            )
            baseline = (
                _content_analysis_snapshot(previous, script)
                if isinstance(previous, dict)
                else current
            )
            errors = result.get("errors") if isinstance(result.get("errors"), dict) else {}

            music_status = str(result.get("music_analysis_status") or "FAILED")
            if music_status == "SUCCESS":
                music_intent = result.get("music_intent")
                music_error = None
            elif baseline.get("music_analysis_status") == "SUCCESS":
                music_status = "SUCCESS"
                music_intent = baseline.get("music_intent")
                music_error = None
            else:
                music_status = "FAILED"
                music_intent = None
                music_error = errors.get("music")

            subtitle_status = str(result.get("subtitle_analysis_status") or "FAILED")
            if subtitle_status == "SUCCESS":
                subtitle_units = result.get("subtitle_units")
                subtitle_error = None
            elif baseline.get("subtitle_analysis_status") == "SUCCESS":
                subtitle_status = "SUCCESS"
                subtitle_units = baseline.get("subtitle_units")
                subtitle_error = None
            else:
                subtitle_status = "FAILED"
                subtitle_units = None
                subtitle_error = errors.get("subtitle")

            title_status = str(result.get("title_analysis_status") or "FAILED")
            if title_status == "SUCCESS":
                title = result.get("title")
                title_error = None
            elif baseline.get("title_analysis_status") == "SUCCESS":
                title_status = "SUCCESS"
                title = baseline.get("title")
                title_error = None
            else:
                title_status = "FAILED"
                title = None
                title_error = errors.get("title")

            analyzed_at = _now()
            snapshot = {
                **current,
                "script_sha256": expected_script_sha256,
                "script_length": len(script),
                "overall_status": _analysis_overall_status(
                    music_status, subtitle_status, title_status
                ),
                "music_analysis_status": music_status,
                "subtitle_analysis_status": subtitle_status,
                "title_analysis_status": title_status,
                "music_intent": music_intent,
                "subtitle_units": subtitle_units,
                "title": title,
                "errors": {
                    "music": music_error,
                    "subtitle": subtitle_error,
                    "title": title_error,
                    "request": None,
                },
                "schema_version": result.get("schema_version")
                or baseline.get("schema_version"),
                "prompt_version": result.get("prompt_version")
                or baseline.get("prompt_version"),
                "subtitle_prompt_version": result.get("subtitle_prompt_version")
                or baseline.get("subtitle_prompt_version"),
                "model": result.get("model") or baseline.get("model"),
                "provider_request_id": result.get("provider_request_id")
                or baseline.get("provider_request_id"),
                "provider_attempts": int(result.get("provider_attempts") or 0),
                "cache_hit": result.get("cache_hit") is True,
                "cacheable": result.get("cacheable") is True,
                "analyzed_at": analyzed_at,
                "invalidated_reason": None,
                "invalidated_at": None,
            }
            subtitle_result_refreshed = (
                str(result.get("subtitle_analysis_status") or "").upper()
                == "SUCCESS"
                and isinstance(result.get("subtitle_units"), list)
            )
            subtitles = _object(item["subtitles_json"], _default_subtitles())
            semantic_mapping = (
                dict(subtitles.get("semantic_mapping") or {})
                if isinstance(subtitles.get("semantic_mapping"), dict)
                else {}
            )
            current_analysis_identity = subtitle_analysis_sha256(snapshot)
            mapped_analysis_identity = str(
                semantic_mapping.get("analysis_subtitle_sha256") or ""
            ).strip()
            invalidate_rendered_subtitles = (
                subtitle_result_refreshed
                and bool(subtitles.get("render_cues"))
                and current_analysis_identity is not None
                and (
                    not mapped_analysis_identity
                    or mapped_analysis_identity != current_analysis_identity
                )
            )
            if invalidate_rendered_subtitles:
                subtitles["render_cues"] = []
                subtitles["bound_video_asset_id"] = None
                subtitles["overflow_risk"] = False
                subtitles["review_reason"] = None
                subtitles["status"] = (
                    "READY" if subtitles.get("raw_cues") else "NOT_AVAILABLE"
                )
                subtitles["semantic_mapping"] = {
                    "schema": "jyd.semantic-caption-mapping.v1",
                    "status": "NOT_REQUESTED",
                    "reason_code": "SUBTITLE_ANALYSIS_VERSION_CHANGED",
                    "reason_summary": "字幕分析版本或断句结果已更新，需重新生成预览",
                    "analysis_prompt_version": snapshot.get("prompt_version"),
                    "analysis_subtitle_prompt_version": snapshot.get(
                        "subtitle_prompt_version"
                    ),
                }
            if title_status == "SUCCESS" and isinstance(title, dict):
                line_1 = str(title.get("line_1") or "").strip()
                line_2 = str(title.get("line_2") or "").strip()
                settings = _object(item["settings_json"], {})
                postprocess = dict(settings.get("postprocess") or {})
                canonical_title = {"line_1": line_1, "line_2": line_2}
                postprocess.update(
                    {
                        "title": canonical_title,
                        "cover_title": canonical_title,
                        "top_title": {"label": line_1, "headline": line_2},
                    }
                )
                settings["postprocess"] = postprocess
                connection.execute(
                    "UPDATE project_items SET settings_json=? WHERE item_id=?",
                    (_json(settings), item_id),
                )
            if invalidate_rendered_subtitles:
                next_status = (
                    "BASE_VIDEO_READY"
                    if item["current_base_video_asset_id"]
                    else (
                        "AUDIO_READY" if item["current_audio_asset_id"] else "DRAFT"
                    )
                )
                connection.execute(
                    """
                    UPDATE project_items
                    SET content_analysis_json=?, subtitles_json=?,
                        current_video_asset_id=NULL, status=?, updated_at=?
                    WHERE item_id=?
                    """,
                    (
                        _json(snapshot),
                        _json(subtitles),
                        next_status,
                        analyzed_at,
                        item_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET revision=revision+1, updated_at=?
                    WHERE project_id=?
                    """,
                    (analyzed_at, project_id),
                )
                self._refresh_project_status(connection, project_id, now=analyzed_at)
            else:
                connection.execute(
                    "UPDATE project_items SET content_analysis_json=?, updated_at=? WHERE item_id=?",
                    (_json(snapshot), analyzed_at, item_id),
                )
                connection.execute(
                    "UPDATE projects SET updated_at=? WHERE project_id=?",
                    (analyzed_at, project_id),
                )
        return True

    def fail_item_content_analysis(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        expected_script_sha256: str,
        error: dict[str, str],
        previous: dict[str, Any] | None = None,
    ) -> bool:
        failed = {
            "music_analysis_status": "FAILED",
            "subtitle_analysis_status": "FAILED",
            "title_analysis_status": "FAILED",
            "music_intent": None,
            "subtitle_units": None,
            "title": None,
            "errors": {"music": error, "subtitle": error, "title": error},
            "provider_attempts": 0,
            "cache_hit": False,
            "cacheable": False,
        }
        completed = self.complete_item_content_analysis(
            owner_user_id,
            project_id,
            item_id,
            expected_script_sha256=expected_script_sha256,
            result=failed,
            previous=previous,
        )
        if not completed:
            return False
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            script = str(item["script_text"])
            if _script_sha256(script) != expected_script_sha256:
                return False
            snapshot = _content_analysis_snapshot(
                _object(item["content_analysis_json"], {}), script
            )
            snapshot["errors"] = {
                **dict(snapshot.get("errors") or {}),
                "request": error,
            }
            now = _now()
            connection.execute(
                "UPDATE project_items SET content_analysis_json=?, updated_at=? WHERE item_id=?",
                (_json(snapshot), now, item_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE project_id=?", (now, project_id)
            )
        return True

    def mark_item_visual_analysis_pending(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        expected_script_sha256: str,
        candidate_request: dict[str, Any],
    ) -> bool:
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            script = str(item["script_text"])
            if _script_sha256(script) != expected_script_sha256:
                return False
            snapshot = _visual_analysis_snapshot(
                _object(item["visual_analysis_json"], {}), script
            )
            snapshot.update(
                {
                    "analysis_status": "PENDING",
                    "mapping_status": "NOT_REQUESTED",
                    "catalog_version": candidate_request.get("catalog_version"),
                    "candidate_set_sha256": _visual_candidate_set_sha256(
                        candidate_request
                    ),
                    "candidate_request": candidate_request,
                    "decisions": [],
                    "visual_plan": [],
                    "error": None,
                    "request_count": int(snapshot.get("request_count") or 0) + 1,
                    "requested_at": _now(),
                    "invalidated_reason": None,
                    "invalidated_at": None,
                    "bound_audio_asset_id": item["current_audio_asset_id"],
                    "raw_cues_sha256": _raw_cues_sha256(
                        _object(item["subtitles_json"], _default_subtitles()).get(
                            "raw_cues", []
                        )
                    ),
                }
            )
            now = _now()
            connection.execute(
                "UPDATE project_items SET visual_analysis_json=?, updated_at=? WHERE item_id=?",
                (_json(snapshot), now, item_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE project_id=?", (now, project_id)
            )
        return True

    def complete_item_visual_analysis(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        expected_script_sha256: str,
        result: dict[str, Any],
        recipe: dict[str, Any],
        mapping_status: str,
        mapping_error: dict[str, str] | None = None,
    ) -> bool:
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            script = str(item["script_text"])
            if _script_sha256(script) != expected_script_sha256:
                return False
            snapshot = _visual_analysis_snapshot(
                _object(item["visual_analysis_json"], {}), script
            )
            response_catalog = result.get("catalog_version") or recipe.get(
                "catalog_version"
            )
            if (
                snapshot.get("analysis_status") != "PENDING"
                or snapshot.get("catalog_version") != response_catalog
                or snapshot.get("candidate_set_sha256")
                != result.get("candidate_set_sha256")
            ):
                return False
            analyzed_at = _now()
            snapshot.update(
                {
                    "analysis_status": str(result.get("analysis_status") or "FAILED"),
                    "mapping_status": mapping_status,
                    "catalog_version": result.get("catalog_version")
                    or snapshot.get("catalog_version"),
                    "decisions": (
                        list(result.get("decisions") or [])
                        if isinstance(result.get("decisions"), list)
                        else []
                    ),
                    "visual_plan": (
                        list(result.get("visual_plan") or [])
                        if isinstance(result.get("visual_plan"), list)
                        else []
                    ),
                    "mapped_candidates": (
                        list(result.get("mapped_candidates") or [])
                        if isinstance(result.get("mapped_candidates"), list)
                        else []
                    ),
                    "recipe": recipe,
                    "error": mapping_error or result.get("error"),
                    "provider_request_id": result.get("provider_request_id"),
                    "provider_attempts": int(result.get("provider_attempts") or 0),
                    "cache_hit": result.get("cache_hit") is True,
                    "cacheable": result.get("cacheable") is True,
                    "analyzed_at": analyzed_at,
                    "invalidated_reason": None,
                    "invalidated_at": None,
                    "bound_audio_asset_id": item["current_audio_asset_id"],
                    "raw_cues_sha256": _raw_cues_sha256(
                        _object(item["subtitles_json"], _default_subtitles()).get(
                            "raw_cues", []
                        )
                    ),
                    "revision": int(snapshot.get("revision") or 0) + 1,
                }
            )
            connection.execute(
                "UPDATE project_items SET visual_analysis_json=?, updated_at=? WHERE item_id=?",
                (_json(snapshot), analyzed_at, item_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE project_id=?",
                (analyzed_at, project_id),
            )
        return True

    def invalidate_item_visual_analysis_for_catalog(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        expected_script_sha256: str,
        candidate_request: dict[str, Any],
        recipe: dict[str, Any],
    ) -> bool:
        """Drop an incompatible saved plan and leave the item ready for explicit retry."""

        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            script = str(item["script_text"])
            if _script_sha256(script) != expected_script_sha256:
                return False
            snapshot = _visual_analysis_snapshot(
                _object(item["visual_analysis_json"], {}), script
            )
            now = _now()
            snapshot.update(
                {
                    "analysis_status": "NOT_REQUESTED",
                    "mapping_status": "NOT_REQUESTED",
                    "catalog_version": candidate_request.get("catalog_version"),
                    "candidate_set_sha256": _visual_candidate_set_sha256(
                        candidate_request
                    ),
                    "candidate_request": candidate_request,
                    "visual_plan": [],
                    "decisions": [],
                    "mapped_candidates": [],
                    "recipe": recipe,
                    "error": {
                        "code": "VISUAL_CATALOG_CHANGED",
                        "summary": "新版素材目录与旧视觉计划不兼容，请重新分析此条",
                    },
                    "cache_hit": False,
                    "cacheable": False,
                    "analyzed_at": now,
                    "invalidated_reason": "VISUAL_CATALOG_CHANGED",
                    "invalidated_at": now,
                    "bound_audio_asset_id": item["current_audio_asset_id"],
                    "raw_cues_sha256": _raw_cues_sha256(
                        _object(item["subtitles_json"], _default_subtitles()).get(
                            "raw_cues", []
                        )
                    ),
                    "revision": int(snapshot.get("revision") or 0) + 1,
                }
            )
            connection.execute(
                "UPDATE project_items SET visual_analysis_json=?, updated_at=? WHERE item_id=?",
                (_json(snapshot), now, item_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE project_id=?",
                (now, project_id),
            )
        return True

    def fail_item_visual_analysis(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        expected_script_sha256: str,
        expected_catalog_version: str,
        expected_candidate_set_sha256: str,
        error: dict[str, str],
    ) -> bool:
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            script = str(item["script_text"])
            if _script_sha256(script) != expected_script_sha256:
                return False
            snapshot = _visual_analysis_snapshot(
                _object(item["visual_analysis_json"], {}), script
            )
            if (
                snapshot.get("analysis_status") != "PENDING"
                or snapshot.get("catalog_version") != expected_catalog_version
                or snapshot.get("candidate_set_sha256")
                != expected_candidate_set_sha256
            ):
                return False
            snapshot.update(
                {
                    "analysis_status": "FAILED",
                    "mapping_status": "FAILED",
                    "error": error,
                    "cache_hit": False,
                    "cacheable": False,
                    "analyzed_at": _now(),
                    "revision": int(snapshot.get("revision") or 0) + 1,
                }
            )
            now = _now()
            connection.execute(
                "UPDATE project_items SET visual_analysis_json=?, updated_at=? WHERE item_id=?",
                (_json(snapshot), now, item_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE project_id=?", (now, project_id)
            )
        return True

    def update_item_seam_visual_analysis(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        expected_script_sha256: str,
        seam_analysis: dict[str, Any],
        recipe: dict[str, Any] | None = None,
    ) -> bool:
        """Persist a seam-only visual supplement without replacing the main plan."""

        if not isinstance(seam_analysis, dict):
            raise ValueError("连接处视觉分析结果无效")
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            item = self._owned_item(connection, project_id, item_id)
            script = str(item["script_text"])
            if _script_sha256(script) != expected_script_sha256:
                return False
            snapshot = _visual_analysis_snapshot(
                _object(item["visual_analysis_json"], {}), script
            )
            analyzed_at = _now()
            snapshot["seam_analysis"] = {
                **dict(seam_analysis),
                "analyzed_at": analyzed_at,
            }
            if recipe is not None:
                snapshot["recipe"] = recipe
            snapshot["bound_audio_asset_id"] = item["current_audio_asset_id"]
            snapshot["raw_cues_sha256"] = _raw_cues_sha256(
                _object(item["subtitles_json"], _default_subtitles()).get(
                    "raw_cues", []
                )
            )
            snapshot["revision"] = int(snapshot.get("revision") or 0) + 1
            connection.execute(
                "UPDATE project_items SET visual_analysis_json=?, updated_at=? WHERE item_id=?",
                (_json(snapshot), analyzed_at, item_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE project_id=?",
                (analyzed_at, project_id),
            )
        return True

    def update_item_visual_overlays(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        overlays: list[dict[str, Any]],
        expected_revision: int,
        catalog_version: str,
        library_id: str = DEFAULT_LIBRARY_ID,
        media_policy: str = "image_only",
    ) -> dict[str, Any]:
        if not isinstance(overlays, list):
            raise ValueError("语义贴图配方必须是数组")
        clean_library_id = str(library_id).strip()
        if not clean_library_id:
            raise ValueError("语义视觉素材库 ID 无效")
        if media_policy not in MEDIA_POLICIES:
            raise ValueError("语义视觉媒体策略无效")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(overlays, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"第 {index} 个语义贴图不是对象")
            overlay_id = str(raw.get("overlay_id") or "").strip()
            asset_id = str(raw.get("asset_id") or "").strip()
            concept_id = str(raw.get("concept_id") or "").strip()
            corner = str(raw.get("corner") or "").strip()
            start_us = raw.get("start_us")
            duration_us = raw.get("duration_us")
            scale = raw.get("scale")
            opacity = raw.get("opacity")
            media_type = str(raw.get("media_type") or "").strip()
            renderer = str(raw.get("renderer") or "").strip()
            resource_path = str(raw.get("resource_path") or "").strip()
            if not overlay_id or overlay_id in seen or not asset_id or not concept_id:
                raise ValueError("语义贴图 ID、素材或概念无效")
            if corner not in VISUAL_CORNERS:
                raise ValueError("语义贴图位置无效")
            if type(start_us) is not int or start_us < 0:
                raise ValueError("语义贴图开始时间无效")
            if type(duration_us) is not int or not 100_000 <= duration_us <= 30_000_000:
                raise ValueError("语义贴图持续时间无效")
            if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0.05 <= float(scale) <= 2.0:
                raise ValueError("语义贴图缩放无效")
            if isinstance(opacity, bool) or not isinstance(opacity, (int, float)) or not 0.0 <= float(opacity) <= 1.0:
                raise ValueError("语义贴图透明度无效")
            if (media_type, renderer) not in {
                ("image", "jyd_sticker_bundle"),
                ("video", "video_overlay"),
            }:
                raise ValueError("语义视觉媒体类型或渲染器无效")
            resource = Path(resource_path)
            if not resource_path or resource.is_absolute() or ".." in resource.parts:
                raise ValueError("语义视觉资源路径必须是素材库内相对路径")
            if media_type == "video":
                source_start_us = raw.get("source_start_us")
                if type(source_start_us) is not int or source_start_us < 0:
                    raise ValueError("语义视频素材起始时间无效")
                if not isinstance(raw.get("mute"), bool):
                    raise ValueError("语义视频静音参数无效")
                if raw.get("fit") not in {"cover", "contain"}:
                    raise ValueError("语义视频填充方式无效")
            seen.add(overlay_id)
            normalized.append(
                {
                    **{key: value for key, value in raw.items() if key != "loop"},
                    "overlay_id": overlay_id,
                    "asset_id": asset_id,
                    "concept_id": concept_id,
                    "corner": corner,
                    "start_us": start_us,
                    "duration_us": duration_us,
                    "scale": float(scale),
                    "opacity": float(opacity),
                    "media_type": media_type,
                    "renderer": renderer,
                    "resource_path": resource_path,
                    "enabled": raw.get("enabled") is not False,
                    "selection_mode": "manual",
                    "manual": True,
                    "locked": raw.get("locked") is True,
                    "requires_review": False,
                }
            )
        enabled = sorted(
            (item for item in normalized if item["enabled"]),
            key=lambda item: item["start_us"],
        )
        for previous, current in zip(enabled, enabled[1:]):
            if current["start_us"] < previous["start_us"] + previous["duration_us"]:
                raise ValueError("同一时间只能显示一个语义视觉素材")
        with self._transaction() as connection:
            project = self._owned_project(connection, owner_user_id, project_id)
            if int(project["revision"]) != int(expected_revision):
                raise ProjectRevisionConflict("项目已被其他操作更新，请刷新后重试")
            item = self._owned_item(connection, project_id, item_id)
            if str(item["status"]) in ACTIVE_ITEM_STATUSES:
                raise ValueError("当前脚本行正在生成，不能修改语义贴图")
            script = str(item["script_text"])
            snapshot = _visual_analysis_snapshot(
                _object(item["visual_analysis_json"], {}), script
            )
            snapshot["recipe"] = {
                "schema": RECIPE_SCHEMA,
                "library_id": clean_library_id,
                "catalog_version": catalog_version,
                "media_policy": media_policy,
                "timing_policy_version": "sentence-v1",
                "used_asset_ids": sorted(
                    {
                        str(item.get("asset_id") or "")
                        for item in normalized
                        if item.get("enabled") is not False
                        and str(item.get("asset_id") or "")
                    }
                ),
                "overlays": normalized,
            }
            snapshot["bound_audio_asset_id"] = item["current_audio_asset_id"]
            snapshot["raw_cues_sha256"] = _raw_cues_sha256(
                _object(item["subtitles_json"], _default_subtitles()).get("raw_cues", [])
            )
            snapshot["revision"] = int(snapshot.get("revision") or 0) + 1
            now = _now()
            connection.execute(
                "UPDATE project_items SET visual_analysis_json=?, updated_at=? WHERE item_id=?",
                (_json(snapshot), now, item_id),
            )
            connection.execute(
                "UPDATE projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project_id),
            )
        return self.get_project(owner_user_id, project_id)

    def set_item_subtitles(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        subtitles: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(subtitles, dict):
            raise ValueError("字幕数据必须是对象")
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            self._owned_item(connection, project_id, item_id)
            now = _now()
            connection.execute(
                "UPDATE project_items SET subtitles_json=?, updated_at=? WHERE item_id=?",
                (_json(subtitles), now, item_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE project_id=?",
                (now, project_id),
            )
        return self.get_project(owner_user_id, project_id)

    def add_link(
        self,
        *,
        owner_user_id: str,
        project_id: str,
        system: str,
        relation: str,
        external_id: str,
        item_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        values = [str(value or "").strip() for value in (system, relation, external_id)]
        if not all(values):
            raise ValueError("外部关联的系统、关系和编号不能为空")
        clean_system, clean_relation, clean_external_id = values
        with self._transaction() as connection:
            self._owned_project(connection, owner_user_id, project_id)
            if item_id:
                self._owned_item(connection, project_id, item_id)
            existing = connection.execute(
                """
                SELECT * FROM project_links
                WHERE project_id=? AND system=? AND relation=? AND external_id=?
                """,
                (project_id, clean_system, clean_relation, clean_external_id),
            ).fetchone()
            if existing is not None:
                return self._link_payload(existing)
            link_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO project_links(
                    link_id, project_id, item_id, system, relation,
                    external_id, metadata_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    project_id,
                    item_id,
                    clean_system,
                    clean_relation,
                    clean_external_id,
                    _json(metadata or {}),
                    _now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM project_links WHERE link_id=?", (link_id,)
            ).fetchone()
            return self._link_payload(row)

    def find_project_by_link(
        self,
        *,
        owner_user_id: str,
        system: str,
        relation: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        values = [str(value or "").strip() for value in (system, relation, external_id)]
        if not all(values):
            raise ValueError("外部关联的系统、关系和编号不能为空")
        clean_system, clean_relation, clean_external_id = values
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT p.project_id
                FROM project_links AS link
                JOIN projects AS p ON p.project_id = link.project_id
                WHERE p.owner_user_id=?
                  AND link.system=?
                  AND link.relation=?
                  AND link.external_id=?
                ORDER BY link.rowid DESC
                LIMIT 1
                """,
                (
                    str(owner_user_id or "").strip(),
                    clean_system,
                    clean_relation,
                    clean_external_id,
                ),
            ).fetchone()
            if row is None:
                return None
            return self._project_payload(connection, row["project_id"])

    @staticmethod
    def _next_project_no(connection: sqlite3.Connection) -> str:
        day_key = datetime.now().astimezone().strftime("%Y%m%d")
        row = connection.execute(
            "SELECT last_value FROM project_counters WHERE day_key=?", (day_key,)
        ).fetchone()
        sequence = int(row["last_value"]) + 1 if row is not None else 1
        connection.execute(
            """
            INSERT INTO project_counters(day_key, last_value) VALUES(?, ?)
            ON CONFLICT(day_key) DO UPDATE SET last_value=excluded.last_value
            """,
            (day_key, sequence),
        )
        return f"DH-{day_key}-{sequence:04d}"

    @staticmethod
    def _owned_project(
        connection: sqlite3.Connection, owner_user_id: str, project_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM projects
            WHERE project_id=? AND owner_user_id=?
            """,
            (str(project_id or "").strip(), str(owner_user_id or "").strip()),
        ).fetchone()
        if row is None:
            raise KeyError("项目不存在")
        return row

    @staticmethod
    def _owned_item(
        connection: sqlite3.Connection, project_id: str, item_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM project_items WHERE item_id=? AND project_id=?",
            (str(item_id or "").strip(), str(project_id or "").strip()),
        ).fetchone()
        if row is None:
            raise KeyError("项目脚本行不存在")
        return row

    @staticmethod
    def _require_editable_inputs(
        connection: sqlite3.Connection, project_id: str
    ) -> None:
        rows = connection.execute(
            "SELECT status FROM project_items WHERE project_id=?", (project_id,)
        ).fetchall()
        if not rows or any(str(row["status"]) != "DRAFT" for row in rows):
            raise ValueError("项目已进入声音生成，不能修改脚本")

    def _assign_input_image(
        self,
        connection: sqlite3.Connection,
        item: sqlite3.Row,
        image: sqlite3.Row,
        *,
        mapping_source: str,
        now: str,
    ) -> None:
        current_asset_id = str(item["current_image_asset_id"] or "")
        if current_asset_id:
            current = connection.execute(
                "SELECT external_ref_json FROM project_assets WHERE asset_id=?",
                (current_asset_id,),
            ).fetchone()
            if current is not None and _object(
                current["external_ref_json"], {}
            ).get("input_image_id") == image["image_id"]:
                return
        current_asset_id = str(item["current_image_asset_id"] or "")
        if current_asset_id:
            current_asset = connection.execute(
                """
                SELECT external_ref_json
                FROM project_assets
                WHERE asset_id=? AND item_id=? AND asset_type='input_image'
                """,
                (current_asset_id, item["item_id"]),
            ).fetchone()
            if (
                current_asset is not None
                and str(
                    _object(current_asset["external_ref_json"], {}).get(
                        "input_image_id"
                    )
                    or ""
                )
                == str(image["image_id"])
            ):
                return
        version = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1
                FROM project_assets
                WHERE item_id=? AND asset_type='input_image'
                """,
                (item["item_id"],),
            ).fetchone()[0]
        )
        asset_id = uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO project_assets(
                asset_id, project_id, item_id, asset_type, version,
                status, source_type, filename, managed_path,
                external_ref_json, metadata_json, created_at, updated_at
            ) VALUES(?, ?, ?, 'input_image', ?, 'READY', 'project_upload',
                     ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                item["project_id"],
                item["item_id"],
                version,
                image["filename"],
                image["managed_path"],
                _json({"input_image_id": image["image_id"]}),
                _json(
                    {
                        "mapping_source": mapping_source,
                        "pool_position": int(image["position"]),
                        "content_type": image["content_type"],
                        "size_bytes": int(image["size_bytes"]),
                        "sha256": image["sha256"],
                    }
                ),
                now,
                now,
            ),
        )
        subtitles = _object(item["subtitles_json"], _default_subtitles())
        subtitles["bound_video_asset_id"] = None
        if subtitles.get("raw_cues"):
            subtitles["status"] = "READY"
        next_status = "AUDIO_READY" if item["current_audio_asset_id"] else "DRAFT"
        connection.execute(
            """
            UPDATE project_items
            SET current_image_asset_id=?, current_base_video_asset_id=NULL,
                current_video_asset_id=NULL, subtitles_json=?, status=?, updated_at=?
            WHERE item_id=?
            """,
            (asset_id, _json(subtitles), next_status, now, item["item_id"]),
        )

    def _set_current_asset(
        self,
        connection: sqlite3.Connection,
        item: sqlite3.Row,
        *,
        asset_id: str,
        asset_type: str,
        source_type: str,
        asset_status: str,
        now: str,
    ) -> None:
        if asset_status != "READY":
            raise ValueError("只有已就绪素材可以设为当前版本")
        if asset_type == "audio":
            subtitles = _default_subtitles()
            subtitles.update(
                {
                    "bound_audio_asset_id": asset_id,
                    "status": "PENDING_TIMESTAMPS",
                }
            )
            connection.execute(
                """
                UPDATE project_items
                SET current_audio_asset_id=?, current_base_video_asset_id=NULL,
                    current_video_asset_id=NULL,
                    subtitles_json=?, status='AUDIO_READY', updated_at=?
                WHERE item_id=?
                """,
                (asset_id, _json(subtitles), now, item["item_id"]),
            )
        elif asset_type == "base_video":
            settings = _object(item["settings_json"], {})
            settings.pop("composition_invalidated_reason", None)
            connection.execute(
                """
                UPDATE project_items
                SET current_base_video_asset_id=?, current_video_asset_id=NULL,
                    settings_json=?, status='BASE_VIDEO_READY', updated_at=?
                WHERE item_id=?
                """,
                (asset_id, _json(settings), now, item["item_id"]),
            )
        elif asset_type == "composition_video":
            subtitles = _object(item["subtitles_json"], _default_subtitles())
            if source_type == "user_upload":
                subtitles["bound_video_asset_id"] = None
                subtitles["status"] = (
                    "INVALIDATED"
                    if subtitles.get("source") or subtitles.get("raw_cues")
                    else "NOT_AVAILABLE"
                )
            else:
                subtitles["bound_video_asset_id"] = asset_id
            connection.execute(
                """
                UPDATE project_items
                SET current_video_asset_id=?, subtitles_json=?,
                    status='COMPOSITION_READY', updated_at=?
                WHERE item_id=?
                """,
                (asset_id, _json(subtitles), now, item["item_id"]),
            )
        elif asset_type == "variant_video":
            connection.execute(
                """
                UPDATE project_items
                SET status='VARIANT_READY', updated_at=?
                WHERE item_id=?
                """,
                (now, item["item_id"]),
            )
        else:
            raise ValueError(f"素材类型不能设为当前版本: {asset_type}")

    def _reconcile_durable_project_state(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        *,
        now: str,
    ) -> int:
        """Repair interrupted local status writes from durable assets and operations.

        A process can stop after an audio file or postprocess preview is committed but
        before its operation/item status is finalized.  Only exact audio remote-item
        matches and terminal postprocess operation sets are recovered here, so a live
        paid request is never retired merely because an older asset exists.
        """

        items = connection.execute(
            """
            SELECT item_id, status, current_audio_asset_id,
                   current_base_video_asset_id, current_video_asset_id,
                   subtitles_json
            FROM project_items
            WHERE project_id=?
            ORDER BY position
            """,
            (project_id,),
        ).fetchall()
        if not items:
            return 0

        active_operations = connection.execute(
            """
            SELECT * FROM project_operations
            WHERE project_id=? AND item_id IS NOT NULL
              AND status IN ('PENDING', 'STARTING', 'RUNNING')
            ORDER BY rowid
            """,
            (project_id,),
        ).fetchall()
        completed_operation_ids: set[str] = set()
        assets_by_id: dict[str, sqlite3.Row] = {}
        audio_asset_ids = {
            str(item["current_audio_asset_id"])
            for item in items
            if item["current_audio_asset_id"]
        }
        if audio_asset_ids:
            placeholders = ",".join("?" for _ in audio_asset_ids)
            asset_rows = connection.execute(
                f"SELECT * FROM project_assets WHERE asset_id IN ({placeholders})",
                tuple(sorted(audio_asset_ids)),
            ).fetchall()
            assets_by_id = {str(asset["asset_id"]): asset for asset in asset_rows}

        for operation in active_operations:
            if str(operation["operation_type"]) != "AUDIO_GENERATE":
                continue
            item = next(
                (
                    value
                    for value in items
                    if str(value["item_id"]) == str(operation["item_id"])
                ),
                None,
            )
            if item is None or not item["current_audio_asset_id"]:
                continue
            asset = assets_by_id.get(str(item["current_audio_asset_id"]))
            if asset is None:
                continue
            operation_payload = _object(operation["payload_json"], {})
            operation_result = _object(operation["result_json"], {})
            asset_external_ref = _object(asset["external_ref_json"], {})
            remote_item_id = str(operation_result.get("item_id") or "").strip()
            if not remote_item_id or remote_item_id != str(
                asset_external_ref.get("remote_item_id") or ""
            ).strip():
                continue
            result_batch_id = str(operation_result.get("batch_id") or "").strip()
            asset_batch_id = str(asset_external_ref.get("batch_id") or "").strip()
            if result_batch_id and asset_batch_id and result_batch_id != asset_batch_id:
                continue
            if operation_payload.get("retry") is True:
                try:
                    result_generation = int(
                        operation_result.get("generation_version") or 0
                    )
                    asset_generation = int(
                        asset_external_ref.get("generation_version") or 0
                    )
                except (TypeError, ValueError):
                    continue
                if result_generation <= 0 or result_generation != asset_generation:
                    continue
            connection.execute(
                """
                UPDATE project_operations
                SET status='SUCCEEDED', error_code=NULL, error_message=NULL,
                    updated_at=?, finished_at=COALESCE(finished_at, ?)
                WHERE operation_id=?
                """,
                (now, now, operation["operation_id"]),
            )
            completed_operation_ids.add(str(operation["operation_id"]))

        active_types_by_item: dict[str, set[str]] = defaultdict(set)
        for operation in active_operations:
            if str(operation["operation_id"]) in completed_operation_ids:
                continue
            active_types_by_item[str(operation["item_id"])].add(
                str(operation["operation_type"])
            )

        repaired_items = 0
        for item in items:
            item_id = str(item["item_id"])
            status = str(item["status"])
            active_types = active_types_by_item.get(item_id, set())
            if status in {"AUDIO_QUEUED", "AUDIO_RUNNING"}:
                if "AUDIO_GENERATE" in active_types:
                    continue
            elif status == "POSTPROCESS_RUNNING":
                if active_types & {"POSTPROCESS_GENERATE", "POSTPROCESS_EXPORT"}:
                    continue
            else:
                continue

            subtitles = _object(item["subtitles_json"], _default_subtitles())
            preview_ready = str(subtitles.get("status") or "") == "PREVIEW_READY"
            if item["current_video_asset_id"] or (
                item["current_base_video_asset_id"] and preview_ready
            ):
                next_status = "COMPOSITION_READY"
            elif item["current_base_video_asset_id"]:
                next_status = "BASE_VIDEO_READY"
            elif item["current_audio_asset_id"]:
                next_status = "AUDIO_READY"
            else:
                next_status = "DRAFT"
            if next_status == status:
                continue
            connection.execute(
                "UPDATE project_items SET status=?, updated_at=? WHERE item_id=?",
                (next_status, now, item_id),
            )
            repaired_items += 1

        recovered = len(completed_operation_ids) + repaired_items
        if recovered:
            self._refresh_project_status(connection, project_id, now=now)
        return recovered

    def _refresh_project_status(
        self, connection: sqlite3.Connection, project_id: str, *, now: str
    ) -> None:
        rows = connection.execute(
            """
            SELECT status, current_audio_asset_id, current_base_video_asset_id,
                   current_video_asset_id
            FROM project_items WHERE project_id=? ORDER BY position
            """,
            (project_id,),
        ).fetchall()
        statuses = [str(row["status"]) for row in rows]
        if any(status in ACTIVE_ITEM_STATUSES for status in statuses):
            project_status = "PROCESSING"
        elif statuses and all(status == "VARIANT_READY" for status in statuses):
            project_status = "VARIANT_READY"
        elif any(status in FAILED_ITEM_STATUSES for status in statuses):
            successful = any(
                row["current_audio_asset_id"]
                or row["current_base_video_asset_id"]
                or row["current_video_asset_id"]
                for row in rows
            )
            project_status = "PARTIAL_FAILED" if successful else "FAILED"
        elif rows and all(
            row["current_video_asset_id"]
            or str(row["status"]) in {"COMPOSITION_READY", "VARIANT_READY"}
            for row in rows
        ):
            project_status = "COMPOSITION_READY"
        elif rows and all(row["current_base_video_asset_id"] for row in rows):
            project_status = "BASE_VIDEO_READY"
        elif rows and all(row["current_audio_asset_id"] for row in rows):
            project_status = "AUDIO_READY"
        else:
            project_status = "DRAFT"
        connection.execute(
            "UPDATE projects SET status=?, updated_at=? WHERE project_id=?",
            (project_status, now, project_id),
        )

    def _project_payload(
        self, connection: sqlite3.Connection, project_id: str
    ) -> dict[str, Any]:
        project = connection.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise KeyError("项目不存在")
        item_rows = connection.execute(
            "SELECT * FROM project_items WHERE project_id=? ORDER BY position",
            (project_id,),
        ).fetchall()
        asset_rows = connection.execute(
            """
            SELECT * FROM project_assets
            WHERE project_id=? ORDER BY item_id, asset_type, version
            """,
            (project_id,),
        ).fetchall()
        operation_rows = connection.execute(
            """
            SELECT * FROM project_operations
            WHERE project_id=? ORDER BY rowid
            """,
            (project_id,),
        ).fetchall()
        link_rows = connection.execute(
            """
            SELECT * FROM project_links
            WHERE project_id=? ORDER BY rowid
            """,
            (project_id,),
        ).fetchall()
        input_image_rows = connection.execute(
            """
            SELECT * FROM project_input_images
            WHERE project_id=? ORDER BY position
            """,
            (project_id,),
        ).fetchall()
        script_source_rows = connection.execute(
            """
            SELECT * FROM project_script_sources
            WHERE project_id=? ORDER BY version
            """,
            (project_id,),
        ).fetchall()
        result_batch_rows = connection.execute(
            """
            SELECT * FROM project_result_batches
            WHERE project_id=? ORDER BY date_key, batch_no
            """,
            (project_id,),
        ).fetchall()

        assets_by_item: dict[str, list[dict[str, Any]]] = {}
        for row in asset_rows:
            asset = self._asset_payload(row)
            if asset["asset_type"] == "input_image":
                input_image_id = str(asset["external_ref"].get("input_image_id") or "")
                if input_image_id:
                    asset["url"] = (
                        f"/api/new/projects/{project_id}/images/{input_image_id}"
                    )
            assets_by_item.setdefault(str(row["item_id"]), []).append(asset)

        items: list[dict[str, Any]] = []
        for row in item_rows:
            assets = assets_by_item.get(str(row["item_id"]), [])
            by_id = {asset["asset_id"]: asset for asset in assets}
            history: dict[str, list[dict[str, Any]]] = {}
            for asset in assets:
                history.setdefault(asset["asset_type"], []).append(asset)
            current_audio = by_id.get(str(row["current_audio_asset_id"] or ""))
            minimax_audio = next(
                (
                    asset
                    for asset in reversed(history.get("audio", []))
                    if asset.get("source_type") == "minimax"
                    and asset.get("status") == "READY"
                ),
                None,
            )
            current_base_video = by_id.get(
                str(row["current_base_video_asset_id"] or "")
            )
            current_video = by_id.get(str(row["current_video_asset_id"] or ""))
            current_image = by_id.get(str(row["current_image_asset_id"] or ""))
            variants = [
                asset
                for asset in assets
                if asset["asset_type"] == "variant_video"
                and asset["status"] == "READY"
            ]
            original_segments = [
                asset
                for asset in assets
                if asset["asset_type"] == "original_video_segment"
            ]
            subtitles = _object(row["subtitles_json"], _default_subtitles())
            item_settings = _object(row["settings_json"], {})
            item_payload = {
                "item_id": row["item_id"],
                "row_key": row["row_key"],
                "position": int(row["position"]),
                "script_text": row["script_text"],
                "status": row["status"],
                "settings": item_settings,
                "inputs": {
                    "image": current_image,
                    "h3_reference_video": (
                        by_id.get(
                            str(
                                item_settings.get("h3", {}).get(
                                    "reference_video_asset_id"
                                )
                                or ""
                            )
                        )
                        if isinstance(item_settings.get("h3"), dict)
                        else None
                    ),
                    "image_mapping_target": item_settings.get("image_mapping_target")
                    is True,
                },
                "outputs": {
                    "audio": current_audio,
                    # The three generation modes share this immutable MiniMax
                    # input. H3 may have a different current authoritative
                    # output audio for its own video/subtitle post-processing.
                    "minimax_audio": minimax_audio,
                    "base_video": current_base_video,
                    "composition_video": current_video,
                    "original_video_segments": original_segments,
                    "variants": variants,
                },
                "asset_history": history,
                "subtitles": subtitles,
                "content_analysis": _content_analysis_snapshot(
                    _object(row["content_analysis_json"], {}),
                    str(row["script_text"]),
                ),
                "visual_analysis": _visual_analysis_snapshot(
                    _object(row["visual_analysis_json"], {}),
                    str(row["script_text"]),
                    current_audio_asset_id=row["current_audio_asset_id"],
                    current_raw_cues=subtitles.get("raw_cues", []),
                    validate_media_binding=True,
                ),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            item_payload["allowed_actions"] = self._item_actions(item_payload)
            items.append(item_payload)

        payload = {
            "schema": "jyd.project.v1",
            "project_id": project["project_id"],
            "project_no": project["project_no"],
            "name": project["name"],
            "status": project["status"],
            "revision": int(project["revision"]),
            "owner": {
                "user_id": project["owner_user_id"],
                "username": project["owner_username"],
            },
            "settings": _object(project["settings_json"], {}),
            "input_images": [
                self._input_image_payload(row) for row in input_image_rows
            ],
            "script_source": (
                self._script_source_payload(script_source_rows[-1])
                if script_source_rows
                else None
            ),
            "script_source_history": [
                self._script_source_payload(row) for row in script_source_rows
            ],
            "result_batches": [
                self._result_batch_payload(row) for row in result_batch_rows
            ],
            "items": items,
            "content_analysis_summary": self._content_analysis_summary(items),
            "visual_analysis_summary": self._visual_analysis_summary(items),
            "operations": [
                self._operation_payload(row) for row in operation_rows
            ],
            "links": [self._link_payload(row) for row in link_rows],
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
        }
        payload["allowed_actions"] = self._project_actions(payload)
        return payload

    @staticmethod
    def _item_actions(item: dict[str, Any]) -> dict[str, bool]:
        status = str(item["status"])
        outputs = item["outputs"]
        audio_ready = outputs["audio"] is not None
        base_video_ready = outputs["base_video"] is not None
        video_ready = outputs["composition_video"] is not None
        base_quality_variant = str(
            (outputs["base_video"] or {}).get("metadata", {}).get("quality_variant")
            or ""
        )
        latest_segments_by_index: dict[str, dict[str, Any]] = {}
        for segment in outputs["original_video_segments"]:
            video_index = str(
                segment.get("external_ref", {}).get("video_index") or ""
            )
            if video_index:
                latest_segments_by_index[video_index] = segment
        segments_are_seedvr2 = bool(latest_segments_by_index) and all(
            str(segment.get("metadata", {}).get("quality_variant") or "")
            == "seedvr2_upscaled"
            for segment in latest_segments_by_index.values()
        )
        seedvr2_ready = (
            base_quality_variant == "seedvr2_upscaled" or segments_are_seedvr2
        )
        preview_ready = (
            base_video_ready
            and str(item.get("subtitles", {}).get("status") or "")
            == "PREVIEW_READY"
            and status in {"COMPOSITION_READY", "VARIANT_READY"}
        )
        composition_ready = video_ready or preview_ready
        active = status in ACTIVE_ITEM_STATUSES
        analysis_status = str(
            item.get("content_analysis", {}).get("overall_status") or "NOT_REQUESTED"
        )
        visual_status = str(
            item.get("visual_analysis", {}).get("analysis_status")
            or "NOT_REQUESTED"
        )
        return {
            "edit_inputs": status == "DRAFT",
            "delete_item": not active and analysis_status != "PENDING",
            "replace_image": status in IMAGE_EDITABLE_ITEM_STATUSES,
            "set_image_mapping_target": True,
            "edit_postprocess": not active,
            "generate_audio": not active,
            "retry_audio": status == "AUDIO_FAILED",
            "download_audio": audio_ready,
            "start_composition": audio_ready and not base_video_ready and not active,
            "retry_composition": status == "COMPOSITION_FAILED" and not base_video_ready,
            "backfill_seedvr2": bool(latest_segments_by_index)
            and not seedvr2_ready
            and not active,
            "start_postprocess": base_video_ready and not composition_ready and not active,
            "retry_postprocess": status == "COMPOSITION_FAILED" and base_video_ready,
            "download_current_video": video_ready,
            "download_base_video": base_video_ready,
            "download_original_materials": bool(
                outputs["original_video_segments"]
            ),
            "upload_current_video": composition_ready and not active,
            "generate_variants": composition_ready and not active,
            "retry_variants": status == "VARIANT_FAILED",
            "analyze_content": not active and analysis_status != "PENDING",
            "retry_content_analysis": not active
            and analysis_status in {"FAILED", "PARTIAL"},
            "analyze_visuals": not active and visual_status != "PENDING",
            "retry_visual_analysis": not active and visual_status == "FAILED",
            "edit_visual_overlays": not active,
        }

    @staticmethod
    def _content_analysis_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        counts = {
            "NOT_REQUESTED": 0,
            "PENDING": 0,
            "SUCCESS": 0,
            "PARTIAL": 0,
            "FAILED": 0,
        }
        for item in items:
            status = str(
                item.get("content_analysis", {}).get("overall_status")
                or "NOT_REQUESTED"
            )
            counts[status if status in counts else "FAILED"] += 1
        if counts["PENDING"]:
            overall = "PENDING"
        elif counts["FAILED"] or counts["PARTIAL"]:
            overall = "PARTIAL" if counts["SUCCESS"] or counts["PARTIAL"] else "FAILED"
        elif counts["SUCCESS"] == len(items) and items:
            overall = "SUCCESS"
        else:
            overall = "NOT_REQUESTED"
        return {
            "status": overall,
            "total": len(items),
            "counts": counts,
            "concurrency_limit": 10,
        }

    @staticmethod
    def _visual_analysis_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        counts = {"NOT_REQUESTED": 0, "PENDING": 0, "SUCCESS": 0, "FAILED": 0}
        overlay_count = 0
        for item in items:
            analysis = item.get("visual_analysis", {})
            status = str(analysis.get("analysis_status") or "NOT_REQUESTED")
            counts[status if status in counts else "FAILED"] += 1
            overlay_count += sum(
                overlay.get("enabled") is not False
                for overlay in analysis.get("recipe", {}).get("overlays", [])
                if isinstance(overlay, dict)
            )
        if counts["PENDING"]:
            overall = "PENDING"
        elif counts["FAILED"]:
            overall = "PARTIAL" if counts["SUCCESS"] else "FAILED"
        elif counts["SUCCESS"] == len(items) and items:
            overall = "SUCCESS"
        else:
            overall = "NOT_REQUESTED"
        return {
            "status": overall,
            "total": len(items),
            "counts": counts,
            "overlay_count": overlay_count,
            "concurrency_limit": 10,
        }

    @staticmethod
    def _project_actions(project: dict[str, Any]) -> dict[str, bool]:
        items = project["items"]
        scoped_items = [
            item
            for item in items
            if item.get("inputs", {}).get("image_mapping_target") is True
        ]
        mapping_items = scoped_items or items
        return {
            "edit_inputs": bool(items)
            and all(item["allowed_actions"]["edit_inputs"] for item in items),
            "analyze_content": bool(items)
            and any(item["allowed_actions"]["analyze_content"] for item in items),
            "retry_content_analysis": any(
                item["allowed_actions"]["retry_content_analysis"] for item in items
            ),
            "analyze_visuals": bool(items)
            and any(item["allowed_actions"]["analyze_visuals"] for item in items),
            "retry_visual_analysis": any(
                item["allowed_actions"]["retry_visual_analysis"] for item in items
            ),
            # Adding an unassigned image, or deleting an unused one, cannot mutate
            # another row's frozen input. When a mapping scope exists, only its
            # rows participate in later bulk remapping.
            "manage_input_images": bool(items),
            "apply_image_mapping": bool(project.get("input_images"))
            and bool(mapping_items)
            and all(
                item["allowed_actions"]["replace_image"] for item in mapping_items
            ),
            "generate_audio": bool(items)
            and all(item["allowed_actions"]["generate_audio"] for item in items),
            "retry_audio": any(
                item["allowed_actions"]["retry_audio"] for item in items
            ),
            "start_composition": bool(items)
            and all(item["allowed_actions"]["start_composition"] for item in items),
            "retry_composition": any(
                item["allowed_actions"]["retry_composition"] for item in items
            ),
            "start_postprocess": bool(items)
            and all(item["allowed_actions"]["start_postprocess"] for item in items),
            "retry_postprocess": any(
                item["allowed_actions"]["retry_postprocess"] for item in items
            ),
            "download_audio": any(
                item["allowed_actions"]["download_audio"] for item in items
            ),
            "download_current_video": any(
                item["allowed_actions"]["download_current_video"] for item in items
            ),
            "download_base_video": any(
                item["allowed_actions"]["download_base_video"] for item in items
            ),
            "download_original_materials": any(
                item["allowed_actions"]["download_original_materials"]
                for item in items
            ),
            "generate_variants": bool(items)
            and all(item["allowed_actions"]["generate_variants"] for item in items),
            "retry_variants": any(
                item["allowed_actions"]["retry_variants"] for item in items
            ),
        }

    def _asset_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        managed_path, _managed_path_ref, _path_error = self._payload_managed_path(
            row["managed_path"]
        )
        path = Path(managed_path) if managed_path else None
        try:
            file_exists = bool(path and path.is_file())
            actual_size_bytes = path.stat().st_size if file_exists and path else 0
        except OSError:
            file_exists = False
            actual_size_bytes = 0
        return {
            "asset_id": row["asset_id"],
            "asset_type": row["asset_type"],
            "version": int(row["version"]),
            "status": row["status"],
            "source_type": row["source_type"],
            "filename": row["filename"],
            "managed_path": managed_path,
            "file_exists": file_exists,
            "actual_size_bytes": actual_size_bytes,
            "external_ref": _object(row["external_ref_json"], {}),
            "metadata": _object(row["metadata_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _input_image_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        managed_path, _managed_path_ref, _path_error = self._payload_managed_path(
            row["managed_path"]
        )
        path = Path(managed_path) if managed_path else None
        try:
            file_exists = bool(path and path.is_file())
            actual_size_bytes = path.stat().st_size if file_exists and path else 0
        except OSError:
            file_exists = False
            actual_size_bytes = 0
        return {
            "image_id": row["image_id"],
            "position": int(row["position"]),
            "filename": row["filename"],
            "content_type": row["content_type"],
            "size_bytes": int(row["size_bytes"]),
            "sha256": row["sha256"],
            "managed_path": managed_path,
            "file_exists": file_exists,
            "actual_size_bytes": actual_size_bytes,
            "url": f"/api/new/projects/{row['project_id']}/images/{row['image_id']}",
            "created_at": row["created_at"],
        }

    def _script_source_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        managed_path, _managed_path_ref, _path_error = self._payload_managed_path(
            row["managed_path"]
        )
        return {
            "source_id": row["source_id"],
            "version": int(row["version"]),
            "filename": row["filename"],
            "content_type": row["content_type"],
            "size_bytes": int(row["size_bytes"]),
            "sha256": row["sha256"],
            "managed_path": managed_path,
            "created_at": row["created_at"],
        }

    @staticmethod
    def _result_batch_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "result_batch_id": row["result_batch_id"],
            "project_id": row["project_id"],
            "date_key": row["date_key"],
            "date_label": row["date_label"],
            "batch_no": int(row["batch_no"]),
            "export_path": row["export_path"],
            "operation_type": row["operation_type"],
            "status": row["status"],
            "jianying_batch_id": row["jianying_batch_id"],
            "error_message": row["error_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _operation_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "operation_id": row["operation_id"],
            "correlation_id": row["correlation_id"],
            "item_id": row["item_id"],
            "operation_type": row["operation_type"],
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "attempt_count": int(row["attempt_count"]),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "payload": _object(row["payload_json"], {}),
            "result": _object(row["result_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    @staticmethod
    def _link_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "link_id": row["link_id"],
            "item_id": row["item_id"],
            "system": row["system"],
            "relation": row["relation"],
            "external_id": row["external_id"],
            "metadata": _object(row["metadata_json"], {}),
            "created_at": row["created_at"],
        }
