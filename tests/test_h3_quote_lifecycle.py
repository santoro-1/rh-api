from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import GenerationBatch, GenerationTask, GenerationSegment, User
from app.services.h3_workbench import cancel_h3_workbench_quote, confirm_h3_workbench_batch, H3WorkbenchError
from tests.conftest import create_user
from tests.test_h3_workbench import (
    _token, _enable_h3_pool, _stage, _finished_audio, _fake_cut,
    _fake_motion_references, _fake_reference_frame, SCRIPT,
)


@pytest.fixture(params=["loop_anchor", "soft_chain"])
def pending_quote(client, monkeypatch, request):
    username = "quote-lifecycle"
    create_user(username)
    token = _token(client, username)
    account = _enable_h3_pool(username)
    image = _stage(client, token, "image", "image.png", "image/png")
    video = _stage(client, token, "video", "video.mp4", "video/mp4")
    audio_batch, audio_item = _finished_audio(client, token, username)
    response = client.post("/api/workbench/h3-audio-sources/approve", json={
        "access_token": token, "audio_batch_id": audio_batch,
        "audio_item_id": audio_item, "audio_generation_version": 1,
    })
    assert response.status_code == 200, response.text
    monkeypatch.setattr("app.services.h3_workbench.inspect_audio_duration", lambda path: 10.0 if "segment-" in Path(path).name else 20.0)
    monkeypatch.setattr("app.services.h3_workbench.cut_audio_segment", _fake_cut)
    monkeypatch.setattr("app.services.h3_workbench.split_h3_motion_reference", _fake_motion_references)
    monkeypatch.setattr("app.services.h3_workbench.extract_reference_frame", _fake_reference_frame)
    response = client.post("/api/workbench/h3-batches/prepare", json={
        "access_token": token, "name": "quote", "request_key": "quote-1",
        "reference_image_asset_ids": [image], "selected_account_ids": [account],
        "defaults": {"continuity_mode": request.param},
        "rows": [{"row_id": "ROW-001", "script_text": SCRIPT, "video_asset_id": video,
                  "audio_batch_id": audio_batch, "audio_item_id": audio_item, "audio_generation_version": 1}],
    })
    assert response.status_code == 201, response.text
    return token, response.json(), username


def cancel(client, token, quote, **overrides):
    return client.post(f"/api/workbench/h3-batches/{quote['batch_id']}/quote/cancel", json={
        "access_token": token, "request_key": "cancel-1", "cancel_quote_confirmed": True,
        "quote_token": quote["quote_recovery"]["quote_token"], **overrides,
    })


def initial_task_count(quote):
    return 1 if quote["continuity_mode"] == "soft_chain" else 2


def test_cancel_quote_idempotent_preserves_assets_and_blocks_late_confirm(client, pending_quote):
    token, quote, _ = pending_quote
    assert quote["quote_recovery"]["can_cancel_quote"]
    response = cancel(client, token, quote)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"
    assert cancel(client, token, quote).status_code == 200
    assert response.json()["items"][0]["segments"]
    confirmed = client.post(f"/api/workbench/h3-batches/{quote['batch_id']}/confirm",
                            json={"access_token": token, "cost_confirmed": True})
    assert confirmed.status_code == 400
    assert "撤销" in confirmed.json()["detail"]
    with SessionLocal() as db:
        assert db.query(GenerationTask).count() == 0


def test_cancel_rejects_wrong_token_owner_and_missing_confirmation(client, pending_quote):
    token, quote, _ = pending_quote
    assert cancel(client, token, quote, quote_token="wrong").status_code == 400
    assert cancel(client, token, quote, cancel_quote_confirmed=False).status_code == 409
    create_user("quote-intruder", h3_access_enabled=True)
    other = _token(client, "quote-intruder")
    rejected = cancel(client, other, quote)
    assert rejected.status_code == 400 and "不存在" in rejected.json()["detail"]
    with SessionLocal() as db:
        assert db.get(GenerationBatch, quote["batch_id"]).status == "AWAITING_COST_CONFIRMATION"


def test_confirmed_task_is_not_quote_cancellable(client, pending_quote):
    token, quote, _ = pending_quote
    response = client.post(f"/api/workbench/h3-batches/{quote['batch_id']}/confirm",
                           json={"access_token": token, "cost_confirmed": True})
    assert response.status_code == 200, response.text
    assert not response.json()["quote_recovery"]["can_cancel_quote"]
    rejected = cancel(client, token, quote)
    assert rejected.status_code == 400 and "不能" in rejected.json()["detail"]
    with SessionLocal() as db:
        assert db.query(GenerationTask).count() == initial_task_count(quote)


def test_parallel_confirm_and_cancel_have_only_one_winner(client, pending_quote):
    _, quote, username = pending_quote
    barrier = Barrier(2)
    def action(kind):
        with SessionLocal() as db:
            user = db.query(User).filter_by(username=username).one()
            barrier.wait(timeout=10)
            try:
                if kind == "confirm":
                    batch = confirm_h3_workbench_batch(db, user, quote["batch_id"], cost_confirmed=True)
                else:
                    batch = cancel_h3_workbench_quote(db, user, quote["batch_id"], request_key="race",
                        token=quote["quote_recovery"]["quote_token"])
                return batch.status
            except H3WorkbenchError:
                db.rollback()
                return "REJECTED"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(action, ["confirm", "cancel"]))
    assert outcomes.count("REJECTED") == 1
    with SessionLocal() as db:
        batch = db.get(GenerationBatch, quote["batch_id"])
        assert db.query(GenerationTask).count() == (initial_task_count(quote) if batch.status == "ACTIVE" else 0)


def test_parallel_confirm_is_idempotent(client, pending_quote):
    _, quote, username = pending_quote
    barrier = Barrier(2)
    def confirm_once(_):
        with SessionLocal() as db:
            user = db.query(User).filter_by(username=username).one()
            barrier.wait(timeout=10)
            return confirm_h3_workbench_batch(db, user, quote["batch_id"], cost_confirmed=True).status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(confirm_once, range(2))) == ["ACTIVE", "ACTIVE"]
    with SessionLocal() as db:
        assert db.query(GenerationTask).count() == initial_task_count(quote)


def test_nonpending_segment_prevents_quote_cancellation(client, pending_quote):
    token, quote, _ = pending_quote
    with SessionLocal() as db:
        segment = db.query(GenerationSegment).first()
        segment.status = "TASK_CREATED"
        db.commit()
    rejected = cancel(client, token, quote)
    assert rejected.status_code == 400
    with SessionLocal() as db:
        assert db.get(GenerationBatch, quote["batch_id"]).status == "AWAITING_COST_CONFIRMATION"
        assert db.query(GenerationSegment).first().status == "TASK_CREATED"
