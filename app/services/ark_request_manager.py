from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
import threading
import time
from typing import Callable, Generic, TypeVar


T = TypeVar("T")
ArkRunner = Callable[[float, float], T]
ARK_HARD_CONCURRENCY_LIMIT = 10


class ArkRequestManagerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class ArkQueueFull(ArkRequestManagerError):
    def __init__(self) -> None:
        super().__init__(
            "ARK_QUEUE_FULL",
            "豆包请求队列已满，请稍后重试",
            status_code=429,
            retry_after_seconds=5,
        )


class ArkQueueWaitTimeout(ArkRequestManagerError):
    def __init__(self) -> None:
        super().__init__(
            "ARK_QUEUE_WAIT_TIMEOUT",
            "豆包请求排队预算已耗尽",
            status_code=504,
        )


class ArkTotalDeadlineExceeded(ArkRequestManagerError):
    def __init__(self) -> None:
        super().__init__(
            "ARK_TOTAL_DEADLINE_EXCEEDED",
            "豆包请求总预算已耗尽",
            status_code=504,
        )


class ArkCircuitOpen(ArkRequestManagerError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "ARK_CIRCUIT_OPEN",
            "豆包服务暂时熔断，请稍后重试",
            status_code=503,
            retry_after_seconds=max(1, int(retry_after_seconds)),
        )


class ArkManagerShuttingDown(ArkRequestManagerError):
    def __init__(self) -> None:
        super().__init__(
            "ARK_MANAGER_SHUTTING_DOWN",
            "豆包请求管理器正在关闭",
            status_code=503,
            retry_after_seconds=5,
        )


class _ArkRetryableResultFailure(RuntimeError):
    retryable = True


@dataclass
class _ArkJob(Generic[T]):
    operation_id: str
    business_key: str
    kind: str
    circuit_key: str
    runner: ArkRunner[T]
    future: Future[T]
    created_at: float
    queue_deadline: float
    total_deadline: float
    started_at: float | None = None
    completed_at: float | None = None


@dataclass
class _CircuitState:
    failures: deque[float] = field(default_factory=deque)
    open_until: float = 0.0
    half_open_active: bool = False


class ArkRequestManager:
    """Dedicated, fair queue for every paid Ark operation in one Web process."""

    def __init__(
        self,
        *,
        max_concurrency: int = ARK_HARD_CONCURRENCY_LIMIT,
        queue_max: int = 200,
        completed_retention_seconds: float = 600.0,
        circuit_failure_threshold: int = 3,
        circuit_window_seconds: float = 60.0,
        circuit_cooldown_seconds: float = 30.0,
    ) -> None:
        if type(max_concurrency) is not int or not (
            1 <= max_concurrency <= ARK_HARD_CONCURRENCY_LIMIT
        ):
            raise ValueError("Ark 并发上限必须为 1-10")
        if type(queue_max) is not int or queue_max < 1:
            raise ValueError("Ark 等待队列上限必须为正整数")
        self.max_concurrency = max_concurrency
        self.queue_max = queue_max
        self.completed_retention_seconds = max(
            0.0, float(completed_retention_seconds)
        )
        self.circuit_failure_threshold = max(1, int(circuit_failure_threshold))
        self.circuit_window_seconds = max(1.0, float(circuit_window_seconds))
        self.circuit_cooldown_seconds = max(1.0, float(circuit_cooldown_seconds))
        self._condition = threading.Condition()
        self._queues: dict[str, deque[_ArkJob[object]]] = {}
        self._kind_order: deque[str] = deque()
        self._by_operation: dict[str, _ArkJob[object]] = {}
        self._by_business: dict[str, _ArkJob[object]] = {}
        self._circuits: dict[str, _CircuitState] = {}
        self._active = 0
        self._accepting = True
        self._stopping = False
        self._queue_wait_samples: deque[float] = deque(maxlen=1000)
        self._total_samples: deque[float] = deque(maxlen=1000)
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._deduplicated = 0
        self._threads: list[threading.Thread] = []
        for index in range(self.max_concurrency):
            thread = threading.Thread(
                target=self._worker,
                name=f"ark-request-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def submit(
        self,
        *,
        operation_id: str,
        business_key: str,
        kind: str,
        circuit_key: str,
        runner: ArkRunner[T],
        total_timeout_seconds: float,
        queue_timeout_seconds: float,
    ) -> Future[T]:
        clean_operation = str(operation_id or "").strip()
        clean_business = str(business_key or "").strip()
        clean_kind = str(kind or "").strip()
        clean_circuit = str(circuit_key or "").strip()
        if not clean_operation or not clean_business or not clean_kind:
            raise ValueError("Ark 请求身份不完整")
        total_timeout = float(total_timeout_seconds)
        queue_timeout = float(queue_timeout_seconds)
        if total_timeout <= 0 or queue_timeout <= 0:
            raise ValueError("Ark 请求预算必须大于 0")
        now = time.monotonic()
        with self._condition:
            self._expire_completed(now)
            if not self._accepting:
                raise ArkManagerShuttingDown()
            duplicate = self._by_operation.get(clean_operation)
            if duplicate is None:
                duplicate = self._by_business.get(clean_business)
            if duplicate is not None:
                self._deduplicated += 1
                return duplicate.future  # type: ignore[return-value]
            if self._queued_count() >= self.queue_max:
                raise ArkQueueFull()
            circuit = self._circuits.get(clean_circuit)
            if circuit is not None and circuit.open_until > now:
                raise ArkCircuitOpen(round(circuit.open_until - now))
            future: Future[T] = Future()
            job: _ArkJob[T] = _ArkJob(
                operation_id=clean_operation,
                business_key=clean_business,
                kind=clean_kind,
                circuit_key=clean_circuit,
                runner=runner,
                future=future,
                created_at=now,
                queue_deadline=now + min(queue_timeout, total_timeout),
                total_deadline=now + total_timeout,
            )
            queue = self._queues.get(clean_kind)
            if queue is None:
                queue = deque()
                self._queues[clean_kind] = queue
                self._kind_order.append(clean_kind)
            queue.append(job)  # type: ignore[arg-type]
            self._by_operation[clean_operation] = job  # type: ignore[assignment]
            self._by_business[clean_business] = job  # type: ignore[assignment]
            self._submitted += 1
            self._condition.notify_all()
            return future

    def diagnostics(self) -> dict[str, object]:
        with self._condition:
            now = time.monotonic()
            queued_jobs = [job for queue in self._queues.values() for job in queue]
            return {
                "schema": "runninghub.ark-request-manager-health.v1",
                "accepting": self._accepting,
                "active_count": self._active,
                "queued_count": len(queued_jobs),
                "queue_max": self.queue_max,
                "hard_limit": self.max_concurrency,
                "oldest_queued_seconds": (
                    round(max(now - job.created_at for job in queued_jobs), 3)
                    if queued_jobs
                    else 0.0
                ),
                "queue_wait_p95_seconds": self._percentile(
                    self._queue_wait_samples, 0.95
                ),
                "queue_wait_p99_seconds": self._percentile(
                    self._queue_wait_samples, 0.99
                ),
                "total_p95_seconds": self._percentile(self._total_samples, 0.95),
                "total_p99_seconds": self._percentile(self._total_samples, 0.99),
                "submitted_count": self._submitted,
                "completed_count": self._completed,
                "failed_count": self._failed,
                "deduplicated_count": self._deduplicated,
                "queued_by_kind": {
                    kind: len(queue) for kind, queue in self._queues.items() if queue
                },
                "circuit_open_keys": sorted(
                    key
                    for key, state in self._circuits.items()
                    if state.open_until > now
                ),
            }

    def shutdown(self, *, wait_seconds: float = 10.0) -> None:
        with self._condition:
            self._accepting = False
            self._stopping = True
            for queue in self._queues.values():
                while queue:
                    job = queue.popleft()
                    if not job.future.done():
                        job.future.set_exception(ArkManagerShuttingDown())
            self._queues.clear()
            self._kind_order.clear()
            self._condition.notify_all()
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._stopping and not self._kind_order:
                    self._condition.wait()
                if self._stopping:
                    return
                job = self._next_job()
                if job is None:
                    self._condition.wait(timeout=0.1)
                    continue
                now = time.monotonic()
                if now >= job.queue_deadline:
                    self._finish_error(job, ArkQueueWaitTimeout(), now)
                    continue
                if now >= job.total_deadline:
                    self._finish_error(job, ArkTotalDeadlineExceeded(), now)
                    continue
                job.started_at = now
                self._active += 1
                self._queue_wait_samples.append(now - job.created_at)
            try:
                remaining = job.total_deadline - time.monotonic()
                if remaining <= 0:
                    raise ArkTotalDeadlineExceeded()
                result = job.runner(remaining, (job.started_at or 0.0) - job.created_at)
                if time.monotonic() > job.total_deadline:
                    raise ArkTotalDeadlineExceeded()
            except BaseException as exc:
                with self._condition:
                    self._record_failure(job.circuit_key, exc)
                    self._finish_error(job, exc, time.monotonic(), active=True)
            else:
                with self._condition:
                    retryable_code = self._retryable_result_code(result)
                    if retryable_code is None:
                        self._record_success(job.circuit_key)
                    else:
                        self._record_failure(
                            job.circuit_key,
                            _ArkRetryableResultFailure(retryable_code),
                        )
                    self._finish_success(job, result, time.monotonic())

    def _next_job(self) -> _ArkJob[object] | None:
        checks = len(self._kind_order)
        now = time.monotonic()
        for _ in range(checks):
            kind = self._kind_order.popleft()
            queue = self._queues.get(kind)
            if not queue:
                self._queues.pop(kind, None)
                continue
            job = queue.popleft()
            if queue:
                self._kind_order.append(kind)
            else:
                self._queues.pop(kind, None)
            circuit = self._circuits.get(job.circuit_key)
            if circuit is not None:
                if circuit.open_until > now:
                    self._requeue(job)
                    continue
                if circuit.open_until > 0:
                    if circuit.half_open_active:
                        self._requeue(job)
                        continue
                    circuit.half_open_active = True
            return job
        return None

    def _requeue(self, job: _ArkJob[object]) -> None:
        queue = self._queues.get(job.kind)
        if queue is None:
            queue = deque()
            self._queues[job.kind] = queue
            self._kind_order.append(job.kind)
        queue.append(job)

    def _finish_success(self, job: _ArkJob[object], result: object, now: float) -> None:
        job.completed_at = now
        self._active = max(0, self._active - 1)
        self._completed += 1
        self._total_samples.append(now - job.created_at)
        if not job.future.done():
            job.future.set_result(result)
        self._condition.notify_all()

    def _finish_error(
        self,
        job: _ArkJob[object],
        exc: BaseException,
        now: float,
        *,
        active: bool = False,
    ) -> None:
        job.completed_at = now
        if active:
            self._active = max(0, self._active - 1)
        self._failed += 1
        self._total_samples.append(now - job.created_at)
        if not job.future.done():
            job.future.set_exception(exc)
        self._condition.notify_all()

    def _record_success(self, key: str) -> None:
        if not key:
            return
        state = self._circuits.get(key)
        if state is not None:
            state.failures.clear()
            state.open_until = 0.0
            state.half_open_active = False

    def _record_failure(self, key: str, exc: BaseException) -> None:
        if not key or not bool(getattr(exc, "retryable", False)):
            state = self._circuits.get(key)
            if state is not None:
                state.half_open_active = False
            return
        now = time.monotonic()
        state = self._circuits.setdefault(key, _CircuitState())
        while state.failures and now - state.failures[0] > self.circuit_window_seconds:
            state.failures.popleft()
        state.failures.append(now)
        half_open_failed = state.half_open_active
        state.half_open_active = False
        if half_open_failed or len(state.failures) >= self.circuit_failure_threshold:
            state.open_until = now + self.circuit_cooldown_seconds

    def _expire_completed(self, now: float) -> None:
        expired = [
            operation_id
            for operation_id, job in self._by_operation.items()
            if job.completed_at is not None
            and now - job.completed_at >= self.completed_retention_seconds
        ]
        for operation_id in expired:
            job = self._by_operation.pop(operation_id, None)
            if job is not None and self._by_business.get(job.business_key) is job:
                self._by_business.pop(job.business_key, None)

    def _queued_count(self) -> int:
        return sum(len(queue) for queue in self._queues.values())

    @staticmethod
    def _retryable_result_code(result: object) -> str | None:
        if not isinstance(result, dict):
            return None
        candidates: list[object] = []
        error = result.get("error")
        if isinstance(error, dict):
            candidates.append(error.get("code"))
        errors = result.get("errors")
        if isinstance(errors, dict):
            for branch_error in errors.values():
                if isinstance(branch_error, dict):
                    candidates.append(branch_error.get("code"))
        retryable_prefixes = (
            "ARK_TIMEOUT",
            "ARK_CONNECTION",
            "ARK_UPSTREAM",
            "ARK_RATE_LIMIT",
            "ARK_QUEUE",
            "ARK_TOTAL_DEADLINE",
        )
        for raw in candidates:
            code = str(raw or "").strip().upper()
            if code.startswith(retryable_prefixes):
                return code
        return None

    @staticmethod
    def _percentile(values: deque[float], quantile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
        return round(ordered[index], 3)
