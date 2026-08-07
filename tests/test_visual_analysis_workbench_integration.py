from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from app.database import SessionLocal
from app.models import ArkConfig, User
from app.services.security import encrypt_secret
from app.services.visual_analysis.analysis import analyze_visual_context
from tests.conftest import create_user


RUNNINGHUB_ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_ROOT = RUNNINGHUB_ROOT.parent.parent / "公寓" / "jyd_plain_json_probe"
WORKBENCH_SRC = WORKBENCH_ROOT / "src"
CATALOG_ROOT = WORKBENCH_ROOT / "data" / "libraries" / "semantic_visual_library"
if not WORKBENCH_SRC.is_dir():
    raise RuntimeError(f"跨项目验收缺少剪映工作台源码: {WORKBENCH_SRC}")
sys.path.append(str(WORKBENCH_SRC))

from jyd_probe.project_visual_analysis import _validated_remote_result  # noqa: E402
from jyd_probe.semantic_visuals import (  # noqa: E402
    build_visual_recipe,
    load_semantic_visual_catalog,
    map_visual_candidates_to_raw_cues,
    recall_semantic_visual_candidates,
)


SCRIPT = "每天吃一个鸡蛋。鸡蛋里挑骨头。这不是鸡蛋。讨论鸡蛋这个词。"


class FakeArkClient:
    def __init__(self) -> None:
        self.calls = 0

    def create_chat_completion(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        request = json.loads(kwargs["messages"][1]["content"])
        decisions = []
        for index, candidate in enumerate(request["candidates"]):
            show = index == 0
            usages = [
                "literal",
                "idiom",
                "negated",
                "meta_mention",
            ]
            reasons = [
                "LITERAL_CONCRETE_OBJECT",
                "SKIP_IDIOM",
                "SKIP_NEGATED",
                "SKIP_META_MENTION",
            ]
            decisions.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "decision": "SHOW" if show else "SKIP",
                    "concept_id": "food.egg",
                    "usage": usages[index],
                    "importance": 0.95 if show else 0.1,
                    "confidence": 0.98,
                    "reason_code": reasons[index],
                }
            )
        return {
            "id": "visual-cross-project",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "schema_version": "jyd.visual-analysis.v1",
                                "script_sha256": request["script_sha256"],
                                "catalog_version": request["catalog_version"],
                                "decisions": decisions,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
        }


def test_literal_egg_survives_but_idiom_negation_and_meta_mentions_do_not() -> None:
    catalog = load_semantic_visual_catalog(CATALOG_ROOT)
    request = recall_semantic_visual_candidates(SCRIPT, catalog)
    user = create_user("visual-cross-project", with_config=False)
    with SessionLocal() as db:
        attached = db.get(User, user.id)
        db.add(
            ArkConfig(
                user=attached,
                enabled=True,
                api_key_encrypted=encrypt_secret("visual-cross-project-key"),
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                model="test-model",
                timeout_seconds=30,
                max_retries=0,
            )
        )
        db.commit()

    fake = FakeArkClient()
    with SessionLocal() as db:
        remote = analyze_visual_context(
            db,
            db.get(User, user.id),
            payload=request,
            client_factory=lambda _config: fake,
        )
    consumed = _validated_remote_result(remote, candidate_request=request)
    mapped = map_visual_candidates_to_raw_cues(
        SCRIPT,
        request["candidates"],
        [{"start_us": 0, "end_us": 12_000_000, "text": SCRIPT}],
    )
    recipe = build_visual_recipe(
        catalog=catalog,
        mapped_candidates=mapped,
        decisions=consumed["decisions"],
    )

    assert fake.calls == 1
    assert [item["decision"] for item in consumed["decisions"]] == [
        "SHOW",
        "SKIP",
        "SKIP",
        "SKIP",
    ]
    assert len(recipe["overlays"]) == 1
    assert recipe["overlays"][0]["concept_id"] == "food.egg"
    assert Path(recipe["overlays"][0]["bundle_path"]).is_dir()
