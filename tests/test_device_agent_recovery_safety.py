import pytest

from app.database import SessionLocal
from app.services.device_auth.models import WorkbenchDeviceGrant
from tests.conftest import create_user
from tests.test_device_agent_http_runtime import central, flow, ring, seed, raw_signed
from tests.test_device_agent_recovery_contract import interrupted, expire, confirm
from tests.test_device_auth_client_contract import new_session, TestIdentity
from jyd_probe.device_agent_client import AgentRequestAuthorizer
from jyd_probe.device_agent_journal import AgentJournal
from jyd_probe.device_auth_protocol import DeviceAuthorizationError


def test_revoked_device_can_resolve_original_execution_not_start_new_work(central, monkeypatch, client, ring):
    agent, calls, journal, controller = interrupted(central, monkeypatch)
    seed(central, "job-2")
    expire(central)
    with SessionLocal() as db:
        grant = db.get(WorkbenchDeviceGrant, central.flow.registration["grant_id"])
        grant.status, grant.revision = "REVOKED", grant.revision + 1
        db.commit()
    central.flow.session.close()
    session = new_session(client, ring, central.flow.user, central.flow.identity)
    central.api.authorizer = AgentRequestAuthorizer(session)
    controller.session = session
    try:
        confirm(controller, controller.prepare("job-1"), "close")
        assert agent.run_forever(once=True) == 1 and len(calls) == 1
        assert central.store.get_status("job-2")["status"] == "pending"
    finally:
        session.close()


@pytest.mark.parametrize("foreign_user", [False, True])
def test_other_account_or_key_cannot_inspect_or_resolve_original_execution(central, monkeypatch, client, ring, foreign_user):
    agent, calls, journal, controller = interrupted(central, monkeypatch)
    expire(central)
    review = controller.prepare("job-1")
    user = create_user("foreign-recovery-user") if foreign_user else central.flow.user
    identity = central.flow.identity if foreign_user else TestIdentity()
    if not foreign_user:
        identity.initialize_for_activation()
    session = new_session(client, ring, user, identity)
    with journal._connect() as db:
        receipt = journal._record(db.execute("SELECT * FROM receipts").fetchone())
    from jyd_probe.device_auth_protocol import canonical_json, sha256_b64
    body = {"execution_id": review["execution_id"], "payload_hash": sha256_b64(canonical_json(receipt["claim"]["payload"]))}
    try:
        for suffix in ("prepare", "resolve"):
            path = "/api/agents/processor-01/jobs/job-1/recovery/" + suffix
            if suffix == "resolve":
                view = controller._reviews[review["review_id"]][1]
                body = {"execution_id": review["execution_id"], "review_hash": view["review_hash"],
                        "request_id": "c" * 32, "resolution": "failed", "result": None,
                        "error": "foreign close", "confirm_stopped": True, "confirm_reviewed": True}
            headers = raw_signed(central, path, body, authorizer=AgentRequestAuthorizer(session))
            response = central.browser.post(path, headers=headers, json=body)
            assert response.status_code == 409
            assert response.json()["code"] == "DEVICE_AGENT_ASSIGNMENT_MISMATCH"
        assert central.store.get_status("job-1")["status"] == "running"
    finally:
        session.close()


def test_running_local_process_prevents_even_recovery_inspection(central, monkeypatch):
    agent, calls, journal, controller = interrupted(central, monkeypatch)
    before = len(central.opener.paths)
    with AgentJournal(central.root).single_process():
        with pytest.raises(DeviceAuthorizationError) as error:
            controller.prepare("job-1")
        assert error.value.code == "DEVICE_AGENT_ALREADY_RUNNING"
    assert len(central.opener.paths) == before


def test_local_receipt_change_cannot_confirm_stale_review(central, monkeypatch):
    agent, calls, journal, controller = interrupted(central, monkeypatch)
    expire(central)
    review = controller.prepare("job-1")
    with journal._connect() as db:
        db.execute("UPDATE receipts SET payload_json='{}'")
    before = len(central.opener.paths)
    with pytest.raises(DeviceAuthorizationError) as error:
        confirm(controller, review, "close")
    assert error.value.code == "DEVICE_AGENT_REVIEW_CHANGED"
    assert len(central.opener.paths) == before
    assert central.store.get_status("job-1")["status"] == "running"


def test_local_ack_failure_preserves_original_recovery_for_restart(central, monkeypatch):
    agent, calls, journal, controller = interrupted(central, monkeypatch)
    expire(central)
    def failed_ack(receipt):
        raise OSError("isolated disk error")
    monkeypatch.setattr(journal, "acknowledge_recovery", failed_ack)
    with pytest.raises(OSError):
        confirm(controller, controller.prepare("job-1"), "close")
    assert controller.records()[0]["phase"] == "recovery_pending"
    before = central.store.get_status("job-1")
    assert agent.run_forever(once=True) == 0
    assert len(calls) == 1 and controller.records() == []
    assert central.store.get_status("job-1") == before
