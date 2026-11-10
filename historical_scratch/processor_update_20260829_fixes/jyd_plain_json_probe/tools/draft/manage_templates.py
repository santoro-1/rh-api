from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.template_library import TemplateLibrary  # noqa: E402


try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理剪映模板库。")
    parser.add_argument("--library-root", default="", help="模板库目录；不填使用 jyd_plain_json_probe/template_library")

    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="导入一个剪映草稿模板")
    import_parser.add_argument("--source-draft-dir", required=True, help="源剪映草稿目录")
    import_parser.add_argument("--template-id", default="", help="模板 id；不填按源目录名生成")
    import_parser.add_argument("--name", default="", help="模板展示名")
    import_parser.add_argument("--replace", action="store_true", help="如果 template-id 已存在则覆盖")
    import_parser.add_argument("--no-auto-decrypt", action="store_true", help="关闭自动解密")
    import_parser.add_argument("--force-decrypt", action="store_true", help="强制调用 jy-draftc 解密")
    import_parser.add_argument("--decrypt-work-root", default="", help="解密工作目录")
    import_parser.add_argument("--jy-draftc-exe", default="", help="jy-draftc.exe 路径")
    import_parser.add_argument("--jy-install-dir", default="", help="包含 videoeditor.dll 的剪映安装目录")
    import_parser.add_argument("--jy-draftc-debug", action="store_true", help="给 jy-draftc.exe 传 --debug")

    subparsers.add_parser("list", help="列出模板库")

    show_parser = subparsers.add_parser("show", help="查看模板详情")
    show_parser.add_argument("--template-id", required=True, help="模板 id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        library = TemplateLibrary(args.library_root or None)
        if args.command == "import":
            record = library.import_template(
                args.source_draft_dir,
                template_id=args.template_id,
                name=args.name,
                replace=args.replace,
                auto_decrypt=not args.no_auto_decrypt,
                force_decrypt=args.force_decrypt,
                decrypt_work_root=args.decrypt_work_root or None,
                jy_draftc_exe=args.jy_draftc_exe or None,
                jy_install_dir=args.jy_install_dir or None,
                jy_draftc_debug=args.jy_draftc_debug,
            )
            print(json.dumps(record.as_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "list":
            print(json.dumps([record.as_dict() for record in library.list()], ensure_ascii=False, indent=2))
            return 0

        if args.command == "show":
            print(json.dumps(library.get(args.template_id).as_dict(), ensure_ascii=False, indent=2))
            return 0

        raise RuntimeError(f"不支持的命令: {args.command}")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
