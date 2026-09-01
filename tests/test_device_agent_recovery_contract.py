"""Original-device recovery against actual central HTTP and cloud permits."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from tests.test_device_agent_http_runtime import central, flow, ring, seed, runner, COMMON
from jyd_probe.device_agent_journal import AgentJournal
from jyd_probe.device_agent_recovery import AgentRecoveryController
from jyd_probe.device_agent_transport import AgentRequestError
from jyd_probe.device_auth_protocol import DeviceAuthorizationError


def interrupted(central, monkeypatch, *, output=False):
    seed(central)
    if output:
        draft = central.root.parent / "finished-draft"
        draft.mkdir()
        (draft / "draft_content.json").write_text('{"duration":1000000}', encoding="utf-8")
        video = central.root.parent / "finished.mp4"
        video.write_bytes(b'\x00\x00\x00\x18ftypisom' + b'\x00' * 100)
        with central.store._transaction() as db:
            db.execute("UPDATE jobs SET payload_json=? WHERE job_id='job-1'", (json.dumps({
                "source": {"type": "video"}, "output": {"draft_root": str(draft.parent),
                "draft_name": draft.name, "mp4_path": str(video)}}),))
    def crash(payload):
        raise SystemExit("simulated stopped process")
    agent, calls = runner(central, monkeypatch, render=crash)
    with pytest.raises(SystemExit):
        agent.run_forever(once=True)
    journal = AgentJournal(central.root)
    controller = AgentRecoveryController(agent, central.flow.session, journal)
    return agent, calls, journal, controller


def expire(central):
    with central.store._transaction() as db:
        db.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00' WHERE job_id='job-1'")


def confirm(controller, review, choice):
    return controller.resolve(review["review_id"], choice, confirm_stopped=True, confirm_reviewed=True)


def test_manual_close_preserves_execution_and_does_not_render_or_requeue(central, monkeypatch):
    agent, calls, journal, controller = interrupted(central, monkeypatch)
    old = central.store.get_status("job-1")
    expire(central)
    review = controller.prepare("job-1")
    assert review["can_resolve"] and review["candidate"] is None
    result = confirm(controller, review, "close")
    status = central.store.get_status("job-1")
    assert result["acknowledged"] and status["status"] == "failed"
    assert status["agent_execution"] == old["agent_execution"]
    assert status["retry_count"] == 0 and len(calls) == 1
    assert status["agent_manual_recovery"]["confirmed_stopped"] is True
    assert "expires_at" in status and not journal.has_unresolved_execution()
    assert controller.records() == []
    with central.store._transaction() as db:
        assert db.execute("SELECT assigned_agent_id FROM jobs WHERE job_id='job-1'").fetchone()[0] == agent.agent_id
    with journal._connect() as db:
        row = db.execute("SELECT * FROM recovery_audit").fetchone()
        assert row["acknowledged_at"] is not None
        assert central.flow.session._login_token not in row["request_json"]


def test_recovery_confirmation_loss_reuses_original_request_without_render(central, monkeypatch):
    agent, calls, journal, controller = interrupted(central, monkeypatch)
    expire(central)
    review = controller.prepare("job-1")
    central.opener.drop_after = "/recovery/resolve"
    with pytest.raises(AgentRequestError):
        confirm(controller, review, "close")
    assert journal.has_unresolved_execution()
    record = controller.records()[0]
    assert record["phase"] == "recovery_pending"
    status = central.store.get_status("job-1")
    assert status["status"] == "failed"
    assert agent.run_forever(once=True) == 0
    assert len(calls) == 1 and controller.records() == []
    assert central.store.get_status("job-1") == status


def test_existing_output_requires_review_and_is_not_regenerated(central, monkeypatch):
    agent, calls, journal, controller = interrupted(central, monkeypatch, output=True)
    expire(central)
    review = controller.prepare("job-1")
    assert review["candidate"] is not None, review
    assert len(review["candidate"]["evidence"]) == 2
    with pytest.raises(DeviceAuthorizationError):
        controller.resolve(review["review_id"], "accept-output", confirm_stopped=True, confirm_reviewed=False)
    confirm(controller, review, "accept-output")
    result = central.store.get_status("job-1")["result"]
    assert result == review["candidate"]["result"] and result["exported"] is True
    assert result["manual_recovery"]["schema"] == "publicvideo.agent-output-review.v1"
    assert len(calls) == 1 and controller.records() == []


def test_changed_output_is_not_adopted_and_can_be_reviewed_again(central, monkeypatch):
    agent, calls, journal, controller = interrupted(central, monkeypatch, output=True)
    expire(central)
    review = controller.prepare("job-1")
    assert review["candidate"] is not None, review
    (central.root.parent / "finished.mp4").write_bytes(b'\x00\x00\x00\x18ftypisom' + b'x' * 100)
    with pytest.raises(DeviceAuthorizationError) as error:
        confirm(controller, review, "accept-output")
    assert error.value.code == "DEVICE_AGENT_OUTPUT_CHANGED"
    assert controller.records()[0]["phase"] == "executing"
    assert central.store.get_status("job-1")["status"] == "running"
    revised_review = controller.prepare("job-1")
    assert revised_review["candidate"] is not None, revised_review
    confirm(controller, revised_review, "accept-output")
    assert len(calls) == 1


def test_active_lease_and_changed_central_state_prevent_manual_close(central, monkeypatch):
    agent, calls, journal, controller = interrupted(central, monkeypatch)
    review = controller.prepare("job-1")
    assert review["can_resolve"] is False
    with pytest.raises(DeviceAuthorizationError) as error:
        confirm(controller, review, "close")
    assert error.value.code == "DEVICE_AGENT_EXECUTION_ACTIVE"
    expire(central)
    review = controller.prepare("job-1")
    central.api.post("/api/agents/processor-01/jobs/job-1/heartbeat", {"execution_id": review["execution_id"]})
    with pytest.raises(AgentRequestError) as error:
        confirm(controller, review, "close")
    assert error.value.code == "DEVICE_AGENT_REVIEW_CHANGED"
    assert controller.records()[0]["phase"] == "executing"
    assert len(calls) == 1
    expire(central)
    confirm(controller, controller.prepare("job-1"), "close")


def test_sync_original_central_result_needs_no_new_execution(central, monkeypatch):
    agent, calls, journal, controller = interrupted(central, monkeypatch)
    record = controller.records()[0]
    original = {"exported": True, "output_mp4": "previous-result.mp4"}
    central.api.post("/api/agents/processor-01/jobs/job-1/complete", {
        "execution_id": record["execution_id"], "result": original})
    old = central.store.get_status("job-1")
    review = controller.prepare("job-1")
    assert review["status"] == "completed" and not review["can_resolve"]
    confirm(controller, review, "sync")
    assert controller.records() == [] and len(calls) == 1
    assert central.store.get_status("job-1") == old


def test_recovery_no_common_password_or_wrong_execution_or_changed_claim(central, monkeypatch):
    agent, calls, journal, controller = interrupted(central, monkeypatch)
    for suffix in ("prepare", "resolve"):
        response = central.browser.post("/api/agents/processor-01/jobs/job-1/recovery/" + suffix,
                                        headers=COMMON, json={})
        assert response.status_code == 409
    with pytest.raises(AgentRequestError):
        central.api.post("/api/agents/processor-01/jobs/job-1/recovery/prepare", {
            "execution_id": "a" * 32, "payload_hash": "x" * 43})
    with journal._connect() as db:
        db.execute("UPDATE receipts SET claim_json=?", (json.dumps({"payload": {"changed": True}}),))
    with pytest.raises(AgentRequestError) as error:
        controller.prepare("job-1")
    assert error.value.code == "DEVICE_AGENT_RECEIPT_CONFLICT"
    assert len(calls) == 1 and central.store.get_status("job-1")["status"] == "running"


def test_recovery_replay_cannot_change_previously_confirmed_conclusion(central, monkeypatch):
    agent, calls, journal, controller = interrupted(central, monkeypatch)
    expire(central)
    confirm(controller, controller.prepare("job-1"), "close")
    with journal._connect() as db:
        body = json.loads(db.execute("SELECT request_json FROM recovery_audit").fetchone()[0])
    before = central.store.get_status("job-1")
    changed = deepcopy(body)
    changed["error"] = "a different conclusion"
    with pytest.raises(AgentRequestError) as error:
        central.api.post("/api/agents/processor-01/jobs/job-1/recovery/resolve", changed)
    assert error.value.code == "DEVICE_AGENT_RESULT_CONFLICT"
    malformed = {**body, "resolution": []}
    with pytest.raises(AgentRequestError) as error:
        central.api.post("/api/agents/processor-01/jobs/job-1/recovery/resolve", malformed)
    assert error.value.code == "INVALID_AGENT_RECOVERY"
    assert central.store.get_status("job-1") == before
