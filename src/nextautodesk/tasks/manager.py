"""Composed task execution: queueing, step sequencing, cancellation, persistence.

A task is *one job* on the shared :class:`~nextautodesk.execution.SerialExecutor`:
the task runner executes every step through
:meth:`DesktopService.execute_action_inline` so it never re-enters (and deadlocks
on) the serial queue it already owns.

State machine::

    queued ──► running ──► succeeded
      │           ├──────► failed
      │           ├──────► cancelled      (cooperative cancellation)
      │           ├──────► timed_out      (whole-task timeout, executor watchdog)
      └──────────────────► cancelled      (cancelled before it started)

    queued/running + process restart ──► interrupted  (recover() on startup)

Per-step timeouts use a private ``threading.Timer`` that flips a step-local
cancel event. The event handed to the action layer is a combined view of the
step event and the job cancellation event, so a whole-task cancel and a step
timeout both stop the currently executing step, while the task runner can tell
them apart afterwards.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import Future
from typing import Any

from ..config import AppConfig
from ..errors import (
    ErrorCode,
    ExecutorUnavailable,
    NextAutoDeskError,
    TaskCancelled,
    TaskNotCancellable,
    TaskNotFound,
    TaskTimeoutError,
    ValidationFailed,
)
from ..logging_setup import action_var, task_id_var
from ..models import TaskRecord, TaskStatus, TaskStepResult, TaskStepSpec, utc_now
from .store import TaskStore

LOGGER = logging.getLogger(__name__)

#: How long ``cancel()`` waits for the task to reach a terminal state before
#: returning the (still running) record with ``cancel_requested_at`` set.
_CANCEL_WAIT_S = 2.0

#: Default interval for the background retention cleanup task.
_CLEANUP_INTERVAL_S = 3600.0

#: Step output strings longer than this are truncated before persistence.
_MAX_PERSISTED_STRING = 4096

#: Step output keys that hold binary payloads and must never be persisted.
_BINARY_OUTPUT_KEYS = frozenset({"image_base64"})


class _CombinedCancel:
    """``threading.Event``-compatible view of (task cancel OR step timeout).

    The action layer only calls ``is_set()`` and ``wait()`` on its cancel event;
    the proxy merges the job-level cancellation event (owned by the executor)
    with the per-step timeout event (owned by the task runner).
    """

    __slots__ = ("_step", "_task")

    def __init__(self, task_event: threading.Event, step_event: threading.Event) -> None:
        self._task = task_event
        self._step = step_event

    def is_set(self) -> bool:
        return self._task.is_set() or self._step.is_set()

    def set(self) -> None:
        self._step.set()

    def clear(self) -> None:
        self._step.clear()

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            if deadline is None:
                self._step.wait(0.05)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._step.wait(min(remaining, 0.05))
        return True


def _normalise_steps(steps: Iterable[TaskStepSpec | Mapping[str, Any]]) -> list[TaskStepSpec]:
    specs: list[TaskStepSpec] = []
    for item in steps:
        if isinstance(item, TaskStepSpec):
            specs.append(item)
        elif isinstance(item, Mapping):
            specs.append(TaskStepSpec.model_validate(dict(item)))
        else:
            raise ValidationFailed(
                "task steps must be objects", details={"received": type(item).__name__}
            )
    return specs


def _error_dict(exc: NextAutoDeskError) -> dict[str, Any]:
    return {
        "code": exc.code.value,
        "message": exc.message,
        "details": exc.details,
    }


def _sanitise_value(value: Any) -> Any:
    """Recursively omit binary payloads and truncate huge strings."""

    if isinstance(value, str):
        if len(value) > _MAX_PERSISTED_STRING:
            return f"<omitted {len(value)} chars>"
        return value
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.strip().lower() in _BINARY_OUTPUT_KEYS:
                # Screenshot steps embed the PNG in base64; persisting it would
                # grow SQLite without bound. Keep a marker (and the saved
                # filename / metadata siblings) instead.
                cleaned[f"{key}_omitted"] = True
                if isinstance(item, str):
                    cleaned[f"{key}_chars"] = len(item)
                continue
            cleaned[key] = _sanitise_value(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitise_value(item) for item in value]
    return value


def _sanitise_output(output: Mapping[str, Any] | None) -> dict[str, Any]:
    """Prepare an action output dict for durable step-result persistence."""

    cleaned = _sanitise_value(dict(output or {}))
    return dict(cleaned) if isinstance(cleaned, Mapping) else {}


class TaskManager:
    """Submit, run, cancel and query tasks on the serial desktop executor."""

    def __init__(
        self,
        *,
        service: Any,
        store: TaskStore,
        config: AppConfig,
        cleanup_interval_s: float | None = None,
    ) -> None:
        self.service = service
        self.store = store
        self.config = config
        self._lock = threading.RLock()
        #: task id -> executor job id
        self._jobs: dict[str, str] = {}
        #: task id -> executor job cancel event (shared with the job function)
        self._cancel_events: dict[str, threading.Event] = {}
        self._started = False
        interval = (
            float(cleanup_interval_s)
            if cleanup_interval_s is not None
            else _CLEANUP_INTERVAL_S
        )
        if interval <= 0:
            raise ValueError("cleanup_interval_s must be positive")
        self._cleanup_interval_s = interval
        self._cleanup_stop = threading.Event()
        self._cleanup_thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._cleanup_stop.clear()
            thread = self._cleanup_thread
            if thread is None or not thread.is_alive():
                thread = threading.Thread(
                    target=self._cleanup_loop,
                    name="nextautodesk-task-cleanup",
                    daemon=True,
                )
                self._cleanup_thread = thread
                thread.start()
        LOGGER.info("task manager started (max_queue=%d)", self.config.tasks.max_queue_size)

    def _cleanup_loop(self) -> None:
        """Periodically apply the task retention policy in the background."""

        while not self._cleanup_stop.wait(self._cleanup_interval_s):
            try:
                deleted = self.store.cleanup()
            except Exception:  # pragma: no cover - retention must never kill the thread
                LOGGER.exception("periodic task retention cleanup failed")
                continue
            if deleted:
                LOGGER.info("periodic retention cleanup removed %d task(s)", deleted)

    def recover(self) -> int:
        """Recover persisted state after a restart; returns interrupted task count."""

        recovered = self.store.recover_interrupted()
        try:
            self.store.cleanup()
        except Exception:  # pragma: no cover - retention must never block startup
            LOGGER.exception("task retention cleanup failed")
        return recovered

    def shutdown(self) -> None:
        """Best-effort cooperative cancellation of everything still queued/running."""

        self._cleanup_stop.set()
        cleanup_thread = self._cleanup_thread
        self._cleanup_thread = None
        if cleanup_thread is not None and cleanup_thread.is_alive():
            cleanup_thread.join(timeout=2.0)
        with self._lock:
            self._started = False
            job_ids = list(self._jobs.values())
            events = list(self._cancel_events.values())
            self._jobs.clear()
            self._cancel_events.clear()
        for event in events:
            event.set()
        executor = getattr(self.service, "executor", None)
        if executor is not None:
            for job_id in job_ids:
                try:
                    executor.cancel(job_id)
                except Exception:  # pragma: no cover - defensive
                    LOGGER.debug("failed to cancel task job %s", job_id, exc_info=True)
        LOGGER.info("task manager stopped")

    # -- submission --------------------------------------------------------
    def submit(
        self,
        steps: Sequence[TaskStepSpec | Mapping[str, Any]],
        *,
        name: str | None = None,
        timeout_ms: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        client_request_id: str | None = None,
    ) -> TaskRecord:
        """Validate, persist and enqueue a composed task."""

        specs = _normalise_steps(steps)
        self._validate(specs, timeout_ms=timeout_ms)
        timeout = int(timeout_ms or self.config.tasks.default_timeout_ms)
        now = utc_now()
        record = TaskRecord(
            id=uuid.uuid4().hex,
            name=name,
            status="queued",
            steps=specs,
            created_at=now,
            queued_at=now,
            timeout_ms=timeout,
            metadata=dict(metadata or {}),
            client_request_id=client_request_id,
        )
        self.store.insert(record)
        executor = getattr(self.service, "executor", None)
        if executor is None:
            error = ExecutorUnavailable("desktop executor is not available")
            self._finalise_failure(record.id, error)
            raise error
        # Publish the cancellation handle *before* queueing, then call submit
        # without holding the manager lock: executor.submit needs the executor
        # lock, while the worker's done-callback takes the manager lock. Holding
        # the manager lock across submit creates an ABBA deadlock.
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[record.id] = cancel_event
        try:
            job = executor.submit(
                f"task:{record.id}",
                self._make_job(record.id),
                timeout_ms=timeout,
                cancel_event=cancel_event,
            )
        except NextAutoDeskError as exc:
            with self._lock:
                self._cancel_events.pop(record.id, None)
            self._finalise_failure(record.id, exc)
            raise
        except Exception as exc:  # pragma: no cover - defensive: executor contract violation
            with self._lock:
                self._cancel_events.pop(record.id, None)
            wrapped = ExecutorUnavailable(f"cannot queue task: {type(exc).__name__}: {exc}")
            self._finalise_failure(record.id, wrapped)
            raise wrapped from exc
        with self._lock:
            self._jobs[record.id] = job.id
        # A cancel() may have landed between persisting "queued" and publishing
        # the event above; honour it now instead of losing the request.
        queued_record = self.store.get(record.id)
        if queued_record is not None and queued_record.cancel_requested_at is not None:
            cancel_event.set()
            with contextlib.suppress(Exception):  # pragma: no cover - defensive
                executor.cancel(job.id)
        job.future.add_done_callback(lambda future: self._finalize(record.id, future))
        LOGGER.info(
            "task queued: %d step(s)",
            len(specs),
            extra={"task_id": record.id},
        )
        return record

    def _make_job(self, task_id: str) -> Any:
        def run(cancel_event: threading.Event) -> TaskRecord | None:
            return self._run(task_id, cancel_event)

        return run

    # -- validation --------------------------------------------------------
    def _validate(self, specs: list[TaskStepSpec], *, timeout_ms: int | None) -> None:
        if not specs:
            raise ValidationFailed("a task must contain at least one step")
        max_steps = self.config.tasks.max_steps
        if len(specs) > max_steps:
            raise ValidationFailed(
                f"a task may contain at most {max_steps} steps",
                details={"steps": len(specs), "max_steps": max_steps},
            )
        if timeout_ms is not None and timeout_ms <= 0:
            raise ValidationFailed("timeout_ms must be positive", details={"timeout_ms": timeout_ms})
        actions = getattr(self.service, "actions", None)
        if actions is None:  # pragma: no cover - defensive (fake services)
            return
        for step in specs:
            handler = actions.get(step.action)  # raises UnknownAction (404)
            params = dict(step.params or {})
            params.pop("strategy", None)
            handler.parse_params(params)  # raises ValidationFailed (422)

    # -- execution ---------------------------------------------------------
    def _run(self, task_id: str, job_cancel: threading.Event) -> TaskRecord | None:
        record = self.store.get(task_id)
        if record is None:  # pragma: no cover - store and executor stay in sync
            LOGGER.warning("task %s vanished before execution", task_id)
            return None
        token = task_id_var.set(task_id)
        try:
            if job_cancel.is_set():
                return self._finish_cancelled(record)
            started_at = utc_now()
            self.store.update_fields(task_id, {"status": "running", "started_at": started_at})
            record.status = "running"
            record.started_at = started_at
            LOGGER.info("task started", extra={"task_id": task_id})

            step_results: list[TaskStepResult] = []
            steps = record.steps
            failure: dict[str, Any] | None = None
            cancelled = False
            index = 0
            while index < len(steps):
                step = steps[index]
                if job_cancel.is_set():
                    cancelled = True
                    step_results.extend(
                        self._placeholder_results(steps, index, status="cancelled")
                    )
                    break
                result, outcome = self._run_step(task_id, index, step, job_cancel)
                step_results.append(result)
                self.store.update_fields(task_id, {"step_results": step_results})
                if result.status == "failed":
                    LOGGER.warning(
                        "task step %d (%s) failed: %s",
                        index,
                        step.action,
                        (result.error or {}).get("message"),
                        extra={"task_id": task_id, "action": step.action},
                    )
                if outcome in {"cancelled"}:
                    cancelled = True
                    step_results.extend(
                        self._placeholder_results(steps, index + 1, status="cancelled")
                    )
                    break
                if outcome in {"failed", "timed_out"} and not step.continue_on_error:
                    failure = dict(result.error or {})
                    failure.setdefault("step_index", index)
                    failure.setdefault("action", step.action)
                    step_results.extend(
                        self._placeholder_results(steps, index + 1, status="skipped")
                    )
                    break
                index += 1
                if not cancelled and outcome in {"ok", "failed", "timed_out"}:
                    delay_ms = step.delay_after_ms
                    if delay_ms and not self._sleep(delay_ms / 1000.0, job_cancel):
                        cancelled = True
                        step_results.extend(
                            self._placeholder_results(steps, index, status="cancelled")
                        )
                        break

            finished_at = utc_now()
            if cancelled:
                status: TaskStatus = "cancelled"
                error = {
                    "code": ErrorCode.TASK_CANCELLED.value,
                    "message": "task cancelled",
                    "details": {},
                }
            elif failure is not None:
                status = "failed"
                error = failure
            else:
                status = "succeeded"
                error = None
            self.store.update_fields(
                task_id,
                {
                    "status": status,
                    "finished_at": finished_at,
                    "error": error,
                    "step_results": step_results,
                },
            )
            LOGGER.info(
                "task finished with status %s after %d step(s)", status, len(step_results),
                extra={"task_id": task_id, "status": status},
            )
            return self.store.get(task_id)
        finally:
            task_id_var.reset(token)

    def _run_step(
        self,
        task_id: str,
        index: int,
        step: TaskStepSpec,
        job_cancel: threading.Event,
    ) -> tuple[TaskStepResult, str]:
        """Execute one step and classify the outcome."""

        step_cancel = threading.Event()
        timed_out = threading.Event()
        combined = _CombinedCancel(job_cancel, step_cancel)
        timeout_ms = step.timeout_ms or self.config.tasks.step_timeout_ms

        def _on_timeout() -> None:
            timed_out.set()
            step_cancel.set()

        timer = threading.Timer(timeout_ms / 1000.0, _on_timeout)
        timer.daemon = True
        timer.start()
        started_at = utc_now()
        started_monotonic = time.monotonic()
        action_token = action_var.set(step.action)
        result: TaskStepResult | None = None
        outcome = "ok"
        try:
            params = dict(step.params or {})
            strategy = params.pop("strategy", None)
            action_result = self.service.execute_action_inline(
                step.action,
                params,
                strategy=strategy,
                cancel=combined,
                task_id=task_id,
                step_index=index,
            )
            finished_at = utc_now()
            duration_ms = round((time.monotonic() - started_monotonic) * 1000.0, 3)
            if timed_out.is_set():
                result = self._failed_result(
                    index, step, started_at, finished_at, duration_ms, timed_out=True,
                    timeout_ms=timeout_ms,
                )
                outcome = "timed_out"
            else:
                result = TaskStepResult(
                    index=index,
                    action=step.action,
                    name=step.name,
                    status="succeeded",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    output=_sanitise_output(action_result.output),
                    strategies=list(action_result.strategies or []),
                )
                outcome = "ok"
        except NextAutoDeskError as exc:
            finished_at = utc_now()
            duration_ms = round((time.monotonic() - started_monotonic) * 1000.0, 3)
            if timed_out.is_set():
                result = self._failed_result(
                    index, step, started_at, finished_at, duration_ms, timed_out=True,
                    timeout_ms=timeout_ms,
                )
                outcome = "timed_out"
            elif isinstance(exc, TaskCancelled) or job_cancel.is_set():
                result = TaskStepResult(
                    index=index,
                    action=step.action,
                    name=step.name,
                    status="cancelled",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    error=_error_dict(exc),
                )
                outcome = "cancelled"
            else:
                result = TaskStepResult(
                    index=index,
                    action=step.action,
                    name=step.name,
                    status="failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    error=_error_dict(exc),
                )
                outcome = "failed"
        except Exception as exc:
            finished_at = utc_now()
            duration_ms = round((time.monotonic() - started_monotonic) * 1000.0, 3)
            result = TaskStepResult(
                index=index,
                action=step.action,
                name=step.name,
                status="failed",
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                error={
                    "code": ErrorCode.INTERNAL_ERROR.value,
                    "message": f"{type(exc).__name__}: {exc}",
                    "details": {},
                },
            )
            outcome = "failed"
            LOGGER.exception(
                "task step %d (%s) crashed", index, step.action, extra={"task_id": task_id}
            )
        finally:
            timer.cancel()
            action_var.reset(action_token)
        assert result is not None  # every branch above assigns it
        return result, outcome

    def _failed_result(
        self,
        index: int,
        step: TaskStepSpec,
        started_at: Any,
        finished_at: Any,
        duration_ms: float,
        *,
        timed_out: bool,
        timeout_ms: int,
    ) -> TaskStepResult:
        return TaskStepResult(
            index=index,
            action=step.action,
            name=step.name,
            status="failed",
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            error={
                "code": ErrorCode.ACTION_TIMEOUT.value if timed_out else ErrorCode.ACTION_FAILED.value,
                "message": (
                    f"step {index} ({step.action}) timed out after {timeout_ms}ms"
                    if timed_out
                    else f"step {index} ({step.action}) failed"
                ),
                "details": {"step_index": index, "action": step.action, "timeout_ms": timeout_ms},
            },
        )

    @staticmethod
    def _placeholder_results(
        steps: Sequence[TaskStepSpec], start: int, *, status: str
    ) -> list[TaskStepResult]:
        return [
            TaskStepResult(
                index=index,
                action=step.action,
                name=step.name,
                status=status,  # type: ignore[arg-type]
            )
            for index, step in enumerate(steps[start:], start=start)
        ]

    @staticmethod
    def _sleep(seconds: float, cancel_event: threading.Event) -> bool:
        """Cancel-aware sleep; returns ``False`` when cancelled."""

        deadline = time.monotonic() + seconds
        while True:
            if cancel_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            cancel_event.wait(min(remaining, 0.05))

    def _finish_cancelled(self, record: TaskRecord) -> TaskRecord:
        now = utc_now()
        self.store.update_fields(
            record.id,
            {
                "status": "cancelled",
                "finished_at": now,
                "error": {
                    "code": ErrorCode.TASK_CANCELLED.value,
                    "message": "task cancelled before it started",
                    "details": {},
                },
            },
        )
        return self.store.get(record.id) or record

    # -- finalisation ------------------------------------------------------
    def _finalize(self, task_id: str, future: Future) -> None:
        """Done-callback: reconcile the persisted record with the executor outcome."""

        with self._lock:
            self._jobs.pop(task_id, None)
            self._cancel_events.pop(task_id, None)
        try:
            record = self.store.get(task_id)
        except Exception:  # pragma: no cover - database failure during callback
            LOGGER.exception("cannot finalise task %s", task_id)
            return
        if record is None:  # pragma: no cover
            return
        try:
            exc = future.exception()
        except Exception:  # pragma: no cover - cancelled futures
            exc = None
        now = utc_now()
        if isinstance(exc, TaskTimeoutError):
            self.store.update_fields(
                task_id,
                {"status": "timed_out", "finished_at": now, "error": _error_dict(exc)},
            )
            LOGGER.warning("task timed out", extra={"task_id": task_id})
            return
        if record.is_terminal:
            return
        if exc is None:
            self.store.update_fields(
                task_id,
                {
                    "status": "succeeded",
                    "finished_at": now,
                    "error": None,
                },
            )
            return
        if isinstance(exc, TaskCancelled):
            self.store.update_fields(
                task_id,
                {"status": "cancelled", "finished_at": now, "error": _error_dict(exc)},
            )
            return
        error = _error_dict(exc) if isinstance(exc, NextAutoDeskError) else {
            "code": ErrorCode.INTERNAL_ERROR.value,
            "message": f"{type(exc).__name__}: {exc}",
            "details": {},
        }
        self.store.update_fields(
            task_id, {"status": "failed", "finished_at": now, "error": error}
        )
        LOGGER.error("task failed", extra={"task_id": task_id})

    def _finalise_failure(self, task_id: str, error: NextAutoDeskError) -> None:
        try:
            self.store.update_fields(
                task_id,
                {"status": "failed", "finished_at": utc_now(), "error": _error_dict(error)},
            )
        except Exception:  # pragma: no cover - defensive
            LOGGER.exception("cannot persist failure for task %s", task_id)

    # -- queries / cancellation -------------------------------------------
    def get(self, task_id: str) -> TaskRecord:
        record = self.store.get(task_id)
        if record is None:
            raise TaskNotFound(
                f"task {task_id!r} not found", details={"task_id": task_id}
            )
        return record

    def list(
        self,
        *,
        status: str | TaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[TaskRecord], int]:
        return self.store.list(status=status, limit=limit, offset=offset)

    def cancel(self, task_id: str) -> TaskRecord:
        """Request cooperative cancellation and return the (possibly final) record."""

        with self._lock:
            record = self.store.get(task_id)
            if record is None:
                raise TaskNotFound(f"task {task_id!r} not found", details={"task_id": task_id})
            if record.is_terminal:
                raise TaskNotCancellable(
                    f"task {task_id!r} already finished with status {record.status!r}",
                    details={"task_id": task_id, "status": record.status},
                )
            if record.cancel_requested_at is None:
                now = utc_now()
                self.store.update_fields(task_id, {"cancel_requested_at": now})
                record.cancel_requested_at = now
            event = self._cancel_events.get(task_id)
            job_id = self._jobs.get(task_id)
        if event is not None:
            event.set()
        executor = getattr(self.service, "executor", None)
        if job_id is not None and executor is not None:
            try:
                executor.cancel(job_id)
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("executor.cancel(%s) failed", job_id, exc_info=True)
        return self._await_terminal(task_id)

    def _await_terminal(self, task_id: str) -> TaskRecord:
        deadline = time.monotonic() + _CANCEL_WAIT_S
        while time.monotonic() < deadline:
            record = self.store.get(task_id)
            if record is None:  # pragma: no cover
                raise TaskNotFound(
                    f"task {task_id!r} not found", details={"task_id": task_id}
                )
            if record.is_terminal:
                return record
            time.sleep(0.02)
        record = self.store.get(task_id)
        if record is None:  # pragma: no cover
            raise TaskNotFound(f"task {task_id!r} not found", details={"task_id": task_id})
        return record


__all__ = ["TaskManager"]
