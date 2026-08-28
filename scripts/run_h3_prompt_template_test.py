from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models import RunningHubExecutionAccount  # noqa: E402
from app.services.audio import inspect_audio_duration  # noqa: E402
from app.services.alignment.registry import get_alignment_provider  # noqa: E402
from app.services.h3.duration import plan_h3_duration  # noqa: E402
from app.services.h3.graph import (  # noqa: E402
    H3GraphBuildRequest,
    load_default_h3_graph_builder,
)
from app.services.h3.motion_references import (  # noqa: E402
    assign_h3_motion_references,
    split_h3_motion_reference,
)
from app.services.h3.segmentation import (  # noqa: E402
    H3TimestampedSegment,
    plan_h3_aligned_segments,
)
from app.services.media_segmentation import (  # noqa: E402
    MediaSegmentationError,
    cut_audio_segment,
)
from app.services.runninghub import RunningHubClient, RunningHubError  # noqa: E402
from app.services.runninghub_pool import (  # noqa: E402
    execution_account_configuration_ready,
)
from app.services.security import decrypt_secret  # noqa: E402
from app.services.video_merge import merge_segment_videos  # noqa: E402
from app.services.workflow_configs import get_system_workflow_config  # noqa: E402


WORKFLOW_KEY = "minimax_h3_ref2va"
STATE_VERSION = "h3.prompt-template-test.v2"
DEFAULT_TEMPLATE_PATH = Path(__file__).with_name("h3_video_edit_prompt_template.txt")
PICTURE_TEMPLATE_PATH = Path(__file__).with_name(
    "h3_picture_anchor_prompt_template.txt"
)
VISUAL_MODE_VIDEO = "video_primary"
VISUAL_MODE_PICTURE = "picture_primary"
VISUAL_MODES = {VISUAL_MODE_VIDEO, VISUAL_MODE_PICTURE}
MAX_REFERENCE_IMAGES = 4
SAMPLING_STEP_OPTIONS = (4, 6, 8)
FINAL_STATUSES = {"SUCCESS", "FAILED", "CANCELLED"}
H3_SECTIONS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
SUPPORTED_VARIABLES = {
    "SEGMENT_TEXT",
    "SEGMENT_INDEX",
    "SEGMENT_COUNT",
    "CUTOFF_IF_NOT_FINAL",
    "SUPPORTING_FACE_REFERENCE_CLAUSE",
    "USER_DIRECTION_IF_PRESENT",
}
_VARIABLE_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
_PROMPT_TAG_RE = re.compile(
    r"(?i)</?d>|<\s*(?:subject|picture|video|audio)\s+\d+\s*>|<cutoff>"
)


@dataclass(frozen=True)
class AccountSummary:
    account_id: int
    label: str
    instance_type: str
    max_concurrent_tasks: int


@dataclass(frozen=True)
class AccountCredentials:
    account_id: int
    label: str
    api_key: str
    base_url: str
    workflow_id: str
    instance_type: str
    access_password: str
    max_concurrent_tasks: int


@dataclass(frozen=True)
class TestInput:
    reference_video: Path
    reference_audio: Path
    script_text: str
    output_root: Path
    account_id: int
    aspect_ratio: str
    megapixels: float
    seed: int
    sampling_steps: int = 4
    visual_mode: str = VISUAL_MODE_VIDEO
    reference_images: tuple[Path, ...] = ()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_sampling_steps(value: object) -> int:
    if type(value) is not int or value not in SAMPLING_STEP_OPTIONS:
        choices = "/".join(str(option) for option in SAMPLING_STEP_OPTIONS)
        raise ValueError(f"H3 测试采样步数只能选择 {choices}")
    return value


def apply_test_sampling_steps(graph: Any, sampling_steps: int) -> tuple[str, str]:
    steps = validate_sampling_steps(sampling_steps)
    try:
        workflow = json.loads(str(graph.workflow_json))
        scheduler_inputs = workflow["248"]["inputs"]
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("H3 测试动态图缺少合法的节点 248") from exc
    if not isinstance(scheduler_inputs, dict):
        raise RuntimeError("H3 测试动态图节点 248 输入结构不合法")
    scheduler_inputs["steps"] = steps
    workflow_json = json.dumps(
        workflow,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return workflow_json, sha256_text(workflow_json)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def render_prompt_template(
    template: str,
    *,
    segment_text: str,
    segment_index: int = 1,
    segment_count: int = 1,
    reference_image_count: int = 0,
) -> str:
    clean_template = str(template or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    clean_text = str(segment_text or "").strip()
    if not clean_template:
        raise ValueError("H3 Prompt 模板不能为空")
    if not clean_text:
        raise ValueError("文案不能为空")
    if "\x00" in clean_template or "\x00" in clean_text:
        raise ValueError("模板和文案不能包含空字符")
    if _PROMPT_TAG_RE.search(clean_text):
        raise ValueError("文案不能包含 H3 引用标签、对话标签或 <cutoff>")
    if not 1 <= segment_index <= segment_count:
        raise ValueError("H3 分段序号不合法")
    if not 0 <= reference_image_count <= MAX_REFERENCE_IMAGES:
        raise ValueError(f"H3 参考图片数量必须为 0～{MAX_REFERENCE_IMAGES} 张")
    unknown = sorted(set(_VARIABLE_RE.findall(clean_template)) - SUPPORTED_VARIABLES)
    if unknown:
        raise ValueError(f"H3 Prompt 模板包含未知变量：{', '.join(unknown)}")
    if clean_template.count("{{SEGMENT_TEXT}}") != 1:
        raise ValueError("H3 Prompt 模板必须且只能包含一个 {{SEGMENT_TEXT}}")
    supporting_labels = [
        f"<Picture {index}>" for index in range(2, reference_image_count + 1)
    ]
    if supporting_labels:
        if len(supporting_labels) == 1:
            label_text = supporting_labels[0]
        else:
            label_text = ", ".join(supporting_labels[:-1]) + f", and {supporting_labels[-1]}"
        supporting_clause = (
            f"The supporting picture{'s' if len(supporting_labels) > 1 else ''} "
            f"{label_text} refine{'s' if len(supporting_labels) == 1 else ''} facial "
            "identity and structure only; <Picture 1> remains authoritative for hairstyle, "
            "body presentation, skin-tone rendering, wardrobe, accessories, environment, "
            "camera-original rendering, camera geometry, framing, and spatial scale."
        )
    else:
        supporting_clause = ""
    values = {
        "SEGMENT_TEXT": clean_text,
        "SEGMENT_INDEX": str(segment_index),
        "SEGMENT_COUNT": str(segment_count),
        "CUTOFF_IF_NOT_FINAL": " <cutoff>" if segment_index < segment_count else "",
        "SUPPORTING_FACE_REFERENCE_CLAUSE": supporting_clause,
        "USER_DIRECTION_IF_PRESENT": "",
    }
    prompt = _VARIABLE_RE.sub(lambda match: values[match.group(1)], clean_template)
    if "{{" in prompt or "}}" in prompt:
        raise ValueError("H3 Prompt 编译后仍有未解析的模板变量")
    positions: list[int] = []
    for section in H3_SECTIONS:
        marker = f"{section}:"
        if prompt.count(marker) != 1:
            raise ValueError(f"H3 Prompt 必须且只能包含一个 {marker}")
        positions.append(prompt.index(marker))
    if positions != sorted(positions):
        raise ValueError("H3 Prompt 六个段落的顺序不正确")
    if len(prompt) > 7000:
        raise ValueError(f"H3 Prompt 编译后不能超过 7000 个字符（当前 {len(prompt)}）")
    return prompt


def validate_reference_configuration(selection: TestInput) -> None:
    validate_sampling_steps(selection.sampling_steps)
    if selection.visual_mode not in VISUAL_MODES:
        raise ValueError("H3 视觉参考模式不合法")
    images = tuple(Path(path).resolve() for path in selection.reference_images)
    if len(images) > MAX_REFERENCE_IMAGES:
        raise ValueError(f"H3 测试最多选择 {MAX_REFERENCE_IMAGES} 张参考图片")
    if selection.visual_mode == VISUAL_MODE_PICTURE and not images:
        raise ValueError("图片主锚点模式必须选择至少 1 张参考图片")
    if selection.visual_mode == VISUAL_MODE_VIDEO and images:
        raise ValueError("仅视频模式不会上传图片；请清空图片或切换到图片主锚点模式")
    missing = [str(path) for path in images if not path.is_file()]
    if missing:
        raise ValueError(f"参考图片不存在：{missing[0]}")
    image_hashes = [sha256_file(path) for path in images]
    if len(image_hashes) != len(set(image_hashes)):
        raise ValueError("参考图片不能包含重复内容")


def template_path_for(selection: TestInput) -> Path:
    validate_reference_configuration(selection)
    return (
        PICTURE_TEMPLATE_PATH
        if selection.visual_mode == VISUAL_MODE_PICTURE
        else DEFAULT_TEMPLATE_PATH
    )


def plan_input_audio(
    script_text: str,
    audio_path: Path,
    audio_duration_seconds: float,
    *,
    generation_tail_seconds: float = 0.1,
) -> tuple[list[H3TimestampedSegment], dict[str, Any]]:
    """Keep a legal short input whole; align and split every overlong input."""

    clean_text = str(script_text or "").strip()
    if not clean_text:
        raise ValueError("文案不能为空")
    try:
        plan_h3_duration(audio_duration_seconds, generation_tail_seconds)
    except ValueError as single_segment_error:
        if audio_duration_seconds + generation_tail_seconds < 4:
            raise
        print(
            "音频超过 H3 单段时长，正在通过 FunASR 对齐原稿并规划自动分段…",
            flush=True,
        )
        try:
            alignment = align_with_local_funasr(audio_path, clean_text)
        except (MediaSegmentationError, RuntimeError) as exc:
            raise RuntimeError(
                "长音频自动分段无法启动或调用本机 FunASR："
                f"{exc}"
            ) from exc
        if not alignment.tokens:
            raise RuntimeError("FunASR 没有返回可用于 H3 自动分段的字词时间戳")
        try:
            plans = plan_h3_aligned_segments(
                clean_text,
                alignment.tokens,
                audio_duration_seconds,
                generation_tail_seconds=generation_tail_seconds,
            )
        except ValueError as exc:
            raise RuntimeError(f"音频与原稿无法形成 H3 安全分段：{exc}") from exc
        return plans, {
            "mode": "funasr_aligned",
            "provider": alignment.provider,
            "match_ratio": alignment.match_ratio,
            "single_segment_error": str(single_segment_error),
        }
    return [
        H3TimestampedSegment(
            index=0,
            script_text=clean_text,
            start_seconds=0.0,
            end_seconds=audio_duration_seconds,
            boundary_strength="strong",
        )
    ], {
        "mode": "single_segment",
        "provider": None,
        "match_ratio": None,
    }


def align_with_local_funasr(audio_path: Path, script_text: str):
    """Reuse a healthy ASR or temporarily start only the local ASR service."""

    provider = get_alignment_provider("funasr_http")
    try:
        return provider.align(audio_path, script_text)
    except MediaSegmentationError as first_error:
        print("本机 ASR 尚未运行，正在临时启动…", flush=True)
        try:
            from media_node.launcher import _load_worker_env, _start_asr

            _load_worker_env()
            process = _start_asr()
        except Exception as exc:  # noqa: BLE001 - startup diagnostics must be preserved
            raise RuntimeError(f"{first_error}；自动启动失败：{exc}") from exc
        try:
            return provider.align(audio_path, script_text)
        finally:
            if process is not None and process.poll() is None:
                print("自动分段完成，正在关闭临时 ASR 服务…", flush=True)
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()


def select_video_output(result: dict[str, Any]) -> dict[str, Any]:
    values = result.get("results")
    if not isinstance(values, list):
        raise RuntimeError("RunningHub SUCCESS 响应缺少 results")
    matches = [
        item
        for item in values
        if isinstance(item, dict)
        and item.get("url")
        and str(item.get("nodeId") or "") == "387"
        and str(item.get("outputType") or "").lower().lstrip(".") == "mp4"
    ]
    if not matches:
        raise RuntimeError("RunningHub SUCCESS 响应中没有节点 387 的 MP4 输出")
    return matches[0]


def list_accounts() -> list[AccountSummary]:
    with SessionLocal() as db:
        workflow = get_system_workflow_config(db, WORKFLOW_KEY)
        if not workflow.is_enabled or not workflow.ai_app_id:
            raise RuntimeError("本机数据库中的 H3 系统工作流尚未启用或未配置")
        accounts = list(
            db.scalars(
                select(RunningHubExecutionAccount)
                .where(RunningHubExecutionAccount.is_enabled.is_(True))
                .order_by(RunningHubExecutionAccount.id)
            ).all()
        )
        return [
            AccountSummary(
                account_id=account.id,
                label=account.label,
                instance_type=workflow.instance_type,
                max_concurrent_tasks=max(int(account.max_concurrent_tasks), 1),
            )
            for account in accounts
            if execution_account_configuration_ready(account)
        ]


def load_credentials(account_id: int) -> AccountCredentials:
    with SessionLocal() as db:
        account = db.get(RunningHubExecutionAccount, account_id)
        if (
            account is None
            or not account.is_enabled
            or not execution_account_configuration_ready(account)
        ):
            raise RuntimeError("所选 RunningHub 执行账号不存在、已停用或配置不完整")
        workflow = get_system_workflow_config(db, WORKFLOW_KEY)
        if not workflow.is_enabled or not workflow.ai_app_id:
            raise RuntimeError("H3 系统工作流尚未启用或未配置")
        encrypted_password = workflow.settings.get("access_password_encrypted")
        return AccountCredentials(
            account_id=account.id,
            label=account.label,
            api_key=decrypt_secret(account.api_key_encrypted),
            base_url=account.base_url,
            workflow_id=workflow.ai_app_id,
            instance_type=workflow.instance_type,
            access_password=(
                decrypt_secret(str(encrypted_password), label="H3 工作流访问密码")
                if encrypted_password
                else ""
            ),
            max_concurrent_tasks=max(int(account.max_concurrent_tasks), 1),
        )


def collect_inputs_with_window(accounts: list[AccountSummary]) -> TestInput | None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    if not accounts:
        raise RuntimeError("本机数据库中没有可用的 RunningHub H3 执行账号")
    root = tk.Tk()
    root.title("H3 动态提示词自动分段测试")
    root.geometry("940x790")
    root.minsize(800, 700)

    visual_mode_var = tk.StringVar(value=VISUAL_MODE_PICTURE)
    video_var = tk.StringVar()
    primary_image_var = tk.StringVar()
    supporting_images_var = tk.StringVar()
    supporting_images: list[Path] = []
    audio_var = tk.StringVar()
    output_var = tk.StringVar(value=str(PROJECT_ROOT / "h3_prompt_test_outputs"))
    account_labels = [
        f"{item.account_id}｜{item.label}｜并发 {item.max_concurrent_tasks}"
        for item in accounts
    ]
    account_var = tk.StringVar(value=account_labels[0])
    aspect_var = tk.StringVar(value="9:16 (Portrait Widescreen)")
    megapixels_var = tk.StringVar(value="1.0")
    seed_var = tk.StringVar(value="0")
    sampling_steps_var = tk.StringVar(value="4")
    result: dict[str, TestInput | None] = {"value": None}

    outer = ttk.Frame(root, padding=16)
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(1, weight=1)

    mode_row = ttk.LabelFrame(outer, text="视觉参考模式", padding=8)
    mode_row.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
    ttk.Radiobutton(
        mode_row,
        text="图片主锚点（图片决定人物、穿搭、场景和构图；视频提供动作）",
        variable=visual_mode_var,
        value=VISUAL_MODE_PICTURE,
    ).pack(anchor="w")
    ttk.Radiobutton(
        mode_row,
        text="仅视频（沿用昨天的测试方式，不上传图片）",
        variable=visual_mode_var,
        value=VISUAL_MODE_VIDEO,
    ).pack(anchor="w", pady=(4, 0))

    def pick_video() -> None:
        value = filedialog.askopenfilename(
            title="选择 H3 参考视频",
            filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.webm"), ("所有文件", "*.*")],
        )
        if value:
            video_var.set(value)

    def pick_audio() -> None:
        value = filedialog.askopenfilename(
            title="选择与文案一致的成品音频",
            filetypes=[("音频文件", "*.mp3 *.wav *.m4a *.aac *.flac"), ("所有文件", "*.*")],
        )
        if value:
            audio_var.set(value)

    def pick_primary_image() -> None:
        value = filedialog.askopenfilename(
            title="选择 Picture 1 主锚点图片",
            filetypes=[
                ("图片文件", "*.jpg *.jpeg *.png *.webp"),
                ("所有文件", "*.*"),
            ],
        )
        if value:
            primary_image_var.set(value)

    def pick_supporting_images() -> None:
        values = filedialog.askopenfilenames(
            title="选择辅助人脸图片（最多 3 张）",
            filetypes=[
                ("图片文件", "*.jpg *.jpeg *.png *.webp"),
                ("所有文件", "*.*"),
            ],
        )
        if not values:
            return
        if len(values) > MAX_REFERENCE_IMAGES - 1:
            messagebox.showerror(
                "图片过多",
                f"辅助人脸图片最多 {MAX_REFERENCE_IMAGES - 1} 张",
                parent=root,
            )
            return
        supporting_images[:] = [Path(value).resolve() for value in values]
        supporting_images_var.set("；".join(str(path) for path in supporting_images))

    def clear_supporting_images() -> None:
        supporting_images.clear()
        supporting_images_var.set("")

    def pick_output() -> None:
        value = filedialog.askdirectory(title="选择测试输出目录")
        if value:
            output_var.set(value)

    rows = [
        ("参考视频（动作）", video_var, pick_video),
        ("Picture 1 主图", primary_image_var, pick_primary_image),
        ("辅助人脸图", supporting_images_var, pick_supporting_images),
        ("成品音频", audio_var, pick_audio),
        ("输出目录", output_var, pick_output),
    ]
    image_widgets: list[tk.Widget] = []
    for row_index, (label, variable, command) in enumerate(rows, start=1):
        ttk.Label(outer, text=label).grid(row=row_index, column=0, sticky="w", pady=6)
        entry = ttk.Entry(outer, textvariable=variable)
        entry.grid(
            row=row_index, column=1, sticky="ew", padx=(12, 8), pady=6
        )
        button = ttk.Button(outer, text="选择…", command=command)
        button.grid(
            row=row_index, column=2, sticky="e", pady=6
        )
        if row_index in {2, 3}:
            image_widgets.extend((entry, button))

    clear_button = ttk.Button(outer, text="清空辅助图", command=clear_supporting_images)
    clear_button.grid(row=3, column=3, sticky="e", padx=(8, 0), pady=6)
    image_widgets.append(clear_button)

    ttk.Label(
        outer,
        text="图片模式：第一张固定为 <Picture 1>；辅助图只补充同一人物的人脸身份。",
        foreground="#555555",
    ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 6))

    def update_image_controls(*_args: object) -> None:
        state = "normal" if visual_mode_var.get() == VISUAL_MODE_PICTURE else "disabled"
        for widget in image_widgets:
            widget.configure(state=state)

    visual_mode_var.trace_add("write", update_image_controls)
    update_image_controls()

    ttk.Label(outer, text="执行账号").grid(row=7, column=0, sticky="w", pady=6)
    ttk.Combobox(
        outer,
        textvariable=account_var,
        values=account_labels,
        state="readonly",
    ).grid(row=7, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=6)

    settings_row = ttk.Frame(outer)
    settings_row.grid(row=8, column=0, columnspan=3, sticky="ew", pady=6)
    ttk.Label(settings_row, text="画幅").pack(side="left")
    ttk.Combobox(
        settings_row,
        textvariable=aspect_var,
        values=(
            "9:16 (Portrait Widescreen)",
            "16:9 (Widescreen)",
            "1:1 (Square)",
        ),
        state="readonly",
        width=31,
    ).pack(side="left", padx=(8, 18))
    ttk.Label(settings_row, text="MP").pack(side="left")
    ttk.Entry(settings_row, textvariable=megapixels_var, width=8).pack(
        side="left", padx=(8, 18)
    )
    ttk.Label(settings_row, text="Seed").pack(side="left")
    ttk.Entry(settings_row, textvariable=seed_var, width=11).pack(
        side="left", padx=(8, 18)
    )
    ttk.Label(settings_row, text="节点 248 步数").pack(side="left")
    ttk.Combobox(
        settings_row,
        textvariable=sampling_steps_var,
        values=SAMPLING_STEP_OPTIONS,
        state="readonly",
        width=5,
    ).pack(side="left", padx=(8, 0))

    ttk.Label(
        outer,
        text="文案（必须与成品音频一致；长音频会通过 FunASR 自动切成 4～15 秒 H3 分段）",
    ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(10, 6))
    script_box = tk.Text(outer, height=14, wrap="word", font=("Microsoft YaHei UI", 10))
    script_box.grid(row=10, column=0, columnspan=3, sticky="nsew")
    outer.rowconfigure(10, weight=1)

    ttk.Label(
        outer,
        text=(
            "点击继续会先完成无费用分段和 Prompt 预览；控制台将显示实际段数，确认后才提交对应次数的付费调用。"
        ),
        foreground="#8a4b08",
    ).grid(row=11, column=0, columnspan=3, sticky="w", pady=(10, 4))

    def submit() -> None:
        try:
            video = Path(video_var.get().strip()).resolve()
            audio = Path(audio_var.get().strip()).resolve()
            output = Path(output_var.get().strip()).resolve()
            text = script_box.get("1.0", "end").strip()
            if not video.is_file():
                raise ValueError("请选择有效的参考视频")
            if not audio.is_file():
                raise ValueError("请选择有效的成品音频")
            if not text:
                raise ValueError("请填写文案")
            mode = visual_mode_var.get()
            images: tuple[Path, ...] = ()
            if mode == VISUAL_MODE_PICTURE:
                primary_image = Path(primary_image_var.get().strip()).resolve()
                images = (primary_image, *supporting_images)
            account_index = account_labels.index(account_var.get())
            selection = TestInput(
                reference_video=video,
                reference_audio=audio,
                script_text=text,
                output_root=output,
                account_id=accounts[account_index].account_id,
                aspect_ratio=aspect_var.get(),
                megapixels=float(megapixels_var.get()),
                seed=int(seed_var.get()),
                sampling_steps=int(sampling_steps_var.get()),
                visual_mode=mode,
                reference_images=images,
            )
            validate_reference_configuration(selection)
            result["value"] = selection
        except (ValueError, OSError) as exc:
            messagebox.showerror("输入不完整", str(exc), parent=root)
            return
        root.destroy()

    buttons = ttk.Frame(outer)
    buttons.grid(row=12, column=0, columnspan=3, sticky="e", pady=(10, 0))
    ttk.Button(buttons, text="取消", command=root.destroy).pack(side="left", padx=6)
    ttk.Button(buttons, text="生成预览并继续", command=submit).pack(side="left")
    root.mainloop()
    return result["value"]


def make_run_directory(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = output_root / f"h3-prompt-test-{stamp}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = output_root / f"h3-prompt-test-{stamp}-{suffix}"
    candidate.mkdir(parents=False)
    return candidate


def poll_task(
    client: RunningHubClient,
    task_id: str,
    checkpoint_path: Path,
    state: dict[str, Any],
    segment_state: dict[str, Any],
    *,
    poll_interval: float,
) -> dict[str, Any]:
    while True:
        result = client.query_task(task_id)
        status = record_remote_segment_result(
            client=client,
            checkpoint_path=checkpoint_path,
            state=state,
            segment_state=segment_state,
            result=result,
        )
        if status in FINAL_STATUSES:
            return result
        time.sleep(poll_interval)


def build_client(credentials: AccountCredentials) -> RunningHubClient:
    return RunningHubClient(
        api_key=credentials.api_key,
        base_url=credentials.base_url,
        ai_app_id=credentials.workflow_id,
        submission_type="raw-workflow",
        access_password=credentials.access_password,
    )


def record_remote_segment_result(
    *,
    client: RunningHubClient,
    checkpoint_path: Path,
    state: dict[str, Any],
    segment_state: dict[str, Any],
    result: dict[str, Any],
) -> str:
    task_id = str(segment_state.get("task_id") or "").strip()
    status = str(result.get("status") or "").upper()
    previous_status = str(segment_state.get("remote_status") or "").upper()
    if status != previous_status:
        print(f"RunningHub task={task_id} status={status or 'UNKNOWN'}", flush=True)
    segment_state["remote_status"] = status
    segment_state["last_polled_at"] = utc_now()
    if status in {"QUEUED", "RUNNING"}:
        write_json(checkpoint_path, state)
        return status
    if status not in FINAL_STATUSES:
        write_json(checkpoint_path, state)
        raise RuntimeError(f"RunningHub 返回未知任务状态：{status or '空'}")
    if status != "SUCCESS":
        segment_state["status"] = "remote_failed"
        segment_state["usage"] = result.get("usage")
        segment_state["failed_reason"] = result.get("failedReason")
        state["status"] = "segment_failed"
        write_json(checkpoint_path, state)
        return status

    selected = select_video_output(result)
    segment_dir = Path(str(segment_state["directory"]))
    destination = segment_dir / "result.mp4"
    client.download_result(str(selected["url"]), destination)
    segment_state.update(
        {
            "status": "success",
            "completed_at": utc_now(),
            "result_path": str(destination),
            "result_sha256": sha256_file(destination),
            "usage": result.get("usage"),
            "output": {
                "node_id": str(selected.get("nodeId") or ""),
                "output_type": str(selected.get("outputType") or ""),
            },
        }
    )
    write_json(checkpoint_path, state)
    return status


def finish_remote_segment(
    *,
    client: RunningHubClient,
    checkpoint_path: Path,
    state: dict[str, Any],
    segment_state: dict[str, Any],
    poll_interval: float,
) -> Path:
    task_id = str(segment_state.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("H3 分段检查点缺少远端 taskId")
    result = poll_task(
        client,
        task_id,
        checkpoint_path,
        state,
        segment_state,
        poll_interval=poll_interval,
    )
    if str(result.get("status") or "").upper() != "SUCCESS":
        raise RuntimeError("H3 远端任务未成功；脚本不会自动重新付费提交")
    return Path(str(segment_state["result_path"]))


def prepare_preview(selection: TestInput) -> tuple[Path, Path, dict[str, Any]]:
    validate_reference_configuration(selection)
    template_path = template_path_for(selection)
    template = template_path.read_text(encoding="utf-8")
    reference_images = tuple(path.resolve() for path in selection.reference_images)
    run_dir = make_run_directory(selection.output_root)
    checkpoint_path = run_dir / "checkpoint.json"
    state: dict[str, Any] = {
        "version": STATE_VERSION,
        "status": "preparing_preview",
        "created_at": utc_now(),
        "account_id": selection.account_id,
        "run_directory": str(run_dir),
        "template_path": str(template_path),
        "template_sha256": sha256_text(template),
        "input": {
            "reference_video": str(selection.reference_video),
            "reference_video_sha256": sha256_file(selection.reference_video),
            "visual_mode": selection.visual_mode,
            "reference_images": [
                {
                    "path": str(path),
                    "sha256": sha256_file(path),
                    "role": (
                        "primary_visual_spatial_anchor"
                        if index == 0
                        else "supporting_face_identity"
                    ),
                }
                for index, path in enumerate(reference_images)
            ],
            "reference_audio": str(selection.reference_audio),
            "reference_audio_sha256": sha256_file(selection.reference_audio),
            "script_text": selection.script_text,
            "aspect_ratio": selection.aspect_ratio,
            "megapixels": selection.megapixels,
            "seed": selection.seed,
            "sampling_steps": selection.sampling_steps,
        },
        "segments": [],
    }
    write_json(checkpoint_path, state)
    try:
        duration = inspect_audio_duration(selection.reference_audio)
        state["input"]["audio_duration_seconds"] = duration
        plans, alignment = plan_input_audio(
            selection.script_text,
            selection.reference_audio,
            duration,
        )
        state["alignment"] = alignment
        print("正在准备参考视频动作片段…", flush=True)
        motion_clips = split_h3_motion_reference(
            selection.reference_video,
            run_dir / "motion-references",
        )
        assigned_clips = assign_h3_motion_references(
            motion_clips,
            len(plans),
            seed_material="\0".join(
                (
                    str(state["input"]["reference_video_sha256"]),
                    str(state["input"]["reference_audio_sha256"]),
                    sha256_text(selection.script_text),
                )
            ),
        )
        builder = load_default_h3_graph_builder()
        combined_prompts: list[str] = []
        for position, (plan, motion_clip) in enumerate(
            zip(plans, assigned_clips, strict=True),
            start=1,
        ):
            segment_dir = run_dir / "segments" / f"segment-{position:03d}"
            segment_dir.mkdir(parents=True, exist_ok=True)
            if (
                len(plans) == 1
                and plan.start_seconds <= 0.001
                and abs(plan.end_seconds - duration) <= 0.01
            ):
                suffix = selection.reference_audio.suffix.lower() or ".audio"
                segment_audio = segment_dir / f"input-audio{suffix}"
                shutil.copy2(selection.reference_audio, segment_audio)
            else:
                segment_audio = segment_dir / "input-audio.mp3"
                cut_audio_segment(
                    selection.reference_audio,
                    segment_audio,
                    start_seconds=plan.start_seconds,
                    end_seconds=plan.end_seconds,
                )
            probed_duration = inspect_audio_duration(segment_audio)
            duration_plan = plan_h3_duration(probed_duration, 0.1)
            prompt = render_prompt_template(
                template,
                segment_text=plan.script_text,
                segment_index=position,
                segment_count=len(plans),
                reference_image_count=len(reference_images),
            )
            prompt_path = segment_dir / "prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            graph = builder.build(
                H3GraphBuildRequest(
                    prompt=prompt,
                    reference_video=f"preview/motion-{motion_clip.index + 1:03d}.mp4",
                    reference_audio=f"preview/audio-{position:03d}.mp3",
                    reference_images=tuple(
                        f"preview/picture-{index:03d}{path.suffix.lower() or '.png'}"
                        for index, path in enumerate(reference_images, start=1)
                    ),
                    audio_duration_seconds=probed_duration,
                    generation_tail_seconds=0.1,
                    aspect_ratio=selection.aspect_ratio,
                    megapixels=selection.megapixels,
                    multiple=32,
                    seed=selection.seed,
                )
            )
            _preview_workflow_json, preview_graph_sha256 = apply_test_sampling_steps(
                graph,
                selection.sampling_steps,
            )
            segment_state = {
                "index": position,
                "status": "preview_ready",
                "directory": str(segment_dir),
                "script_text": plan.script_text,
                "start_seconds": plan.start_seconds,
                "end_seconds": plan.end_seconds,
                "planned_audio_duration_seconds": plan.duration_seconds,
                "audio_duration_seconds": probed_duration,
                "requested_generation_duration_seconds": (
                    duration_plan.requested_generation_duration_seconds
                ),
                "quantized_frame_count": duration_plan.quantized_frame_count,
                "boundary_strength": plan.boundary_strength,
                "audio_path": str(segment_audio),
                "audio_sha256": sha256_file(segment_audio),
                "reference_video_path": str(motion_clip.path),
                "reference_video_sha256": motion_clip.sha256,
                "motion_reference_index": motion_clip.index,
                "prompt_path": str(prompt_path),
                "prompt_sha256": sha256_text(prompt),
                "sampling_steps": selection.sampling_steps,
                "preview_graph_sha256": preview_graph_sha256,
            }
            state["segments"].append(segment_state)
            combined_prompts.append(
                f"===== SEGMENT {position}/{len(plans)} =====\n{prompt}"
            )
            write_json(checkpoint_path, state)
        prompts_path = run_dir / ("prompt.txt" if len(plans) == 1 else "prompts.txt")
        prompts_path.write_text(
            (
                Path(str(state["segments"][0]["prompt_path"])).read_text(
                    encoding="utf-8"
                )
                if len(plans) == 1
                else "\n\n".join(combined_prompts)
            ),
            encoding="utf-8",
        )
        state.update(
            {
                "status": "preview_ready",
                "prepared_at": utc_now(),
                "segment_count": len(plans),
                "prompts_path": str(prompts_path),
            }
        )
        write_json(checkpoint_path, state)
        return run_dir, checkpoint_path, state
    except Exception as exc:
        state["status"] = "preview_failed"
        state["preview_error"] = str(exc)
        write_json(checkpoint_path, state)
        raise


def confirmation_phrase(call_count: int) -> str:
    suffix = "" if call_count == 1 else "S"
    return f"SUBMIT {call_count} H3 CALL{suffix}"


def merge_results(
    run_dir: Path,
    checkpoint_path: Path,
    state: dict[str, Any],
) -> Path:
    outputs = [Path(str(segment["result_path"])) for segment in state["segments"]]
    if not outputs or any(not path.is_file() for path in outputs):
        raise RuntimeError("H3 分段结果不完整，不能合并")
    destination = run_dir / "result.mp4"
    if len(outputs) == 1:
        shutil.copy2(outputs[0], destination)
    else:
        print(f"正在合并 {len(outputs)} 个 H3 分段视频…", flush=True)
        merge_segment_videos(outputs, destination)
    state.update(
        {
            "status": "success",
            "completed_at": utc_now(),
            "result_path": str(destination),
            "result_sha256": sha256_file(destination),
        }
    )
    write_json(checkpoint_path, state)
    write_json(
        run_dir / "result.json",
        {
            key: state.get(key)
            for key in (
                "version",
                "status",
                "created_at",
                "completed_at",
                "result_path",
                "result_sha256",
                "segment_count",
                "alignment",
                "input",
                "segments",
            )
        },
    )
    return destination


def upload_reference_images(
    client: RunningHubClient,
    checkpoint_path: Path,
    state: dict[str, Any],
) -> tuple[str, ...]:
    input_state = state.get("input")
    if not isinstance(input_state, dict):
        raise RuntimeError("检查点缺少 H3 输入快照")
    images = input_state.get("reference_images") or []
    if not isinstance(images, list):
        raise RuntimeError("检查点中的 H3 参考图片快照损坏")
    uploaded_images = state.setdefault("uploaded_reference_images", {})
    if not isinstance(uploaded_images, dict):
        raise RuntimeError("检查点中的 H3 图片上传记录损坏")
    remote_images: list[str] = []
    for position, image in enumerate(images, start=1):
        if not isinstance(image, dict):
            raise RuntimeError("检查点中的 H3 参考图片快照损坏")
        image_sha = str(image.get("sha256") or "").strip()
        image_path = Path(str(image.get("path") or ""))
        if not image_sha or not image_path.is_file():
            raise RuntimeError(f"第 {position} 张 H3 参考图片不存在或快照损坏")
        image_remote = str(uploaded_images.get(image_sha) or "")
        if not image_remote:
            print(f"上传第 {position} 张参考图片…", flush=True)
            image_remote = client.upload_file(image_path)
            uploaded_images[image_sha] = image_remote
            write_json(checkpoint_path, state)
        remote_images.append(image_remote)
    return tuple(remote_images)


def submit_remote_segment(
    *,
    client: RunningHubClient,
    credentials: AccountCredentials,
    checkpoint_path: Path,
    state: dict[str, Any],
    segment: dict[str, Any],
    uploaded_videos: dict[str, str],
    remote_reference_images: tuple[str, ...],
    builder: Any,
) -> None:
    segment["status"] = "uploading"
    write_json(checkpoint_path, state)
    video_sha = str(segment["reference_video_sha256"])
    video_remote = str(uploaded_videos.get(video_sha) or "")
    if not video_remote:
        print(f"上传第 {segment['index']} 段参考视频…", flush=True)
        video_remote = client.upload_file(Path(str(segment["reference_video_path"])))
        uploaded_videos[video_sha] = video_remote
        segment["remote_reference_video"] = video_remote
        write_json(checkpoint_path, state)
    audio_remote = str(segment.get("remote_audio") or "")
    if not audio_remote:
        print(f"上传第 {segment['index']} 段音频…", flush=True)
        audio_remote = client.upload_file(Path(str(segment["audio_path"])))
        segment["remote_audio"] = audio_remote
        write_json(checkpoint_path, state)
    prompt = Path(str(segment["prompt_path"])).read_text(encoding="utf-8")
    graph = builder.build(
        H3GraphBuildRequest(
            prompt=prompt,
            reference_video=video_remote,
            reference_audio=audio_remote,
            reference_images=remote_reference_images,
            audio_duration_seconds=float(segment["audio_duration_seconds"]),
            generation_tail_seconds=0.1,
            aspect_ratio=str(state["input"]["aspect_ratio"]),
            megapixels=float(state["input"]["megapixels"]),
            multiple=32,
            seed=int(state["input"]["seed"]),
        )
    )
    sampling_steps = validate_sampling_steps(
        int(state["input"].get("sampling_steps", 4))
    )
    workflow_json, dynamic_graph_sha256 = apply_test_sampling_steps(
        graph,
        sampling_steps,
    )
    segment.update(
        {
            "status": "submitting",
            "sampling_steps": sampling_steps,
            "dynamic_graph_sha256": dynamic_graph_sha256,
            "submission_started_at": utc_now(),
        }
    )
    write_json(checkpoint_path, state)
    try:
        task_id = client.submit_task(
            {
                "workflow": workflow_json,
                "addMetadata": True,
                "instanceType": credentials.instance_type,
                "usePersonalQueue": False,
            }
        )
    except RunningHubError as exc:
        segment["status"] = (
            "submission_ambiguous"
            if exc.submission_outcome_unknown
            else "submission_rejected"
        )
        segment["submission_error"] = str(exc)
        segment["submission_error_code"] = exc.error_code
        state["status"] = "segment_submission_failed"
        write_json(checkpoint_path, state)
        raise
    segment["task_id"] = task_id
    segment["status"] = "submitted"
    segment["submitted_at"] = utc_now()
    write_json(checkpoint_path, state)
    print(
        f"已提交第 {segment['index']}/{len(state['segments'])} 段：task={task_id}",
        flush=True,
    )


def execute_segments(
    *,
    client: RunningHubClient,
    credentials: AccountCredentials,
    run_dir: Path,
    checkpoint_path: Path,
    state: dict[str, Any],
    poll_interval: float,
) -> Path | None:
    segments = state.get("segments")
    if not isinstance(segments, list) or not segments:
        raise RuntimeError("检查点没有可执行的 H3 分段")

    in_flight: list[dict[str, Any]] = []
    for segment in segments:
        result_path = Path(str(segment.get("result_path") or ""))
        if segment.get("status") == "success" and result_path.is_file():
            continue
        if segment.get("task_id"):
            result = client.query_task(str(segment["task_id"]))
            status = record_remote_segment_result(
                client=client,
                checkpoint_path=checkpoint_path,
                state=state,
                segment_state=segment,
                result=result,
            )
            if status in {"QUEUED", "RUNNING"}:
                in_flight.append(segment)
            elif status != "SUCCESS":
                raise RuntimeError("H3 远端任务未成功；脚本不会自动重新付费提交")

    blocked = [
        segment
        for segment in segments
        if not segment.get("task_id")
        and segment.get("status") in {"submitting", "submission_ambiguous"}
    ]
    if blocked:
        raise RuntimeError(
            "至少一个分段在提交时中断且没有取得 taskId；为防止重复付费，脚本拒绝自动重提"
        )
    pending = [
        segment
        for segment in segments
        if segment.get("status") != "success" and not segment.get("task_id")
    ]
    local_concurrency_limit = max(int(credentials.max_concurrent_tasks), 1)
    if pending:
        account_status = client.get_account_status()
        current_remote_tasks = max(int(account_status.current_task_count), 0)
        external_remote_tasks = max(current_remote_tasks - len(in_flight), 0)
        local_concurrency_limit = max(
            int(credentials.max_concurrent_tasks) - external_remote_tasks,
            0,
        )
        if local_concurrency_limit == 0 and not in_flight:
            raise RuntimeError(
                f"所选 RunningHub 账号当前已有 {current_remote_tasks} 个远端任务，"
                f"已占满配置的 {credentials.max_concurrent_tasks} 个并发槽位；本次不提交"
            )
        phrase = confirmation_phrase(len(pending))
        print("\nH3 自动分段测试预览", flush=True)
        print(f"  执行账号：{credentials.account_id}｜{credentials.label}", flush=True)
        print(
            f"  账号并发：{credentials.max_concurrent_tasks}｜"
            f"其他任务占用：{external_remote_tasks}｜"
            f"本脚本并发窗口：{local_concurrency_limit}",
            flush=True,
        )
        print(f"  自动分段：{len(segments)} 段", flush=True)
        print(
            f"  节点 248 采样步数：{int(state['input'].get('sampling_steps', 4))}",
            flush=True,
        )
        visual_mode = str(
            state.get("input", {}).get("visual_mode") or VISUAL_MODE_VIDEO
        )
        reference_images = state.get("input", {}).get("reference_images") or []
        print(
            "  视觉模式："
            + (
                f"图片主锚点（{len(reference_images)} 张图片 + 动作视频）"
                if visual_mode == VISUAL_MODE_PICTURE
                else "仅视频"
            ),
            flush=True,
        )
        for segment in segments:
            print(
                f"    {segment['index']}/{len(segments)}："
                f"{float(segment['audio_duration_seconds']):.3f} 秒｜"
                f"{segment['script_text']}",
                flush=True,
            )
        print(f"  最终 Prompt 预览：{state['prompts_path']}", flush=True)
        print(f"  检查点：{checkpoint_path}", flush=True)
        print(f"  输出目录：{run_dir}", flush=True)
        if account_status.remain_coins is not None:
            print(f"  账号余额：{account_status.remain_coins} RH 币", flush=True)
        print(
            f"\n本次还会产生 {len(pending)} 次付费 H3 调用。请先核对分段文案和 Prompt。",
            flush=True,
        )
        answer = input(f"如确认，请输入 {phrase}：").strip()
        if answer != phrase:
            state["status"] = "cancelled_before_upload"
            state["cancelled_at"] = utc_now()
            write_json(checkpoint_path, state)
            print("已取消；未提交新的付费任务。", flush=True)
            return None
        state["status"] = "running_segments"
        state["confirmed_at"] = utc_now()
        state["confirmed_call_count"] = len(pending)
        state["execution_concurrency"] = {
            "account_limit": credentials.max_concurrent_tasks,
            "external_remote_tasks_at_start": external_remote_tasks,
            "local_window": local_concurrency_limit,
        }
        write_json(checkpoint_path, state)

    pending_queue = list(pending)
    uploaded_videos = state.setdefault("uploaded_motion_references", {})
    if not isinstance(uploaded_videos, dict):
        raise RuntimeError("检查点中的 H3 动作视频上传记录损坏")
    remote_reference_images: tuple[str, ...] = ()
    builder: Any = None
    if pending_queue:
        remote_reference_images = upload_reference_images(
            client,
            checkpoint_path,
            state,
        )
        builder = load_default_h3_graph_builder()

    failures: list[dict[str, Any]] = []
    while pending_queue or in_flight:
        while (
            pending_queue
            and not failures
            and len(in_flight) < local_concurrency_limit
        ):
            segment = pending_queue.pop(0)
            submit_remote_segment(
                client=client,
                credentials=credentials,
                checkpoint_path=checkpoint_path,
                state=state,
                segment=segment,
                uploaded_videos=uploaded_videos,
                remote_reference_images=remote_reference_images,
                builder=builder,
            )
            in_flight.append(segment)

        if not in_flight:
            if pending_queue and not failures:
                raise RuntimeError(
                    "所选 RunningHub 账号没有可用并发槽位；剩余分段尚未提交"
                )
            break

        for segment in list(in_flight):
            result = client.query_task(str(segment["task_id"]))
            status = record_remote_segment_result(
                client=client,
                checkpoint_path=checkpoint_path,
                state=state,
                segment_state=segment,
                result=result,
            )
            if status in FINAL_STATUSES:
                in_flight.remove(segment)
                if status != "SUCCESS":
                    failures.append(segment)

        if in_flight:
            time.sleep(poll_interval)

    if failures:
        state["status"] = "segment_failed"
        write_json(checkpoint_path, state)
        failed_indexes = ", ".join(str(segment["index"]) for segment in failures)
        raise RuntimeError(
            f"H3 第 {failed_indexes} 段远端任务未成功；脚本不会自动重新付费提交"
        )
    return merge_results(run_dir, checkpoint_path, state)


def run_new_test(selection: TestInput, *, poll_interval: float) -> Path | None:
    run_dir, checkpoint_path, state = prepare_preview(selection)
    credentials = load_credentials(selection.account_id)
    client = build_client(credentials)
    return execute_segments(
        client=client,
        credentials=credentials,
        run_dir=run_dir,
        checkpoint_path=checkpoint_path,
        state=state,
        poll_interval=poll_interval,
    )


def resume_test(checkpoint_path: Path, *, poll_interval: float) -> Path:
    checkpoint_path = checkpoint_path.resolve()
    state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if state.get("version") != STATE_VERSION:
        raise RuntimeError("检查点版本不支持")
    result_path = Path(str(state.get("result_path") or ""))
    if state.get("status") == "success" and result_path.is_file():
        return result_path
    credentials = load_credentials(int(state["account_id"]))
    result = execute_segments(
        client=build_client(credentials),
        credentials=credentials,
        run_dir=checkpoint_path.parent,
        checkpoint_path=checkpoint_path,
        state=state,
        poll_interval=poll_interval,
    )
    if result is None:
        raise RuntimeError("已取消继续提交剩余 H3 分段")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="H3 动态 Prompt 自动分段测试；默认打开本地选择窗口"
    )
    parser.add_argument("--resume", type=Path, help="从已有 checkpoint.json 恢复查询/下载")
    parser.add_argument("--reference-video", type=Path)
    parser.add_argument(
        "--visual-mode",
        choices=sorted(VISUAL_MODES),
        default=VISUAL_MODE_VIDEO,
        help="video_primary=仅视频；picture_primary=图片主锚点",
    )
    parser.add_argument(
        "--reference-image",
        type=Path,
        action="append",
        default=[],
        help="图片主锚点模式可重复传入，第一张为 Picture 1，最多 4 张",
    )
    parser.add_argument("--reference-audio", type=Path)
    parser.add_argument("--script-file", type=Path, help="UTF-8 文案文件")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--account-id", type=int)
    parser.add_argument(
        "--aspect-ratio",
        default="9:16 (Portrait Widescreen)",
    )
    parser.add_argument("--megapixels", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--sampling-steps",
        type=int,
        choices=SAMPLING_STEP_OPTIONS,
        default=4,
        help="本次测试覆盖节点 248 的采样步数",
    )
    parser.add_argument("--poll-interval", type=float, default=8.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.poll_interval < 1:
        raise ValueError("轮询间隔不能小于 1 秒")
    if args.resume:
        result = resume_test(args.resume, poll_interval=args.poll_interval)
        print(f"完成：{result}", flush=True)
        return 0

    accounts = list_accounts()
    explicit = all(
        value is not None
        for value in (
            args.reference_video,
            args.reference_audio,
            args.script_file,
            args.output_dir,
            args.account_id,
        )
    )
    if explicit:
        selection = TestInput(
            reference_video=args.reference_video.resolve(),
            reference_audio=args.reference_audio.resolve(),
            script_text=args.script_file.read_text(encoding="utf-8").strip(),
            output_root=args.output_dir.resolve(),
            account_id=args.account_id,
            aspect_ratio=args.aspect_ratio,
            megapixels=args.megapixels,
            seed=args.seed,
            sampling_steps=args.sampling_steps,
            visual_mode=args.visual_mode,
            reference_images=tuple(path.resolve() for path in args.reference_image),
        )
    else:
        selection = collect_inputs_with_window(accounts)
        if selection is None:
            print("已取消。", flush=True)
            return 0
    result = run_new_test(selection, poll_interval=args.poll_interval)
    if result is not None:
        print(f"\n生成完成：{result}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断。若已经获得 taskId，可使用 --resume checkpoint.json 继续查询。")
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001 - operator tool must surface third-party failures
        print(f"\n失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
