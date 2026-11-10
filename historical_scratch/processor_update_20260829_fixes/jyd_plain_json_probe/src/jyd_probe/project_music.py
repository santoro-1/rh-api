from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Any, Mapping

from .draft_factory import probe_video_duration_us
from .music_matching import (
    MusicProfileError,
    MusicProfileMatcher,
    NoEligibleMusicError,
)


MUSIC_SELECTION_SCHEMA = "jyd.project-music-selection.v1"
MUSIC_SELECTION_MODES = {"auto", "manual"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _script_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def automatic_music_identity_counts(
    project: Mapping[str, Any], *, excluded_item_ids: set[str] | None = None
) -> dict[str, int]:
    """Count retained automatic selections so a project can avoid monotony."""

    excluded = excluded_item_ids or set()
    counts: dict[str, int] = {}
    for item in project.get("items") or []:
        if not isinstance(item, Mapping) or str(item.get("item_id") or "") in excluded:
            continue
        settings = item.get("settings") if isinstance(item.get("settings"), Mapping) else {}
        postprocess = (
            settings.get("postprocess")
            if isinstance(settings.get("postprocess"), Mapping)
            else {}
        )
        if postprocess.get("bgm_selection_mode") != "auto":
            continue
        identity = str(postprocess.get("bgm_identity") or "").strip()
        if identity:
            counts[identity] = counts.get(identity, 0) + 1
    return counts


def item_video_duration_us(item: Mapping[str, Any]) -> int:
    """Return the best local duration fact without calling any external service."""

    outputs = item.get("outputs") if isinstance(item.get("outputs"), dict) else {}
    base_video = (
        outputs.get("base_video") if isinstance(outputs.get("base_video"), dict) else {}
    )
    base_metadata = (
        base_video.get("metadata")
        if isinstance(base_video.get("metadata"), dict)
        else {}
    )
    duration = base_metadata.get("duration_us")
    if type(duration) is int and duration > 0:
        return duration

    # H3 assets created by older builds did not persist duration_us even though
    # the merged base video was already present on disk.  Probe that local file
    # before falling back to audio/subtitle timing so existing projects remain
    # usable when caption bindings are stale or incomplete.
    base_video_path = str(base_video.get("managed_path") or "").strip()
    if base_video_path:
        try:
            duration = probe_video_duration_us(base_video_path)
        except (OSError, RuntimeError, ValueError):
            duration = 0
        if duration > 0:
            return duration

    segment_ends = []
    for segment in outputs.get("original_video_segments") or []:
        if not isinstance(segment, dict):
            continue
        segment_metadata = (
            segment.get("metadata")
            if isinstance(segment.get("metadata"), dict)
            else {}
        )
        end_seconds = segment_metadata.get("end_seconds")
        if type(end_seconds) in {int, float} and end_seconds > 0:
            segment_ends.append(round(float(end_seconds) * 1_000_000))
    if segment_ends:
        return max(segment_ends)

    audio = outputs.get("audio") if isinstance(outputs.get("audio"), dict) else {}
    metadata = audio.get("metadata") if isinstance(audio.get("metadata"), dict) else {}
    duration = metadata.get("duration_us")
    if type(duration) is int and duration > 0:
        return duration

    subtitles = item.get("subtitles") if isinstance(item.get("subtitles"), dict) else {}
    bound_audio_id = str(subtitles.get("bound_audio_asset_id") or "")
    current_audio_id = str(audio.get("asset_id") or "")
    if not bound_audio_id or not current_audio_id or bound_audio_id == current_audio_id:
        cue_ends = []
        for cue in subtitles.get("raw_cues") or []:
            if not isinstance(cue, dict):
                continue
            end_us = cue.get("end_us")
            if type(end_us) is int and end_us > 0:
                cue_ends.append(end_us)
        if cue_ends:
            return max(cue_ends)

    return 0


def manual_music_selection(
    item: Mapping[str, Any], bgm_identity: str
) -> dict[str, Any]:
    identity = str(bgm_identity or "").strip()
    return {
        "schema": MUSIC_SELECTION_SCHEMA,
        "status": "MANUAL",
        "selection_source": "manual",
        "bgm_identity": identity or None,
        "reason_code": "USER_SELECTED" if identity else "USER_SELECTED_NONE",
        "reason_summary": "使用用户手动选择的 BGM" if identity else "用户明确选择无 BGM",
        "script_sha256": _script_sha256(str(item.get("script_text") or "")),
        "audio_asset_id": (
            (item.get("outputs") or {}).get("audio") or {}
        ).get("asset_id"),
        "video_duration_us": item_video_duration_us(item),
        "selected_at": _now(),
    }


class ProjectMusicSelector:
    """Resolve one row to exactly one BGM identity or an explicit no-BGM fallback."""

    def __init__(
        self,
        matcher: MusicProfileMatcher,
        available_bgm: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.matcher = matcher
        self.available_bgm = dict(available_bgm)

    def resolve_auto(
        self,
        project: Mapping[str, Any],
        item: Mapping[str, Any],
        *,
        recent_identity_counts: Mapping[str, int] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        duration_us = item_video_duration_us(item)
        return self._resolve(
            project,
            item,
            duration_us=duration_us,
            require_duration=True,
            recent_identity_counts=recent_identity_counts,
        )

    def resolve_for_analysis(
        self,
        project: Mapping[str, Any],
        item: Mapping[str, Any],
        *,
        recent_identity_counts: Mapping[str, int] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Choose a visible preliminary Top1 before generated audio has a duration."""

        return self._resolve(
            project,
            item,
            duration_us=0,
            require_duration=False,
            recent_identity_counts=recent_identity_counts,
        )

    def _resolve(
        self,
        project: Mapping[str, Any],
        item: Mapping[str, Any],
        *,
        duration_us: int,
        require_duration: bool,
        recent_identity_counts: Mapping[str, int] | None,
    ) -> tuple[str, dict[str, Any]]:
        analysis = (
            item.get("content_analysis")
            if isinstance(item.get("content_analysis"), dict)
            else {}
        )
        reason_code = "MUSIC_ANALYSIS_UNAVAILABLE"
        reason_summary = "音乐分析未成功"
        if require_duration and duration_us <= 0:
            reason_code = "DURATION_UNAVAILABLE"
            reason_summary = "当前音频或视频缺少可用的真实时长"
        elif analysis.get("music_analysis_status") == "SUCCESS" and isinstance(
            analysis.get("music_intent"), dict
        ):
            try:
                snapshot = self.matcher.snapshot()
                excluded = set(snapshot.get("assets_by_identity", {})) - set(
                    self.available_bgm
                )
                result = self.matcher.recommend(
                    analysis["music_intent"],
                    video_duration_us=duration_us,
                    excluded_identities=excluded,
                    recent_identity_counts=recent_identity_counts,
                )
                identity = str(result.get("bgm_identity") or "")
                if not identity or identity not in self.available_bgm:
                    raise NoEligibleMusicError("Top1 音乐当前不可用")
                return identity, {
                    "schema": MUSIC_SELECTION_SCHEMA,
                    "status": "SUCCESS",
                    **result,
                    "script_sha256": _script_sha256(
                        str(item.get("script_text") or "")
                    ),
                    "audio_asset_id": (
                        (item.get("outputs") or {}).get("audio") or {}
                    ).get("asset_id"),
                    "video_duration_us": duration_us,
                    "selected_at": _now(),
                    "reason_code": None,
                    "reason_summary": None,
                }
            except (MusicProfileError, NoEligibleMusicError, OSError, ValueError) as exc:
                reason_code = "LOCAL_MATCH_FAILED"
                reason_summary = str(exc)[:240]

        project_settings = (
            project.get("settings") if isinstance(project.get("settings"), dict) else {}
        )
        default_identity = str(project_settings.get("default_bgm_identity") or "").strip()
        if default_identity and default_identity in self.available_bgm:
            return default_identity, self._fallback(
                item,
                duration_us=duration_us,
                source="project_default",
                identity=default_identity,
                reason_code=reason_code,
                reason_summary=reason_summary,
            )
        return "", self._fallback(
            item,
            duration_us=duration_us,
            source="none",
            identity="",
            reason_code=reason_code,
            reason_summary=reason_summary,
        )

    @staticmethod
    def _fallback(
        item: Mapping[str, Any],
        *,
        duration_us: int,
        source: str,
        identity: str,
        reason_code: str,
        reason_summary: str,
    ) -> dict[str, Any]:
        return {
            "schema": MUSIC_SELECTION_SCHEMA,
            "status": "FALLBACK",
            "selection_source": source,
            "bgm_identity": identity or None,
            "reason_code": reason_code,
            "reason_summary": reason_summary,
            "script_sha256": _script_sha256(str(item.get("script_text") or "")),
            "audio_asset_id": (
                (item.get("outputs") or {}).get("audio") or {}
            ).get("asset_id"),
            "video_duration_us": duration_us,
            "selected_at": _now(),
        }
