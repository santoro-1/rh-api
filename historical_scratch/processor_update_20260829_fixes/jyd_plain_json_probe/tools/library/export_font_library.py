from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT / "data" / "libraries"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.cli import load_plain_draft_json  # noqa: E402
from jyd_probe.draft_crypto import prepare_plain_draft_dir  # noqa: E402
from jyd_probe.font_export import export_font_library, refresh_font_library_metadata  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从剪映草稿复制全部实际使用的字体文件。")
    parser.add_argument("--draft-dir", default="", help="字体来源剪映草稿目录")
    parser.add_argument(
        "--output-dir",
        default=str(WORKSPACE_ROOT / "font_library"),
        help="字体素材库目录",
    )
    parser.add_argument("--replace", action="store_true", help="重新复制已经收录的字体")
    parser.add_argument("--refresh-library", action="store_true", help="从已有字体文件刷新名称等元数据")
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
        if args.refresh_library:
            refreshed = refresh_font_library_metadata(args.output_dir)
            print(json.dumps(refreshed, ensure_ascii=False, indent=2))
            if not args.draft_dir:
                return 0
        if not args.draft_dir:
            raise ValueError("必须提供 --draft-dir，或使用 --refresh-library 刷新已有字体库")
        source_dir = Path(args.draft_dir).expanduser().resolve()
        prepared = prepare_plain_draft_dir(
            source_dir,
            auto_decrypt=not args.no_auto_decrypt,
            force_decrypt=args.force_decrypt,
            work_root=Path(args.decrypt_work_root).expanduser().resolve() if args.decrypt_work_root else None,
            exe=Path(args.jy_draftc_exe).expanduser().resolve() if args.jy_draftc_exe else None,
            install_dir=Path(args.jy_install_dir).expanduser().resolve() if args.jy_install_dir else None,
            debug=args.jy_draftc_debug,
        )
        result = export_font_library(
            load_plain_draft_json(prepared.draft_dir),
            args.output_dir,
            source_draft_dir=prepared.source_dir,
            analyzed_draft_dir=prepared.draft_dir,
            source_label=str(source_dir),
            replace=args.replace,
        )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
