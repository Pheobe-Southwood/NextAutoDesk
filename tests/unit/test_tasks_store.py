"""TaskStore unit tests (SQLite persistence, recovery, retention, threading)."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest
from test_api_support import make_config

from nextautodesk.models import TaskRecord, TaskStepResult, TaskStepSpec, utc_now
from nextautodesk.tasks.store import TaskStore


@pytest.fixture()
def store(tmp_path: Path) -> TaskStore:
    config = make_config(tmp_path)
    task_store = TaskStore(config)
    task_store.initialize()
    try:
        yield task_store
    finally:
        task_store.close()


def _record(task_id: str, **overrides: object) -> TaskRecord:
    data = {
        "id": task_id,
        "name": f"task-{task_id}",
        "steps": [TaskStepSpec(action="mouse.move", params={"x": 1, "y": 2})],
        "metadata": {"origin": "test"},
        "client_request_id": f"client-{task_id}",
        "timeout_ms": 1234,
    }
    data.update(overrides)
    return TaskRecord(**data)  # type: ignore[arg-type]


def test_initialize_creates_wal_database(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    task_store = TaskStore(config)
    task_store.initialize()
    try:
        assert config.storage.database_path.is_file()
        mode = task_store._connection().execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        task_store.close()


def test_insert_and_get_roundtrip(store: TaskStore) -> None:
    record = _record("task-1")
    store.insert(record)

    loaded = store.get("task-1")
    assert loaded is not None
    assert loaded.id == "task-1"
    assert loaded.name == "task-task-1"
    assert loaded.status == "queued"
    assert loaded.timeout_ms == 1234
    assert loaded.client_request_id == "client-task-1"
    assert loaded.metadata == {"origin": "test"}
    assert loaded.steps[0].action == "mouse.move"
    assert loaded.steps[0].params == {"x": 1, "y": 2}
    assert loaded.created_at.tzinfo is not None
    assert loaded.queued_at.tzinfo is not None
    assert store.get("missing") is None


def test_update_persists_all_columns(store: TaskStore) -> None:
    record = _record("task-2")
    store.insert(record)
    started = utc_now()
    finished = started + timedelta(milliseconds=42)
    record.status = "succeeded"
    record.started_at = started
    record.finished_at = finished
    record.step_results = [
        TaskStepResult(
            index=0,
            action="mouse.move",
            status="succeeded",
            started_at=started,
            finished_at=finished,
            duration_ms=42.0,
            output={"position": {"x": 1, "y": 2}},
        )
    ]
    store.update(record)

    loaded = store.get("task-2")
    assert loaded is not None
    assert loaded.status == "succeeded"
    assert loaded.finished_at is not None
    assert loaded.duration_ms is not None and loaded.duration_ms >= 40
    assert loaded.step_results[0].output == {"position": {"x": 1, "y": 2}}


def test_update_fields_only_touches_given_columns(store: TaskStore) -> None:
    store.insert(_record("task-3"))
    assert store.update_fields("task-3", {"status": "running", "started_at": utc_now()})
    loaded = store.get("task-3")
    assert loaded is not None
    assert loaded.status == "running"
    assert loaded.started_at is not None
    # other columns untouched
    assert loaded.name == "task-task-3"

    assert store.update_fields(
        "task-3",
        {
            "status": "failed",
            "error": {"code": "ACTION_FAILED", "message": "boom", "details": {}},
            "cancel_requested_at": utc_now(),
            "step_results": [
                TaskStepResult(index=0, action="mouse.move", status="failed")
            ],
        },
    )
    loaded = store.get("task-3")
    assert loaded is not None
    assert loaded.error == {"code": "ACTION_FAILED", "message": "boom", "details": {}}
    assert loaded.cancel_requested_at is not None
    assert loaded.step_results[0].status == "failed"

    assert store.update_fields("missing-task", {"status": "failed"}) is False
    with pytest.raises(ValueError):
        store.update_fields("task-3", {"bogus": 1})


def test_list_ordering_pagination_and_status_filter(store: TaskStore) -> None:
    now = utc_now()
    statuses = ["succeeded", "failed", "succeeded", "cancelled", "queued"]
    for index, status in enumerate(statuses):
        store.insert(
            _record(
                f"task-{index}",
                status=status,
                created_at=now - timedelta(seconds=10 - index),
                queued_at=now - timedelta(seconds=10 - index),
            )
        )

    items, total = store.list()
    assert total == 5
    assert items[0].id == "task-4"  # newest first
    assert items[-1].id == "task-0"

    page, page_total = store.list(limit=2, offset=1)
    assert page_total == 5
    assert len(page) == 2
    assert page[0].id == "task-3"

    succeeded, succeeded_total = store.list(status="succeeded")
    assert succeeded_total == 2
    assert {item.id for item in succeeded} == {"task-0", "task-2"}

    # limit is clamped to a sane range
    clamped, _ = store.list(limit=0)
    assert len(clamped) == 1


def test_recover_interrupted(store: TaskStore) -> None:
    store.insert(_record("q1", status="queued"))
    store.insert(_record("r1", status="running"))
    store.insert(_record("s1", status="succeeded"))

    assert store.recover_interrupted() == 2
    assert store.get("q1").status == "interrupted"  # type: ignore[union-attr]
    assert store.get("r1").status == "interrupted"  # type: ignore[union-attr]
    assert store.get("q1").finished_at is not None  # type: ignore[union-attr]
    assert store.get("q1").error["code"] == "TASK_INTERRUPTED"  # type: ignore[union-attr,index]
    assert store.get("s1").status == "succeeded"  # type: ignore[union-attr]
    assert store.recover_interrupted() == 0


def test_cleanup_retention_and_max_records(store: TaskStore) -> None:
    now = utc_now()
    store.insert(
        _record("old", status="succeeded", created_at=now - timedelta(days=30))
    )
    store.insert(
        _record("old-running", status="running", created_at=now - timedelta(days=30))
    )
    store.insert(_record("recent", status="succeeded", created_at=now))

    assert store.cleanup(retention_days=7, max_records=10_000) == 1
    assert store.get("old") is None
    assert store.get("old-running") is not None
    assert store.get("recent") is not None

    # retention disabled, cap by record count: keep the newest one
    assert store.cleanup(retention_days=0, max_records=1) == 1
    _, total = store.list()
    assert total == 1


def test_counts(store: TaskStore) -> None:
    store.insert(_record("a", status="queued"))
    store.insert(_record("b", status="running"))
    store.insert(_record("c", status="succeeded"))
    store.insert(_record("d", status="succeeded"))

    counts = store.counts()
    assert counts["total"] == 4
    assert counts["queued"] == 1
    assert counts["running"] == 1
    assert counts["succeeded"] == 2
    assert counts["failed"] == 0
    assert counts["timed_out"] == 0


def test_latest(store: TaskStore) -> None:
    assert store.latest() is None
    now = utc_now()
    store.insert(_record("first", created_at=now - timedelta(seconds=5)))
    store.insert(_record("second", created_at=now))
    assert store.latest().id == "second"  # type: ignore[union-attr]


def test_concurrent_inserts_are_thread_safe(store: TaskStore) -> None:
    errors: list[BaseException] = []

    def worker(worker_id: int) -> None:
        try:
            for index in range(5):
                store.insert(_record(f"w{worker_id}-{index}"))
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    _, total = store.list(limit=100)
    assert total == 20
    for worker_id in range(4):
        for index in range(5):
            assert store.get(f"w{worker_id}-{index}") is not None
