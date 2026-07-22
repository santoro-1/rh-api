from __future__ import annotations

from app.workflows.base import WorkflowAdapter
from app.workflows.digital_human import DigitalHumanWorkflow


_WORKFLOWS: dict[str, WorkflowAdapter] = {
    "digital_human": DigitalHumanWorkflow(),
}


def get_workflow(key: str) -> WorkflowAdapter:
    try:
        return _WORKFLOWS[key]
    except KeyError as exc:
        raise ValueError(f"不支持的工作流：{key}") from exc


def list_workflows() -> list[WorkflowAdapter]:
    return list(_WORKFLOWS.values())
