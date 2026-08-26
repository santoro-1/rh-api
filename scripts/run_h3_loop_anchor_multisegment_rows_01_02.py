from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = Path(__file__).resolve().parent
for candidate in (PROJECT_ROOT, SCRIPTS_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    RunningHubExecutionAccount,
)
from app.services.h3.graph import (  # noqa: E402
    H3GraphBuildRequest,
    load_default_h3_graph_builder,
)
from app.services.h3.prompt import (  # noqa: E402
    H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION,
    H3PromptRequest,
    compile_loop_anchor_ref2va_prompt,
)
from app.services.runninghub import RunningHubClient, RunningHubError  # noqa: E402
from app.services.security import decrypt_secret  # noqa: E402


SOURCE_ROWS = (1, 2)
EXECUTION_ACCOUNT_ID = 1
AUTHORIZED_PAID_CALLS = 4
EXPERIMENT_ID = "20260824-loop-anchor-multisegment-rows01-02"
STATE_VERSION = "h3.loop-anchor.multisegment.once.v1"
FINAL_STATUSES = {"SUCCESS", "FAILED", "CANCELLED"}
SOURCE_COLLECTION = (
    PROJECT_ROOT / "data" / "H3_第01-10条最终汇总_20260824_012004"
)
ANCHOR_ROOT = (
    PROJECT_ROOT / "data" / "h3-temp" / "20260823-v7-picture1-temp-a"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _write_json(path, state)


def _checked_file(value: object, label: str) -> Path:
    path = Path(str(value or "")).resolve()
    data_root = get_settings().data_dir.resolve()
    if path != data_root and data_root not in path.parents:
        raise RuntimeError(f"{label}不在项目 data 目录内")
    if not path.is_file():
        raise RuntimeError(f"{label}不存在：{path}")
    return path


def _manifest(row: int) -> dict[str, Any]:
    path = SOURCE_COLLECTION / f"{row:02d}" / "输入与参数.json"
    if not path.is_file():
        raise RuntimeError(f"第 {row} 条输入汇总不存在")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or int(value.get("row") or 0) != row:
        raise RuntimeError(f"第 {row} 条输入汇总结构异常")
    return value


def _source_snapshots() -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    with SessionLocal() as db:
        for row in SOURCE_ROWS:
            manifest = _manifest(row)
            batch = db.scalar(
                select(GenerationBatch)
                .where(GenerationBatch.id == str(manifest.get("batch_id") or ""))
                .options(
                    selectinload(GenerationBatch.h3_config),
                    selectinload(GenerationBatch.items)
                    .selectinload(GenerationBatchItem.h3_config),
                    selectinload(GenerationBatch.items)
                    .selectinload(GenerationBatchItem.segments)
                    .selectinload(GenerationSegment.h3_config),
                )
            )
            if batch is None or batch.h3_config is None or len(batch.items) != 1:
                raise RuntimeError(f"第 {row} 条来源批次不存在或结构异常")
            item = batch.items[0]
            if item.h3_config is None or item.h3_config.segment_count != 2:
                raise RuntimeError(f"第 {row} 条必须是两个 H3 分段")
            segments = sorted(item.segments, key=lambda value: value.segment_index)
            if len(segments) != 2 or [value.segment_index for value in segments] != [0, 1]:
                raise RuntimeError(f"第 {row} 条分段序号不完整")
            manifest_segments = sorted(
                manifest.get("segments") or [], key=lambda value: int(value["segment"])
            )
            if len(manifest_segments) != 2:
                raise RuntimeError(f"第 {row} 条汇总分段不是两段")

            video_path = _checked_file(
                manifest["reference_video"]["copied_path"],
                f"第 {row} 条参考视频",
            )
            anchor_path = _checked_file(
                ANCHOR_ROOT / f"r{row:02d}" / "picture-1-from-video-0.5s.png",
                f"第 {row} 条对应封面帧",
            )
            row_snapshot = {
                "row": row,
                "source_batch_id": batch.id,
                "source_item_id": item.id,
                "cover_name": str(manifest["reference_video"]["source_original_name"]),
                "full_audio_duration_seconds": float(manifest["audio"]["duration_seconds"]),
                "video_path": str(video_path),
                "image_path": str(anchor_path),
                "aspect_ratio": str(manifest["generation"]["aspect_ratio"]),
                "megapixels": float(manifest["generation"]["megapixels"]),
                "multiple": int(manifest["generation"]["multiple"]),
                "generation_tail_seconds": 0.1,
                "segments": [],
            }
            row_snapshot["video_path_sha256"] = _sha256_file(video_path)
            row_snapshot["image_path_sha256"] = _sha256_file(anchor_path)
            for segment, manifest_segment in zip(segments, manifest_segments, strict=True):
                if segment.h3_config is None:
                    raise RuntimeError(f"第 {row} 条第 {segment.segment_index + 1} 段快照缺失")
                audio_path = _checked_file(
                    manifest_segment["audio_path"],
                    f"第 {row} 条第 {segment.segment_index + 1} 段音频",
                )
                audio_duration = (
                    float(segment.h3_config.requested_generation_duration_seconds)
                    - float(batch.h3_config.generation_tail_seconds)
                )
                if not 4 <= audio_duration <= 15:
                    raise RuntimeError(
                        f"第 {row} 条第 {segment.segment_index + 1} 段时长异常：{audio_duration}"
                    )
                row_snapshot["segments"].append(
                    {
                        "segment_index": segment.segment_index,
                        "segment_count": 2,
                        "source_segment_id": segment.id,
                        "script_text": str(manifest_segment["script_text"]),
                        "audio_path": str(audio_path),
                        "audio_path_sha256": _sha256_file(audio_path),
                        "audio_duration_seconds": audio_duration,
                        "seed": int(segment.h3_config.input_sha256[:16], 16),
                    }
                )
            snapshots.append(row_snapshot)
    if len(snapshots) != 2 or sum(len(value["segments"]) for value in snapshots) != 4:
        raise RuntimeError("双素材多分段预检数量不是 2 条/4 段")
    return snapshots


def _compile_prompt(row: dict[str, Any], segment: dict[str, Any]) -> str:
    prompt = compile_loop_anchor_ref2va_prompt(
        H3PromptRequest(
            segment_text=str(segment["script_text"]),
            segment_duration_seconds=float(segment["audio_duration_seconds"]),
            segment_index=int(segment["segment_index"]),
            segment_count=int(segment["segment_count"]),
            identity_image_count=1,
            has_continuity_anchor=False,
            include_dialogue_transcript=True,
        )
    )
    invariants = (
        "serves as both the first frame and the final frame of [Shot 1]",
        "The first visible frame corresponds to <Picture 1>.",
        "The final visible frame corresponds to <Picture 1>",
        "during the remaining visual tail",
        f"<d>[Chinese] {segment['script_text']}</d>",
        "<Audio 1>: fully_copy",
    )
    if any(value not in prompt for value in invariants):
        raise RuntimeError(
            f"第 {row['row']} 条第 {segment['segment_index'] + 1} 段 Prompt 不变量缺失"
        )
    if "<cutoff>" in prompt or "<Picture 2>" in prompt or "<Picture 6>" in prompt:
        raise RuntimeError(
            f"第 {row['row']} 条第 {segment['segment_index'] + 1} 段 Prompt 出现冲突标签"
        )
    return prompt


def _build_graph(
    row: dict[str, Any],
    segment: dict[str, Any],
    prompt: str,
    *,
    video_remote: str,
    audio_remote: str,
    image_remote: str,
) -> Any:
    return load_default_h3_graph_builder().build(
        H3GraphBuildRequest(
            prompt=prompt,
            reference_video=video_remote,
            reference_audio=audio_remote,
            reference_images=(image_remote,),
            audio_duration_seconds=float(segment["audio_duration_seconds"]),
            generation_tail_seconds=float(row["generation_tail_seconds"]),
            aspect_ratio=str(row["aspect_ratio"]),
            megapixels=float(row["megapixels"]),
            multiple=int(row["multiple"]),
            seed=int(segment["seed"]),
        )
    )


def _assert_graph(
    row: dict[str, Any], segment: dict[str, Any], prompt: str, graph: Any
) -> None:
    workflow = json.loads(graph.workflow_json)
    label = f"第 {row['row']} 条第 {segment['segment_index'] + 1} 段"
    if workflow["83"]["inputs"]["text"] != prompt:
        raise RuntimeError(f"{label}工作流 Prompt 注入失败")
    if int(workflow["243"]["inputs"]["noise_seed"]) != int(segment["seed"]):
        raise RuntimeError(f"{label}工作流 seed 变化")
    reference_keys = sorted(
        key for key in workflow["108"]["inputs"] if key.startswith("ref_images.")
    )
    if reference_keys != ["ref_images.ref_image_0"]:
        raise RuntimeError(f"{label}工作流不是单封面图输入")


def _account_client() -> tuple[RunningHubClient, dict[str, Any]]:
    with SessionLocal() as db:
        account = db.scalar(
            select(RunningHubExecutionAccount)
            .where(RunningHubExecutionAccount.id == EXECUTION_ACCOUNT_ID)
            .options(selectinload(RunningHubExecutionAccount.h3_capability))
        )
        if account is None or not account.is_enabled:
            raise RuntimeError("指定 RunningHub 执行账号不存在或已停用")
        capability = account.h3_capability
        if capability is None or not capability.is_enabled or not capability.workflow_id:
            raise RuntimeError("指定 RunningHub 执行账号未开通 H3")
        client = RunningHubClient(
            decrypt_secret(account.api_key_encrypted),
            account.base_url,
            capability.workflow_id,
            submission_type="raw-workflow",
            access_password=decrypt_secret(
                capability.access_password_encrypted,
                label="H3 工作流访问密码",
            ),
        )
        safe = {
            "execution_account_id": account.id,
            "label": account.label,
            "workflow_id": capability.workflow_id,
            "instance_type": capability.instance_type,
            "max_concurrent_tasks": min(
                account.max_concurrent_tasks,
                capability.max_concurrent_tasks,
            ),
        }
    return client, safe


def _call_key(row: int, segment_index: int) -> str:
    return f"row_{row:02d}_segment_{segment_index + 1:02d}"


def _load_or_create_state(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("version") != STATE_VERSION or state.get("plan") != plan:
            raise RuntimeError("已有检查点与本次双素材多分段计划不同，拒绝继续")
        return state
    calls = []
    for row in plan["sources"]:
        for segment in row["segments"]:
            calls.append(
                {
                    "key": _call_key(row["row"], segment["segment_index"]),
                    "row": row["row"],
                    "segment_index": segment["segment_index"],
                    "status": "planned",
                }
            )
    state = {
        "version": STATE_VERSION,
        "created_at": _now(),
        "updated_at": _now(),
        "plan": plan,
        "uploads": {},
        "calls": calls,
    }
    _write_json(path, state)
    return state


def _upload_once(
    client: RunningHubClient,
    state_path: Path,
    state: dict[str, Any],
    key: str,
    path: Path,
) -> str:
    digest = _sha256_file(path)
    existing = state["uploads"].get(key)
    if existing:
        if existing.get("sha256") != digest:
            raise RuntimeError(f"素材 {key} 已变化，拒绝复用检查点")
        return str(existing["remote_name"])
    print(f"UPLOAD {key}: {path.name}", flush=True)
    remote_name = client.upload_file(path)
    state["uploads"][key] = {
        "path": str(path),
        "sha256": digest,
        "remote_name": remote_name,
        "uploaded_at": _now(),
    }
    _save_state(state_path, state)
    return remote_name


def _submitted_count(state: dict[str, Any]) -> int:
    return sum(
        1 for call in state["calls"] if call.get("status") not in {None, "planned"}
    )


def _submit(
    client: RunningHubClient,
    state_path: Path,
    state: dict[str, Any],
    call: dict[str, Any],
    graph: Any,
    *,
    instance_type: str,
) -> None:
    if call.get("task_id"):
        return
    if call.get("status") != "planned":
        raise RuntimeError(f"{call['key']} 曾发生不确定提交，拒绝自动重提")
    if _submitted_count(state) >= AUTHORIZED_PAID_CALLS:
        raise RuntimeError("本次实验四次付费调用额度已经用完")
    call.update(
        {
            "status": "submitting",
            "submission_started_at": _now(),
            "dynamic_graph_sha256": graph.dynamic_graph_sha256,
        }
    )
    _save_state(state_path, state)
    try:
        task_id = client.submit_task(
            {
                "workflow": graph.workflow_json,
                "addMetadata": True,
                "instanceType": instance_type,
                "usePersonalQueue": False,
            }
        )
    except RunningHubError as exc:
        call.update(
            {
                "status": (
                    "submission_ambiguous"
                    if exc.submission_outcome_unknown
                    else "submission_rejected"
                ),
                "submission_error": str(exc),
                "submission_outcome_unknown": exc.submission_outcome_unknown,
            }
        )
        _save_state(state_path, state)
        raise
    call.update({"status": "submitted", "task_id": task_id, "submitted_at": _now()})
    _save_state(state_path, state)
    print(f"SUBMITTED {call['key']}: task={task_id}", flush=True)


def _select_video(result: dict[str, Any]) -> dict[str, Any]:
    values = result.get("results")
    if not isinstance(values, list):
        raise RuntimeError("RunningHub SUCCESS 响应缺少 results")
    for value in values:
        if (
            isinstance(value, dict)
            and str(value.get("nodeId") or "") == "387"
            and str(value.get("outputType") or "").lower().lstrip(".") == "mp4"
            and value.get("url")
        ):
            return value
    raise RuntimeError("RunningHub SUCCESS 响应缺少节点 387 的 MP4")


def _poll_and_download(
    client: RunningHubClient,
    state_path: Path,
    state: dict[str, Any],
    output_dir: Path,
) -> None:
    last_status: dict[str, str] = {}
    while True:
        unfinished = False
        for call in state["calls"]:
            if call.get("status") in {"success", "remote_failed"}:
                continue
            task_id = call.get("task_id")
            if not task_id:
                raise RuntimeError(f"{call['key']} 缺少 task_id")
            result = client.query_task(str(task_id))
            status = str(result.get("status") or "").upper()
            if last_status.get(call["key"]) != status:
                print(f"POLL {call['key']}: {status}", flush=True)
                last_status[call["key"]] = status
            call["remote_status"] = status
            call["last_polled_at"] = _now()
            if status in FINAL_STATUSES:
                call["query_result"] = result
                call["usage"] = result.get("usage")
                if status != "SUCCESS":
                    call["status"] = "remote_failed"
                else:
                    selected = _select_video(result)
                    output_path = output_dir / f"{call['key']}.mp4"
                    if not output_path.is_file():
                        client.download_result(str(selected["url"]), output_path)
                    call.update(
                        {
                            "status": "success",
                            "output_path": str(output_path),
                            "output_sha256": _sha256_file(output_path),
                            "output_metadata": selected,
                            "completed_at": _now(),
                        }
                    )
            elif status in {"QUEUED", "RUNNING"}:
                unfinished = True
            else:
                call["status"] = "unknown_remote_status"
                call["query_result"] = result
                _save_state(state_path, state)
                raise RuntimeError(f"未知 RunningHub 状态：{status or '空'}")
            _save_state(state_path, state)
        if not unfinished:
            return
        time.sleep(10)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run two >15s source audios as four paid H3 loop-anchor segments."
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    if args.wait and not args.execute:
        raise RuntimeError("--wait 必须和 --execute 一起使用")

    sources = _source_snapshots()
    output_dir = PROJECT_ROOT / "data" / "h3-loop-anchor" / EXPERIMENT_ID
    output_dir.mkdir(parents=True, exist_ok=True)
    prompts: dict[str, str] = {}
    source_plan: list[dict[str, Any]] = []
    for row in sources:
        safe_row = dict(row)
        safe_row["segments"] = []
        for segment in row["segments"]:
            key = _call_key(int(row["row"]), int(segment["segment_index"]))
            prompt = _compile_prompt(row, segment)
            prompts[key] = prompt
            (output_dir / f"{key}_prompt.txt").write_text(prompt, encoding="utf-8")
            placeholder = _build_graph(
                row,
                segment,
                prompt,
                video_remote=f"preflight-row-{row['row']:02d}.mp4",
                audio_remote=f"preflight-{key}.mp3",
                image_remote=f"preflight-row-{row['row']:02d}-picture-1.png",
            )
            _assert_graph(row, segment, prompt, placeholder)
            safe_segment = dict(segment)
            safe_segment["prompt_sha256"] = _sha256_bytes(prompt.encode("utf-8"))
            safe_row["segments"].append(safe_segment)
        source_plan.append(safe_row)

    plan = {
        "experiment_id": EXPERIMENT_ID,
        "prompt_template_version": H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION,
        "authorized_paid_call_limit": AUTHORIZED_PAID_CALLS,
        "source_row_count": 2,
        "segment_count": 4,
        "rows": list(SOURCE_ROWS),
        "sources": source_plan,
        "continuity_mode": "fast",
        "same_picture_1_across_segments_of_each_row": True,
        "same_reference_video_across_segments_of_each_row": True,
        "segment_audio_changes_per_call": True,
        "automatic_paid_retry": False,
    }
    state_path = output_dir / "state.json"
    state = _load_or_create_state(state_path, plan)
    _write_json(
        output_dir / "preflight.json",
        {
            "checked_at": _now(),
            "source_row_count": 2,
            "segment_count": 4,
            "paid_call_count": 4,
            "rows": list(SOURCE_ROWS),
            "prompt_template_version": H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION,
            "fast_mode": True,
            "graph_validation_passed": True,
            "same_picture_1_within_each_row": True,
            "source_files_verified": True,
        },
    )
    print(
        json.dumps(
            {
                "experiment_id": EXPERIMENT_ID,
                "prompt_template_version": H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION,
                "rows": [
                    {
                        "row": row["row"],
                        "cover": row["cover_name"],
                        "full_audio_duration_seconds": row[
                            "full_audio_duration_seconds"
                        ],
                        "segments": [
                            {
                                "segment": segment["segment_index"] + 1,
                                "audio_duration_seconds": segment[
                                    "audio_duration_seconds"
                                ],
                                "seed": segment["seed"],
                            }
                            for segment in row["segments"]
                        ],
                    }
                    for row in sources
                ],
                "paid_calls": 4,
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    if not args.execute:
        print("PREPARED_ONLY: no upload and no paid call", flush=True)
        return 0

    client, safe_account = _account_client()
    account_status = client.get_account_status()
    remaining_calls = sum(
        1 for call in state["calls"] if call.get("status") == "planned"
    )
    if account_status.current_task_count + remaining_calls > safe_account["max_concurrent_tasks"]:
        raise RuntimeError("当前 H3 账号并发不足以安全提交剩余四段")
    state["safe_account"] = safe_account
    state["account_preflight"] = {
        "checked_at": _now(),
        "current_task_count": account_status.current_task_count,
        "remain_coins": (
            str(account_status.remain_coins)
            if account_status.remain_coins is not None
            else None
        ),
        "remain_money": (
            str(account_status.remain_money)
            if account_status.remain_money is not None
            else None
        ),
        "currency": account_status.currency,
        "api_type": account_status.api_type,
    }
    _save_state(state_path, state)

    calls = {str(call["key"]): call for call in state["calls"]}
    for row in sources:
        row_number = int(row["row"])
        video_remote = _upload_once(
            client,
            state_path,
            state,
            f"row_{row_number:02d}_video",
            Path(row["video_path"]),
        )
        image_remote = _upload_once(
            client,
            state_path,
            state,
            f"row_{row_number:02d}_picture_1",
            Path(row["image_path"]),
        )
        for segment in row["segments"]:
            key = _call_key(row_number, int(segment["segment_index"]))
            audio_remote = _upload_once(
                client,
                state_path,
                state,
                f"{key}_audio",
                Path(segment["audio_path"]),
            )
            graph = _build_graph(
                row,
                segment,
                prompts[key],
                video_remote=video_remote,
                audio_remote=audio_remote,
                image_remote=image_remote,
            )
            _assert_graph(row, segment, prompts[key], graph)
            _submit(
                client,
                state_path,
                state,
                calls[key],
                graph,
                instance_type=safe_account["instance_type"],
            )
    print("QUEUED: exactly 2 source audios / 4 paid H3 segment calls", flush=True)
    if args.wait:
        _poll_and_download(client, state_path, state, output_dir)
        failed = [call for call in state["calls"] if call.get("status") != "success"]
        if failed:
            raise RuntimeError("至少一段远端任务失败；不会自动付费重试")
        print(f"COMPLETE: {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
