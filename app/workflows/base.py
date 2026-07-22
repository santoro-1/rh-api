from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.config import Settings
from app.models import GenerationTask
from app.services.storage import safe_relative_path


@dataclass(frozen=True)
class WorkflowAsset:
    """A locally stored user asset, identified independently of any UI form."""

    name: str
    kind: str
    relative_path: str
    original_name: str


@dataclass(frozen=True)
class WorkflowOutput:
    """A selected downloadable output returned by a workflow."""

    url: str
    extension: str
    metadata: dict[str, Any] = field(default_factory=dict)


class WorkflowAdapter(Protocol):
    key: str
    display_name: str
    default_ai_app_id: str
    default_prompt: str

    def validate_parameters(
        self, parameters: dict[str, Any], asset_metadata: dict[str, Any]
    ) -> dict[str, Any]: ...

    def serialize_input(
        self,
        assets: list[WorkflowAsset],
        parameters: dict[str, Any],
        asset_metadata: dict[str, Any],
    ) -> dict[str, Any]: ...

    def assets_for_task(self, task: GenerationTask) -> list[WorkflowAsset]: ...

    def build_payload(
        self,
        task: GenerationTask,
        uploaded_files: dict[str, str],
        *,
        ai_app_id: str,
        instance_type: str,
        settings: dict[str, Any],
    ) -> dict[str, Any]: ...

    def select_output(self, task: GenerationTask, result: dict[str, Any]) -> WorkflowOutput | None: ...


def resolve_asset_path(asset: WorkflowAsset, settings: Settings) -> Path:
    """Resolve one persisted workflow asset while preserving path traversal checks."""

    return safe_relative_path(asset.relative_path, settings.data_dir)
