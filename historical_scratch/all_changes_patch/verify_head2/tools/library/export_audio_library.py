from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT / "data" / "libraries"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.audio_export import export_audio_library  # noqa: E402
from jyd_probe.audio_catalog import AudioCatalog  # noqa: E402
from jyd_probe.cli import load_plain_draft_json  # noqa: E402
from jyd_probe.draft_crypto import prepare_plain_draft_dir  # noqa: E402


AUTO_CATEGORY_PREFIXES = ("音乐采集", "音效采集")
AUTO_CATEGORY_SEPARATORS = " _-—－"


def category_from_draft_name(draft_name: str) -> str:
    """Extract a category from names such as `音乐采集_轻松`."""

    name = draft_name.strip()
    for prefix in AUTO_CATEGORY_PREFIXES:
        if not name.startswith(prefix):
            continue
        remainder = name[len(prefix) :]
        if not remainder or remainder[0] not in AUTO_CATEGORY_SEPARATORS:
            break
        category = remainder.lstrip(AUTO_CATEGORY_SEPARATORS).strip()
        if category:
            return category
        break
    raise ValueError(
        "无法从草稿名识别分类；请使用“音乐采集_分类名”或“音效采集_分类名”，"
        "也可以改用 --category 明确指定"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从剪映采集草稿批量复制全部音频素材。")
    parser.add_argument("--draft-dir", required=True, help="音乐或音效采集草稿目录")
    parser.add_argument(
        "--output-dir",
        default=str(WORKSPACE_ROOT / "audio_library"),
        help="音频素材库目录，默认使用工作区 audio_library",
    )
    parser.add_argument("--replace", action="store_true", help="重新复制已经收录的音频文件")
    category_group = parser.add_mutually_exclusive_group()
    category_group.add_argument("--category", default="", help="将本次提取的全部音频加入指定分类")
    category_group.add_argument(
        "--category-from-draft-name",
        action="store_true",
        help="从“音乐采集_轻松”这类草稿名自动读取分类",
    )
    parser.add_argument("--no-auto-decrypt", action="store_true", help="关闭自动解密")
    parser.add_argument("--force-decrypt", action="store_true", help="强制复制并解密草稿")
    parser.add_argument("--decrypt-work-root", default="", help="自动解密工作目录")
    parser.add_argument("--jy-draftc-exe", default="", help="jy-draftc.exe 路径")
    parser.add_argument("--jy-install-dir", default="", help="剪映安装目录")
    parser.add_argument("--jy-draftc-debug", action="store_true", help="输出 jy-draftc 调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source_dir = Path(args.draft_dir).expanduser().resolve()
        category_name = args.category.strip()
        if args.category_from_draft_name:
            category_name = category_from_draft_name(source_dir.name)

        prepared = prepare_plain_draft_dir(
            source_dir,
            auto_decrypt=not args.no_auto_decrypt,
            force_decrypt=args.force_decrypt,
            work_root=Path(args.decrypt_work_root).expanduser().resolve() if args.decrypt_work_root else None,
            exe=Path(args.jy_draftc_exe).expanduser().resolve() if args.jy_draftc_exe else None,
            install_dir=Path(args.jy_install_dir).expanduser().resolve() if args.jy_install_dir else None,
            debug=args.jy_draftc_debug,
        )
        data = load_plain_draft_json(prepared.draft_dir)
        result = export_audio_library(
            data,
            args.output_dir,
            source_label=str(source_dir),
            replace=args.replace,
        )
        payload = result.as_dict()
        if category_name:
            identities = [
                str(item.get("identity", ""))
                for item in result.assets
                if item.get("identity")
            ]
            payload["classification"] = AudioCatalog(result.output_dir).assign_many_to_category(
                identities,
                category_name,
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
