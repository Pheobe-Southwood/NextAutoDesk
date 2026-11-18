"""TaskManager unit tests: transitions, failure handling, cancellation, timeouts."""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from test_api_support import make_env, running

from nextautodesk.errors import (
    ActionFailed,
    ExecutorUnavailable,
    QueueFull,
    TaskNotCancellable,
    TaskNotFound,
    UnknownAction,
    ValidationFailed,
)
from nextautodesk.models import TaskRecord, TaskStepSpec, utc_now
from nextautodesk.tasks.manager import TaskManager


def _wait_terminal(env, task_id: str, timeout: float = 5.0) -> TaskRecord:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = env.task_store.get(task_id)
        if record is not None and record.is_terminal:
            return record
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach a terminal state within {timeout}s")


def _wait_until(env, task_id: str, predicate, timeout: float = 3.0) -> TaskRecord:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = env.task_store.get(task_id)
        if record is not None and predicate(record):
            return record
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} never satisfied the predicate")


def _age_task(env, task_id: str, days: int) -> None:
    """Backdate a terminal task so the retention policy considers it old."""

    env.task_store.update_fields(task_id, {"created_at": utc_now() - timedelta(days=days)})


def test_successful_task_runs_steps_in_order(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        record = env.manager.submit(
            [
                TaskStepSpec(action="mouse.move", params={"x": 1, "y": 2}, name="move"),
                TaskStepSpec(
                    action="keyboard.type",
                    params={"text": "hello", "strategy": "direct"},
                ),
            ],
            name="demo",
            metadata={"origin": "test"},
            client_request_id="client-1",
        )
        assert record.status == "queued"
        assert record.steps[0].name == "move"
        final = _wait_terminal(env, record.id)

        assert final.status == "succeeded"
        assert final.error is None
        assert final.started_at is not None and final.finished_at is not None
        assert final.duration_ms is not None and final.duration_ms >= 0
        assert [step.status for step in final.step_results] == ["succeeded", "succeeded"]
        assert final.step_results[0].output["action"] == "mouse.move"
        assert final.metadata == {"origin": "test"}
        assert final.client_request_id == "client-1"
        assert env.recorder.names() == ["mouse.move", "keyboard.type"]
        # the strategy override is stripped from the action parameters
        assert "strategy" not in env.recorder.params_for("keyboard.type")[0]
        # and the record is readable through the manager API
        assert env.manager.get(record.id).status == "succeeded"


def test_failure_stops_task_and_marks_remaining_steps_skipped(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        env.handler("keyboard.type").error = ActionFailed("typing exploded")
        record = env.manager.submit(
            [
                TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1}),
                TaskStepSpec(action="keyboard.type", params={"text": "boom"}),
                TaskStepSpec(action="mouse.move", params={"x": 2, "y": 2}),
            ]
        )
        final = _wait_terminal(env, record.id)

        assert final.status == "failed"
        assert final.error is not None
        assert final.error["code"] == "ACTION_FAILED"
        assert final.error["step_index"] == 1
        statuses = [step.status for step in final.step_results]
        assert statuses == ["succeeded", "failed", "skipped"]
        assert final.step_results[1].error["code"] == "ACTION_FAILED"
        # the third step never ran
        assert env.recorder.names().count("mouse.move") == 1


def test_continue_on_error_lets_task_succeed(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        env.handler("keyboard.type").error = ActionFailed("typing exploded")
        record = env.manager.submit(
            [
                TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1}),
                TaskStepSpec(
                    action="keyboard.type",
                    params={"text": "boom"},
                    continue_on_error=True,
                ),
                TaskStepSpec(action="mouse.move", params={"x": 2, "y": 2}),
            ]
        )
        final = _wait_terminal(env, record.id)

        assert final.status == "succeeded"
        assert [step.status for step in final.step_results] == [
            "succeeded",
            "failed",
            "succeeded",
        ]
        assert env.recorder.names() == ["mouse.move", "keyboard.type", "mouse.move"]


def test_step_timeout_fails_the_task_with_action_timeout(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        env.handler("mouse.move").delay_s = 5.0
        record = env.manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1}, timeout_ms=100)]
        )
        final = _wait_terminal(env, record.id)

        assert final.status == "failed"
        assert final.error is not None
        assert final.error["code"] == "ACTION_TIMEOUT"
        assert final.step_results[0].status == "failed"
        assert final.step_results[0].error["code"] == "ACTION_TIMEOUT"
        # the action was aborted well before its 5s delay
        assert final.duration_ms is not None and final.duration_ms < 3000


def test_whole_task_timeout_marks_task_timed_out(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        env.handler("mouse.move").delay_s = 5.0
        record = env.manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1})],
            timeout_ms=200,
        )
        final = _wait_terminal(env, record.id)

        assert final.status == "timed_out"
        assert final.error is not None
        assert final.error["code"] == "TASK_TIMEOUT"
        assert final.duration_ms is not None and final.duration_ms < 3000


def test_cancel_running_task(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        env.handler("mouse.move").delay_s = 5.0
        record = env.manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1})]
        )
        _wait_until(env, record.id, lambda item: item.status == "running")
        cancelled = env.manager.cancel(record.id)

        assert cancelled.status == "cancelled"
        assert cancelled.cancel_requested_at is not None
        assert cancelled.step_results[0].status == "cancelled"


def test_cancel_queued_task_before_it_starts(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        env.handler("keyboard.type").delay_s = 0.5
        first = env.manager.submit(
            [TaskStepSpec(action="keyboard.type", params={"text": "slow"})]
        )
        _wait_until(env, first.id, lambda item: item.status == "running")
        second = env.manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 9, "y": 9})]
        )
        cancelled = env.manager.cancel(second.id)
        first_final = _wait_terminal(env, first.id)

        assert cancelled.status == "cancelled"
        assert cancelled.started_at is None
        assert first_final.status == "succeeded"


def test_cancel_finished_task_is_rejected(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        record = env.manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1})]
        )
        _wait_terminal(env, record.id)
        with pytest.raises(TaskNotCancellable):
            env.manager.cancel(record.id)


def test_step_results_are_persisted_incrementally(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        env.handler("mouse.move").delay_s = 0.0
        env.handler("keyboard.type").delay_s = 0.6
        record = env.manager.submit(
            [
                TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1}),
                TaskStepSpec(action="keyboard.type", params={"text": "slow"}),
            ]
        )
        partial = _wait_until(
            env, record.id, lambda item: item.status == "running" and len(item.step_results) == 1
        )
        assert partial.step_results[0].status == "succeeded"
        assert len(partial.step_results) == 1
        _wait_terminal(env, record.id)


def test_validation_errors(tmp_path: Path) -> None:
    with running(tmp_path, max_steps=2) as env:
        with pytest.raises(ValidationFailed):
            env.manager.submit([])
        with pytest.raises(ValidationFailed):
            env.manager.submit(
                [
                    TaskStepSpec(action="mouse.move"),
                    TaskStepSpec(action="mouse.move"),
                    TaskStepSpec(action="mouse.move"),
                ]
            )
        with pytest.raises(ValidationFailed):
            env.manager.submit([TaskStepSpec(action="mouse.move")], timeout_ms=0)
        with pytest.raises(UnknownAction):
            env.manager.submit([TaskStepSpec(action="does.not.exist")])


def test_unknown_task_lookup_raises(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        with pytest.raises(TaskNotFound):
            env.manager.get("missing-task")
        with pytest.raises(TaskNotFound):
            env.manager.cancel("missing-task")


def test_submit_fails_when_executor_is_not_running(tmp_path: Path) -> None:
    env = make_env(tmp_path)  # service (and thus the executor) never started
    try:
        with pytest.raises(ExecutorUnavailable):
            env.manager.submit([TaskStepSpec(action="mouse.move")])
        latest = env.task_store.latest()
        assert latest is not None
        assert latest.status == "failed"
        assert latest.error["code"] == "EXECUTOR_UNAVAILABLE"
    finally:
        env.close()


def test_queue_full_is_reported_and_recorded(tmp_path: Path) -> None:
    with running(tmp_path, max_queue_size=1) as env:
        env.handler("keyboard.type").delay_s = 0.4
        first = env.manager.submit(
            [TaskStepSpec(action="keyboard.type", params={"text": "slow"})]
        )
        _wait_until(env, first.id, lambda item: item.status == "running")
        env.manager.submit([TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1})])
        with pytest.raises(QueueFull):
            env.manager.submit([TaskStepSpec(action="mouse.move", params={"x": 2, "y": 2})])
        latest = env.task_store.latest()
        assert latest is not None
        assert latest.status == "failed"
        assert latest.error["code"] == "QUEUE_FULL"
        _wait_terminal(env, first.id)


def test_recover_marks_interrupted_tasks(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    try:
        now = utc_now()
        env.task_store.insert(
            TaskRecord(id="running-task", steps=[], status="running", created_at=now, queued_at=now)
        )
        env.task_store.insert(
            TaskRecord(id="queued-task", steps=[], status="queued", created_at=now, queued_at=now)
        )
        env.task_store.insert(
            TaskRecord(id="done-task", steps=[], status="succeeded", created_at=now, queued_at=now)
        )
        assert env.manager.recover() == 2
        assert env.task_store.get("running-task").status == "interrupted"  # type: ignore[union-attr]
        assert env.task_store.get("queued-task").status == "interrupted"  # type: ignore[union-attr]
        assert env.task_store.get("done-task").status == "succeeded"  # type: ignore[union-attr]
    finally:
        env.close()


def test_manager_list_and_status_filter(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        first = env.manager.submit([TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1})])
        _wait_terminal(env, first.id)
        second = env.manager.submit([TaskStepSpec(action="mouse.move", params={"x": 2, "y": 2})])
        _wait_terminal(env, second.id)

        items, total = env.manager.list()
        assert total == 2
        assert len(items) == 2
        succeeded, succeeded_total = env.manager.list(status="succeeded", limit=1, offset=0)
        assert succeeded_total == 2
        assert len(succeeded) == 1


def test_executor_counters_track_failed_and_cancelled_tasks(tmp_path: Path) -> None:
    """Manager-level check of the executor counter fixes (failed/cancelled)."""

    with running(tmp_path) as env:
        # a task-level failure is recorded in the task store; the *executor*
        # failed counter increments when a queued job itself raises.
        def boom(cancel: threading.Event) -> None:
            raise ActionFailed("executor-level failure")

        broken_job = env.service.executor.submit("counter-boom", boom)
        with pytest.raises(ActionFailed):
            broken_job.future.result(timeout=5)

        env.handler("mouse.move").delay_s = 2.0
        blocker = env.manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 3, "y": 4})]
        )
        _wait_until(env, blocker.id, lambda record: record.status == "running")
        queued = env.manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 5, "y": 6})]
        )
        started = time.monotonic()
        cancelled = env.manager.cancel(queued.id)
        queue_cancel_elapsed = time.monotonic() - started

        stats = env.service.executor.stats()
        assert stats.failed >= 1
        assert stats.cancelled >= 1
        # queued cancellation completes immediately instead of waiting for the worker
        assert cancelled.status == "cancelled"
        assert queue_cancel_elapsed < 1.0

        env.manager.cancel(blocker.id)
        _wait_terminal(env, blocker.id)


def test_periodic_retention_cleanup_runs_and_stops(tmp_path: Path) -> None:
    with running(tmp_path) as env:
        old = env.manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 1, "y": 1})]
        )
        assert _wait_terminal(env, old.id).status == "succeeded"
        _age_task(env, old.id, days=30)

        cleanup_manager = TaskManager(
            service=env.service,
            store=env.task_store,
            config=env.config,
            cleanup_interval_s=0.05,
        )
        cleanup_manager.start()
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and env.task_store.get(old.id) is not None:
                time.sleep(0.02)
            assert env.task_store.get(old.id) is None
        finally:
            cleanup_manager.shutdown()
        assert cleanup_manager._cleanup_thread is None

        # once stopped, the background thread must not delete anything
        fresh = env.manager.submit(
            [TaskStepSpec(action="mouse.move", params={"x": 2, "y": 2})]
        )
        assert _wait_terminal(env, fresh.id).status == "succeeded"
        _age_task(env, fresh.id, days=30)
        time.sleep(0.2)
        assert env.task_store.get(fresh.id) is not None


def test_binary_and_oversized_step_outputs_are_sanitised_before_persistence(
    tmp_path: Path,
) -> None:
    with running(tmp_path) as env:
        env.handler("screen.screenshot").output = {
            "format": "png",
            "saved": False,
            "image_base64": "A" * 20_000,
            "screenshot": {"name": "screen-1.png"},
            "note": "B" * 10_000,
        }
        record = env.manager.submit(
            [TaskStepSpec(action="screen.screenshot", params={})]
        )
        final = _wait_terminal(env, record.id)

    assert final.status == "succeeded"
    output = final.step_results[0].output
    assert "image_base64" not in output
    assert output["image_base64_omitted"] is True
    assert output["image_base64_chars"] == 20_000
    # smaller sibling fields (e.g. the saved screenshot name) survive
    assert output["screenshot"] == {"name": "screen-1.png"}
    assert output["note"] == "<omitted 10000 chars>"
    assert len(json.dumps(output)) < 1000
