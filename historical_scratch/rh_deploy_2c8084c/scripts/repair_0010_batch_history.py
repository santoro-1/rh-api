from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import struct
import uuid
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable


PAGE_HEADER_SIZE = 8
SEGMENT_PATH_RE = re.compile(
    r"^uploads/(?P<user_id>\d+)/(?P<group_id>[0-9a-f-]{36})/"
    r"segments/segment-(?P<index>\d+)\.mp3$"
)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(9):
        current = data[offset + index]
        if index == 8:
            return (value << 8) | current, offset + 9
        value = (value << 7) | (current & 0x7F)
        if current < 0x80:
            return value, offset + index + 1
    raise ValueError("invalid SQLite varint")


def _decode_record(payload: bytes) -> list[Any]:
    header_size, serial_offset = _read_varint(payload, 0)
    if header_size < serial_offset or header_size > len(payload):
        raise ValueError("invalid record header")

    serial_types: list[int] = []
    while serial_offset < header_size:
        serial_type, serial_offset = _read_varint(payload, serial_offset)
        serial_types.append(serial_type)
    if serial_offset != header_size:
        raise ValueError("record header did not end cleanly")

    values: list[Any] = []
    body_offset = header_size
    for serial_type in serial_types:
        if serial_type == 0:
            values.append(None)
            continue
        if serial_type in {8, 9}:
            values.append(serial_type - 8)
            continue
        if 1 <= serial_type <= 6:
            sizes = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}
            size = sizes[serial_type]
            raw = payload[body_offset : body_offset + size]
            if len(raw) != size:
                raise ValueError("truncated integer")
            values.append(int.from_bytes(raw, "big", signed=True))
            body_offset += size
            continue
        if serial_type == 7:
            raw = payload[body_offset : body_offset + 8]
            if len(raw) != 8:
                raise ValueError("truncated float")
            values.append(struct.unpack(">d", raw)[0])
            body_offset += 8
            continue
        if serial_type in {10, 11}:
            raise ValueError("reserved serial type")

        size = (
            (serial_type - 12) // 2
            if serial_type % 2 == 0
            else (serial_type - 13) // 2
        )
        raw = payload[body_offset : body_offset + size]
        if len(raw) != size:
            raise ValueError("truncated text/blob")
        values.append(raw if serial_type % 2 == 0 else raw.decode("utf-8"))
        body_offset += size

    if body_offset != len(payload):
        raise ValueError("record payload has trailing bytes")
    return values


def _cell_payload(
    database: bytes,
    *,
    page_size: int,
    page_offset: int,
    cell_offset: int,
) -> bytes:
    payload_size, cursor = _read_varint(database, page_offset + cell_offset)
    _, cursor = _read_varint(database, cursor)
    usable_size = page_size
    max_local = usable_size - 35
    min_local = ((usable_size - 12) * 32 // 255) - 23

    if payload_size <= max_local:
        local_size = payload_size
    else:
        local_size = min_local + (
            (payload_size - min_local) % (usable_size - 4)
        )
        if local_size > max_local:
            local_size = min_local

    local_end = cursor + local_size
    page_end = page_offset + page_size
    if local_end > page_end:
        raise ValueError("cell crosses page boundary")
    payload = bytearray(database[cursor:local_end])
    if local_size == payload_size:
        return bytes(payload)

    if local_end + 4 > page_end:
        raise ValueError("missing overflow pointer")
    overflow_page = int.from_bytes(database[local_end : local_end + 4], "big")
    remaining = payload_size - local_size
    visited: set[int] = set()
    while remaining:
        if overflow_page <= 0 or overflow_page in visited:
            raise ValueError("invalid overflow chain")
        visited.add(overflow_page)
        overflow_offset = (overflow_page - 1) * page_size
        next_page = int.from_bytes(
            database[overflow_offset : overflow_offset + 4], "big"
        )
        chunk_size = min(remaining, usable_size - 4)
        payload.extend(
            database[
                overflow_offset + 4 : overflow_offset + 4 + chunk_size
            ]
        )
        remaining -= chunk_size
        overflow_page = next_page
    return bytes(payload)


def _index_cell_payload(
    database: bytes,
    *,
    page_size: int,
    page_offset: int,
    cell_offset: int,
) -> bytes:
    payload_size, cursor = _read_varint(database, page_offset + cell_offset)
    usable_size = page_size
    max_local = ((usable_size - 12) * 64 // 255) - 23
    min_local = ((usable_size - 12) * 32 // 255) - 23
    if payload_size <= max_local:
        local_size = payload_size
    else:
        local_size = min_local + (
            (payload_size - min_local) % (usable_size - 4)
        )
        if local_size > max_local:
            local_size = min_local

    local_end = cursor + local_size
    page_end = page_offset + page_size
    if local_end > page_end:
        raise ValueError("index cell crosses page boundary")
    payload = bytearray(database[cursor:local_end])
    if local_size == payload_size:
        return bytes(payload)

    if local_end + 4 > page_end:
        raise ValueError("missing index overflow pointer")
    overflow_page = int.from_bytes(database[local_end : local_end + 4], "big")
    remaining = payload_size - local_size
    visited: set[int] = set()
    while remaining:
        if overflow_page <= 0 or overflow_page in visited:
            raise ValueError("invalid index overflow chain")
        visited.add(overflow_page)
        overflow_offset = (overflow_page - 1) * page_size
        next_page = int.from_bytes(
            database[overflow_offset : overflow_offset + 4], "big"
        )
        chunk_size = min(remaining, usable_size - 4)
        payload.extend(
            database[
                overflow_offset + 4 : overflow_offset + 4 + chunk_size
            ]
        )
        remaining -= chunk_size
        overflow_page = next_page
    return bytes(payload)


def _scan_deleted_rows(
    database: bytes,
    *,
    page_size: int,
    page_number: int,
    column_count: int,
    validator: Callable[[list[Any]], bool],
) -> list[list[Any]]:
    """Scan stale leaf-cell bytes left behind after SQLite secure_delete=OFF."""

    page_offset = (page_number - 1) * page_size
    page = database[page_offset : page_offset + page_size]
    if not page or page[0] != 0x0D:
        raise RuntimeError(
            f"page {page_number} is not a SQLite table leaf page"
        )

    recovered: dict[tuple[Any, ...], list[Any]] = {}
    for cell_offset in range(PAGE_HEADER_SIZE, page_size):
        try:
            payload = _cell_payload(
                database,
                page_size=page_size,
                page_offset=page_offset,
                cell_offset=cell_offset,
            )
            values = _decode_record(payload)
        except (IndexError, UnicodeDecodeError, ValueError):
            continue
        if len(values) != column_count or not validator(values):
            continue
        recovered[tuple(values)] = values
    return list(recovered.values())


def _scan_database_rows(
    database: bytes,
    *,
    page_size: int,
    column_count: int,
    validator: Callable[[list[Any]], bool],
) -> list[list[Any]]:
    """Search all surviving leaf pages because SQLite may already reuse roots."""

    page_count = len(database) // page_size
    recovered: dict[tuple[Any, ...], list[Any]] = {}
    for page_number in range(2, page_count + 1):
        page_offset = (page_number - 1) * page_size
        if database[page_offset] != 0x0D:
            continue
        for row in _scan_deleted_rows(
            database,
            page_size=page_size,
            page_number=page_number,
            column_count=column_count,
            validator=validator,
        ):
            recovered[tuple(row)] = row
    return list(recovered.values())


def _scan_index_rows(
    database: bytes,
    *,
    page_size: int,
    page_number: int,
    column_count: int,
    validator: Callable[[list[Any]], bool],
) -> list[list[Any]]:
    page_offset = (page_number - 1) * page_size
    if database[page_offset] != 0x0A:
        raise RuntimeError(f"page {page_number} is not an index leaf page")
    recovered: dict[tuple[Any, ...], list[Any]] = {}
    for cell_offset in range(PAGE_HEADER_SIZE, page_size):
        try:
            payload = _index_cell_payload(
                database,
                page_size=page_size,
                page_offset=page_offset,
                cell_offset=cell_offset,
            )
            values = _decode_record(payload)
        except (IndexError, UnicodeDecodeError, ValueError):
            continue
        if len(values) != column_count or not validator(values):
            continue
        recovered[tuple(values)] = values
    return list(recovered.values())


def _looks_like_uuid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 36
        and value.count("-") == 4
    )


def _latest_by_id(rows: list[list[Any]], updated_index: int) -> list[list[Any]]:
    latest: dict[str, list[Any]] = {}
    for row in rows:
        current = latest.get(row[0])
        if current is None or str(row[updated_index]) > str(
            current[updated_index]
        ):
            latest[row[0]] = row
    return list(latest.values())


def _table_root(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(
        "SELECT rootpage FROM sqlite_schema "
        "WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"table not found: {table}")
    return int(row[0])


def _index_root(connection: sqlite3.Connection, index: str) -> int:
    row = connection.execute(
        "SELECT rootpage FROM sqlite_schema "
        "WHERE type = 'index' AND name = ?",
        (index,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"index not found: {index}")
    return int(row[0])


def recover_index_records(source: Path) -> dict[str, list[list[Any]]]:
    database = source.read_bytes()
    page_size = int.from_bytes(database[16:18], "big")
    if page_size == 1:
        page_size = 65536
    specifications = {
        "item_ids": (
            "sqlite_autoindex_generation_batch_items_1",
            2,
            lambda row: _looks_like_uuid(row[0]) and isinstance(row[1], int),
        ),
        "item_batch_keys": (
            "sqlite_autoindex_generation_batch_items_3",
            3,
            lambda row: (
                _looks_like_uuid(row[0])
                and isinstance(row[1], str)
                and isinstance(row[2], int)
            ),
        ),
        "audio_ids": (
            "sqlite_autoindex_audio_generation_tasks_1",
            2,
            lambda row: _looks_like_uuid(row[0]) and isinstance(row[1], int),
        ),
        "audio_item_ids": (
            "sqlite_autoindex_audio_generation_tasks_2",
            2,
            lambda row: _looks_like_uuid(row[0]) and isinstance(row[1], int),
        ),
        "audio_planned_ids": (
            "sqlite_autoindex_audio_generation_tasks_3",
            2,
            lambda row: _looks_like_uuid(row[0]) and isinstance(row[1], int),
        ),
        "segment_ids": (
            "sqlite_autoindex_generation_segments_1",
            2,
            lambda row: _looks_like_uuid(row[0]) and isinstance(row[1], int),
        ),
        "segment_item_indexes": (
            "sqlite_autoindex_generation_segments_2",
            3,
            lambda row: (
                _looks_like_uuid(row[0])
                and isinstance(row[1], int)
                and isinstance(row[2], int)
            ),
        ),
        "video_task_ids": (
            "sqlite_autoindex_generation_tasks_1",
            2,
            lambda row: _looks_like_uuid(row[0]) and isinstance(row[1], int),
        ),
        "video_task_segment_ids": (
            "ix_generation_tasks_segment_id",
            2,
            lambda row: _looks_like_uuid(row[0]) and isinstance(row[1], int),
        ),
    }
    recovered: dict[str, list[list[Any]]] = {}
    with sqlite3.connect(source) as connection:
        for label, (index, column_count, validator) in specifications.items():
            recovered[label] = _scan_index_rows(
                database,
                page_size=page_size,
                page_number=_index_root(connection, index),
                column_count=column_count,
                validator=validator,
            )
    return recovered


def recover_rows(source: Path) -> dict[str, list[list[Any]]]:
    database = source.read_bytes()
    page_size = int.from_bytes(database[16:18], "big")
    if page_size == 1:
        page_size = 65536

    with sqlite3.connect(source) as connection:
        batches = {
            row[0]
            for row in connection.execute("SELECT id FROM generation_batches")
        }
        _table_root(connection, "generation_batch_items")
        _table_root(connection, "audio_generation_tasks")
        _table_root(connection, "generation_segments")

    items = _scan_database_rows(
        database,
        page_size=page_size,
        column_count=11,
        validator=lambda row: (
            _looks_like_uuid(row[0])
            and row[1] in batches
            and isinstance(row[2], int)
            and row[2] > 0
            and isinstance(row[3], str)
            and isinstance(row[4], str)
            and isinstance(json.loads(row[4]), dict)
            and isinstance(row[9], str)
            and isinstance(row[10], str)
        ),
    )
    items = _latest_by_id(items, 10)
    item_ids = {row[0] for row in items}

    audio_tasks = _scan_database_rows(
        database,
        page_size=page_size,
        column_count=38,
        validator=lambda row: (
            _looks_like_uuid(row[0])
            and isinstance(row[1], int)
            and isinstance(row[2], int)
            and row[3] in item_ids
            and _looks_like_uuid(row[6])
            and isinstance(row[11], str)
            and isinstance(json.loads(row[12]), dict)
            and isinstance(row[21], str)
            and isinstance(row[27], str)
            and isinstance(row[28], str)
        ),
    )
    audio_tasks = _latest_by_id(audio_tasks, 28)

    segments = _scan_database_rows(
        database,
        page_size=page_size,
        column_count=15,
        validator=lambda row: (
            _looks_like_uuid(row[0])
            and row[1] in item_ids
            and isinstance(row[2], int)
            and row[2] > 0
            and isinstance(row[3], str)
            and isinstance(row[6], str)
            and isinstance(row[8], str)
            and isinstance(row[10], str)
            and isinstance(row[13], str)
            and isinstance(row[14], str)
        ),
    )
    segments = _latest_by_id(segments, 14)
    return {
        "generation_batch_items": items,
        "audio_generation_tasks": audio_tasks,
        "generation_segments": segments,
    }


def _row_key_from_task(task: sqlite3.Row) -> str:
    match = re.search(
        r"generated-(?P<row_key>.+)-\d+\.mp3$",
        str(task["audio_original_name"]),
        flags=re.IGNORECASE,
    )
    if match:
        return match.group("row_key")
    return f"SCRIPT-{int(task['segment_index']):03d}"


def _ltx_script(prompt: str) -> str:
    match = re.match(r"^.+?：“(?P<script>.*)”$", prompt, flags=re.DOTALL)
    return match.group("script") if match else prompt


def reconstruct_missing_rows(
    source: Path,
    recovered: dict[str, list[list[Any]]],
    transcripts: dict[str, str],
) -> dict[str, list[list[Any]]]:
    """Rebuild rows whose stale SQLite cell headers were already overwritten."""

    with sqlite3.connect(source) as connection:
        connection.row_factory = sqlite3.Row
        empty_batches = connection.execute(
            "SELECT b.* FROM generation_batches b "
            "LEFT JOIN generation_batch_items i ON i.batch_id = b.id "
            "GROUP BY b.id HAVING COUNT(i.id) = 0 "
            "ORDER BY b.created_at"
        ).fetchall()
        tasks = connection.execute(
            "SELECT id, user_id, workflow_type, audio_path, "
            "audio_original_name, image_original_name, prompt, "
            "audio_duration_seconds, created_at, updated_at "
            "FROM generation_tasks "
            "WHERE batch_item_id IS NULL AND segment_id IS NULL "
            "ORDER BY created_at, id"
        ).fetchall()

    grouped_tasks: dict[tuple[int, str, str], list[sqlite3.Row]] = {}
    for task in tasks:
        match = SEGMENT_PATH_RE.match(str(task["audio_path"]))
        if not match:
            continue
        key = (
            int(task["user_id"]),
            str(task["workflow_type"]),
            str(PurePosixPath(task["audio_path"]).parents[1]),
        )
        grouped_tasks.setdefault(key, []).append(task)
    for rows in grouped_tasks.values():
        rows.sort(
            key=lambda row: int(
                SEGMENT_PATH_RE.match(str(row["audio_path"]))["index"]
            )
        )

    exact_items = {
        (row[1], row[3]): row
        for row in recovered["generation_batch_items"]
    }
    exact_segments = {
        row[6]: row for row in recovered["generation_segments"]
    }
    final_items: list[list[Any]] = []
    final_segments: list[list[Any]] = []
    used_groups: set[tuple[int, str, str]] = set()

    for batch in empty_batches:
        candidates = [
            (key, rows)
            for key, rows in grouped_tasks.items()
            if key not in used_groups
            and key[0] == int(batch["user_id"])
            and key[1] == str(batch["workflow_type"])
            and str(rows[0]["created_at"]) >= str(batch["created_at"])
        ]
        candidates.sort(key=lambda candidate: str(candidate[1][0]["created_at"]))
        selected = candidates[: int(batch["total_items"])]
        if len(selected) != int(batch["total_items"]):
            raise RuntimeError(
                f"could not match all rows for batch {batch['id']}"
            )

        for row_number, (group_key, group) in enumerate(selected, start=1):
            used_groups.add(group_key)
            row_key = _row_key_from_task(group[0])
            exact_item = exact_items.get((str(batch["id"]), row_key))
            item_id = (
                exact_item[0]
                if exact_item
                else str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"runninghub-recovery:{batch['id']}:{group_key[2]}",
                    )
                )
            )
            script_parts = [
                transcripts.get(str(task["audio_path"]))
                or (
                    _ltx_script(str(task["prompt"]))
                    if task["workflow_type"] == "ltx_lip_sync"
                    else f"历史音频片段 {index}"
                )
                for index, task in enumerate(group, start=1)
            ]
            speech_script = "".join(script_parts)
            if exact_item:
                item_row = exact_item
            else:
                manifest: dict[str, str] = {
                    "source_row_number": str(row_number + 1),
                    "row_id": row_key,
                    "speech_script": speech_script,
                }
                if batch["workflow_type"] == "ltx_lip_sync":
                    prompt = str(group[0]["prompt"])
                    prefix = prompt.split("：“", 1)[0]
                    manifest.update(
                        {
                            "prompt_prefix": prefix,
                            "source_video_file": str(
                                group[0]["image_original_name"]
                            ).rsplit("-", 1)[0]
                            + ".mp4",
                            "positive_prompt": f"{prefix}：“{speech_script}”",
                        }
                    )
                else:
                    manifest.update(
                        {
                            "prompt": str(group[0]["prompt"]),
                            "image_file": str(group[0]["image_original_name"]),
                        }
                    )
                item_row = [
                    item_id,
                    str(batch["id"]),
                    row_number,
                    row_key,
                    json.dumps(manifest, ensure_ascii=False),
                    "SEGMENTING",
                    "SEGMENTS_CREATED",
                    None,
                    None,
                    str(batch["created_at"]),
                    max(str(task["updated_at"]) for task in group),
                ]
            final_items.append(item_row)

            elapsed = 0.0
            for segment_index, task in enumerate(group, start=1):
                exact_segment = exact_segments.get(str(task["audio_path"]))
                if exact_segment:
                    segment_row = exact_segment
                    elapsed = float(segment_row[5])
                else:
                    duration = float(task["audio_duration_seconds"])
                    segment_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"runninghub-recovery:{task['id']}",
                        )
                    )
                    segment_row = [
                        segment_id,
                        item_id,
                        segment_index,
                        script_parts[segment_index - 1],
                        elapsed,
                        elapsed + duration,
                        str(task["audio_path"]),
                        (
                            str(task["audio_path"]).rsplit(".", 1)[0] + ".mp4"
                            if task["workflow_type"] == "ltx_lip_sync"
                            else None
                        ),
                        str(task["prompt"]),
                        (
                            "minimax_sentence_timestamp"
                            if task["workflow_type"] == "ltx_lip_sync"
                            else "punctuation_estimate"
                        ),
                        "TASK_CREATED",
                        None,
                        None,
                        str(task["created_at"]),
                        str(task["updated_at"]),
                    ]
                    elapsed += duration
                final_segments.append(segment_row)

    final_item_ids = {row[0] for row in final_items}
    final_audio_tasks = [
        row
        for row in recovered["audio_generation_tasks"]
        if row[3] in final_item_ids
    ]
    return {
        "generation_batch_items": final_items,
        "audio_generation_tasks": final_audio_tasks,
        "generation_segments": final_segments,
    }


def _insert_rows(
    connection: sqlite3.Connection,
    table: str,
    rows: list[list[Any]],
) -> None:
    columns = [
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table})")
    ]
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    connection.executemany(
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
        rows,
    )


def apply_recovery(
    target: Path,
    recovered: dict[str, list[list[Any]]],
) -> Path:
    backup = target.with_name(
        f"{target.stem}-before-0010-repair-{datetime.now():%Y%m%d-%H%M%S}.db"
    )
    shutil.copy2(target, backup)

    with sqlite3.connect(target) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        affected_batches = {
            row[1] for row in recovered["generation_batch_items"]
        }
        placeholders = ", ".join("?" for _ in affected_batches)
        existing = connection.execute(
            "SELECT COUNT(*) FROM generation_batch_items "
            f"WHERE batch_id IN ({placeholders})",
            tuple(affected_batches),
        ).fetchone()[0]
        if existing:
            raise RuntimeError(
                "target already contains items for the affected batches"
            )

        connection.execute("BEGIN IMMEDIATE")
        _insert_rows(
            connection,
            "generation_batch_items",
            recovered["generation_batch_items"],
        )

        current_audio_rows = [
            [*row, 1, None]
            for row in recovered["audio_generation_tasks"]
        ]
        _insert_rows(
            connection,
            "audio_generation_tasks",
            current_audio_rows,
        )
        _insert_rows(
            connection,
            "generation_segments",
            recovered["generation_segments"],
        )

        for segment in recovered["generation_segments"]:
            segment_id = segment[0]
            item_id = segment[1]
            audio_path = segment[6]
            matching = connection.execute(
                "SELECT id FROM generation_tasks WHERE audio_path = ?",
                (audio_path,),
            ).fetchall()
            if len(matching) != 1:
                raise RuntimeError(
                    f"expected one video task for {audio_path}, "
                    f"found {len(matching)}"
                )
            connection.execute(
                "UPDATE generation_tasks "
                "SET batch_item_id = NULL, segment_id = ? WHERE id = ?",
                (segment_id, matching[0][0]),
            )

        recovered_item_ids = {
            row[0] for row in recovered["generation_batch_items"]
        }
        restored_task_count = connection.execute(
            "SELECT COUNT(*) FROM generation_tasks "
            "WHERE segment_id IS NOT NULL"
        ).fetchone()[0]
        if restored_task_count != len(recovered["generation_segments"]):
            raise RuntimeError("not every recovered segment was linked")
        linked_item_count = connection.execute(
            "SELECT COUNT(DISTINCT batch_item_id) FROM generation_segments"
        ).fetchone()[0]
        if linked_item_count != len(recovered_item_ids):
            raise RuntimeError("not every recovered batch item has segments")
        connection.commit()
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recover batch rows removed by the original SQLite 0010 migration."
        )
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write recovered rows to --target; default is dry-run",
    )
    parser.add_argument(
        "--diagnose-indexes",
        action="store_true",
        help="also print recoverable stale index records",
    )
    parser.add_argument(
        "--transcripts",
        type=Path,
        help="optional JSON map from relative segment audio path to text",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    recovered = recover_rows(source)
    transcripts = (
        json.loads(args.transcripts.read_text(encoding="utf-8"))
        if args.transcripts
        else {}
    )
    recovered = reconstruct_missing_rows(source, recovered, transcripts)
    summary = {
        table: len(rows) for table, rows in recovered.items()
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for table, rows in recovered.items():
        print(table, [row[0] for row in rows])
    if args.diagnose_indexes:
        print(
            json.dumps(
                recover_index_records(args.source.resolve()),
                ensure_ascii=False,
                indent=2,
            )
        )

    if not args.apply:
        return 0
    if args.target is None:
        parser.error("--target is required with --apply")
    backup = apply_recovery(args.target.resolve(), recovered)
    print(f"backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
