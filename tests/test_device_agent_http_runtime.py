"""Real website permits -> real central HTTP -> actual Agent runtime.

Only native identity and renderer/COM are mocked. Storage is isolated; neither a
production website nor an actual processing machine is contacted.
"""

from contextlib import nullcontext
from io import BytesIO
import json
import threading
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.services.device_auth.models import WorkbenchDeviceGrant
from app.services.device_auth.protocol import public_jwk
from tests.test_device_agent_contract import flow, ring, ORIGIN, set_mode
from tests.test_device_auth_client_contract import new_session, TestIdentity, approve
from tests.conftest import create_user
from jyd_probe.device_agent_client import AgentRequestAuthorizer
from jyd_probe.device_agent_journal import AgentJournal
from jyd_probe.device_agent_protocol import AgentRequestContext
from jyd_probe.device_agent_transport import (
    AgentApiClient,
    AgentRequestError,
    CHALLENGE_PATH,
)
from jyd_probe.device_auth_protocol import DeviceAuthorizationError
from jyd_probe.device_local_execution import (
    authorized_local_unit,
    render_operation_scopes,
)
from jyd_probe import device_trust_roots, render_agent
from jyd_probe.web_api import WebApiSettings, create_app

COMMON = {"Authorization": "Bearer central-test-password"}


class CentralOpener:
    """urllib boundary adapter preserving actual request encoding and HTTP routes."""

    def __init__(self, client):
        self.client, self.paths, self.drop_after = client, [], None
        self.before = None

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        self.paths.append(path)
        if self.before is not None:
            self.before(path)
        result = self.client.post(
            request.full_url,
            content=request.data,
            headers=dict(request.header_items()),
            follow_redirects=False,
        )
        if result.status_code >= 300:
            raise HTTPError(
                request.full_url,
                result.status_code,
                "test",
                result.headers,
                BytesIO(result.content),
            )
        if self.drop_after and path.endswith(self.drop_after):
            self.drop_after = None
            raise URLError("simulated lost response after central commit")
        return BytesIO(result.content)


@pytest.fixture
def central(flow, ring, tmp_path, monkeypatch):
    monkeypatch.setattr(
        device_trust_roots,
        "TRUSTED_ISSUERS",
        (
            {
                "origin": ring.config.origin,
                "environment": ring.config.environment,
                "keys": [
                    {"kid": kid, "jwk": public_jwk(key)}
                    for kid, key in ring.verification_keys.items()
                ],
            },
        ),
    )
    settings = WebApiSettings(
        storage_root=tmp_path / "storage",
        template_library_root=tmp_path / "templates",
        default_draft_root=tmp_path / "drafts",
        audio_library_root=tmp_path / "audio",
        admin_password="internal-test",
        admin_session_secret="test-admin-secret",
        site_password="test-operator",
        site_session_secret="test-site-secret",
        execution_mode="agent",
        agent_token="central-test-password",
        database_path=tmp_path / "central-http.db",
        auth_authority=True,
        auth_server_url=ring.config.origin,
    )
    for path in (
        settings.storage_root,
        settings.template_library_root,
        settings.default_draft_root,
        settings.audio_library_root,
    ):
        path.mkdir(parents=True, exist_ok=True)
    app = create_app(settings)
    browser = TestClient(app, base_url=ORIGIN)
    api = AgentApiClient(
        ORIGIN,
        "central-test-password",
        agent_id="processor-01",
        authorizer=flow.authorizer,
    )
    opener = CentralOpener(browser)
    api._opener = opener
    monkeypatch.setattr(
        render_agent,
        "initialize_ui_automation_in_current_thread",
        lambda: nullcontext(),
    )
    value = SimpleNamespace(
        app=app,
        browser=browser,
        store=app.state.agent_authorization_gate.store,
        api=api,
        opener=opener,
        root=tmp_path / "receipts",
        flow=flow,
    )
    yield value
    browser.close()


def seed(central, job_id="job-1", *, owner=None, skip_export=False, batch_id=None):
    user_id = central.flow.user.id if owner is None else owner
    central.store.add_job(
        job_id,
        {"source": {"type": "video"}, "output": {"skip_export": skip_export}},
        {
            "job_id": job_id,
            "status": "pending",
            "batch_id": batch_id,
            "device_authorization": {
                "user_id": user_id,
                "mode": "OFF",
                "waiting": False,
            },
        },
    )


def runner(central, monkeypatch, *, render=None):
    calls = []

    def render_mock(payload):
        with authorized_local_unit(render_operation_scopes(payload)) as decision:
            assert decision.user_id == central.flow.user.id
            calls.append(payload)
            return (
                render(payload)
                if render
                else SimpleNamespace(
                    as_dict=lambda: {"exported": True, "output_mp4": "test-result.mp4"}
                )
            )

    monkeypatch.setattr(render_agent, "run_render_job", render_mock)
    agent = render_agent.RenderAgent(
        central.api, agent_id="processor-01", name="Test", journal_root=central.root
    )
    return agent, calls


def raw_signed(central, path, payload, *, authorizer=None, agent_id="processor-01"):
    challenge = central.browser.post(
        CHALLENGE_PATH,
        headers=COMMON,
        json={"agent_id": agent_id, "path": path, "payload": payload},
    )
    assert challenge.status_code == 200, challenge.text
    context = AgentRequestContext.for_request(ORIGIN, agent_id, path, payload)
    headers = (authorizer or central.flow.authorizer).headers(
        context, challenge.json()["nonce"]
    )
    return {**COMMON, **headers}


def test_actual_http_agent_register_claim_start_render_and_idempotent_report(
    central, monkeypatch
):
    seed(central)
    agent, calls = runner(central, monkeypatch)
    assert agent.run_forever(once=True) == 0
    assert len(calls) == 1
    status = central.store.get_status("job-1")
    assert status["status"] == "completed" and status["retry_count"] == 0
    assert (
        status["agent_device_authorization"]["device_id"]
        == central.flow.registration["device_id"]
    )
    body = {
        "execution_id": status["agent_execution"]["execution_id"],
        "result": status["result"],
    }
    again = central.api.post("/api/agents/processor-01/jobs/job-1/complete", body)
    assert again == {
        key: value
        for key, value in status.items()
        if key not in {"retry_count", "cancel_requested"}
    }
    for suffix, changed in (
        ("complete", {**body, "result": {"exported": False}}),
        ("fail", {"execution_id": body["execution_id"], "error": "late error"}),
    ):
        with pytest.raises(AgentRequestError) as error:
            central.api.post("/api/agents/processor-01/jobs/job-1/" + suffix, changed)
        assert error.value.code == "DEVICE_AGENT_RESULT_CONFLICT"
    assert central.store.get_status("job-1") == status
    journal = AgentJournal(central.root)
    assert journal.pending(ORIGIN, "processor-01", central.flow.user.id) == []
    with journal._connect() as db:
        serialized = json.dumps(
            [dict(row) for row in db.execute("SELECT * FROM receipts")]
        )
    assert central.flow.session._login_token not in serialized
    assert "agent_permit" not in serialized and "DPoP" not in serialized


@pytest.mark.parametrize(
    "stage,initial_renders", [("/claim", 0), ("/start", 0), ("/complete", 1)]
)
def test_lost_response_restart_uses_original_receipt_not_second_render(
    central, monkeypatch, client, ring, stage, initial_renders
):
    seed(central)
    agent, calls = runner(central, monkeypatch)
    central.opener.drop_after = stage
    assert agent.run_forever(once=True) == 1
    assert len(calls) == initial_renders
    old_status = central.store.get_status("job-1")
    if stage == "/claim":
        with central.store._transaction() as db:
            db.execute(
                "UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00' WHERE job_id='job-1'"
            )
        central.store.recover_expired_leases()
    central.flow.session.close()
    session = new_session(client, ring, central.flow.user, central.flow.identity)
    central.api.authorizer = AgentRequestAuthorizer(session)
    replacement = render_agent.RenderAgent(
        central.api,
        agent_id="processor-01",
        name="Updated version",
        journal_root=central.root,
    )
    assert replacement.run_forever(once=True) == 0
    assert len(calls) == 1
    status = central.store.get_status("job-1")
    assert status["status"] == "completed" and status["retry_count"] == 0
    if "agent_execution" in old_status:
        assert (
            status["agent_execution"]["execution_id"]
            == old_status["agent_execution"]["execution_id"]
        )
    assert central.flow.identity.created == 1
    assert not any(path.endswith("/register") for path in session._transport.paths)
    session.close()


def test_crashed_execution_stays_uncertain_and_is_not_restarted(central, monkeypatch):
    seed(central)

    def crash(payload):
        raise SystemExit("simulated interrupted process")

    agent, calls = runner(central, monkeypatch, render=crash)
    with pytest.raises(SystemExit):
        agent.run_forever(once=True)
    assert len(calls) == 1
    next_agent = render_agent.RenderAgent(
        central.api, agent_id="processor-01", name="Restart", journal_root=central.root
    )
    assert next_agent.run_forever(once=True) == 1 and len(calls) == 1
    status = central.store.get_status("job-1")
    assert status["status"] == "running" and status["retry_count"] == 0
    pending = AgentJournal(central.root).pending(
        ORIGIN, "processor-01", central.flow.user.id
    )
    assert len(pending) == 1 and pending[0]["phase"] == "executing"


def test_revoked_during_render_can_finish_but_cannot_start_another_job(
    central, monkeypatch
):
    seed(central)
    seed(central, "job-2")

    def revoke_then_finish(payload):
        with SessionLocal() as db:
            grant = db.get(WorkbenchDeviceGrant, central.flow.registration["grant_id"])
            grant.status, grant.revision = "REVOKED", grant.revision + 1
            db.commit()
        return SimpleNamespace(as_dict=lambda: {"exported": True})

    agent, calls = runner(central, monkeypatch, render=revoke_then_finish)
    assert agent.run_forever(once=True) == 0 and len(calls) == 1
    assert central.store.get_status("job-1")["status"] == "completed"
    assert agent.run_forever(once=True) == 1 and len(calls) == 1
    assert central.store.get_status("job-2")["status"] == "pending"


def test_lost_completed_report_is_recovered_even_after_device_revoked(
    central, monkeypatch, client, ring
):
    seed(central)
    agent, calls = runner(central, monkeypatch)
    central.opener.drop_after = "/complete"
    assert agent.run_forever(once=True) == 1
    with SessionLocal() as db:
        grant = db.get(WorkbenchDeviceGrant, central.flow.registration["grant_id"])
        grant.status, grant.revision = "REVOKED", grant.revision + 1
        db.commit()
    central.flow.session.close()
    session = new_session(client, ring, central.flow.user, central.flow.identity)
    central.api.authorizer = AgentRequestAuthorizer(session)
    before = len(central.opener.paths)
    assert agent.run_forever(once=True) == 0 and len(calls) == 1
    assert "/api/agents/register" not in central.opener.paths[before:]
    assert (
        AgentJournal(central.root).pending(ORIGIN, "processor-01", central.flow.user.id)
        == []
    )
    session.close()


def test_old_token_and_replayed_bound_http_requests_cannot_mutate(central):
    seed(central)
    for path, body in (
        ("/api/agents/register", {"agent_id": "processor-01"}),
        ("/api/agents/processor-01/claim", {}),
        ("/api/agents/processor-01/heartbeat", {}),
        ("/api/agents/processor-01/jobs/job-1/start", {"execution_id": "a" * 32}),
        ("/api/agents/processor-01/jobs/job-1/heartbeat", {}),
        ("/api/agents/processor-01/jobs/job-1/complete", {"result": {}}),
        ("/api/agents/processor-01/jobs/job-1/fail", {}),
    ):
        rejected = central.browser.post(path, headers=COMMON, json=body)
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "DEVICE_AGENT_PROTOCOL_REQUIRED"
    body = {"agent_id": "processor-01", "name": "Test"}
    headers = raw_signed(central, "/api/agents/register", body)
    assert (
        central.browser.post(
            "/api/agents/register", headers=headers, json=body
        ).status_code
        == 200
    )
    assert (
        central.browser.post("/api/agents/register", headers=headers, json=body).json()[
            "code"
        ]
        == "AGENT_PROOF_REPLAYED"
    )
    assert central.store.get_status("job-1")["status"] == "pending"


def test_foreign_account_cannot_register_over_busy_agent_or_report_its_job(
    central, client, ring
):
    seed(central)
    central.api.post("/api/agents/register", {"agent_id": "processor-01"})
    central.api.post("/api/agents/processor-01/claim")
    user = create_user("other-agent-account")
    session = new_session(client, ring, user, central.flow.identity)
    registration = session.register(
        label="same machine different account", client_version="test"
    )
    approve(central.flow.admin, registration)
    session.refresh()
    other = AgentRequestAuthorizer(session)
    for path, payload in (
        ("/api/agents/register", {"agent_id": "processor-01"}),
        ("/api/agents/processor-01/claim", {}),
        (
            "/api/agents/processor-01/jobs/job-1/complete",
            {"execution_id": "d" * 32, "result": {}},
        ),
    ):
        headers = raw_signed(central, path, payload, authorizer=other)
        result = central.browser.post(path, headers=headers, json=payload)
        assert (
            result.status_code == 409
            and result.json()["code"] == "DEVICE_AGENT_ASSIGNMENT_MISMATCH"
        )
    assert central.store.get_status("job-1")["status"] == "running"
    session.close()


@pytest.mark.parametrize("mode", ["OFF", "OBSERVE"])
def test_signed_rollout_actual_agent_without_initializing_a_key(
    central, client, ring, monkeypatch, mode
):
    set_mode(mode)
    identity = TestIdentity()
    session = new_session(client, ring, central.flow.user, identity)
    central.api.authorizer = AgentRequestAuthorizer(session)
    seed(central)
    agent, calls = runner(central, monkeypatch)
    assert agent.run_forever(once=True) == 0 and len(calls) == 1
    assert identity.created == 0
    session.close()


def test_second_local_agent_cannot_enter_while_first_holds_execution_lock(central):
    first, second = AgentJournal(central.root), AgentJournal(central.root)
    with first.single_process():
        with pytest.raises(DeviceAuthorizationError) as error:
            with second.single_process():
                pytest.fail("second process lock admitted")
        assert error.value.code == "DEVICE_AGENT_ALREADY_RUNNING"
    with second.single_process():
        pass


def test_another_agent_label_does_not_give_same_key_a_second_execution_slot(central):
    seed(central)
    seed(central, "job-2")
    central.api.post("/api/agents/register", {"agent_id": "processor-01"})
    assert (
        central.api.post("/api/agents/processor-01/claim")["job"]["job_id"] == "job-1"
    )
    other = AgentApiClient(
        ORIGIN,
        "central-test-password",
        agent_id="processor-02",
        authorizer=central.flow.authorizer,
    )
    other._opener = central.opener
    other.post("/api/agents/register", {"agent_id": "processor-02"})
    assert other.post("/api/agents/processor-02/claim")["job"] is None
    assert central.store.get_status("job-2")["status"] == "pending"


def test_changed_http_body_is_rejected_without_consuming_original_request(central):
    path = "/api/agents/register"
    body = {"agent_id": "processor-01", "name": "Original"}
    headers = raw_signed(central, path, body)
    changed = central.browser.post(
        path, headers=headers, json={**body, "name": "Replaced"}
    )
    assert changed.status_code == 403
    original = central.browser.post(path, headers=headers, json=body)
    assert original.status_code == 200
    assert central.store.get_agent("processor-01")["name"] == "Original"


def test_foreign_key_report_and_unstarted_success_cannot_finish_job(
    central, client, ring
):
    seed(central)
    central.api.post("/api/agents/register", {"agent_id": "processor-01"})
    central.api.post("/api/agents/processor-01/claim")
    path = "/api/agents/processor-01/jobs/job-1/complete"
    body = {"execution_id": "a" * 32, "result": {"exported": True}}
    with pytest.raises(AgentRequestError) as error:
        central.api.post(path, body)
    assert error.value.code == "DEVICE_AGENT_EXECUTION_UNCERTAIN"
    identity = TestIdentity()
    identity.initialize_for_activation()
    session = new_session(client, ring, central.flow.user, identity)
    headers = raw_signed(
        central, path, body, authorizer=AgentRequestAuthorizer(session)
    )
    rejected = central.browser.post(path, headers=headers, json=body)
    assert (
        rejected.status_code == 409
        and rejected.json()["code"] == "DEVICE_AGENT_ASSIGNMENT_MISMATCH"
    )
    assert central.store.get_status("job-1")["status"] == "running"
    session.close()


def test_cancel_before_start_releases_assignment_without_rendering(
    central, monkeypatch
):
    central.store.add_batch({"batch_id": "batch-cancel"})
    seed(central, batch_id="batch-cancel")

    def cancel_before_start(path):
        if path.endswith("/start"):
            assert central.store.cancel_batch("batch-cancel") == ["job-1"]
            central.opener.before = None

    central.opener.before = cancel_before_start
    agent, calls = runner(central, monkeypatch)
    assert agent.run_forever(once=True) == 0 and calls == []
    assert central.store.get_status("job-1")["status"] == "cancelled"
    assert central.store.get_agent("processor-01")["current_job_id"] is None
    assert (
        AgentJournal(central.root).pending(ORIGIN, "processor-01", central.flow.user.id)
        == []
    )
    seed(central, "job-2")
    assert agent.run_forever(once=True) == 0 and len(calls) == 1


def test_missing_local_receipt_cannot_restart_an_already_started_central_job(
    central, monkeypatch
):
    seed(central)
    agent, calls = runner(central, monkeypatch)
    central.opener.drop_after = "/start"
    assert agent.run_forever(once=True) == 1 and calls == []
    missing = render_agent.RenderAgent(
        central.api,
        agent_id="processor-01",
        name="Missing receipts",
        journal_root=central.root.parent / "empty-receipts",
    )
    assert missing.run_forever(once=True) == 1 and calls == []
    assert agent.run_forever(once=True) == 0 and len(calls) == 1


def test_definitive_renderer_failure_is_reported_once_without_retry(
    central, monkeypatch
):
    seed(central)

    def failed_render(payload):
        raise RuntimeError("potentially sensitive internal error text")

    agent, calls = runner(central, monkeypatch, render=failed_render)
    assert agent.run_forever(once=True) == 0 and len(calls) == 1
    status = central.store.get_status("job-1")
    assert status["status"] == "failed" and status["retry_count"] == 0
    assert "sensitive" not in status["error"]
    assert agent.run_forever(once=True) == 0 and len(calls) == 1


def test_restored_or_stale_agent_status_file_never_requeues_original_execution(
    central, tmp_path
):
    from jyd_probe.task_store import SQLiteTaskStore

    seed(central)
    central.api.post("/api/agents/register", {"agent_id": "processor-01"})
    claimed = central.api.post("/api/agents/processor-01/claim")["job"]
    execution_id = "d" * 32
    central.api.post(
        "/api/agents/processor-01/jobs/job-1/start", {"execution_id": execution_id}
    )
    old = central.store.get_status("job-1")
    restored = SQLiteTaskStore(tmp_path / "restored.db")
    restored.import_legacy_job("job-1", claimed["payload"], old, replace_existing=True)
    status = restored.get_status("job-1")
    assert status["status"] == "running" and status["agent_recovery_required"] is True
    assert status["assigned_agent_id"] == "processor-01" and status["retry_count"] == 0
    assert restored.pending_count() == 0
    restored.register_agent("embedded-local", {})
    assert restored.claim_job("embedded-local") is None
    central.api.post(
        "/api/agents/processor-01/jobs/job-1/complete",
        {"execution_id": execution_id, "result": {"exported": True}},
    )
    completed = central.store.get_status("job-1")
    central.store.import_legacy_job(
        "job-1", claimed["payload"], old, replace_existing=True
    )
    assert central.store.get_status("job-1") == completed
