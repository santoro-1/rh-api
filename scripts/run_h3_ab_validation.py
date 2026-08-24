from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import subprocess
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

from app.database import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.services.h3.graph import H3GraphBuildRequest, load_default_h3_graph_builder  # noqa: E402
from app.services.h3.postprocess import (  # noqa: E402
    H3_OUTPUT_CONTRACT_VERSION,
    postprocess_h3_result,
)
from app.services.h3.prompt import H3PromptRequest, compile_ref2va_prompt  # noqa: E402
from app.services.runninghub import RunningHubClient, RunningHubError  # noqa: E402
from app.services.security import decrypt_secret  # noqa: E402


STATE_VERSION = "h3.ab.real.v1"
FINAL_REMOTE_STATUSES = {"SUCCESS", "FAILED", "CANCELLED"}
UPLOAD_REUSE_SECONDS = 20 * 60 * 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_state(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        state = {
            "version": STATE_VERSION,
            "created_at": _now(),
            "updated_at": _now(),
            "plan": expected,
            "uploads": {},
            "calls": [
                {"number": 1, "name": "segment_01_fast", "status": "planned"},
                {"number": 2, "name": "segment_02_fast", "status": "planned"},
                {"number": 3, "name": "segment_02_soft_chain", "status": "planned"},
            ],
        }
        _write_json(path, state)
        return state
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != STATE_VERSION:
        raise RuntimeError("检查点与本次 A/B 计划不一致，拒绝继续付费提交")
    current_plan = state.get("plan")
    if not isinstance(current_plan, dict):
        raise RuntimeError("检查点与本次 A/B 计划不一致，拒绝继续付费提交")
    if current_plan != expected:
        current_without_limit = dict(current_plan)
        expected_without_limit = dict(expected)
        previous_limit = current_without_limit.pop("authorized_paid_call_limit", None)
        next_limit = expected_without_limit.pop("authorized_paid_call_limit", None)
        if current_without_limit != expected_without_limit:
            raise RuntimeError("检查点与本次 A/B 计划不一致，拒绝继续付费提交")
        if (
            type(previous_limit) is not int
            or type(next_limit) is not int
            or previous_limit not in {3, 4}
            or next_limit != previous_limit + 1
        ):
            raise RuntimeError("付费调用上限只能在用户明确授权后逐次增加 1")
        state["plan"] = expected
        state.setdefault("authorization_updates", []).append(
            {
                "updated_at": _now(),
                "previous_paid_call_limit": previous_limit,
                "authorized_paid_call_limit": next_limit,
                "reason": (
                    "用户明确授权补回首次无输出调用"
                    if previous_limit == 3
                    else "用户明确授权完成缺失的 soft-chain 对照"
                ),
            }
        )
    calls = state.get("calls")
    if not isinstance(calls, list):
        raise RuntimeError("检查点调用记录损坏，拒绝继续付费提交")
    first = next((call for call in calls if call.get("name") == "segment_01_fast"), None)
    replacement = next(
        (call for call in calls if call.get("name") == "segment_01_fast_replacement"),
        None,
    )
    if (
        first
        and first.get("status") == "unrecoverable_no_saved_output"
        and replacement is None
        and int(state["plan"]["authorized_paid_call_limit"]) >= 4
    ):
        calls.append(
            {
                "number": max(int(call.get("number") or 0) for call in calls) + 1,
                "name": "segment_01_fast_replacement",
                "status": "planned",
                "replaces_call_number": int(first["number"]),
            }
        )
    _save(path, state)
    return state


def _save(state_path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _write_json(state_path, state)


def _ensure_zero_cost_retry(
    state_path: Path,
    state: dict[str, Any],
    *,
    original_name: str,
    retry_name: str,
) -> dict[str, Any]:
    existing = next(
        (call for call in state["calls"] if call.get("name") == retry_name),
        None,
    )
    if existing is not None:
        return existing
    original = next(
        (call for call in state["calls"] if call.get("name") == original_name),
        None,
    )
    if original is None or original.get("status") != "remote_failed":
        raise RuntimeError("只有已经明确失败的远端任务可以创建受控重试")
    usage = ((original.get("query_result") or {}).get("usage") or {})
    if any(
        value not in {None, "", "0", 0}
        for value in (
            usage.get("consumeCoins"),
            usage.get("consumeMoney"),
            usage.get("thirdPartyConsumeMoney"),
            usage.get("taskCostTime"),
        )
    ):
        raise RuntimeError("失败任务已经产生消耗，拒绝自动创建额外重试")
    attempted = sum(1 for call in state["calls"] if call.get("status") != "planned")
    authorized_limit = int(state["plan"]["authorized_paid_call_limit"])
    if attempted >= authorized_limit:
        raise RuntimeError(f"累计 {authorized_limit} 次真实调用额度已经用完")
    retry = {
        "number": max(int(call.get("number") or 0) for call in state["calls"]) + 1,
        "name": retry_name,
        "status": "planned",
        "retries_call_number": int(original["number"]),
        "retry_reason": "远端任务运行 0 秒且未产生可见费用，用户授权额度内受控重试",
    }
    state["calls"].append(retry)
    _save(state_path, state)
    return retry


def _run_ffmpeg(command: list[str], message: str) -> None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("本机未安装 ffmpeg，无法生成 A/B 本地成片") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{message}：处理超时") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"{message}：{completed.stderr[-1000:]}")


def _ensure_join_artifact(
    state_path: Path,
    state: dict[str, Any],
    *,
    output_dir: Path,
    artifact_name: str,
    first_video: Path,
    second_video: Path,
) -> Path:
    for path in (first_video, second_video):
        if not path.is_file():
            raise RuntimeError(f"A/B 本地成片缺少输入：{path}")
    target = output_dir / f"{artifact_name}.mp4"
    existing = state.get("artifacts", {}).get(artifact_name, {})
    if (
        not target.is_file()
        or existing.get("output_contract_version") != H3_OUTPUT_CONTRACT_VERSION
    ):
        _run_ffmpeg(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-y",
                "-i", str(first_video), "-i", str(second_video),
                "-filter_complex",
                (
                    "[0:v]setpts=PTS-STARTPTS[v0];"
                    "[0:a]asetpts=PTS-STARTPTS[a0];"
                    "[1:v]setpts=PTS-STARTPTS[v1];"
                    "[1:a]asetpts=PTS-STARTPTS[a1];"
                    "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
                ),
                "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "18",
                "-x264-params", "bframes=0",
                "-c:a", "aac", "-b:a", "192k", "-ar", "32000",
                "-movflags", "+faststart", str(target),
            ],
            f"合并 {artifact_name} H3 原生音画失败",
        )
    state.setdefault("artifacts", {})[artifact_name] = {
        "path": str(target),
        "sha256": _sha256(target),
        "audio_policy": "h3_generated_audio_preserved",
        "output_contract_version": H3_OUTPUT_CONTRACT_VERSION,
        "updated_at": _now(),
    }
    _save(state_path, state)
    return target


def _account(user_id: int) -> tuple[str, str]:
    with SessionLocal() as db:
        user = db.scalar(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.runninghub_config))
        )
        if user is None or not user.is_active:
            raise RuntimeError("指定本地用户不存在或已停用")
        config = user.runninghub_config
        if config is None or not config.api_key_encrypted:
            raise RuntimeError("指定本地用户没有 RunningHub API Key")
        return decrypt_secret(config.api_key_encrypted), config.base_url


def _upload_once(
    client: RunningHubClient,
    state_path: Path,
    state: dict[str, Any],
    key: str,
    path: Path,
) -> str:
    current = state["uploads"].get(key)
    digest = _sha256(path)
    if current:
        if current.get("sha256") != digest:
            raise RuntimeError(f"素材 {key} 已变化，拒绝沿用旧检查点")
        try:
            uploaded_at = datetime.fromisoformat(str(current["uploaded_at"]))
            if uploaded_at.tzinfo is None:
                uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - uploaded_at).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = float("inf")
        if 0 <= age <= UPLOAD_REUSE_SECONDS:
            return str(current["remote_name"])
        state.setdefault("upload_history", {}).setdefault(key, []).append(current)
        print(f"REUPLOAD {key}: previous media may have expired", flush=True)
    else:
        print(f"UPLOAD {key}: {path.name}", flush=True)
    remote_name = client.upload_file(path)
    state["uploads"][key] = {
        "path": str(path),
        "sha256": digest,
        "remote_name": remote_name,
        "uploaded_at": _now(),
    }
    _save(state_path, state)
    return remote_name


def _assert_submission_slot(state: dict[str, Any], call: dict[str, Any]) -> None:
    if call.get("task_id") or call.get("status") == "success":
        return
    attempted = sum(1 for value in state["calls"] if value.get("status") != "planned")
    authorized_limit = int(state["plan"]["authorized_paid_call_limit"])
    if attempted >= authorized_limit:
        raise RuntimeError(f"累计 {authorized_limit} 次真实调用额度已经用完")


def _select_video(result: dict[str, Any]) -> dict[str, Any]:
    values = result.get("results")
    if not isinstance(values, list):
        raise RuntimeError("RunningHub SUCCESS 响应缺少 results")
    candidates = [
        item
        for item in values
        if isinstance(item, dict)
        and item.get("url")
        and str(item.get("nodeId") or "") == "387"
        and str(item.get("outputType") or "").lower().lstrip(".")
        == "mp4"
    ]
    if not candidates:
        raise RuntimeError("RunningHub SUCCESS 响应中没有节点 387 的 MP4 输出")
    return candidates[0]


def _prompt(
    *,
    text: str,
    duration: float,
    index: int,
    has_anchor: bool,
) -> str:
    return compile_ref2va_prompt(
        H3PromptRequest(
            segment_text=text,
            segment_duration_seconds=duration,
            segment_index=index,
            segment_count=2,
            identity_image_count=0,
            has_continuity_anchor=has_anchor,
        )
    )


def _poll(
    client: RunningHubClient,
    state_path: Path,
    state: dict[str, Any],
    call: dict[str, Any],
) -> dict[str, Any]:
    last_status = ""
    last_print = 0.0
    while True:
        result = client.query_task(str(call["task_id"]))
        status = str(result.get("status") or "").upper()
        now = time.monotonic()
        if status != last_status or now - last_print >= 30:
            print(f"CALL {call['number']} task={call['task_id']} status={status}", flush=True)
            last_status = status
            last_print = now
        call["remote_status"] = status
        call["last_polled_at"] = _now()
        if status in FINAL_REMOTE_STATUSES:
            call["query_result"] = result
            call["status"] = "remote_success" if status == "SUCCESS" else "remote_failed"
            _save(state_path, state)
            return result
        if status not in {"QUEUED", "RUNNING"}:
            call["status"] = "unknown_remote_status"
            call["query_result"] = result
            _save(state_path, state)
            raise RuntimeError(f"未知 RunningHub 任务状态：{status or '空'}")
        _save(state_path, state)
        time.sleep(8)


def _finish_result(
    *,
    client: RunningHubClient,
    state_path: Path,
    state: dict[str, Any],
    call: dict[str, Any],
    result: dict[str, Any],
    output_dir: Path,
    visible_duration: float,
    needs_anchor: bool,
) -> None:
    if call.get("status") == "success":
        return
    selected = _select_video(result)
    extension = str(selected.get("outputType") or "mp4").lower().lstrip(".")
    raw = output_dir / f"call_{call['number']:02d}_{call['name']}.raw.{extension}"
    if not raw.exists():
        client.download_result(str(selected["url"]), raw)
    normalized = postprocess_h3_result(
        raw,
        needs_continuity_anchor=needs_anchor,
    )
    call.update(
        {
            "status": "success",
            "completed_at": _now(),
            "raw_path": str(raw),
            "raw_sha256": _sha256(raw),
            "normalized_path": str(normalized.video_path),
            "normalized_sha256": normalized.video_sha256,
            "anchor_path": str(normalized.anchor_path) if normalized.anchor_path else None,
            "anchor_sha256": normalized.anchor_sha256,
            "postprocess_contract_version": H3_OUTPUT_CONTRACT_VERSION,
            "usage": result.get("usage"),
            "output_metadata": selected,
        }
    )
    _save(state_path, state)


def _refresh_success_postprocess(
    state_path: Path,
    state: dict[str, Any],
    call: dict[str, Any],
    *,
    needs_anchor: bool,
) -> None:
    if call.get("status") != "success":
        raise RuntimeError(f"H3 成功调用状态异常：{call.get('name')}")
    raw = Path(str(call.get("raw_path") or ""))
    if not raw.is_file():
        raise RuntimeError(f"H3 原始音画缺失：{raw}")
    if call.get("postprocess_contract_version") == H3_OUTPUT_CONTRACT_VERSION:
        return
    normalized = postprocess_h3_result(raw, needs_continuity_anchor=needs_anchor)
    call.update(
        {
            "normalized_path": str(normalized.video_path),
            "normalized_sha256": normalized.video_sha256,
            "anchor_path": str(normalized.anchor_path) if normalized.anchor_path else None,
            "anchor_sha256": normalized.anchor_sha256,
            "postprocess_contract_version": H3_OUTPUT_CONTRACT_VERSION,
            "postprocess_refreshed_at": _now(),
        }
    )
    _save(state_path, state)


def _run_call(
    *,
    client: RunningHubClient,
    state_path: Path,
    state: dict[str, Any],
    call: dict[str, Any],
    output_dir: Path,
    text: str,
    duration: float,
    index: int,
    video_remote: str,
    audio_remote: str,
    anchor_remote: str | None,
    seed: int,
    needs_anchor: bool,
) -> None:
    if call.get("status") == "success":
        print(f"CALL {call['number']} already complete", flush=True)
        return
    if call.get("status") == "submitting" and not call.get("task_id"):
        raise RuntimeError(
            f"第 {call['number']} 次提交结果不明，检查点无 task_id；为避免重复扣费，拒绝自动重提"
        )
    if not call.get("task_id") and call.get("status") != "planned":
        raise RuntimeError(
            f"第 {call['number']} 次已经发生过提交尝试但没有 task_id；为避免额外调用，拒绝自动重提"
        )
    if call.get("status") == "remote_failed":
        raise RuntimeError(f"第 {call['number']} 次远端任务已失败，不会自动追加付费调用")

    has_anchor = bool(anchor_remote)
    prompt = _prompt(text=text, duration=duration, index=index, has_anchor=has_anchor)
    graph = load_default_h3_graph_builder().build(
        H3GraphBuildRequest(
            prompt=prompt,
            reference_video=video_remote,
            reference_audio=audio_remote,
            reference_images=(anchor_remote,) if anchor_remote else (),
            audio_duration_seconds=duration,
            generation_tail_seconds=0.5,
            aspect_ratio="16:9 (Widescreen)",
            megapixels=1.0,
            multiple=32,
            seed=seed,
        )
    )
    call.update(
        {
            "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "dynamic_graph_sha256": graph.dynamic_graph_sha256,
            "audio_duration_seconds": duration,
            "requested_generation_duration_seconds": graph.duration.requested_generation_duration_seconds,
            "effective_generation_duration_seconds": graph.duration.effective_generation_duration_seconds,
            "quantized_frame_count": graph.duration.quantized_frame_count,
            "seed": seed,
            "has_anchor": has_anchor,
        }
    )

    if not call.get("task_id"):
        _assert_submission_slot(state, call)
        call["status"] = "submitting"
        call["submission_started_at"] = _now()
        _save(state_path, state)
        try:
            task_id = client.submit_task(
                {
                    "workflow": graph.workflow_json,
                    "addMetadata": True,
                    "instanceType": "plus",
                    "usePersonalQueue": False,
                }
            )
        except RunningHubError as exc:
            call["status"] = (
                "submission_ambiguous"
                if exc.submission_outcome_unknown
                else "submission_rejected"
            )
            call["submission_error"] = str(exc)
            call["submission_error_code"] = exc.error_code
            call["submission_outcome_unknown"] = exc.submission_outcome_unknown
            _save(state_path, state)
            raise
        call["task_id"] = task_id
        call["status"] = "submitted"
        call["submitted_at"] = _now()
        _save(state_path, state)
        print(f"CALL {call['number']} submitted task={task_id}", flush=True)

    result = call.get("query_result")
    if (
        not isinstance(result, dict)
        or call.get("remote_status") != "SUCCESS"
        or not result.get("results")
    ):
        result = _poll(client, state_path, state, call)
    if str(result.get("status") or "").upper() != "SUCCESS":
        raise RuntimeError(f"第 {call['number']} 次远端任务未成功，不会继续后续调用")
    _finish_result(
        client=client,
        state_path=state_path,
        state=state,
        call=call,
        result=result,
        output_dir=output_dir,
        visible_duration=duration,
        needs_anchor=needs_anchor,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the explicitly authorized H3 A/B validation")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--reference-video", required=True, type=Path)
    parser.add_argument("--segment-1", required=True, type=Path)
    parser.add_argument("--segment-2", required=True, type=Path)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--authorized-paid-call-limit",
        type=int,
        choices=(3, 4, 5),
        default=3,
        help="Cumulative paid-call ceiling explicitly authorized by the user",
    )
    parser.add_argument(
        "--retry-zero-cost-fast-failure",
        action="store_true",
        help="Use one remaining authorized submission to retry a zero-cost segment-2 fast failure",
    )
    parser.add_argument(
        "--prompt-access-password",
        action="store_true",
        help="Read the workflow access password from a hidden terminal prompt",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    reference_video = args.reference_video.resolve()
    segment_1 = args.segment_1.resolve()
    segment_2 = args.segment_2.resolve()
    for path in (reference_video, segment_1, segment_2):
        if not path.is_file():
            raise RuntimeError(f"素材不存在：{path}")

    segment_1_duration = 5.23
    segment_2_duration = 3.52102
    plan = {
        "workflow_id": str(args.workflow_id),
        "user_id": args.user_id,
        "reference_video": {"path": str(reference_video), "sha256": _sha256(reference_video)},
        "segment_1": {"path": str(segment_1), "sha256": _sha256(segment_1), "duration": segment_1_duration},
        "segment_2": {"path": str(segment_2), "sha256": _sha256(segment_2), "duration": segment_2_duration},
        "script": "总要等到好状态才去干事儿的人，其实就是一种认知低下的表现。高认知的人都知道，执行力大于一切。",
        "segment_texts": [
            "总要等到好状态才去干事儿的人，其实就是一种认知低下的表现。",
            "高认知的人都知道，执行力大于一切。",
        ],
        "aspect_ratio": "16:9 (Widescreen)",
        "megapixels": 1.0,
        "multiple": 32,
        "generation_tail_seconds": 0.5,
        "instance_type": "plus",
        "seed": args.seed,
        "authorized_paid_call_limit": args.authorized_paid_call_limit,
    }
    state_path = output_dir / "checkpoint.json"
    state = _load_state(state_path, plan)
    if args.retry_zero_cost_fast_failure:
        _ensure_zero_cost_retry(
            state_path,
            state,
            original_name="segment_02_fast",
            retry_name="segment_02_fast_retry",
        )
    api_key, base_url = _account(args.user_id)
    access_password = (
        getpass.getpass("Workflow access password: ")
        if args.prompt_access_password
        else ""
    )
    client = RunningHubClient(
        api_key=api_key,
        base_url=base_url,
        ai_app_id=str(args.workflow_id),
        submission_type="raw-workflow",
        access_password=access_password,
    )
    account_status = client.get_account_status()
    if account_status.current_task_count != 0 and not any(
        call.get("task_id") and call.get("status") != "success" for call in state["calls"]
    ):
        raise RuntimeError("RunningHub 账号已有远端任务；为避免混淆本次付费审计，暂不提交")
    state["account_preflight"] = {
        "checked_at": _now(),
        "current_task_count": account_status.current_task_count,
        "remain_coins": str(account_status.remain_coins) if account_status.remain_coins is not None else None,
        "remain_money": str(account_status.remain_money) if account_status.remain_money is not None else None,
        "currency": account_status.currency,
        "api_type": account_status.api_type,
    }
    _save(state_path, state)

    video_remote = _upload_once(client, state_path, state, "reference_video", reference_video)
    segment_1_remote = _upload_once(client, state_path, state, "segment_01", segment_1)
    segment_2_remote = _upload_once(client, state_path, state, "segment_02", segment_2)

    segment_1_call = next(
        (
            call
            for call in state["calls"]
            if call.get("name") == "segment_01_fast"
            and call.get("status") != "unrecoverable_no_saved_output"
        ),
        None,
    ) or next(
        (
            call
            for call in state["calls"]
            if call.get("name") == "segment_01_fast_replacement"
        ),
        None,
    )
    if segment_1_call is None:
        raise RuntimeError("第 1 段旧输出不可恢复，且没有追加替代调用授权")
    segment_2_fast_call = next(
        (
            call
            for call in state["calls"]
            if call.get("name") == "segment_02_fast"
            and call.get("status") != "remote_failed"
        ),
        None,
    ) or next(
        (
            call
            for call in state["calls"]
            if call.get("name") == "segment_02_fast_retry"
        ),
        None,
    )
    if segment_2_fast_call is None:
        raise RuntimeError("第 2 段 fast 已失败；没有显式受控重试记录")
    segment_2_soft_call = next(
        call for call in state["calls"] if call.get("name") == "segment_02_soft_chain"
    )

    _run_call(
        client=client,
        state_path=state_path,
        state=state,
        call=segment_1_call,
        output_dir=output_dir,
        text=plan["segment_texts"][0],
        duration=segment_1_duration,
        index=0,
        video_remote=video_remote,
        audio_remote=segment_1_remote,
        anchor_remote=None,
        seed=args.seed,
        needs_anchor=True,
    )
    _run_call(
        client=client,
        state_path=state_path,
        state=state,
        call=segment_2_fast_call,
        output_dir=output_dir,
        text=plan["segment_texts"][1],
        duration=segment_2_duration,
        index=1,
        video_remote=video_remote,
        audio_remote=segment_2_remote,
        anchor_remote=None,
        seed=args.seed,
        needs_anchor=False,
    )

    _refresh_success_postprocess(
        state_path, state, segment_1_call, needs_anchor=True
    )
    _refresh_success_postprocess(
        state_path, state, segment_2_fast_call, needs_anchor=False
    )
    first_normalized = Path(str(segment_1_call.get("normalized_path") or ""))
    fast_normalized = Path(str(segment_2_fast_call.get("normalized_path") or ""))
    _ensure_join_artifact(
        state_path,
        state,
        output_dir=output_dir,
        artifact_name="fast_join",
        first_video=first_normalized,
        second_video=fast_normalized,
    )

    anchor_path = Path(str(segment_1_call.get("anchor_path") or ""))
    if not anchor_path.is_file():
        raise RuntimeError("第 1 段完整 H3 成片尾帧缺失，拒绝提交 soft-chain 任务")
    _assert_submission_slot(state, segment_2_soft_call)
    anchor_remote = _upload_once(client, state_path, state, "segment_01_anchor", anchor_path)
    _run_call(
        client=client,
        state_path=state_path,
        state=state,
        call=segment_2_soft_call,
        output_dir=output_dir,
        text=plan["segment_texts"][1],
        duration=segment_2_duration,
        index=1,
        video_remote=video_remote,
        audio_remote=segment_2_remote,
        anchor_remote=anchor_remote,
        seed=args.seed,
        needs_anchor=False,
    )
    _refresh_success_postprocess(
        state_path, state, segment_2_soft_call, needs_anchor=False
    )
    soft_normalized = Path(str(segment_2_soft_call.get("normalized_path") or ""))
    _ensure_join_artifact(
        state_path,
        state,
        output_dir=output_dir,
        artifact_name="soft_chain_join",
        first_video=first_normalized,
        second_video=soft_normalized,
    )
    state["finished_at"] = _now()
    _save(state_path, state)
    print(f"COMPLETE checkpoint={state_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
