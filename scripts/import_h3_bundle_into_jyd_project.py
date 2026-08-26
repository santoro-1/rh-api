from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


def _compact(value: object) -> str:
    return "".join(str(value or "").split())


def _run(*args: str) -> None:
    subprocess.run(args, check=True)


def _duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _prepare_handoffs(bundle_root: Path) -> list[Path]:
    handoff_root = bundle_root / "_JYD交接"
    handoff_root.mkdir(parents=True, exist_ok=True)
    manifests: list[Path] = []
    bundle_token = hashlib.sha256(str(bundle_root).encode("utf-8")).hexdigest()[:16]

    for number in range(1, 11):
        row = f"{number:02d}"
        row_dir = bundle_root / row
        metadata_path = row_dir / "输入与参数.json"
        master_path = bundle_root / f"{row}_最终成片.mp4"
        if not metadata_path.is_file() or not master_path.is_file():
            raise FileNotFoundError(f"第 {row} 条缺少输入参数或最终成片")

        metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        script = str(metadata.get("script_text") or "").strip()
        segments = metadata.get("segments")
        if not script or not isinstance(segments, list) or not segments:
            raise ValueError(f"第 {row} 条脚本或分段信息不完整")
        segment_texts = [str(item.get("script_text") or "").strip() for item in segments]
        if not all(segment_texts) or _compact("".join(segment_texts)) != _compact(script):
            raise ValueError(f"第 {row} 条分段文案与冻结脚本不一致")

        row_handoff = handoff_root / row
        row_handoff.mkdir(parents=True, exist_ok=True)
        silent_video = row_handoff / f"{row}_H3无声底片.mp4"
        h3_audio = row_handoff / f"{row}_H3权威音轨.wav"
        cues_path = row_handoff / f"{row}_H3分段窗口.json"
        manifest_path = row_handoff / f"{row}_handoff.v2.json"

        _run(
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(master_path),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-an",
            str(silent_video),
        )
        _run(
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(master_path),
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(h3_audio),
        )

        total_audio_seconds = _duration(h3_audio)
        weights = [
            max(float(item.get("effective_duration_seconds") or 0), 0.001)
            for item in segments
        ]
        weight_total = sum(weights)
        cues: list[dict[str, Any]] = []
        cursor = 0.0
        for index, (text, weight) in enumerate(zip(segment_texts, weights, strict=True)):
            end = (
                total_audio_seconds
                if index == len(segment_texts) - 1
                else cursor + total_audio_seconds * weight / weight_total
            )
            cues.append(
                {
                    "text": text,
                    "start_seconds": round(cursor, 6),
                    "end_seconds": round(end, 6),
                }
            )
            cursor = end
        cues_path.write_text(
            json.dumps(cues, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        segment_ids = [
            str(item.get("runninghub_task_id") or f"{row}-{index + 1}")
            for index, item in enumerate(segments)
        ]
        external_project_id = f"h3-final-bundle-{bundle_token}-row-{row}"
        identity = {
            "project_id": external_project_id,
            "schema_version": "h3.jyd_handoff.v2",
            "segment_ids": segment_ids,
        }
        handoff_id = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        manifest = {
            "schema_version": "h3.jyd_handoff.v2",
            "project_id": external_project_id,
            "handoff_id": handoff_id,
            "h3_master": {
                "path": str(master_path.resolve()),
                "audio_video_pair": "h3_generated",
            },
            "base_video": {
                "role": "base_video",
                "path": str(silent_video.resolve()),
                "audio_policy": "separate_h3_generated_audio",
                "source_segment_ids": segment_ids,
            },
            "authoritative_audio": {
                "path": str(h3_audio.resolve()),
                "source": "h3_generated_audio",
                "timeline_start_seconds": 0.0,
                "reuse_once": True,
            },
            "subtitles": {
                "raw_cues_asset": str(cues_path.resolve()),
                "timing_source": "h3_segment_windows_then_funasr",
            },
            "source": {"row_key": row, "script_text": script},
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifests.append(manifest_path)
    return manifests


def _attach_to_project(
    *,
    jyd_root: Path,
    database_path: Path,
    project_id: str,
    owner_user_id: str,
    manifests: list[Path],
) -> dict[str, Any]:
    sys.path.insert(0, str(jyd_root / "src"))
    from jyd_probe.h3_handoff import load_h3_handoff
    from jyd_probe.project_store import ProjectStore, _now, _object

    store = ProjectStore(database_path)
    handoffs = [load_h3_handoff(path) for path in manifests]
    with store._transaction() as connection:
        project = store._owned_project(connection, owner_user_id, project_id)
        rows = connection.execute(
            "SELECT * FROM project_items WHERE project_id=? ORDER BY position",
            (project_id,),
        ).fetchall()
        if len(rows) != len(handoffs):
            raise ValueError("目标工作台项目行数不是 10，停止导入")

        now = _now()
        imported: list[dict[str, str]] = []
        for item, handoff in zip(rows, handoffs, strict=True):
            if str(item["row_key"]).zfill(2) != handoff["row_key"].zfill(2):
                raise ValueError(f"目标项目行号与 H3 第 {handoff['row_key']} 条不一致")
            if _compact(item["script_text"]) != _compact(handoff["script_text"]):
                raise ValueError(f"目标项目第 {handoff['row_key']} 条文案不一致")

            existing = connection.execute(
                "SELECT project_id,item_id FROM project_links "
                "WHERE system='h3_workbench' AND relation='imported_handoff' "
                "AND external_id=?",
                (handoff["handoff_id"],),
            ).fetchone()
            if existing is not None:
                if existing["project_id"] != project_id or existing["item_id"] != item["item_id"]:
                    raise ValueError(f"第 {handoff['row_key']} 条交接已绑定到其它项目")
                imported.append({"row_key": handoff["row_key"], "status": "already_imported"})
                continue

            audio_version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM project_assets "
                    "WHERE item_id=? AND asset_type='audio'",
                    (item["item_id"],),
                ).fetchone()[0]
            )
            video_version = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM project_assets "
                    "WHERE item_id=? AND asset_type='base_video'",
                    (item["item_id"],),
                ).fetchone()[0]
            )
            audio_id = uuid.uuid4().hex
            video_id = uuid.uuid4().hex
            script_sha256 = hashlib.sha256(
                handoff["script_text"].encode("utf-8")
            ).hexdigest()
            audio_duration_us = round(_duration(handoff["audio_path"]) * 1_000_000)
            video_duration_us = round(
                _duration(handoff["base_video_path"]) * 1_000_000
            )
            connection.execute(
                """
                INSERT INTO project_assets(
                    asset_id,project_id,item_id,asset_type,version,status,source_type,
                    filename,managed_path,external_ref_json,metadata_json,created_at,updated_at
                ) VALUES(?,?,?,'audio',?,'READY','h3_handoff',?,?,?, ?,?,?)
                """,
                (
                    audio_id,
                    project_id,
                    item["item_id"],
                    audio_version,
                    handoff["audio_path"].name,
                    str(handoff["audio_path"]),
                    _json({"handoff_id": handoff["handoff_id"]}),
                    _json(
                        {
                            "authoritative": True,
                            "timeline_start_seconds": 0.0,
                            "source": "h3_generated_audio",
                            "duration_us": audio_duration_us,
                            "script_sha256": script_sha256,
                        }
                    ),
                    now,
                    now,
                ),
            )
            store._set_current_asset(
                connection,
                item,
                asset_id=audio_id,
                asset_type="audio",
                source_type="h3_handoff",
                asset_status="READY",
                now=now,
            )
            subtitles = {
                "source": "h3_segment_windows",
                "raw_cues": handoff["raw_cues"],
                "render_cues": handoff["raw_cues"],
                "bound_audio_asset_id": audio_id,
                "bound_video_asset_id": None,
                "style": {},
                "status": "READY",
            }
            connection.execute(
                "UPDATE project_items SET subtitles_json=?,updated_at=? WHERE item_id=?",
                (_json(subtitles), now, item["item_id"]),
            )
            connection.execute(
                """
                INSERT INTO project_assets(
                    asset_id,project_id,item_id,asset_type,version,status,source_type,
                    filename,managed_path,external_ref_json,metadata_json,created_at,updated_at
                ) VALUES(?,?,?,'base_video',?,'READY','h3_handoff',?,?,?, ?,?,?)
                """,
                (
                    video_id,
                    project_id,
                    item["item_id"],
                    video_version,
                    handoff["base_video_path"].name,
                    str(handoff["base_video_path"]),
                    _json({"handoff_id": handoff["handoff_id"]}),
                    _json(
                        {
                            "audio_policy": "separate_h3_generated_audio",
                            "source_segment_ids": handoff["manifest"]["base_video"]["source_segment_ids"],
                            "h3_master_path": str(handoff["master_path"]),
                            "duration_us": video_duration_us,
                        }
                    ),
                    now,
                    now,
                ),
            )
            current = store._owned_item(connection, project_id, item["item_id"])
            store._set_current_asset(
                connection,
                current,
                asset_id=video_id,
                asset_type="base_video",
                source_type="h3_handoff",
                asset_status="READY",
                now=now,
            )
            settings = _object(item["settings_json"], {})
            settings["source_workbench"] = "minimax_h3_ref2va"
            connection.execute(
                "UPDATE project_items SET settings_json=?,updated_at=? WHERE item_id=?",
                (_json(settings), now, item["item_id"]),
            )
            connection.execute(
                """
                INSERT INTO project_links(
                    link_id,project_id,item_id,system,relation,external_id,metadata_json,created_at
                ) VALUES(?,?,?,'h3_workbench','imported_handoff',?,?,?)
                """,
                (
                    uuid.uuid4().hex,
                    project_id,
                    item["item_id"],
                    handoff["handoff_id"],
                    _json(
                        {
                            "manifest_path": str(handoff["manifest_path"]),
                            "handoff_schema": handoff["schema_version"],
                        }
                    ),
                    now,
                ),
            )
            imported.append({"row_key": handoff["row_key"], "status": "imported"})

        project_settings = _object(project["settings_json"], {})
        project_settings["source_workbench"] = "minimax_h3_ref2va"
        connection.execute(
            "UPDATE projects SET settings_json=?,revision=revision+1,updated_at=? WHERE project_id=?",
            (_json(project_settings), now, project_id),
        )
        store._refresh_project_status(connection, project_id, now=now)
    project_payload = store.get_project(owner_user_id, project_id)
    return {
        "project_id": project_id,
        "project_no": project_payload["project_no"],
        "project_status": project_payload["status"],
        "items": imported,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a 10-row H3 bundle and attach it to one existing JYD project"
    )
    parser.add_argument("--bundle-root", required=True)
    parser.add_argument("--jyd-root", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--owner-user-id", required=True)
    args = parser.parse_args()

    bundle_root = Path(args.bundle_root).expanduser().resolve()
    manifests = _prepare_handoffs(bundle_root)
    result = _attach_to_project(
        jyd_root=Path(args.jyd_root).expanduser().resolve(),
        database_path=Path(args.database).expanduser().resolve(),
        project_id=args.project_id,
        owner_user_id=args.owner_user_id,
        manifests=manifests,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
