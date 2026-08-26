from __future__ import annotations

import argparse
import difflib
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
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
from app.services.h3.prompt import H3PromptRequest, compile_ref2va_prompt  # noqa: E402
from app.services.runninghub import RunningHubClient, RunningHubError  # noqa: E402
from app.services.security import decrypt_secret  # noqa: E402


SOURCE_REQUEST_KEY = "h3-v7-temp-0823a-r02"
SOURCE_ROW_NUMBER = 1
SOURCE_SEGMENT_INDEX = 0
EXECUTION_ACCOUNT_ID = 1
FIXED_SEED = 3202009804649012916
AUTHORIZED_PAID_CALLS = 2
EXPERIMENT_ID = "20260823-transcript-vs-audio-only-r02-seg1"
STATE_VERSION = "h3.transcript-ab.once.v1"
FINAL_STATUSES = {"SUCCESS", "FAILED", "CANCELLED"}


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


def _absolute_data_path(relative_path: str) -> Path:
    path = Path(relative_path)
    if not path.is_absolute():
        path = get_settings().data_dir / path
    resolved = path.resolve()
    data_root = get_settings().data_dir.resolve()
    if resolved != data_root and data_root not in resolved.parents:
        raise RuntimeError("实验素材不在项目 data 目录内")
    if not resolved.is_file():
        raise RuntimeError(f"实验素材不存在：{resolved}")
    return resolved


def _source_snapshot() -> dict[str, Any]:
    with SessionLocal() as db:
        batch = db.scalar(
            select(GenerationBatch)
            .where(GenerationBatch.request_key == SOURCE_REQUEST_KEY)
            .options(
                selectinload(GenerationBatch.h3_config),
                selectinload(GenerationBatch.items)
                .selectinload(GenerationBatchItem.h3_config),
                selectinload(GenerationBatch.items)
                .selectinload(GenerationBatchItem.segments)
                .selectinload(GenerationSegment.h3_config),
            )
        )
        if batch is None or batch.h3_config is None:
            raise RuntimeError("找不到指定的 H3 A/B 来源批次")
        item = next(
            (value for value in batch.items if value.row_number == SOURCE_ROW_NUMBER),
            None,
        )
        if item is None or item.h3_config is None:
            raise RuntimeError("找不到指定的 H3 A/B 来源行")
        segment = next(
            (
                value
                for value in item.segments
                if value.segment_index == SOURCE_SEGMENT_INDEX
            ),
            None,
        )
        if segment is None or segment.h3_config is None:
            raise RuntimeError("找不到指定的 H3 A/B 来源分段")
        try:
            image_snapshots = json.loads(batch.h3_config.reference_images_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("来源批次的参考图快照损坏") from exc
        if not isinstance(image_snapshots, list) or len(image_snapshots) != 1:
            raise RuntimeError("本次实验必须严格复用一张 Picture 1")
        image_path = str(image_snapshots[0].get("path") or "")
        duration = (
            segment.h3_config.requested_generation_duration_seconds
            - batch.h3_config.generation_tail_seconds
        )
        snapshot = {
            "source_request_key": SOURCE_REQUEST_KEY,
            "source_batch_id": batch.id,
            "source_item_id": item.id,
            "source_segment_id": segment.id,
            "segment_text": segment.script_text,
            "segment_index": segment.segment_index,
            "segment_count": item.h3_config.segment_count,
            "user_direction": item.h3_config.user_direction,
            "video_path": str(_absolute_data_path(item.h3_config.reference_video_path)),
            "audio_path": str(_absolute_data_path(segment.audio_path)),
            "image_paths": [str(_absolute_data_path(image_path))],
            "audio_duration_seconds": float(duration),
            "generation_tail_seconds": float(batch.h3_config.generation_tail_seconds),
            "aspect_ratio": item.h3_config.aspect_ratio,
            "megapixels": float(item.h3_config.megapixels),
            "multiple": int(item.h3_config.multiple),
            "seed": FIXED_SEED,
        }
    for key in ("video_path", "audio_path"):
        snapshot[f"{key}_sha256"] = _sha256_file(Path(snapshot[key]))
    snapshot["image_sha256s"] = [
        _sha256_file(Path(value)) for value in snapshot["image_paths"]
    ]
    return snapshot


def _prompts(source: dict[str, Any]) -> tuple[str, str]:
    common = {
        "segment_text": source["segment_text"],
        "segment_duration_seconds": source["audio_duration_seconds"],
        "segment_index": source["segment_index"],
        "segment_count": source["segment_count"],
        "user_direction": source["user_direction"],
        "identity_image_count": len(source["image_paths"]),
        "has_continuity_anchor": False,
    }
    prompt_a = compile_ref2va_prompt(
        H3PromptRequest(**common, include_dialogue_transcript=True)
    )
    prompt_b = compile_ref2va_prompt(
        H3PromptRequest(**common, include_dialogue_transcript=False)
    )
    return prompt_a, prompt_b


def _assert_prompt_pair(source: dict[str, Any], prompt_a: str, prompt_b: str) -> None:
    transcript = str(source["segment_text"])
    block_a = (
        "From the first audible moment, <Subject 1> (S1) physically speaks "
        "using <Audio 1> and says exactly, "
        f"<d>[Chinese] {transcript}</d> The mouth, lips, jaw, and subtle facial "
        "muscles follow every audible word, pause, and rhythm accurately."
    )
    block_b = (
        "From the first audible moment, <Subject 1> (S1) naturally speaks in "
        "precise synchronization with <Audio 1>. The mouth, lips, jaw, and "
        "subtle facial muscles follow the supplied speech timing, pauses, "
        "rhythm, pace, and delivery accurately and naturally."
    )
    if prompt_a.count(block_a) != 1 or prompt_b.count(block_b) != 1:
        raise RuntimeError("A/B 对白实验块无法唯一定位")
    if prompt_a.replace(block_a, block_b) != prompt_b:
        raise RuntimeError("A/B Prompt 除指定对白块外仍有其它差异")
    if transcript not in prompt_a or "<d>[Chinese]" not in prompt_a:
        raise RuntimeError("Variant A 缺少完整官方 transcript")
    if transcript in prompt_b or "<d>" in prompt_b or "</d>" in prompt_b:
        raise RuntimeError("Variant B 仍然包含 transcript 或 <d> 标签")
    forbidden_b = (
        "no subtitles",
        "no captions",
        "no text",
        "no text overlay",
        "no dialogue text",
        "no burned-in text",
        "无字幕",
        "不要字幕",
        "没有文字",
    )
    lowered_b = prompt_b.lower()
    if any(value.lower() in lowered_b for value in forbidden_b):
        raise RuntimeError("Variant B 意外加入了字幕相关负向词")
    invariants = (
        "<Audio 1> is the complete uploaded Mandarin segment audio physically "
        "spoken by <Subject 1> (S1) and reused as the complete final audio track "
        "with its exact dialogue, voice, pauses, rhythm, pace, tone, and delivery.",
        "<Audio 1>: fully_copy - reuse it 1:1 as the complete final audio track.",
        "overall_soundscape:\n<Audio 1> is the complete final audio track.",
        "The frame remains clean, natural, and unobstructed.",
    )
    for value in invariants:
        if prompt_a.count(value) != 1 or prompt_b.count(value) != 1:
            raise RuntimeError(f"A/B 不变量缺失：{value}")


def _build_graph(
    source: dict[str, Any],
    prompt: str,
    *,
    video_remote: str,
    audio_remote: str,
    image_remotes: tuple[str, ...],
) -> Any:
    return load_default_h3_graph_builder().build(
        H3GraphBuildRequest(
            prompt=prompt,
            reference_video=video_remote,
            reference_audio=audio_remote,
            reference_images=image_remotes,
            audio_duration_seconds=source["audio_duration_seconds"],
            generation_tail_seconds=source["generation_tail_seconds"],
            aspect_ratio=source["aspect_ratio"],
            megapixels=source["megapixels"],
            multiple=source["multiple"],
            seed=source["seed"],
        )
    )


def _assert_graph_pair(graph_a: Any, graph_b: Any) -> None:
    workflow_a = json.loads(graph_a.workflow_json)
    workflow_b = json.loads(graph_b.workflow_json)
    prompt_a = workflow_a["83"]["inputs"].pop("text")
    prompt_b = workflow_b["83"]["inputs"].pop("text")
    if prompt_a == prompt_b:
        raise RuntimeError("A/B 工作流 Prompt 没有形成实验差异")
    if workflow_a != workflow_b:
        raise RuntimeError("A/B 动态工作流除节点 83 Prompt 外存在其它差异")
    if workflow_a["243"]["inputs"]["noise_seed"] != FIXED_SEED:
        raise RuntimeError("A/B 工作流没有使用固定 seed")


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
        api_key = decrypt_secret(account.api_key_encrypted)
        password = decrypt_secret(
            capability.access_password_encrypted,
            label="H3 工作流访问密码",
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
        client = RunningHubClient(
            api_key,
            account.base_url,
            capability.workflow_id,
            submission_type="raw-workflow",
            access_password=password,
        )
    return client, safe


def _load_or_create_state(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("version") != STATE_VERSION or state.get("plan") != plan:
            raise RuntimeError("已有检查点与本次 A/B 计划不同，拒绝继续")
        return state
    state = {
        "version": STATE_VERSION,
        "created_at": _now(),
        "updated_at": _now(),
        "plan": plan,
        "uploads": {},
        "calls": [
            {"variant": "A_transcript", "status": "planned"},
            {"variant": "B_audio_only", "status": "planned"},
        ],
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
        1
        for call in state["calls"]
        if call.get("status") not in {None, "planned"}
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
        raise RuntimeError(
            f"{call['variant']} 曾发生不确定提交；为避免重复扣费，拒绝自动重提"
        )
    if _submitted_count(state) >= AUTHORIZED_PAID_CALLS:
        raise RuntimeError("本次实验两次付费调用额度已经用完")
    call.update(
        {
            "status": "submitting",
            "submission_started_at": _now(),
            "seed": FIXED_SEED,
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
    print(f"SUBMITTED {call['variant']}: task={task_id}", flush=True)


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
                raise RuntimeError(f"{call['variant']} 缺少 task_id")
            result = client.query_task(str(task_id))
            status = str(result.get("status") or "").upper()
            if last_status.get(call["variant"]) != status:
                print(f"POLL {call['variant']}: {status}", flush=True)
                last_status[call["variant"]] = status
            call["remote_status"] = status
            call["last_polled_at"] = _now()
            if status in FINAL_STATUSES:
                call["query_result"] = result
                call["usage"] = result.get("usage")
                if status != "SUCCESS":
                    call["status"] = "remote_failed"
                else:
                    selected = _select_video(result)
                    output_path = output_dir / f"{call['variant']}.mp4"
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
        description="Run one fixed-seed H3 transcript vs audio-only paid A/B pair."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Upload shared assets and submit exactly two paid H3 calls.",
    )
    args = parser.parse_args()

    output_dir = PROJECT_ROOT / "data" / "h3-ab" / EXPERIMENT_ID
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "state.json"
    source = _source_snapshot()
    prompt_a, prompt_b = _prompts(source)
    _assert_prompt_pair(source, prompt_a, prompt_b)

    (output_dir / "variant_A_transcript_prompt.txt").write_text(
        prompt_a, encoding="utf-8"
    )
    (output_dir / "variant_B_audio_only_prompt.txt").write_text(
        prompt_b, encoding="utf-8"
    )
    prompt_diff = "".join(
        difflib.unified_diff(
            prompt_a.splitlines(keepends=True),
            prompt_b.splitlines(keepends=True),
            fromfile="Variant A: transcript",
            tofile="Variant B: audio-only",
        )
    )
    (output_dir / "prompt.diff").write_text(prompt_diff, encoding="utf-8")

    plan = {
        "experiment_id": EXPERIMENT_ID,
        "hypothesis": "完整中文 transcript 可能提高 H3 视觉文字出现率",
        "source": source,
        "authorized_paid_call_limit": AUTHORIZED_PAID_CALLS,
        "variants": ["A_transcript", "B_audio_only"],
        "only_prompt_difference": "spoken dialogue block",
        "negative_subtitle_terms_added": False,
    }
    state = _load_or_create_state(state_path, plan)

    placeholder_graph_a = _build_graph(
        source,
        prompt_a,
        video_remote="preflight-video.mp4",
        audio_remote="preflight-audio.mp3",
        image_remotes=("preflight-picture-1.png",),
    )
    placeholder_graph_b = _build_graph(
        source,
        prompt_b,
        video_remote="preflight-video.mp4",
        audio_remote="preflight-audio.mp3",
        image_remotes=("preflight-picture-1.png",),
    )
    _assert_graph_pair(placeholder_graph_a, placeholder_graph_b)
    preflight = {
        "checked_at": _now(),
        "prompt_a_sha256": _sha256_bytes(prompt_a.encode("utf-8")),
        "prompt_b_sha256": _sha256_bytes(prompt_b.encode("utf-8")),
        "prompt_only_diff_verified": True,
        "workflow_only_node_83_text_diff_verified": True,
        "fixed_seed": FIXED_SEED,
        "audio_definition_unchanged": True,
        "audio_fully_copy_unchanged": True,
        "overall_soundscape_unchanged": True,
        "negative_subtitle_terms_added": False,
        "source_files_verified": True,
    }
    _write_json(output_dir / "preflight.json", preflight)
    print(f"PREFLIGHT OK: {output_dir}", flush=True)
    print(prompt_diff, flush=True)
    if not args.execute:
        print("DRY RUN ONLY: no upload and no paid call", flush=True)
        return 0

    client, safe_account = _account_client()
    account_status = client.get_account_status()
    if account_status.current_task_count + 2 > safe_account["max_concurrent_tasks"]:
        raise RuntimeError(
            "当前 RunningHub 任务数不足以同时提交严格 A/B 两条，请稍后再运行"
        )
    state["safe_account"] = safe_account
    state["account_preflight"] = {
        "checked_at": _now(),
        "current_task_count": account_status.current_task_count,
        "remain_coins": str(account_status.remain_coins) if account_status.remain_coins is not None else None,
        "remain_money": str(account_status.remain_money) if account_status.remain_money is not None else None,
        "currency": account_status.currency,
        "api_type": account_status.api_type,
    }
    _save_state(state_path, state)

    video_remote = _upload_once(
        client, state_path, state, "video", Path(source["video_path"])
    )
    audio_remote = _upload_once(
        client, state_path, state, "audio", Path(source["audio_path"])
    )
    image_remotes = tuple(
        _upload_once(client, state_path, state, f"picture_{index}", Path(path))
        for index, path in enumerate(source["image_paths"], start=1)
    )
    graph_a = _build_graph(
        source,
        prompt_a,
        video_remote=video_remote,
        audio_remote=audio_remote,
        image_remotes=image_remotes,
    )
    graph_b = _build_graph(
        source,
        prompt_b,
        video_remote=video_remote,
        audio_remote=audio_remote,
        image_remotes=image_remotes,
    )
    _assert_graph_pair(graph_a, graph_b)

    calls = {call["variant"]: call for call in state["calls"]}
    _submit(
        client,
        state_path,
        state,
        calls["A_transcript"],
        graph_a,
        instance_type=safe_account["instance_type"],
    )
    _submit(
        client,
        state_path,
        state,
        calls["B_audio_only"],
        graph_b,
        instance_type=safe_account["instance_type"],
    )
    _poll_and_download(client, state_path, state, output_dir)
    failed = [call for call in state["calls"] if call.get("status") != "success"]
    if failed:
        raise RuntimeError("A/B 中至少一条远端任务失败；不会自动重试")
    print(f"A/B COMPLETE: {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
