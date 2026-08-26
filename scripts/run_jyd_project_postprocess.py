from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path


def _load_coordinator(jyd_root: Path):
    sys.path.insert(0, str(jyd_root / "src"))
    from jyd_probe.asset_admin import AssetAdminCatalog
    from jyd_probe.caption_alignment import FunASRCaptionAligner
    from jyd_probe.music_matching import MusicProfileMatcher
    from jyd_probe.project_postprocess import ProjectPostprocessCoordinator
    from jyd_probe.project_store import ProjectStore
    from jyd_probe.semantic_visuals import load_semantic_visual_catalog
    from jyd_probe.web_api import (
        FONT_LIBRARY_ROOT,
        SEMANTIC_VISUAL_LIBRARY_ROOT,
        RenderJobQueue,
        _combined_library_items,
        _decorate_audio_snapshot,
        _list_font_library,
        _list_system_fonts,
        default_settings,
    )

    settings = default_settings()
    render_queue = RenderJobQueue(settings)
    store = ProjectStore(render_queue.store.path)
    asset_admin = AssetAdminCatalog(settings.storage_root / "asset_admin.json")
    fonts = asset_admin.decorate(
        "font",
        _combined_library_items(
            settings, FONT_LIBRARY_ROOT, "font_library", _list_font_library
        ),
    )
    fonts = _list_system_fonts() + fonts
    fonts = [
        item
        for item in fonts
        if item.get("available")
        and item.get("enabled", True)
        and item.get("path")
        and Path(str(item["path"])).is_file()
        and Path(str(item["path"])).stat().st_size > 1024
    ]
    fonts.sort(
        key=lambda item: (
            str(item.get("identity") or "") != "resource_id:7244518590332801592",
            not str(item.get("identity") or "").startswith("system:simhei"),
            str(item.get("name") or "").casefold(),
        )
    )
    audio = _decorate_audio_snapshot(
        render_queue.audio_catalog.snapshot(), asset_admin
    ).get("assets", [])
    bgm_assets = [
        item
        for item in audio
        if isinstance(item, dict)
        and item.get("identity")
        and item.get("enabled", True)
        and not item.get("deleted", False)
    ]
    semantic_catalog = load_semantic_visual_catalog(SEMANTIC_VISUAL_LIBRARY_ROOT)
    aligner = (
        FunASRCaptionAligner(
            settings.asr_base_url,
            timeout_seconds=settings.asr_timeout_seconds,
            shared_token=settings.asr_shared_token,
        )
        if settings.asr_base_url
        else None
    )
    coordinator = ProjectPostprocessCoordinator(
        store,
        render_queue,
        storage_root=settings.storage_root,
        draft_root=settings.default_draft_root,
        fonts=fonts,
        bgm_assets=bgm_assets,
        music_matcher=MusicProfileMatcher(settings.audio_library_root),
        caption_aligner=aligner,
        require_precise_alignment=settings.asr_required,
        semantic_visual_library_root=semantic_catalog.root,
        semantic_visual_catalog=semantic_catalog,
    )
    return settings, store, coordinator, fonts, bgm_assets


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run JYD 4B subtitles/BGM/nameplate for one existing project"
    )
    parser.add_argument("--jyd-root", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument(
        "--bgm-identity", default="music_id:7340141583693269011"
    )
    parser.add_argument("--layout-profile", default="seated")
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--sync-only", action="store_true")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()

    _, store, coordinator, fonts, bgm_assets = _load_coordinator(
        Path(args.jyd_root).expanduser().resolve()
    )
    bgm_identities = {str(item.get("identity") or "") for item in bgm_assets}
    if args.bgm_identity not in bgm_identities:
        raise ValueError(f"指定 BGM 不在当前可用素材库：{args.bgm_identity}")
    project = coordinator.sync(args.owner_user_id, args.project_id)
    if len(project.get("items", [])) != 10:
        raise ValueError("目标项目不是 10 行，停止批量后处理")
    font_identity = str(fonts[0].get("identity") or "")
    item_settings = [
        {
            "item_id": item["item_id"],
            "font_identity": font_identity,
            "bgm_identity": args.bgm_identity,
            "bgm_selection_mode": "manual",
            "text_color": "#FFFFFF",
            "layout_profile": args.layout_profile,
        }
        for item in project["items"]
    ]
    result = (
        project
        if args.sync_only or args.export
        else coordinator.start(
            args.owner_user_id,
            args.project_id,
            idempotency_key=f"h3-final-10-postprocess-{uuid.uuid4().hex}",
            item_settings=item_settings,
        )
    )
    print(
        json.dumps(
            {
                "start": result,
                "font_identity": font_identity,
                "bgm_identity": args.bgm_identity,
                "layout_profile": args.layout_profile,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    if args.export:
        for item in project["items"]:
            if item.get("outputs", {}).get("composition_video") is not None:
                continue
            coordinator.export_preview(
                args.owner_user_id,
                args.project_id,
                item["item_id"],
                idempotency_key=f"h3-final-export-{item['row_key']}-{uuid.uuid4().hex}",
            )
    if args.wait_seconds <= 0:
        return 0

    deadline = time.monotonic() + args.wait_seconds
    while time.monotonic() < deadline:
        current = coordinator.sync(args.owner_user_id, args.project_id)
        summary = {
            str(item["row_key"]): str(item["status"])
            for item in current.get("items", [])
        }
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        current_items = current.get("items", [])
        if args.export:
            finished = all(
                item.get("outputs", {}).get("composition_video") is not None
                or str(item.get("status")) == "COMPOSITION_FAILED"
                for item in current_items
            )
        else:
            finished = all(
                status
                in {"COMPOSITION_READY", "VARIANT_READY", "COMPOSITION_FAILED"}
                for status in summary.values()
            )
        if finished:
            break
        time.sleep(10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
