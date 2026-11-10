# 原始代码来源：
# D:\工作内容\轻盈健\数字人\runninghub_mvp\app\services\ltx_workbench.py
# 用途：LTX 工作台固定提示词生成，以及禁止外部自定义提示词的原始校验逻辑。

from __future__ import annotations

from typing import Any

from app.config import Settings


LTX_WORKFLOW = "ltx_lip_sync"
FIXED_PROMPT_PREFIX = "一个人用中文说"


class LtxWorkbenchError(ValueError):
    """A workbench row cannot enter the unified LTX preparation flow."""


def compile_ltx_prompt(script_text: str) -> str:
    clean_script = script_text.strip()
    if not clean_script:
        raise LtxWorkbenchError("表格原稿不能为空")
    return f"{FIXED_PROMPT_PREFIX}：“{clean_script}”"


def _clean_rows(raw_rows: Any, settings: Settings) -> list[dict[str, Any]]:
    if not isinstance(raw_rows, list) or not raw_rows:
        raise LtxWorkbenchError("请至少提交一行 LTX 任务")
    if len(raw_rows) > settings.max_batch_items:
        raise LtxWorkbenchError(
            f"单批任务数量不能超过 {settings.max_batch_items}"
        )
    rows: list[dict[str, Any]] = []
    seen_row_ids: set[str] = set()
    for position, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            raise LtxWorkbenchError(f"第 {position} 行格式错误")
        if any(
            str(raw.get(field) or "").strip()
            for field in ("prompt", "prompt_prefix", "promptPrefix")
        ):
            raise LtxWorkbenchError("LTX 提示词由表格原稿固定生成，不能自定义")
        row_id = str(raw.get("row_id") or raw.get("rowId") or "").strip()
        script_text = str(
            raw.get("script_text") or raw.get("scriptText") or ""
        ).strip()
        video_asset_id = str(
            raw.get("video_asset_id") or raw.get("videoAssetId") or ""
        ).strip()
        audio_batch_id = str(
            raw.get("audio_batch_id") or raw.get("audioBatchId") or ""
        ).strip()
        audio_item_id = str(
            raw.get("audio_item_id") or raw.get("audioItemId") or ""
        ).strip()
        try:
            audio_generation_version = int(
                raw.get("audio_generation_version")
                or raw.get("audioGenerationVersion")
                or 0
            )
        except (TypeError, ValueError) as exc:
            raise LtxWorkbenchError(f"第 {position} 行声音版本格式错误") from exc
        if not row_id or len(row_id) > 100:
            raise LtxWorkbenchError(f"第 {position} 行任务 ID 长度必须为 1–100")
        if row_id in seen_row_ids:
            raise LtxWorkbenchError(f"任务 ID 重复：{row_id}")
        if not script_text or len(script_text) > 100_000:
            raise LtxWorkbenchError(
                f"第 {position} 行表格原稿长度必须为 1–100000"
            )
        if not video_asset_id:
            raise LtxWorkbenchError(f"第 {position} 行必须绑定源视频")
        if not audio_batch_id or not audio_item_id or audio_generation_version < 1:
            raise LtxWorkbenchError(f"第 {position} 行必须绑定已生成的 MiniMax 音频")
        compile_ltx_prompt(script_text)
        seen_row_ids.add(row_id)
        rows.append(
            {
                "row_id": row_id,
                "script_text": script_text,
                "video_asset_id": video_asset_id,
                "audio_batch_id": audio_batch_id,
                "audio_item_id": audio_item_id,
                "audio_generation_version": audio_generation_version,
            }
        )
    return rows
