from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.cli import load_plain_draft_json  # noqa: E402
from jyd_probe.draft_crypto import prepare_plain_draft_dir  # noqa: E402
from jyd_probe.draft_import_analyzer import (  # noqa: E402
    DEFAULT_HASH_LIMIT_BYTES,
    analyze_draft_import,
)


try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="分析剪映草稿的可替换槽位和服务器迁移依赖。")
    parser.add_argument("--draft-dir", required=True, help="需要分析的剪映草稿目录")
    parser.add_argument("--output", default="", help="报告 JSON 输出路径")
    parser.add_argument("--workspace-root", default="", help="中央音乐、特效和文字素材库根目录")
    parser.add_argument("--hash-all", action="store_true", help="计算包括大型视频在内的所有文件哈希")
    parser.add_argument("--no-hash", action="store_true", help="不计算文件哈希")
    parser.add_argument("--no-auto-decrypt", action="store_true", help="关闭自动解密")
    parser.add_argument("--force-decrypt", action="store_true", help="强制复制并解密草稿")
    parser.add_argument("--decrypt-work-root", default="", help="自动解密工作目录")
    parser.add_argument("--jy-draftc-exe", default="", help="jy-draftc.exe 路径")
    parser.add_argument("--jy-install-dir", default="", help="剪映安装目录")
    parser.add_argument("--jy-draftc-debug", action="store_true", help="输出 jy-draftc 调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.hash_all and args.no_hash:
        print("error: --hash-all 和 --no-hash 不能同时使用", file=sys.stderr)
        return 2
    try:
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
        data = load_plain_draft_json(prepared.draft_dir)
        hash_limit = None if args.hash_all else (-1 if args.no_hash else DEFAULT_HASH_LIMIT_BYTES)
        report = analyze_draft_import(
            data,
            source_draft_dir=prepared.source_dir,
            analyzed_draft_dir=prepared.draft_dir,
            was_decrypted=prepared.was_decrypted,
            workspace_root=args.workspace_root or None,
            hash_limit_bytes=hash_limit,
        )

        output_path = (
            Path(args.output).expanduser().resolve()
            if args.output
            else PROJECT_ROOT / "_draft_import_reports" / f"{source_dir.name}_report.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "status": "completed",
                    "report": str(output_path),
                    "draft": report["draft"],
                    "summary": report["summary"],
                    "warnings": report["warnings"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
