"""Unit tests for the serial desktop executor."""

from __future__ import annotations

import threading
import time

import pytest

from nextautodesk.errors import QueueFull, TaskCancelled, TaskTimeoutError
from nextautodesk.execution import JobState, SerialExecutor


@pytest.fixture()
def executor():
    ex = SerialExecutor(max_queue_size=8, name="test")
    ex.start()
    try:
        yield ex
    finally:
        ex.shutdown(grace_s=5)


def test_jobs_run_in_fifo_order(executor):
    order: list[int] = []
    lock = threading.Lock()

    def make(i: int):
        def fn(cancel: threading.Event):
            with lock:
                order.append(i)
            return i

        return fn

    jobs = [executor.submit(f"job-{i}", make(i)) for i in range(5)]
    results = [job.future.result(timeout=5) for job in jobs]
    assert results == [0, 1, 2, 3, 4]
    assert order == [0, 1, 2, 3, 4]


def test_timeout_cancels_running_job(executor):
    started = threading.Event()
    observed_cancel = threading.Event()

    def fn(cancel: threading.Event):
        started.set()
        # cooperative worker: poll the cancel event
        for _ in range(300):
            if cancel.wait(0.01):
                observed_cancel.set()
                raise TaskCancelled("cancelled by timeout")
        return "finished"

    job = executor.submit("slow", fn, timeout_ms=200)
    assert started.wait(timeout=2)
    with pytest.raises((TaskTimeoutError, TaskCancelled)):
        job.future.result(timeout=5)
    assert observed_cancel.wait(timeout=2)
    assert job.state == JobState.TIMED_OUT
    stats = executor.stats()
    assert stats.timed_out == 1


def test_done_callback_runs_without_executor_lock(executor):
    """Regression: done callbacks must not run while the executor lock is held.

    The worker used to complete the future of a cancelled/abandoned queued job
    under ``self._wake``; a callback that takes any lock the submitter holds (the
    task manager's lock) could then deadlock the executor (ABBA cycle).
    """

    gate = threading.Event()
    started = threading.Event()

    def blocker(cancel: threading.Event):
        started.set()
        gate.wait(timeout=5)
        return "blocked"

    first = executor.submit("blocker", blocker)
    assert started.wait(timeout=2)
    second = executor.submit("abandoned", lambda cancel: "never")
    second.cancel_event.set()
    callback_done = threading.Event()

    def callback(_future):
        # These both take the executor lock; a callback invoked under the lock
        # would block forever and the test would time out.
        executor.stats()
        executor.pending()
        callback_done.set()

    second.future.add_done_callback(callback)
    gate.set()
    first.future.result(timeout=5)
    assert callback_done.wait(timeout=3)
    assert isinstance(second.future.exception(), TaskCancelled)


def test_cancel_queued_job_is_immediate(executor):
    gate = threading.Event()
    started = threading.Event()

    def blocker(cancel: threading.Event):
        started.set()
        gate.wait(timeout=5)
        return "blocked"

    first = executor.submit("blocker", blocker)
    assert started.wait(timeout=2)
    second = executor.submit("queued", lambda cancel: "never")
    started_at = time.monotonic()
    assert executor.cancel(second.id) is True
    elapsed = time.monotonic() - started_at
    # The future is completed synchronously instead of waiting for the worker.
    assert second.state == JobState.CANCELLED
    with pytest.raises(TaskCancelled):
        second.future.result(timeout=0.5)
    assert elapsed < 0.5
    gate.set()
    first.future.result(timeout=5)


def test_cancel_queued_job(executor):
    gate = threading.Event()

    def blocker(cancel: threading.Event):
        gate.wait(timeout=5)
        return "blocked"

    def queued(cancel: threading.Event):  # pragma: no cover - should never run
        return "should-not-run"

    first = executor.submit("blocker", blocker)
    second = executor.submit("queued", queued)
    time.sleep(0.05)
    assert executor.cancel(second.id) is True
    gate.set()
    first.future.result(timeout=5)
    with pytest.raises(TaskCancelled):
        second.future.result(timeout=5)
    assert second.state == JobState.CANCELLED
    assert executor.stats().cancelled >= 1


def test_queue_full_rejected():
    ex = SerialExecutor(max_queue_size=1, name="small")
    ex.start()
    gate = threading.Event()
    try:
        ex.submit("blocker", lambda cancel: gate.wait(timeout=5))
        # the worker may not have picked up the first job yet; wait until it is running
        deadline = time.time() + 2
        while not ex.stats().running and time.time() < deadline:
            time.sleep(0.01)
        ex.submit("second", lambda cancel: None)  # fills the queue
        with pytest.raises(QueueFull):
            ex.submit("third", lambda cancel: None)
    finally:
        gate.set()
        ex.shutdown(grace_s=5)


def test_failure_propagates_and_worker_survives(executor):
    from nextautodesk.errors import ActionFailed

    def boom(cancel: threading.Event):
        raise ActionFailed("nope")

    failing = executor.submit("boom", boom)
    with pytest.raises(ActionFailed):
        failing.future.result(timeout=5)
    ok = executor.submit("ok", lambda cancel: 42)
    assert ok.future.result(timeout=5) == 42
    stats = executor.stats()
    assert stats.completed == 1
    assert stats.failed == 1


def test_shutdown_fails_pending_jobs():
    ex = SerialExecutor(max_queue_size=8, name="shutdown")
    ex.start()

    def blocker(cancel: threading.Event):
        # stops as soon as shutdown requests cancellation
        cancel.wait(timeout=5)
        if cancel.is_set():
            raise TaskCancelled("cancelled by shutdown")
        return "unexpected"

    first = ex.submit("blocker", blocker)
    second = ex.submit("pending", lambda cancel: "never")
    time.sleep(0.05)
    ex.shutdown(grace_s=1)
    with pytest.raises(TaskCancelled):
        first.future.result(timeout=2)
    with pytest.raises(TaskCancelled):
        second.future.result(timeout=2)
    assert not ex.stats().running
    assert ex.stats().state == "stopped"
