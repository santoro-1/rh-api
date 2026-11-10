from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.content_replace import export_text_style_preset  # noqa: E402


# 改这里：有“参考文本样式”的草稿目录。
# 你可以在剪映里手工建一个文本，设置好字体、颜色、下划线、字号、位置等，
# 然后用这个脚本把该文本的样式导出成 JSON。
SOURCE_DRAFT_DIR = r"D:\剪映草稿\JianyingPro Drafts\文本样式来源"


# 改这里：样式 JSON 保存位置。文件已存在时会停止，避免覆盖。
OUTPUT_STYLE_JSON = r"D:\工作内容\轻盈健\公寓\text_style_library\style_01.json"


# 如果参考文本在顶层文本轨道里：
NESTED_DRAFT_INDEX = None
TEXT_TRACK_INDEX = 0
TEXT_SEGMENT_INDEX = 0


# 如果参考文本在复合模板内部，把 NESTED_DRAFT_INDEX 改成 0、1...
# NESTED_DRAFT_INDEX = 0
# TEXT_TRACK_INDEX = 0
# TEXT_SEGMENT_INDEX = 0


if __name__ == "__main__":
    export_text_style_preset(
        SOURCE_DRAFT_DIR,
        OUTPUT_STYLE_JSON,
        nested_draft_index=NESTED_DRAFT_INDEX,
        text_track_index=TEXT_TRACK_INDEX,
        track_index=TEXT_TRACK_INDEX,
        segment_index=TEXT_SEGMENT_INDEX,
    )
    print(f"文本样式已导出: {OUTPUT_STYLE_JSON}")
