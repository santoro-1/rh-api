"""Release audit only: no provider calls and no user databases."""
from pathlib import Path
import sys

import pytest

JYD = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe")
sys.path.insert(0, str(JYD / "src"))
sys.path.insert(0, str(JYD / "tests"))

from test_project_audio_binding import AudioClient
from jyd_probe.project_audio import ProjectAudioCoordinator
from jyd_probe.project_store import ProjectStore


@pytest.mark.parametrize("accepted_before_exit", [False, True])
def test_interrupted_audio_submission_does_not_remain_starting_forever(
    tmp_path, accepted_before_exit
):
    database = tmp_path / "audit.db"
    store = ProjectStore(database)
    project = store.create_project(
        owner_user_id="audit", owner_username="audit", name="audit",
        items=[{"row_key": "1", "script_text": "test"}],
    )
    client = AudioClient()
    coordinator = ProjectAudioCoordinator(
        store, client, storage_root=tmp_path / "storage", max_audio_bytes=1024
    )
    project = coordinator.start(
        "audit", project["project_id"], "mock-token",
        default_voice_asset_id="voice-1", voice_assignments=None,
        settings={}, idempotency_key="initial",
    )
    original_create = client.create_workbench_audio_batch
    item_id = project["items"][0]["item_id"]

    def stop_process(token, payload):
        if accepted_before_exit:
            original_create(token, payload)
        raise SystemExit("simulated processor exit")

    client.create_workbench_audio_batch = stop_process
    with pytest.raises(SystemExit):
        coordinator.retry(
            "audit", project["project_id"], item_id, "mock-token",
            idempotency_key="interrupted", settings={"speed": 1.05},
        )

    client.create_workbench_audio_batch = original_create
    restarted = ProjectAudioCoordinator(
        ProjectStore(database), client,
        storage_root=tmp_path / "storage", max_audio_bytes=1024,
    )
    for _ in range(3):
        snapshot = restarted.sync("audit", project["project_id"], "mock-token")
    replay = restarted.retry(
        "audit", project["project_id"], item_id, "mock-token",
        idempotency_key="interrupted", settings={"speed": 1.05},
    )
    assert len(client.requests) == (2 if accepted_before_exit else 1)
    assert replay["items"][0]["outputs"] == project["items"][0]["outputs"]
    with pytest.raises(ValueError, match="正在生成"):
        restarted.retry(
            "audit", project["project_id"], item_id, "mock-token",
            idempotency_key="new-click", settings={"speed": 1.05},
        )
    # Expected safe behavior: reconcile the same cloud key, or surface an
    # explicit submission-unknown state; never silently stay STARTING forever.
    assert snapshot["operations"][-1]["status"] != "STARTING"
