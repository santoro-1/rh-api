from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    GenerationTask,
    TaskStatus,
)
from app.services.h3_workbench import sync_h3_task_hierarchy
from app.services.task_management import prepare_task_retry


BASE_URL = "http://127.0.0.1:8000"
STATE_FILE = Path(__file__).with_name("test-state.json")
PASSWORD_FILE = Path(
    r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\web_storage\admin_password.txt"
)
VIDEO_PATH = Path(r"D:\工作内容\数字人\徐博士\n封面8-1.mp4")
ORIGINAL_ROOT = Path(
    r"D:\工作内容\轻盈健\数字人\.codex_tmp\h3_xu_doctor_photo_test"
)
HEADSHOT_ROOT = Path(__file__).with_name("headshot")
SHOULDERS_ROOT = Path(__file__).with_name("head_shoulders")

AUDIO_BATCH_ID = "f3919cf0-66c8-46e2-8d38-ee1ebc3772d1"
AUDIO_ITEM_ID = "8e53d06f-6a20-44bb-9225-4c650eda0280"
AUDIO_GENERATION_VERSION = 1
SCRIPT_TEXT = "说一个减肥的冷知识，饿个两三天，然后猛吃个一天，这就是相扑选手核心增重的方法之一。"
FIXED_SEED = 2026082301
MAX_AUTHORIZED_PAID_CALLS = 4

VARIANTS = (
    ("no_images", "0 图基线", ()),
    ("full_photos", "3 张完整原图", tuple(ORIGINAL_ROOT / f"{name}.jpg" for name in "ABC")),
    ("headshots", "同 3 张大头裁剪", tuple(HEADSHOT_ROOT / f"{name}.jpg" for name in "ABC")),
    (
        "head_shoulders",
        "同 3 张头肩裁剪",
        tuple(SHOULDERS_ROOT / f"{name}.jpg" for name in "ABC"),
    ),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_state() -> dict[str, Any]:
    if STATE_FILE.is_file():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "schema": "h3.crop-ab.v6.v1",
        "created_at": utc_now(),
        "authorized_paid_call_cap": MAX_AUTHORIZED_PAID_CALLS,
        "fixed_seed": FIXED_SEED,
        "prompt_template_version": "h3.prompt.ref2va.v6",
        "variants": {},
    }


def write_state(state: dict[str, Any]) -> None:
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(STATE_FILE)


def request_json(
    session: requests.Session,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 120,
) -> dict[str, Any]:
    response = session.request(
        method,
        f"{BASE_URL}{path}",
        json=payload,
        timeout=timeout,
    )
    if not response.ok:
        raise RuntimeError(
            f"{method} {path} -> HTTP {response.status_code}: {response.text[:2000]}"
        )
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {path} returned a non-object payload")
    return value


def login(session: requests.Session) -> str:
    password = PASSWORD_FILE.read_text(encoding="utf-8-sig").strip()
    result = request_json(
        session,
        "POST",
        "/api/auth/center/login",
        payload={"username": "admin", "password": password},
    )
    token = str(result.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Local admin login returned no access token")
    return token


def upload_asset(
    session: requests.Session, token: str, path: Path, kind: str
) -> dict[str, Any]:
    content_types = {
        "image": "image/jpeg",
        "video": "video/mp4",
    }
    with path.open("rb") as stream:
        response = session.post(
            f"{BASE_URL}/api/workbench/batch-assets",
            data={"access_token": token, "kind": kind},
            files={"file": (path.name, stream, content_types[kind])},
            timeout=300,
        )
    if not response.ok:
        raise RuntimeError(
            f"upload {kind} {path} -> HTTP {response.status_code}: {response.text[:2000]}"
        )
    result = response.json()
    asset_id = str(result.get("asset_id") or "").strip()
    if not asset_id:
        raise RuntimeError(f"upload {kind} {path} returned no asset_id")
    return {
        "asset_id": asset_id,
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def selected_accounts(session: requests.Session, token: str) -> list[int]:
    payload = request_json(
        session,
        "POST",
        "/api/workbench/h3-execution-accounts",
        payload={"access_token": token},
    )
    selected = [int(value) for value in payload.get("default_selected_account_ids", [])]
    selectable = {
        int(account["id"])
        for account in payload.get("accounts", [])
        if isinstance(account, dict) and account.get("selectable") is True
    }
    if not selected or not set(selected).issubset(selectable):
        raise RuntimeError("No selectable default H3 account")
    return selected


def verify_audio_source(session: requests.Session, token: str) -> None:
    payload = request_json(
        session,
        "POST",
        "/api/workbench/h3-audio-sources",
        payload={"access_token": token},
    )
    matches = [
        source
        for source in payload.get("sources", [])
        if isinstance(source, dict)
        and source.get("audio_batch_id") == AUDIO_BATCH_ID
        and source.get("audio_item_id") == AUDIO_ITEM_ID
        and int(source.get("audio_generation_version") or 0)
        == AUDIO_GENERATION_VERSION
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one frozen audio source, found {len(matches)}")
    source = matches[0]
    if str(source.get("status") or "").upper() != "SUCCESS":
        raise RuntimeError(f"Frozen audio is not approved: {source.get('status')}")
    if str(source.get("script_text") or "").strip() != SCRIPT_TEXT:
        raise RuntimeError("Frozen audio script no longer matches the comparison contract")


def prepare() -> None:
    for required in (VIDEO_PATH, *[path for _, _, paths in VARIANTS for path in paths]):
        if not required.is_file():
            raise RuntimeError(f"Missing comparison input: {required}")

    state = read_state()
    with requests.Session() as session:
        token = login(session)
        verify_audio_source(session, token)
        accounts = selected_accounts(session, token)
        state["selected_account_ids"] = accounts

        for key, label, paths in VARIANTS:
            record = state["variants"].setdefault(
                key,
                {
                    "label": label,
                    "request_key": f"h3-v6-crop-ab-20260823-{key}",
                    "images": [],
                },
            )
            while len(record["images"]) < len(paths):
                path = paths[len(record["images"])]
                record["images"].append(upload_asset(session, token, path, "image"))
                write_state(state)
            if record.get("batch_id"):
                continue
            if "video" not in record:
                record["video"] = upload_asset(session, token, VIDEO_PATH, "video")
                write_state(state)

            response = request_json(
                session,
                "POST",
                "/api/workbench/h3-batches/prepare",
                payload={
                    "access_token": token,
                    "name": f"徐博士 H3 v6 图片裁剪对照：{label}",
                    "request_key": record["request_key"],
                    "correlation_id": f"h3-v6-crop-ab-{key}",
                    "reference_image_asset_ids": [
                        image["asset_id"] for image in record["images"]
                    ],
                    "defaults": {
                        "continuity_mode": "fast",
                        "user_direction": "",
                        "generation_tail_seconds": 0.5,
                        "resolution": {
                            "aspect_ratio": "9:16 (Portrait Widescreen)",
                            "megapixels": 1.0,
                            "multiple": 32,
                        },
                    },
                    "rows": [
                        {
                            "row_id": key,
                            "script_text": SCRIPT_TEXT,
                            "video_asset_id": record["video"]["asset_id"],
                            "audio_batch_id": AUDIO_BATCH_ID,
                            "audio_item_id": AUDIO_ITEM_ID,
                            "audio_generation_version": AUDIO_GENERATION_VERSION,
                        }
                    ],
                    "selected_account_ids": accounts,
                },
                timeout=600,
            )
            record["batch_id"] = response["batch_id"]
            record["prepared_at"] = utc_now()
            record["prepared_status"] = response.get("status")
            record["fee_snapshot"] = response.get("fee_snapshot")
            record["items"] = response.get("items")
            write_state(state)
            print(
                f"PREPARED {key} batch={record['batch_id']} "
                f"fee={record['fee_snapshot']}"
            )

    estimated = sum(
        int((record.get("fee_snapshot") or {}).get("estimated_paid_calls") or 0)
        for record in state["variants"].values()
    )
    if len(state["variants"]) != 4 or estimated != MAX_AUTHORIZED_PAID_CALLS:
        raise RuntimeError(
            f"Refusing confirmation path: variants={len(state['variants'])}, "
            f"estimated_paid_calls={estimated}"
        )
    state["estimated_paid_calls_total"] = estimated
    state["prepared_complete_at"] = utc_now()
    write_state(state)
    print(f"PREPARE_COMPLETE estimated_paid_calls={estimated}")


def confirm() -> None:
    state = read_state()
    estimated = sum(
        int((record.get("fee_snapshot") or {}).get("estimated_paid_calls") or 0)
        for record in state.get("variants", {}).values()
    )
    if set(state.get("variants", {})) != {value[0] for value in VARIANTS}:
        raise RuntimeError("All four frozen variants must be prepared first")
    if estimated != MAX_AUTHORIZED_PAID_CALLS:
        raise RuntimeError(f"Paid-call estimate must equal 4, got {estimated}")

    with requests.Session() as session:
        token = login(session)
        for key, _, _ in VARIANTS:
            record = state["variants"][key]
            if record.get("confirmed_at"):
                continue
            response = request_json(
                session,
                "POST",
                f"/api/workbench/h3-batches/{record['batch_id']}/confirm",
                payload={"access_token": token, "cost_confirmed": True},
                timeout=180,
            )
            record["confirmed_at"] = utc_now()
            record["confirmed_status"] = response.get("status")
            record["confirmed_fee_snapshot"] = response.get("fee_snapshot")
            write_state(state)
            print(f"CONFIRMED {key} status={response.get('status')}")
    state["confirmed_complete_at"] = utc_now()
    write_state(state)


def _load_batches(batch_ids: list[str]) -> list[GenerationBatch]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(GenerationBatch)
                .options(
                    selectinload(GenerationBatch.items)
                    .selectinload(GenerationBatchItem.segments)
                    .selectinload(GenerationSegment.generation_task)
                    .selectinload(GenerationTask.runninghub_attempts)
                )
                .where(GenerationBatch.id.in_(batch_ids))
            )
            .unique()
            .all()
        )


def freeze_seed() -> None:
    state = read_state()
    batch_ids = [state["variants"][key]["batch_id"] for key, _, _ in VARIANTS]
    with SessionLocal() as db:
        batches = list(
            db.scalars(
                select(GenerationBatch)
                .options(
                    selectinload(GenerationBatch.items)
                    .selectinload(GenerationBatchItem.segments)
                    .selectinload(GenerationSegment.generation_task)
                    .selectinload(GenerationTask.runninghub_attempts)
                )
                .where(GenerationBatch.id.in_(batch_ids))
            )
            .unique()
            .all()
        )
        tasks = [
            segment.generation_task
            for batch in batches
            for item in batch.items
            for segment in item.segments
        ]
        if len(batches) != 4 or len(tasks) != 4 or any(task is None for task in tasks):
            raise RuntimeError(
                f"Expected four materialized single-segment tasks; "
                f"batches={len(batches)} tasks={len(tasks)}"
            )
        for task in tasks:
            assert task is not None
            if task.status != TaskStatus.PENDING.value:
                raise RuntimeError(f"Task {task.id} is already {task.status}")
            if task.runninghub_task_id or task.runninghub_submitted_at:
                raise RuntimeError(f"Task {task.id} has already reached RunningHub")
            if any(attempt.remote_task_id for attempt in task.runninghub_attempts):
                raise RuntimeError(f"Task {task.id} already has a paid remote attempt")
            payload = json.loads(task.input_payload)
            parameters = payload.get("parameters")
            if not isinstance(parameters, dict):
                raise RuntimeError(f"Task {task.id} has no parameter snapshot")
            parameters["seed"] = FIXED_SEED
            task.input_payload = json.dumps(payload, ensure_ascii=False)
        db.commit()

    state["seed_frozen_at"] = utc_now()
    state["fixed_seed"] = FIXED_SEED
    write_state(state)
    audit()


def recover_local_pre_submit() -> None:
    state = read_state()
    batch_ids = [state["variants"][key]["batch_id"] for key, _, _ in VARIANTS]
    with SessionLocal() as db:
        batches = list(
            db.scalars(
                select(GenerationBatch)
                .options(
                    selectinload(GenerationBatch.items)
                    .selectinload(GenerationBatchItem.segments)
                    .selectinload(GenerationSegment.generation_task)
                    .selectinload(GenerationTask.runninghub_attempts)
                )
                .where(GenerationBatch.id.in_(batch_ids))
            )
            .unique()
            .all()
        )
        tasks = [
            segment.generation_task
            for batch in batches
            for item in batch.items
            for segment in item.segments
        ]
        if len(tasks) != 4 or any(task is None for task in tasks):
            raise RuntimeError("Expected exactly four materialized tasks")
        for task in tasks:
            assert task is not None
            attempts = list(task.runninghub_attempts)
            strict = bool(
                task.status == TaskStatus.FAILED.value
                and task.error_code == "SUBMIT_FAILED"
                and task.runninghub_task_id is None
                and task.runninghub_submitted_at is None
                and attempts
                and all(attempt.remote_task_id is None for attempt in attempts)
                and all(attempt.status == "PRE_SUBMISSION_FAILED" for attempt in attempts)
                and "runninghub-upload-" in str(task.error_message or "")
                and "WinError 5" in str(task.error_message or "")
            )
            if not strict:
                raise RuntimeError(
                    f"Task {task.id} is not a strict local pre-submit failure"
                )
        for task in tasks:
            assert task is not None
            prepare_task_retry(task, get_settings())
            segment = task.segment
            if segment is None:
                raise RuntimeError(f"Task {task.id} has no H3 segment")
            if segment.h3_config is not None:
                segment.h3_config.dynamic_workflow_sha256 = None
            segment.status = task.status
            segment.error_code = None
            segment.error_message = None
            sync_h3_task_hierarchy(task)
        db.commit()
    state["strict_local_pre_submit_recovered_at"] = utc_now()
    write_state(state)
    audit()


def audit() -> None:
    state = read_state()
    batch_ids = [
        record["batch_id"]
        for record in state.get("variants", {}).values()
        if record.get("batch_id")
    ]
    batches = _load_batches(batch_ids)
    rows: list[dict[str, Any]] = []
    remote_ids: set[str] = set()
    for batch in batches:
        for item in batch.items:
            for segment in item.segments:
                task = segment.generation_task
                payload = json.loads(task.input_payload) if task else {}
                task_remote_ids = {
                    value
                    for value in (
                        [task.runninghub_task_id]
                        + [attempt.remote_task_id for attempt in task.runninghub_attempts]
                        if task
                        else []
                    )
                    if value
                }
                remote_ids.update(task_remote_ids)
                rows.append(
                    {
                        "row_key": item.row_key,
                        "batch_id": batch.id,
                        "task_id": task.id if task else None,
                        "status": task.status if task else segment.status,
                        "seed": (payload.get("parameters") or {}).get("seed"),
                        "prompt_version": (payload.get("parameters") or {}).get(
                            "prompt_template_version"
                        ),
                        "prompt_chars": len(task.prompt) if task else None,
                        "runninghub_task_id": task.runninghub_task_id if task else None,
                        "remote_attempt_ids": sorted(task_remote_ids),
                        "result_path": task.result_path if task else None,
                        "error_code": task.error_code if task else segment.error_code,
                        "error_message": task.error_message if task else segment.error_message,
                        "attempts": [
                            {
                                "number": attempt.attempt_number,
                                "status": attempt.status,
                                "remote_task_id": attempt.remote_task_id,
                                "error_code": attempt.error_code,
                                "error_message": attempt.error_message,
                            }
                            for attempt in task.runninghub_attempts
                        ]
                        if task
                        else [],
                    }
                )
    if len(remote_ids) > MAX_AUTHORIZED_PAID_CALLS:
        raise RuntimeError(
            f"Hard cap exceeded: {len(remote_ids)} distinct RunningHub task IDs"
        )
    report = {
        "batch_count": len(batches),
        "task_count": len(rows),
        "distinct_remote_task_ids": len(remote_ids),
        "authorized_paid_call_cap": MAX_AUTHORIZED_PAID_CALLS,
        "fixed_seed": FIXED_SEED,
        "rows": sorted(rows, key=lambda value: value["row_key"]),
    }
    state["last_audit_at"] = utc_now()
    state["last_audit"] = report
    write_state(state)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def compact_status() -> None:
    state = read_state()
    label_by_batch = {
        record["batch_id"]: {"key": key, "label": record["label"]}
        for key, record in state.get("variants", {}).items()
        if record.get("batch_id")
    }
    batches = _load_batches(list(label_by_batch))
    rows: list[dict[str, Any]] = []
    remote_ids: set[str] = set()
    for batch in batches:
        identity = label_by_batch[batch.id]
        for item in batch.items:
            for segment in item.segments:
                task = segment.generation_task
                if task is None:
                    continue
                task_remote_ids = {
                    value
                    for value in [task.runninghub_task_id]
                    + [attempt.remote_task_id for attempt in task.runninghub_attempts]
                    if value
                }
                remote_ids.update(task_remote_ids)
                elapsed = (
                    (task.completed_at - task.runninghub_submitted_at).total_seconds()
                    if task.completed_at and task.runninghub_submitted_at
                    else None
                )
                rows.append(
                    {
                        **identity,
                        "status": task.status,
                        "remote_task_id": task.runninghub_task_id,
                        "submitted_at": task.runninghub_submitted_at.isoformat()
                        if task.runninghub_submitted_at
                        else None,
                        "completed_at": task.completed_at.isoformat()
                        if task.completed_at
                        else None,
                        "elapsed_seconds": round(elapsed, 3)
                        if elapsed is not None
                        else None,
                        "result_path": task.result_path,
                        "error_code": task.error_code,
                    }
                )
    print(
        json.dumps(
            {
                "checked_at": utc_now(),
                "distinct_remote_task_ids": len(remote_ids),
                "authorized_paid_call_cap": MAX_AUTHORIZED_PAID_CALLS,
                "rows": sorted(rows, key=lambda value: value["key"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def status() -> None:
    state = read_state()
    with requests.Session() as session:
        token = login(session)
        for key, _, _ in VARIANTS:
            record = state.get("variants", {}).get(key)
            if not record or not record.get("batch_id"):
                continue
            response = request_json(
                session,
                "POST",
                f"/api/workbench/h3-batches/{record['batch_id']}",
                payload={"access_token": token},
            )
            record["last_status"] = response
            print(
                f"STATUS {key} batch={response.get('status')} "
                f"items={[item.get('status') for item in response.get('items', [])]}"
            )
    state["last_status_checked_at"] = utc_now()
    write_state(state)
    audit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "prepare",
            "confirm",
            "freeze-seed",
            "recover-local",
            "audit",
            "compact-status",
            "status",
        ),
    )
    args = parser.parse_args()
    {
        "prepare": prepare,
        "confirm": confirm,
        "freeze-seed": freeze_seed,
        "recover-local": recover_local_pre_submit,
        "audit": audit,
        "compact-status": compact_status,
        "status": status,
    }[args.command]()


if __name__ == "__main__":
    main()
