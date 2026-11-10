from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from jyd_probe.h3_handoff import import_h3_handoff
from jyd_probe.project_store import ProjectStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Import an H3 handoff into JYD")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--owner-user-id", required=True)
    parser.add_argument("--owner-username", default="")
    parser.add_argument("--project-name", required=True)
    parser.add_argument(
        "--database",
        default=os.environ.get(
            "JYD_DATABASE_PATH",
            str(Path(__file__).resolve().parents[2] / "data" / "web_storage" / "control.db"),
        ),
    )
    args = parser.parse_args()
    store = ProjectStore(Path(args.database).expanduser().resolve())
    project = import_h3_handoff(
        store,
        owner_user_id=args.owner_user_id,
        owner_username=args.owner_username,
        project_name=args.project_name,
        manifest_path=args.manifest,
    )
    print(
        json.dumps(
            {
                "schema": "jyd.h3-import-result.v1",
                "project_id": project["project_id"],
                "status": project["status"],
                "item_id": project["items"][0]["item_id"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
