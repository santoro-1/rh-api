from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
SOURCE_01_05_10 = DATA_DIR / "H3_v9_第01-05及10条汇总_20260824_003750"
SOURCE_06_10 = DATA_DIR / "H3_v8_第06-10条汇总_20260823_224544"
USER_PROMPT_SOURCE = Path(
    r"C:\Users\san\.codex\attachments\9569141e-590e-4251-a67d-fa83cfef9878\pasted-text.txt"
)


def _probe_duration(path: Path) -> float:
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


def _concat(inputs: list[Path], output: Path) -> None:
    concat_file = output.with_suffix(".concat.txt")
    concat_file.write_text(
        "".join(
            f"file '{str(path.resolve()).replace(chr(39), chr(39) * 2)}'\n"
            for path in inputs
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "19",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )


def _source_for_row(row: int) -> Path:
    if row <= 5 or row == 10:
        return SOURCE_01_05_10 / f"{row:02d}"
    return SOURCE_06_10 / f"{row:02d}"


def _has_full_transcript(prompt_path: Path) -> bool:
    prompt = prompt_path.read_text(encoding="utf-8")
    return "<d>[Chinese]" in prompt and "</d>" in prompt


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = DATA_DIR / f"H3_第01-10条最终汇总_{stamp}"
    output_root.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, object] = {
        "schema": "h3.rows01-10.final-collection.v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "selection_rule": {
            "rows_01_05": str(SOURCE_01_05_10.resolve()),
            "rows_06_09": str(SOURCE_06_10.resolve()),
            "row_10": str(SOURCE_01_05_10.resolve()),
        },
        "prompt_provenance": {
            "applies_to_all_rows": True,
            "profile_id": "dual_reference_talking_v2",
            "template_version": "h3.prompt.ref2va.v8",
            "classification": "程序编译版",
            "uses_user_prompt_verbatim": False,
            "relationship_to_user_prompt": (
                "吸收了用户原稿中的人物气质、嘴型、动作等目标，并经过官方 H3 skill 与后续规则迭代；"
                "不是用户原稿逐字直传。"
            ),
            "full_chinese_transcript_present": True,
            "subtitle_risk_note": (
                "所有实际分段 Prompt 均包含完整 <d>[Chinese]...台词...</d>；"
                "这与此前提出的字幕触发假设一致，但不能仅凭本批结果证明它是唯一原因。"
            ),
        },
        "rows": [],
    }

    videos: list[Path] = []
    timeline_seconds = 0.0
    timeline_rows: list[dict[str, object]] = []

    for row in range(1, 11):
        source_dir = _source_for_row(row)
        if not source_dir.is_dir():
            raise FileNotFoundError(source_dir)
        row_dir = output_root / f"{row:02d}"
        shutil.copytree(source_dir, row_dir)

        row_manifest_path = row_dir / "输入与参数.json"
        row_manifest = json.loads(row_manifest_path.read_text(encoding="utf-8"))
        video = row_dir / f"{row:02d}_最终成片.mp4"
        if not video.is_file():
            raise FileNotFoundError(video)
        duration = _probe_duration(video)
        prompt_files = sorted((row_dir / "输入" / "分段提示词").glob("段*.txt"))
        transcript_flags = [_has_full_transcript(path) for path in prompt_files]

        provenance = {
            "classification": "程序编译版",
            "template_version": row_manifest["generation"]["prompt_template_version"],
            "uses_user_prompt_verbatim": False,
            "full_chinese_transcript_present_in_every_segment": bool(prompt_files)
            and all(transcript_flags),
        }
        row_manifest["prompt_provenance"] = provenance
        row_manifest["final_video"] = str(video.resolve())
        row_manifest["source_collection"] = str(source_dir.parent.resolve())
        row_manifest_path.write_text(
            json.dumps(row_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        timeline_rows.append(
            {
                "row": row,
                "start_seconds": round(timeline_seconds, 3),
                "end_seconds": round(timeline_seconds + duration, 3),
                "duration_seconds": round(duration, 3),
            }
        )
        timeline_seconds += duration
        videos.append(video)
        manifest["rows"].append(row_manifest)

    combined = output_root / "第01-10条_顺序合并预览.mp4"
    _concat(videos, combined)
    manifest["combined_video"] = str(combined.resolve())
    manifest["combined_duration_seconds"] = _probe_duration(combined)
    manifest["timeline"] = timeline_rows

    if USER_PROMPT_SOURCE.is_file():
        shutil.copy2(USER_PROMPT_SOURCE, output_root / "用户最初提供的完整提示词_仅供对照_本批未原样使用.txt")

    manifest_path = output_root / "第01-10条_输入结果与提示词来源总表.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    readme = [
        "# H3 第 01～10 条最终汇总",
        "",
        "## 最重要的提示词标注",
        "",
        "- 第 01～10 条全部是 `h3.prompt.ref2va.v8` / `dual_reference_talking_v2` 程序编译版。",
        "- 本合集没有任何一条把用户最初提供的长 Prompt 原文逐字直传；因此‘用户原稿直传’标记均为‘否’。",
        "- 原稿已另存为 `用户最初提供的完整提示词_仅供对照_本批未原样使用.txt`，避免继续混淆。",
        "- 每个实际分段 Prompt 都仍包含完整 `<d>[Chinese]...台词...</d>`，这是当前字幕风险的重要嫌疑变量。",
        "- 历史上最接近‘按用户原稿内容改写’的是 PromptProfile v4 的两批结果；它们同样不是原稿逐字直传，见下方历史标注。",
        "",
        "## 文件",
        "",
        "- `第01-10条_顺序合并预览.mp4`：按 01 → 10 合并。",
        "- 每条目录：最终成片、输入图片、参考视频、完整音频、raw cues 和每段完整 Prompt。",
        "- `第01-10条_输入结果与提示词来源总表.json`：机器可读的完整输入和来源。",
        "",
        "## 1～10 输入与播放位置",
        "",
        "| 条目 | 合集起点 | 图片输入 | 图片数 | 参考视频 | 衔接 | Prompt | 原稿直传 | 完整台词 `<d>` |",
        "|---:|---:|---|---:|---|---|---|---|---|",
    ]
    for row_manifest, timing in zip(manifest["rows"], timeline_rows, strict=True):
        start = float(timing["start_seconds"])
        minutes = int(start // 60)
        seconds = start - minutes * 60
        readme.append(
            "| {row:02d} | {minutes:02d}:{seconds:06.3f} | {mode} | {pictures} | {video} | "
            "{continuity} | v8 程序版 | 否 | 是 |".format(
                row=row_manifest["row"],
                minutes=minutes,
                seconds=seconds,
                mode=row_manifest["input_mode"],
                pictures=len(row_manifest["pictures"]),
                video=row_manifest["reference_video"]["source_original_name"],
                continuity=row_manifest["generation"]["continuity_mode"],
            )
        )

    readme.extend(
        [
            "",
            "## 一致性比较注意",
            "",
            "这 10 条并不是只改变单一变量的严格 A/B：01～02 无图，03～05 和 10 为一张透明人脸图，"
            "06～08 为参考视频抽帧图，09 为三张大头照；参考视频、音频、台词和衔接模式也不同。"
            "因此可以用于主观筛选，但不能把一致性差异只归因于图片数量或 Prompt。",
            "",
            "## 历史上与用户原稿最接近的测试",
            "",
            "- 标注：`用户原稿方向改写版（v4，非原文直传）`。",
            "- 批次 `1017dff6-682c-416c-ad58-8da8a1d4b58e`，任务 `2091229908503322626`、`2091229921669251073`。",
            "- 批次 `aa7ad24f-20de-4bd6-9a37-1e22a5352b69`，任务 `2091232138069508099`、`2091232147548626946`。",
            "- 对应旧汇总目录编号 03、04；v4 在原稿基础上加入多图职责、分段台词和程序强制约束，所以只能标‘方向改写’，不能标‘原稿直传’。",
            "",
        ]
    )
    (output_root / "先看这个_汇总说明.md").write_text("\n".join(readme), encoding="utf-8")

    print(
        json.dumps(
            {
                "output_dir": str(output_root.resolve()),
                "combined_video": str(combined.resolve()),
                "manifest": str(manifest_path.resolve()),
                "combined_duration_seconds": manifest["combined_duration_seconds"],
                "rows": len(manifest["rows"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
