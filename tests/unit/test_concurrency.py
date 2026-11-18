"""Lock-ordering regression tests for the executor <-> task-manager boundary.

A worker completes job futures while holding the serial executor lock, and the
task manager registers a done-callback which takes the manager lock. The task
manager must therefore never call ``executor.submit`` while holding its own
lock: that would be an ABBA deadlock.
"""

from __future__ import annotations

import time
from pathlib import Path

from test_api_support import running

from nextautodesk.models import TaskStepSpec


def _lock_held(lock) -> bool:
    """Return whether an RLock is held, on Python 3.12+.

    ``threading.RLock.locked()`` only exists on Python 3.14; CI runs 3.12, so
    fall back to CPython's ``_is_owned()`` for the same-thread case used here.
    """

    try:
        return lock.locked()
    except AttributeError:  # pragma: no cover - Python < 3.14
        return lock._is_owned()


def _wait_terminal(env, task_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = env.task_store.get(task_id)
        if record is not None and record.is_terminal:
            return
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not finish within {timeout}s")


def test_submit_calls_executor_without_holding_manager_lock(tmp_path: Path, monkeypatch) -> None:
    with running(tmp_path) as env:
        executor = env.service.executor
        manager = env.manager
        observations: list[dict[str, bool]] = []
        original_submit = executor.submit

        def checking_submit(*args, **kwargs):
            observations.append(
                {
                    "manager_lock_held": _lock_held(manager._lock),
                    "cancel_event_published": any(manager._cancel_events.values()),
                }
            )
            return original_submit(*args, **kwargs)

        monkeypatch.setattr(executor, "submit", checking_submit)
        record = manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1})]
        )
        _wait_terminal(env, record.id)

        assert observations, "executor.submit was never called"
        assert observations[0]["manager_lock_held"] is False
        # The cancellation handle must be published before queueing, closing the
        # window where cancel() would find neither a record future nor an event.
        assert observations[0]["cancel_event_published"] is True


def test_cancel_immediately_after_submit_is_not_lost(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        env.handler("mouse.move").delay_s = 5.0
        first = env.manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1})]
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if env.task_store.get(first.id).status == "running":
                break
            time.sleep(0.01)
        second = env.manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 2, "y": 2})]
        )
        started = time.monotonic()
        cancelled = env.manager.cancel(second.id)
        elapsed = time.monotonic() - started
        assert cancelled.status == "cancelled"
        assert elapsed < 1.0, f"queued cancellation took {elapsed:.2f}s"
        env.manager.cancel(first.id)
