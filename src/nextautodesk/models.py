"""Shared domain models used across layers.

These models are transport-agnostic on purpose: the API schemas (``api.schemas``)
reuse them where possible so the HTTP contract, the task store and the action
layer speak the same language.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .operations import Operation

MouseButton = Literal["left", "right", "middle"]
ScreenshotFormat = Literal["png", "jpeg"]

TaskStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
]

TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


class MouseState(BaseModel):
    """Pointer position plus the currently pressed mouse buttons."""

    model_config = ConfigDict(extra="forbid")

    position: Point
    buttons: list[MouseButton] = Field(
        default_factory=list, description="Currently pressed buttons (left/right/middle)"
    )


class Size(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(ge=1)
    height: int = Field(ge=1)


class Rect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int
    y: int
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class WindowInfo(BaseModel):
    """A top-level X11 window as reported by the window driver."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(description="X11 window id in hexadecimal form, e.g. '0x3a00007'")
    title: str = ""
    class_name: str | None = None
    instance_name: str | None = None
    pid: int | None = None
    desktop: int | None = None
    geometry: Rect
    state: Literal["normal", "minimized", "maximized", "fullscreen", "unknown"] = "unknown"
    active: bool = False
    transient_for: str | None = None


class WindowSelector(BaseModel):
    """How a window is identified by callers.

    At least one of ``id``/``title``/``class_name``/``active`` must be provided.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, description="Window id (hex) as returned by GET /windows")
    title: str | None = Field(default=None, description="Exact title by default")
    title_regex: bool = Field(default=False, description="Treat ``title`` as a regular expression")
    class_name: str | None = None
    instance_name: str | None = None
    active: bool = Field(default=False, description="Select the currently active window")

    def is_empty(self) -> bool:
        return not any(
            (self.id, self.title, self.class_name, self.instance_name, self.active)
        )


class StrategyOutcome(BaseModel):
    """Metadata produced by the strategy layer for one operation execution."""

    model_config = ConfigDict(extra="allow")

    operation: Operation
    provider: str
    provider_version: str | None = None
    duration_ms: float = 0.0
    engine: str | None = None
    fallback_reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ActionResult(BaseModel):
    """Result of a single action executed synchronously."""

    model_config = ConfigDict(extra="allow")

    action: str
    success: bool = True
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    output: dict[str, Any] = Field(default_factory=dict)
    strategies: list[StrategyOutcome] = Field(default_factory=list)


class TaskStepSpec(BaseModel):
    """One step of a composed task."""

    model_config = ConfigDict(extra="forbid")

    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    name: str | None = None
    timeout_ms: int | None = Field(default=None, ge=1)
    continue_on_error: bool = False
    delay_after_ms: int = Field(default=0, ge=0, le=3_600_000)


class TaskStepResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int
    action: str
    name: str | None = None
    status: Literal["succeeded", "failed", "skipped", "cancelled"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float = 0.0
    output: dict[str, Any] = Field(default_factory=dict)
    strategies: list[StrategyOutcome] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class TaskRecord(BaseModel):
    """Durable task state."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str | None = None
    status: TaskStatus = "queued"
    steps: list[TaskStepSpec] = Field(default_factory=list)
    step_results: list[TaskStepResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    queued_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    timeout_ms: int | None = None
    cancel_requested_at: datetime | None = None
    error: dict[str, Any] | None = None
    client_request_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    @property
    def duration_ms(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or utc_now()
        return (end - self.started_at).total_seconds() * 1000.0


class ProviderAvailability(BaseModel):
    """Runtime availability of one strategy provider."""

    model_config = ConfigDict(extra="allow")

    name: str
    available: bool
    version: str | None = None
    description: str = ""
    supported_operations: list[Operation] = Field(default_factory=list)
    reason: str | None = Field(
        default=None, description="Why the provider is unavailable (missing optional package, etc.)"
    )
    details: dict[str, Any] = Field(default_factory=dict)


class OperationStrategyView(BaseModel):
    """Effective strategy configuration for one operation (secrets-free)."""

    model_config = ConfigDict(extra="allow")

    operation: Operation
    provider: str
    enabled: bool = True
    available: bool
    params: dict[str, Any] = Field(default_factory=dict)
    candidates: list[str] = Field(
        default_factory=list, description="Providers that support this operation and are available"
    )


class HealthCheck(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["ok", "degraded", "error", "unknown"]
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class HealthReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["ok", "degraded", "unhealthy"]
    version: str
    api_version: str
    timestamp: datetime = Field(default_factory=utc_now)
    uptime_s: float = 0.0
    checks: dict[str, HealthCheck] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)


class ScreenshotInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    format: str = "png"
    created_at: datetime
    download_url: str


class ActionInfo(BaseModel):
    """Public catalogue entry for one action (used by ``GET /actions``)."""

    name: str
    summary: str
    category: str
    tier: Literal["primitive", "perception", "composite"] = "primitive"
    mutating: bool
    params_schema: dict[str, Any]
    example: dict[str, Any] = {}


class ExecutorStats(BaseModel):
    model_config = ConfigDict(extra="allow")

    state: str
    pending: int
    running: bool
    current_job: str | None = None
    current_job_started_at: datetime | None = None
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    timed_out: int = 0
    rejected: int = 0


__all__ = [
    "TERMINAL_TASK_STATUSES",
    "ActionResult",
    "ExecutorStats",
    "HealthCheck",
    "HealthReport",
    "MouseButton",
    "MouseState",
    "OperationStrategyView",
    "Point",
    "ProviderAvailability",
    "Rect",
    "ScreenshotFormat",
    "ScreenshotInfo",
    "Size",
    "StrategyOutcome",
    "TaskRecord",
    "TaskStatus",
    "TaskStepResult",
    "TaskStepSpec",
    "WindowInfo",
    "WindowSelector",
    "utc_now",
]
