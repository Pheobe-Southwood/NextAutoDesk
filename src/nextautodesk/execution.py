"""Serial desktop executor.

All *mutating* desktop operations (mouse, keyboard, window changes and composed
tasks) are funnelled through one FIFO worker thread. This makes it structurally
impossible for two callers to fight over the same X11 pointer/keyboard or to
interleave two half-finished drags.

Read-only operations (screenshots, window listing, screen info) deliberately
bypass this queue so monitoring stays responsive while a long typing task runs.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .errors import (
    ActionFailed,
    ExecutorUnavailable,
    NextAutoDeskError,
    QueueFull,
    TaskCancelled,
    TaskTimeoutError,
)
from .models import ExecutorStats

LOGGER = logging.getLogger(__name__)


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass
class Job:
    """One unit of work executed on the serial desktop worker."""

    name: str
    fn: Callable[[threading.Event], Any]
    timeout_ms: int | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    future: Future = field(default_factory=Future)
    state: JobState = JobState.QUEUED
    submitted_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    timed_out: bool = False

    @property
    def queued_ms(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.started_at - self.submitted_at) * 1000.0

    @property
    def duration_ms(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return (end - self.started_at) * 1000.0


class SerialExecutor:
    """Single-worker FIFO executor with cooperative cancellation and timeouts."""

    def __init__(
        self,
        *,
        max_queue_size: int = 256,
        name: str = "desktop",
        logger: logging.Logger | None = None,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be >= 1")
        self.max_queue_size = max_queue_size
        self.name = name
        self._logger = logger or LOGGER
        self._queue: deque[Job] = deque()
        self._pending_ids: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._running = False
        self._stopping = False
        self._current: Job | None = None
        self._counters = {"completed": 0, "failed": 0, "cancelled": 0, "rejected": 0, "timed_out": 0}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            if self._stopping:
                raise ExecutorUnavailable("executor is shutting down")
            self._running = True
            self._thread = threading.Thread(target=self._worker_loop, name=f"nad-{self.name}-executor", daemon=True)
            self._thread.start()
            self._logger.info("serial executor started (max_queue=%d)", self.max_queue_size)

    def shutdown(self, grace_s: float = 10.0) -> None:
        with self._lock:
            if not self._running:
                return
            self._stopping = True
            current = self._current
            if current is not None:
                current.cancel_event.set()
            self._wake.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, grace_s))
            if thread.is_alive():
                self._logger.warning("executor worker still busy after %.1fs grace period", grace_s)
        with self._lock:
            pending = list(self._queue)
            self._queue.clear()
            self._pending_ids.clear()
            self._running = False
        for job in pending:
            job.state = JobState.CANCELLED
            job.finished_at = time.monotonic()
            self._counters["cancelled"] += 1
            if not job.future.done():
                job.future.set_exception(TaskCancelled("executor shut down before job started"))
        self._logger.info("serial executor stopped")

    # -- submission --------------------------------------------------------
    def submit(
        self,
        name: str,
        fn: Callable[[threading.Event], Any],
        *,
        timeout_ms: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Job:
        """Enqueue ``fn(cancel_event)`` and return the job handle.

        ``cancel_event`` lets a caller publish the cancellation handle *before*
        the job is queued (TaskManager uses this to close the cancel-vs-enqueue
        race without holding its own lock across ``submit``).
        """

        job = Job(
            name=name,
            fn=fn,
            timeout_ms=timeout_ms,
            cancel_event=cancel_event if cancel_event is not None else threading.Event(),
        )
        with self._wake:
            if not self._running or self._stopping:
                raise ExecutorUnavailable("desktop executor is not accepting jobs")
            if len(self._queue) >= self.max_queue_size:
                self._counters["rejected"] += 1
                raise QueueFull(
                    f"desktop executor queue is full ({self.max_queue_size} jobs)",
                    details={"queue_size": len(self._queue), "max_queue_size": self.max_queue_size},
                )
            self._queue.append(job)
            self._pending_ids.add(job.id)
            self._wake.notify()
        return job

    def cancel(self, job_id: str) -> bool:
        """Request cooperative cancellation. Returns ``True`` if the job exists."""

        cancelled_job: Job | None = None
        with self._lock:
            current = self._current
            if current is not None and current.id == job_id:
                current.cancel_event.set()
                return True
            for index, job in enumerate(self._queue):
                if job.id == job_id:
                    job.cancel_event.set()
                    del self._queue[index]
                    self._pending_ids.discard(job.id)
                    cancelled_job = job
                    break
        if cancelled_job is not None:
            # Fail the queued job immediately (outside the lock so callbacks are
            # free to take other locks) and let the worker ignore it entirely.
            self._finish_cancelled(cancelled_job)
            return True
        return False

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    # -- worker ------------------------------------------------------------
    def _worker_loop(self) -> None:
        while True:
            with self._wake:
                while not self._queue and not self._stopping:
                    self._wake.wait(timeout=0.5)
                if self._stopping and not self._queue:
                    return
                if not self._queue:
                    continue
                job = self._queue.popleft()
                self._pending_ids.discard(job.id)
                abandoned = job.cancel_event.is_set() or self._stopping
                if not abandoned:
                    self._current = job
                    job.state = JobState.RUNNING
                    job.started_at = time.monotonic()
            if abandoned:
                # Complete the future *outside* the lock: done callbacks (e.g.
                # TaskManager._finalize) may need other locks and must never be
                # invoked while the executor lock is held, or the executor and the
                # task manager can deadlock in an ABBA cycle.
                self._finish_cancelled(job)
                continue
            timer: threading.Timer | None = None
            if job.timeout_ms is not None and job.timeout_ms > 0:
                timer = threading.Timer(job.timeout_ms / 1000.0, self._on_timeout, args=(job,))
                timer.daemon = True
                timer.start()
            try:
                value = job.fn(job.cancel_event)
            except TaskCancelled as exc:
                if job.timed_out:
                    self._finish_timed_out(job)
                else:
                    job.state = JobState.CANCELLED
                    self._counters["cancelled"] += 1
                    job.future.set_exception(exc)
            except NextAutoDeskError as exc:
                if job.timed_out:
                    self._finish_timed_out(job)
                else:
                    job.state = JobState.FAILED
                    self._counters["failed"] += 1
                    job.future.set_exception(exc)
                    self._logger.warning("job %s (%s) failed: %s", job.id[:8], job.name, exc)
            except BaseException as exc:
                if job.timed_out:
                    self._finish_timed_out(job)
                else:
                    error = ActionFailed(
                        f"job {job.name!r} crashed: {type(exc).__name__}: {exc}",
                        details={"job_id": job.id, "job_name": job.name, "exception": type(exc).__name__},
                    )
                    job.state = JobState.FAILED
                    self._counters["failed"] += 1
                    job.future.set_exception(error)
                    self._logger.exception("job %s (%s) crashed", job.id[:8], job.name)
            else:
                if job.timed_out:
                    self._finish_timed_out(job)
                elif job.cancel_event.is_set():
                    job.state = JobState.CANCELLED
                    self._counters["cancelled"] += 1
                    job.future.set_exception(TaskCancelled("job cancelled after completion"))
                else:
                    job.state = JobState.SUCCEEDED
                    job.future.set_result(value)
                    self._counters["completed"] += 1
            finally:
                if timer is not None:
                    timer.cancel()
                job.finished_at = time.monotonic()
                if not job.future.done():  # defensive: never leave a future hanging
                    job.future.set_exception(ActionFailed("job finished without a result"))
                with self._lock:
                    self._current = None
                    self._wake.notify_all()

    def _on_timeout(self, job: Job) -> None:
        if job.finished_at is not None:
            return
        job.timed_out = True
        job.cancel_event.set()
        self._logger.warning("job %s (%s) exceeded timeout (%sms); cancelling", job.id[:8], job.name, job.timeout_ms)

    # -- finish helpers ----------------------------------------------------
    def _finish_cancelled(self, job: Job) -> None:
        job.state = JobState.CANCELLED
        job.finished_at = time.monotonic()
        self._counters["cancelled"] += 1
        if not job.future.done():
            job.future.set_exception(TaskCancelled("job cancelled before it started"))

    def _finish_timed_out(self, job: Job) -> None:
        job.state = JobState.TIMED_OUT
        job.finished_at = time.monotonic()
        self._counters["timed_out"] += 1
        if not job.future.done():
            job.future.set_exception(
                TaskTimeoutError(
                    f"job {job.name!r} timed out after {job.timeout_ms}ms",
                    details={"job_id": job.id, "timeout_ms": job.timeout_ms},
                )
            )

    # -- introspection -----------------------------------------------------
    def stats(self) -> ExecutorStats:
        with self._lock:
            current = self._current
            return ExecutorStats(
                state="running" if self._running and not self._stopping else "stopped",
                pending=len(self._queue),
                running=current is not None,
                current_job=current.name if current else None,
                current_job_started_at=(
                    _monotonic_to_datetime(current.started_at) if current and current.started_at else None
                ),
                completed=self._counters["completed"],
                failed=self._counters["failed"],
                cancelled=self._counters["cancelled"],
                timed_out=self._counters["timed_out"],
                rejected=self._counters["rejected"],
            )


def _monotonic_to_datetime(value: float) -> datetime:
    """Convert a monotonic timestamp to a wall-clock UTC datetime (diagnostics only)."""

    epoch = time.time() - (time.monotonic() - value)
    return datetime.fromtimestamp(epoch, tz=UTC)


__all__ = ["Job", "JobState", "SerialExecutor"]
