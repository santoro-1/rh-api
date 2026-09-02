from __future__ import annotations

from concurrent.futures import wait
import threading
import time

import pytest

from app.services.ark_request_manager import (
    ArkCircuitOpen,
    ArkQueueFull,
    ArkQueueWaitTimeout,
    ArkRequestManager,
)


def test_manager_hard_caps_one_hundred_jobs_at_ten() -> None:
    manager = ArkRequestManager(max_concurrency=10, queue_max=100)
    gate = threading.Event()
    guard = threading.Lock()
    active = 0
    peak = 0

    def runner(_remaining: float, _waited: float) -> int:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        gate.wait(3)
        with guard:
            active -= 1
        return 1

    try:
        futures = [
            manager.submit(
                operation_id=f"op-{index}",
                business_key=f"key-{index}",
                kind="content",
                circuit_key="account",
                runner=runner,
                total_timeout_seconds=10,
                queue_timeout_seconds=8,
            )
            for index in range(100)
        ]
        deadline = time.monotonic() + 2
        while peak < 10 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert peak == 10
        assert manager.diagnostics()["active_count"] == 10
        gate.set()
        done, pending = wait(futures, timeout=5)
        assert len(done) == 100
        assert not pending
    finally:
        gate.set()
        manager.shutdown()


def test_manager_deduplicates_operation_and_business_key() -> None:
    manager = ArkRequestManager(max_concurrency=1, queue_max=5)
    gate = threading.Event()
    calls = 0

    def runner(_remaining: float, _waited: float) -> str:
        nonlocal calls
        calls += 1
        gate.wait(2)
        return "ok"

    try:
        first = manager.submit(
            operation_id="op-1",
            business_key="same-business",
            kind="content",
            circuit_key="account",
            runner=runner,
            total_timeout_seconds=5,
            queue_timeout_seconds=4,
        )
        same_operation = manager.submit(
            operation_id="op-1",
            business_key="different-business",
            kind="content",
            circuit_key="account",
            runner=runner,
            total_timeout_seconds=5,
            queue_timeout_seconds=4,
        )
        same_business = manager.submit(
            operation_id="op-2",
            business_key="same-business",
            kind="visual",
            circuit_key="account",
            runner=runner,
            total_timeout_seconds=5,
            queue_timeout_seconds=4,
        )
        assert first is same_operation is same_business
        gate.set()
        assert first.result(timeout=3) == "ok"
        assert calls == 1
        assert manager.diagnostics()["deduplicated_count"] == 2
    finally:
        gate.set()
        manager.shutdown()


def test_manager_round_robins_content_and_visual_jobs() -> None:
    manager = ArkRequestManager(max_concurrency=1, queue_max=10)
    gate = threading.Event()
    order: list[str] = []

    def runner(name: str, block: bool = False):
        def execute(_remaining: float, _waited: float) -> str:
            order.append(name)
            if block:
                gate.wait(2)
            return name

        return execute

    try:
        futures = [
            manager.submit(
                operation_id="c1",
                business_key="c1",
                kind="content",
                circuit_key="account",
                runner=runner("c1", True),
                total_timeout_seconds=5,
                queue_timeout_seconds=4,
            )
        ]
        deadline = time.monotonic() + 2
        while order != ["c1"] and time.monotonic() < deadline:
            time.sleep(0.01)
        for name, kind in (("c2", "content"), ("c3", "content"), ("v1", "visual")):
            futures.append(
                manager.submit(
                    operation_id=name,
                    business_key=name,
                    kind=kind,
                    circuit_key="account",
                    runner=runner(name),
                    total_timeout_seconds=5,
                    queue_timeout_seconds=4,
                )
            )
        gate.set()
        wait(futures, timeout=4)
        assert order.index("v1") < order.index("c3")
    finally:
        gate.set()
        manager.shutdown()


def test_queue_is_bounded_and_wait_budget_is_enforced() -> None:
    manager = ArkRequestManager(max_concurrency=1, queue_max=1)
    gate = threading.Event()
    try:
        manager.submit(
            operation_id="active",
            business_key="active",
            kind="content",
            circuit_key="account",
            runner=lambda *_: gate.wait(2),
            total_timeout_seconds=5,
            queue_timeout_seconds=4,
        )
        deadline = time.monotonic() + 2
        while manager.diagnostics()["active_count"] != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        waiting = manager.submit(
            operation_id="waiting",
            business_key="waiting",
            kind="content",
            circuit_key="account",
            runner=lambda *_: None,
            total_timeout_seconds=1,
            queue_timeout_seconds=0.05,
        )
        with pytest.raises(ArkQueueFull):
            manager.submit(
                operation_id="overflow",
                business_key="overflow",
                kind="content",
                circuit_key="account",
                runner=lambda *_: None,
                total_timeout_seconds=1,
                queue_timeout_seconds=1,
            )
        time.sleep(0.08)
        gate.set()
        with pytest.raises(ArkQueueWaitTimeout):
            waiting.result(timeout=2)
    finally:
        gate.set()
        manager.shutdown()


def test_retryable_failures_open_circuit() -> None:
    class TemporaryError(RuntimeError):
        retryable = True

    manager = ArkRequestManager(
        max_concurrency=1,
        queue_max=5,
        circuit_failure_threshold=2,
        circuit_cooldown_seconds=2,
    )
    try:
        for index in range(2):
            future = manager.submit(
                operation_id=f"failure-{index}",
                business_key=f"failure-{index}",
                kind="content",
                circuit_key="account",
                runner=lambda *_: (_ for _ in ()).throw(TemporaryError("offline")),
                total_timeout_seconds=2,
                queue_timeout_seconds=1,
            )
            with pytest.raises(TemporaryError):
                future.result(timeout=2)
        with pytest.raises(ArkCircuitOpen):
            manager.submit(
                operation_id="blocked",
                business_key="blocked",
                kind="content",
                circuit_key="account",
                runner=lambda *_: None,
                total_timeout_seconds=2,
                queue_timeout_seconds=1,
            )
    finally:
        manager.shutdown()


def test_degraded_business_result_still_counts_toward_circuit() -> None:
    manager = ArkRequestManager(
        max_concurrency=1,
        queue_max=5,
        circuit_failure_threshold=2,
        circuit_cooldown_seconds=2,
    )
    degraded = {
        "overall_status": "PARTIAL",
        "errors": {
            "music": {"code": "ARK_TIMEOUT", "summary": "timeout"},
            "subtitle": None,
        },
    }
    try:
        for index in range(2):
            future = manager.submit(
                operation_id=f"degraded-{index}",
                business_key=f"degraded-{index}",
                kind="content",
                circuit_key="account",
                runner=lambda *_: degraded,
                total_timeout_seconds=2,
                queue_timeout_seconds=1,
            )
            assert future.result(timeout=2) == degraded
        with pytest.raises(ArkCircuitOpen):
            manager.submit(
                operation_id="blocked-after-degraded",
                business_key="blocked-after-degraded",
                kind="content",
                circuit_key="account",
                runner=lambda *_: None,
                total_timeout_seconds=2,
                queue_timeout_seconds=1,
            )
    finally:
        manager.shutdown()
