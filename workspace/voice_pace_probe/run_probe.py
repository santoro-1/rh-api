from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parent
DEFAULT_TEXT_FILE = ROOT / "calibration_text.txt"
DEFAULT_BASE_URL = "https://api.minimaxi.com"
SUPPORTED_AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav"}
VOICE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{6,254}[A-Za-z0-9]$")
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
DIGIT_RE = re.compile(r"\d")
STRONG_PUNCTUATION = frozenset("。！？!?；;")


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class TextStats:
    han_characters: int
    latin_words: int
    digits: int
    sentence_gaps: int
    paragraph_gaps: int


@dataclass(frozen=True)
class PaceAnalysis:
    target_cpm: float
    tolerance_percent: float
    effective_characters: int
    audio_duration_seconds: float
    target_duration_seconds: float
    natural_cpm: float
    result: str
    extra_pause_budget_seconds: float
    suggested_extra_sentence_pause_seconds: float | None
    suggested_extra_paragraph_pause_seconds: float | None
    estimated_native_speed_if_needed: float | None
    explanation: str


def hidden_creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def clean_path(raw: str) -> Path:
    return Path(raw.strip().strip('"').strip("'")).expanduser().resolve()


def read_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise ProbeError(f"无法读取测试文案：{path}（{exc}）") from exc
    if not text:
        raise ProbeError("测试文案不能为空")
    if len(text) > 1000:
        raise ProbeError("MiniMax 克隆试听文案不能超过 1000 个字符")
    return text


def _has_spoken_text_after(text: str, index: int) -> bool:
    return bool(HAN_RE.search(text[index + 1 :]) or LATIN_WORD_RE.search(text[index + 1 :]))


def inspect_text(text: str) -> TextStats:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    sentence_gaps = sum(
        1
        for paragraph in paragraphs
        for index, character in enumerate(paragraph)
        if character in STRONG_PUNCTUATION
        and _has_spoken_text_after(paragraph, index)
    )
    paragraph_gaps = max(len(paragraphs) - 1, 0)
    return TextStats(
        han_characters=len(HAN_RE.findall(text)),
        latin_words=len(LATIN_WORD_RE.findall(text)),
        digits=len(DIGIT_RE.findall(text)),
        sentence_gaps=sentence_gaps,
        paragraph_gaps=paragraph_gaps,
    )


def probe_duration(audio_path: Path) -> float:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=hidden_creation_flags(),
        )
    except FileNotFoundError as exc:
        raise ProbeError("没有找到 ffprobe，请先把 FFmpeg 加入 PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise ProbeError(f"ffprobe 无法读取音频时长：{detail or audio_path}") from exc
    try:
        duration = float(completed.stdout.strip())
    except ValueError as exc:
        raise ProbeError("ffprobe 返回了无效的音频时长") from exc
    if duration <= 0:
        raise ProbeError("音频时长必须大于 0 秒")
    return duration


def analyze_pace(
    stats: TextStats,
    duration_seconds: float,
    *,
    target_cpm: float,
    tolerance_percent: float = 5.0,
) -> PaceAnalysis:
    count = stats.han_characters
    if count <= 0:
        raise ProbeError("测试文案中没有可统计的中文汉字")
    if target_cpm <= 0:
        raise ProbeError("目标字速必须大于 0")
    target_duration = count / target_cpm * 60.0
    natural_cpm = count / duration_seconds * 60.0
    lower = target_cpm * (1.0 - tolerance_percent / 100.0)
    upper = target_cpm * (1.0 + tolerance_percent / 100.0)

    if lower <= natural_cpm <= upper:
        return PaceAnalysis(
            target_cpm=target_cpm,
            tolerance_percent=tolerance_percent,
            effective_characters=count,
            audio_duration_seconds=duration_seconds,
            target_duration_seconds=target_duration,
            natural_cpm=natural_cpm,
            result="natural_speed_is_suitable",
            extra_pause_budget_seconds=0.0,
            suggested_extra_sentence_pause_seconds=None,
            suggested_extra_paragraph_pause_seconds=None,
            estimated_native_speed_if_needed=None,
            explanation="自然字速已经落在目标上下 5% 范围内，建议保持 speed=1.0，只按听感微调停顿。",
        )

    if natural_cpm > upper:
        pause_budget = max(target_duration - duration_seconds, 0.0)
        weighted_gaps = stats.sentence_gaps + stats.paragraph_gaps * 1.8
        if weighted_gaps > 0:
            sentence_pause = pause_budget / weighted_gaps
            paragraph_pause = sentence_pause * 1.8
            explanation = (
                "自然语速快于目标，可以保持 speed=1.0，仅通过增加停顿降低整体字速。"
                "报告中的数值是需要在现有自然停顿之外增加的平均停顿。"
            )
        else:
            sentence_pause = None
            paragraph_pause = None
            explanation = "自然语速快于目标，但测试文案没有可分配的句间或段间边界。"
        return PaceAnalysis(
            target_cpm=target_cpm,
            tolerance_percent=tolerance_percent,
            effective_characters=count,
            audio_duration_seconds=duration_seconds,
            target_duration_seconds=target_duration,
            natural_cpm=natural_cpm,
            result="pause_only_is_enough",
            extra_pause_budget_seconds=pause_budget,
            suggested_extra_sentence_pause_seconds=sentence_pause,
            suggested_extra_paragraph_pause_seconds=paragraph_pause,
            estimated_native_speed_if_needed=None,
            explanation=explanation,
        )

    estimated_speed = max(0.5, min(2.0, target_cpm / natural_cpm))
    return PaceAnalysis(
        target_cpm=target_cpm,
        tolerance_percent=tolerance_percent,
        effective_characters=count,
        audio_duration_seconds=duration_seconds,
        target_duration_seconds=target_duration,
        natural_cpm=natural_cpm,
        result="pause_only_cannot_reach_target",
        extra_pause_budget_seconds=0.0,
        suggested_extra_sentence_pause_seconds=None,
        suggested_extra_paragraph_pause_seconds=None,
        estimated_native_speed_if_needed=estimated_speed,
        explanation=(
            "自然语速慢于目标，减少停顿也无法可靠达到目标。推荐语速只是以后正式使用时的粗略参考，"
            "本工具不会调用正式 T2A 验证，也建议优先保留自然听感。"
        ),
    )


class MiniMaxClonePreviewClient:
    """Minimal client that only requests the clone API's demo audio."""

    def __init__(self, api_key: str, *, base_url: str, timeout: float = 120.0) -> None:
        if not api_key.strip():
            raise ProbeError("MiniMax API Key 不能为空")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key.strip()}"})

    @staticmethod
    def _check_payload(payload: dict[str, Any], operation: str) -> dict[str, Any]:
        base_resp = payload.get("base_resp") or {}
        status_code = int(base_resp.get("status_code", 0) or 0)
        if status_code:
            message = str(base_resp.get("status_msg") or "未知错误")
            raise ProbeError(f"{operation}失败：[{status_code}] {message}")
        return payload

    def upload_clone_audio(self, audio_path: Path) -> int:
        try:
            with audio_path.open("rb") as source:
                response = self.session.post(
                    f"{self.base_url}/v1/files/upload",
                    data={"purpose": "voice_clone"},
                    files={"file": (audio_path.name, source, "application/octet-stream")},
                    timeout=self.timeout,
                )
            response.raise_for_status()
            payload = self._check_payload(response.json(), "上传参考音频")
            return int(payload["file"]["file_id"])
        except (OSError, requests.RequestException) as exc:
            raise ProbeError(f"上传参考音频失败：{exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ProbeError("上传参考音频返回了无效结果") from exc

    def clone_with_preview(
        self,
        *,
        file_id: int,
        voice_id: str,
        text: str,
        model: str,
    ) -> tuple[bytes, str]:
        body = {
            "file_id": file_id,
            "voice_id": voice_id,
            "text": text,
            "model": model,
            "language_boost": "Chinese",
            "need_noise_reduction": True,
            "need_volume_normalization": True,
            "aigc_watermark": False,
        }
        try:
            response = self.session.post(
                f"{self.base_url}/v1/voice_clone",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = self._check_payload(response.json(), "克隆音色并生成试听")
            demo_url = str(payload.get("demo_audio") or "").strip()
            if not demo_url:
                raise ProbeError("克隆成功，但 MiniMax 没有返回 demo_audio")
            demo = self.session.get(demo_url, timeout=self.timeout)
            demo.raise_for_status()
            return demo.content, demo.headers.get("content-type") or "audio/mpeg"
        except requests.RequestException as exc:
            raise ProbeError(f"克隆音色或下载试听失败：{exc}") from exc
        except ValueError as exc:
            raise ProbeError("克隆音色返回了无效 JSON") from exc

def validate_source_audio(audio_path: Path) -> float:
    if not audio_path.is_file():
        raise ProbeError(f"参考音频不存在：{audio_path}")
    if audio_path.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
        raise ProbeError("参考音频只支持 mp3、m4a 或 wav")
    if audio_path.stat().st_size > 20 * 1024 * 1024:
        raise ProbeError("参考音频不能超过 20 MB")
    duration = probe_duration(audio_path)
    if not 10 <= duration <= 300:
        raise ProbeError("参考音频时长必须在 10 秒到 5 分钟之间")
    return duration


def default_voice_id() -> str:
    return datetime.now().strftime("PaceProbe%Y%m%d%H%M%S")


def validate_voice_id(voice_id: str) -> str:
    clean = voice_id.strip()
    if not VOICE_ID_RE.fullmatch(clean):
        raise ProbeError(
            "voice_id 必须为 8–256 位，以英文字母开头，只能包含字母、数字、-、_，且不能以 - 或 _ 结尾"
        )
    return clean


def default_output_directory(voice_id: str) -> Path:
    return ROOT / "outputs" / f"{datetime.now():%Y%m%d-%H%M%S}-{voice_id}"


def write_report(
    output_dir: Path,
    *,
    mode: str,
    voice_id: str | None,
    model: str | None,
    text_stats: TextStats,
    analysis: PaceAnalysis,
    source_audio_duration: float | None = None,
) -> Path:
    payload = {
        "mode": mode,
        "billing_boundary": (
            "clone 模式只调用 /v1/files/upload 和 /v1/voice_clone；未调用正式 T2A 接口"
            if mode == "clone_preview"
            else "离线分析，未调用任何 MiniMax 接口"
        ),
        "voice_id": voice_id,
        "model": model,
        "source_audio_duration_seconds": source_audio_duration,
        "text_stats": asdict(text_stats),
        "analysis": asdict(analysis),
        "created_at": datetime.now().astimezone().isoformat(),
    }
    target = output_dir / "pace-report.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def print_report(analysis: PaceAnalysis, report_path: Path) -> None:
    print("\n========== 测试结果 ==========")
    print(f"有效中文汉字：{analysis.effective_characters} 字")
    print(f"试听音频时长：{analysis.audio_duration_seconds:.2f} 秒")
    print(f"自然整体字速：{analysis.natural_cpm:.1f} 字/分钟")
    print(f"目标整体字速：{analysis.target_cpm:.1f} 字/分钟")
    print(f"目标音频时长：{analysis.target_duration_seconds:.2f} 秒")
    if analysis.extra_pause_budget_seconds > 0:
        print(f"需要额外增加停顿：共 {analysis.extra_pause_budget_seconds:.2f} 秒")
    if analysis.suggested_extra_sentence_pause_seconds is not None:
        print(
            "建议额外停顿："
            f"句间约 {analysis.suggested_extra_sentence_pause_seconds:.2f} 秒，"
            f"段间约 {analysis.suggested_extra_paragraph_pause_seconds:.2f} 秒"
        )
    if analysis.estimated_native_speed_if_needed is not None:
        print(f"以后正式使用时的粗略 speed 参考：{analysis.estimated_native_speed_if_needed:.3f}")
    print(f"判断：{analysis.explanation}")
    print(f"完整报告：{report_path}")


def run_clone(args: argparse.Namespace) -> int:
    audio_path = clean_path(args.audio)
    source_duration = validate_source_audio(audio_path)
    text_path = clean_path(args.text_file)
    text = read_text(text_path)
    stats = inspect_text(text)
    if stats.latin_words or stats.digits:
        print("提醒：字速只统计中文汉字；当前文案含英文或数字，建议改用默认纯中文文案。")
    voice_id = validate_voice_id(args.voice_id or default_voice_id())
    output_dir = clean_path(args.output) if args.output else default_output_directory(voice_id)
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "calibration-text.txt").write_text(text, encoding="utf-8")

    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        api_key = getpass.getpass("请输入 MiniMax API Key（输入不会显示，也不会保存）：").strip()
    client = MiniMaxClonePreviewClient(api_key, base_url=args.base_url)
    print("正在上传参考音频……")
    remote_file_id = client.upload_clone_audio(audio_path)
    print(f"正在克隆音色并生成试听…… voice_id={voice_id}")
    preview_bytes, _content_type = client.clone_with_preview(
        file_id=remote_file_id,
        voice_id=voice_id,
        text=text,
        model=args.model,
    )
    preview_path = output_dir / "clone-preview.mp3"
    preview_path.write_bytes(preview_bytes)
    preview_duration = probe_duration(preview_path)
    analysis = analyze_pace(stats, preview_duration, target_cpm=args.target_cpm)
    report_path = write_report(
        output_dir,
        mode="clone_preview",
        voice_id=voice_id,
        model=args.model,
        text_stats=stats,
        analysis=analysis,
        source_audio_duration=source_duration,
    )
    print(f"试听文件：{preview_path}")
    print_report(analysis, report_path)
    return 0


def run_analyze(args: argparse.Namespace) -> int:
    audio_path = clean_path(args.audio)
    if not audio_path.is_file():
        raise ProbeError(f"试听音频不存在：{audio_path}")
    text = read_text(clean_path(args.text_file))
    stats = inspect_text(text)
    duration = probe_duration(audio_path)
    analysis = analyze_pace(stats, duration, target_cpm=args.target_cpm)
    output_dir = clean_path(args.output) if args.output else audio_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = write_report(
        output_dir,
        mode="offline_analysis",
        voice_id=None,
        model=None,
        text_stats=stats,
        analysis=analysis,
    )
    print_report(analysis, report_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MiniMax 克隆音色自然字速测试工具")
    subparsers = parser.add_subparsers(dest="command")

    clone = subparsers.add_parser("clone", help="克隆一次并分析返回的 demo_audio")
    clone.add_argument("--audio", required=True, help="10 秒到 5 分钟的参考音频")
    clone.add_argument("--voice-id", help="唯一的 MiniMax voice_id；不填则自动生成")
    clone.add_argument("--text-file", default=str(DEFAULT_TEXT_FILE), help="测试文案 UTF-8 文件")
    clone.add_argument("--model", default="speech-2.8-hd")
    clone.add_argument("--target-cpm", type=float, default=220.0)
    clone.add_argument("--base-url", default=DEFAULT_BASE_URL)
    clone.add_argument("--output", help="输出目录；不填则自动建立 outputs 子目录")
    clone.set_defaults(handler=run_clone)

    analyze = subparsers.add_parser("analyze", help="离线分析已有试听音频")
    analyze.add_argument("--audio", required=True, help="已有试听音频")
    analyze.add_argument("--text-file", default=str(DEFAULT_TEXT_FILE), help="试听时使用的文案")
    analyze.add_argument("--target-cpm", type=float, default=220.0)
    analyze.add_argument("--output", help="报告输出目录；默认写到试听音频旁边")
    analyze.set_defaults(handler=run_analyze)
    return parser


def interactive_arguments() -> list[str]:
    print("MiniMax 克隆音色自然语速测试")
    print("本工具只获取克隆接口的 demo_audio，不调用正式 T2A。\n")
    audio = input("请输入参考音频完整路径：").strip()
    if not audio:
        raise ProbeError("没有输入参考音频路径")
    voice_id = input("voice_id（直接回车自动生成）：").strip()
    arguments = ["clone", "--audio", audio]
    if voice_id:
        arguments.extend(["--voice-id", voice_id])
    return arguments


def main(argv: list[str] | None = None) -> int:
    incoming = list(sys.argv[1:] if argv is None else argv)
    if not incoming:
        incoming = interactive_arguments()
    parser = build_parser()
    args = parser.parse_args(incoming)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return int(args.handler(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProbeError as exc:
        print(f"\n测试失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
