from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _top_level_functions(relative_path: str) -> set[str]:
    source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_audio_worker_delegates_voice_library_jobs_to_speech_service():
    functions = _top_level_functions("app/workers/audio_worker.py")
    source = (
        PROJECT_ROOT / "app" / "workers" / "audio_worker.py"
    ).read_text(encoding="utf-8")

    assert "process_voice_task" not in functions
    assert "recover_interrupted_voice_tasks" not in functions
    assert "app.services.speech.voice_jobs" in source


def test_batch_routes_delegate_status_review_and_lifecycle_rules():
    functions = _top_level_functions("app/routes/batches.py")
    source = (
        PROJECT_ROOT / "app" / "routes" / "batches.py"
    ).read_text(encoding="utf-8")

    assert "summarize_batch" not in functions
    assert "approve_item_audio" not in functions
    assert "retry_failed_batch" not in functions
    assert "app.services.batch_status" in source
    assert "app.services.audio_review" in source
    assert "app.services.batch_lifecycle" in source
