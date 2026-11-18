"""HTTP request/response schemas.

Request models are deliberately strict (``extra="forbid"``) so a typo in a field
name fails fast with ``422 VALIDATION_ERROR`` instead of being silently ignored.
Field names mirror the action parameter models one-to-one: the routes validate a
request here and then hand the dumped payload to :class:`DesktopService`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import InvalidArgument
from ..models import (
    ActionInfo,
    MouseButton,
    OperationStrategyView,
    Point,
    ProviderAvailability,
    Rect,
    ScreenshotFormat,
    ScreenshotInfo,
    Size,
    TaskRecord,
    TaskStepSpec,
    WindowInfo,
)

#: Same grammar as ``nextautodesk.storage._NAME_RE`` (used for path validation).
SAFE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"

class ApiModel(BaseModel):
    """Base class for API payloads: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class ActionOptions(ApiModel):
    """Common options accepted by every mutating action endpoint."""

    strategy: str | None = Field(
        default=None,
        description=(
            "Strategy provider override for this call. Must be installed, enabled and "
            "support the operation; rejected when server.allow_strategy_override is false."
        ),
        examples=["human_movement", "direct"],
    )
    timeout_ms: int | None = Field(
        default=None,
        ge=1,
        le=3_600_000,
        description="Optional execution timeout for this call (defaults to tasks.step_timeout_ms).",
        examples=[60000],
    )


class ErrorDetail(ApiModel):
    code: str = Field(description="Stable error code", examples=["TASK_NOT_FOUND"])
    message: str = Field(description="Human readable message")
    details: dict[str, Any] = Field(default_factory=dict, description="Machine readable context")
    request_id: str | None = Field(default=None, description="Correlation id of the request")


class ErrorResponse(ApiModel):
    error: ErrorDetail


# ---------------------------------------------------------------------------
# mouse
# ---------------------------------------------------------------------------
class MouseMoveRequest(ActionOptions):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"x": 960, "y": 540}]}
    )

    x: float = Field(description="Target X coordinate in screen pixels", examples=[960])
    y: float = Field(description="Target Y coordinate in screen pixels", examples=[540])
    duration_ms: float | None = Field(
        default=None,
        ge=0,
        le=600_000,
        description="Driver interpolation hint; human-like pacing normally comes from the strategy.",
    )


class MouseMoveRelativeRequest(ActionOptions):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"dx": 120, "dy": -40}]}
    )

    dx: float = Field(description="Relative X offset in pixels", examples=[120])
    dy: float = Field(description="Relative Y offset in pixels", examples=[-40])
    duration_ms: float | None = Field(
        default=None,
        ge=0,
        le=600_000,
        description="Driver interpolation hint; human-like pacing normally comes from the strategy.",
    )


class MouseClickRequest(ActionOptions):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"x": 500, "y": 400, "button": "left", "clicks": 1}]},
    )

    x: float | None = Field(default=None, description="Optional move-to X before clicking")
    y: float | None = Field(default=None, description="Optional move-to Y before clicking")
    button: MouseButton = Field(default="left", description="Mouse button", examples=["left"])
    clicks: int = Field(default=1, ge=1, le=3, description="Number of clicks (2 = double click)")
    interval_ms: float | None = Field(
        default=None,
        ge=0,
        le=60_000,
        description="Pause between clicks when clicks > 1 (default comes from the strategy).",
    )


class MouseButtonRequest(ActionOptions):
    """Body of ``POST /mouse/down`` and ``POST /mouse/up``."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"button": "left", "x": 500, "y": 400}]}
    )

    button: MouseButton = Field(default="left", description="Mouse button")
    x: float | None = Field(default=None, description="Optional move-to X before pressing/releasing")
    y: float | None = Field(default=None, description="Optional move-to Y before pressing/releasing")


class MouseHoldRequest(ActionOptions):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"button": "left", "hold_ms": 500}]}
    )

    button: MouseButton = Field(default="left", description="Mouse button to hold")
    hold_ms: int | None = Field(
        default=None, ge=0, le=600_000, description="Hold duration (default from configuration)"
    )
    x: float | None = Field(default=None, description="Optional move-to X before holding")
    y: float | None = Field(default=None, description="Optional move-to Y before holding")


class MouseDragRequest(ActionOptions):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"from_x": 300, "from_y": 300, "to_x": 900, "to_y": 600, "button": "left"}]
        },
    )

    from_x: float | None = Field(default=None, description="Drag start X (default: current position)")
    from_y: float | None = Field(default=None, description="Drag start Y (default: current position)")
    to_x: float = Field(description="Drag end X", examples=[900])
    to_y: float = Field(description="Drag end Y", examples=[600])
    button: MouseButton = Field(default="left", description="Mouse button held during the drag")
    duration_ms: float | None = Field(
        default=None, ge=0, le=600_000, description="Optional drag duration hint"
    )


class MouseScrollRequest(ActionOptions):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"dx": 0, "dy": 3}]}
    )

    dx: int = Field(default=0, ge=-1000, le=1000, description="Horizontal wheel detents (right > 0)")
    dy: int = Field(default=0, ge=-1000, le=1000, description="Vertical wheel detents (down > 0)")
    x: float | None = Field(default=None, description="Optional move-to X before scrolling")
    y: float | None = Field(default=None, description="Optional move-to Y before scrolling")


# ---------------------------------------------------------------------------
# keyboard
# ---------------------------------------------------------------------------
class KeyboardTypeRequest(ActionOptions):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"text": "Hello NextAutoDesk"}]}
    )

    text: str = Field(
        min_length=1,
        max_length=100_000,
        description="Text to type character by character (Unicode supported).",
        examples=["Hello NextAutoDesk"],
    )


class KeyboardPressRequest(ActionOptions):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"key": "enter"}]}
    )

    key: str = Field(
        min_length=1,
        max_length=64,
        description="Key name: letters, digits or names like enter/esc/tab/f1/ctrl.",
        examples=["enter"],
    )


class KeyboardHotkeyRequest(ActionOptions):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"keys": ["ctrl", "c"]}]}
    )

    keys: list[str] = Field(
        min_length=1,
        max_length=8,
        description="Keys pressed together, e.g. ['ctrl', 'c'].",
        examples=[["ctrl", "c"]],
    )


class KeyboardSequenceRequest(ActionOptions):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"keys": ["tab", "enter", "down"]}]}
    )

    keys: list[str] = Field(
        min_length=1,
        max_length=256,
        description="Keys pressed sequentially; the interval is configured per operation.",
        examples=[["tab", "enter"]],
    )


class KeyboardKeyRequest(ActionOptions):
    """Body of ``POST /keyboard/key/down`` and ``POST /keyboard/key/up``."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"key": "shift"}]}
    )

    key: str = Field(min_length=1, max_length=64, description="Key name to press/release")


class KeyboardHoldRequest(ActionOptions):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"key": "a", "hold_ms": 400}]}
    )

    key: str = Field(min_length=1, max_length=64, description="Key name to hold down")
    hold_ms: int | None = Field(
        default=None, ge=0, le=600_000, description="Hold duration (default from configuration)"
    )


# ---------------------------------------------------------------------------
# desktop
# ---------------------------------------------------------------------------
class DesktopWaitRequest(ApiModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"ms": 500}]}
    )

    ms: int = Field(
        ge=0,
        le=600_000,
        description="Milliseconds to wait (cancel-aware); inline and bounded by this value",
        examples=[500],
    )


# ---------------------------------------------------------------------------
# screen
# ---------------------------------------------------------------------------
class ScreenshotRequest(ApiModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"save": True, "format": "png"}]},
    )

    region: Rect | None = Field(
        default=None, description="Optional capture region; full screen when omitted."
    )
    format: ScreenshotFormat = Field(default="png", description="Image format")
    save: bool = Field(default=False, description="Persist the capture under the screenshots directory.")
    name: str | None = Field(
        default=None,
        pattern=SAFE_NAME_PATTERN,
        description="Optional file name for saved captures (extension added when omitted).",
        examples=["login-page.png"],
    )
    inline: bool = Field(
        default=False,
        description="Include the image as base64 in the JSON response (keep false for large screens).",
    )


class ScreenshotListResponse(ApiModel):
    items: list[ScreenshotInfo] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------
class WindowMoveRequest(ApiModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"x": 120, "y": 80}]}
    )

    x: int = Field(description="New window X position", examples=[120])
    y: int = Field(description="New window Y position", examples=[80])


class WindowResizeRequest(ApiModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"width": 960, "height": 540}]}
    )

    width: int = Field(ge=1, le=16_384, description="New window width in pixels")
    height: int = Field(ge=1, le=16_384, description="New window height in pixels")


class WindowListResponse(ApiModel):
    windows: list[WindowInfo] = Field(default_factory=list)
    count: int = 0


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------
class TaskStepRequest(ApiModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"action": "mouse.move", "params": {"x": 500, "y": 400}},
                {"action": "keyboard.type", "params": {"text": "hello"}, "delay_after_ms": 100},
            ]
        },
    )

    action: str = Field(
        min_length=1, max_length=128, description="Action name, e.g. 'mouse.move'", examples=["mouse.move"]
    )
    params: dict[str, Any] = Field(
        default_factory=dict, description="Action parameters validated by the action layer."
    )
    name: str | None = Field(default=None, max_length=128, description="Optional step label")
    timeout_ms: int | None = Field(
        default=None, ge=1, le=3_600_000, description="Per-step timeout (default tasks.step_timeout_ms)."
    )
    continue_on_error: bool = Field(
        default=False, description="Continue with the next step when this step fails."
    )
    delay_after_ms: int = Field(
        default=0, ge=0, le=3_600_000, description="Cancel-aware pause after the step."
    )

    def to_spec(self) -> TaskStepSpec:
        return TaskStepSpec(**self.model_dump())


class TaskSubmitRequest(ApiModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "demo",
                    "steps": [
                        {"action": "mouse.move", "params": {"x": 500, "y": 400}},
                        {"action": "mouse.click", "params": {"button": "left"}},
                    ],
                }
            ]
        },
    )

    name: str | None = Field(default=None, max_length=128, description="Optional task label")
    steps: list[TaskStepRequest] = Field(
        min_length=1, max_length=2000, description="Steps executed sequentially on the serial executor."
    )
    timeout_ms: int | None = Field(
        default=None,
        ge=100,
        le=7_200_000,
        description="Whole-task timeout (default tasks.default_timeout_ms).",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Opaque metadata stored with the task record."
    )
    client_request_id: str | None = Field(
        default=None,
        max_length=128,
        description="Caller-side correlation id stored with the task record.",
    )


class TaskListResponse(ApiModel):
    items: list[TaskRecord] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------
class ActionCatalogResponse(ApiModel):
    actions: list[ActionInfo] = Field(default_factory=list)
    count: int = 0
    tiers: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Action names grouped by API tier (primitive, perception, composite).",
    )


class StrategyCatalogResponse(ApiModel):
    operations: dict[str, OperationStrategyView] = Field(default_factory=dict)
    providers: list[ProviderAvailability] = Field(default_factory=list)


class InfoResponse(ApiModel):
    model_config = ConfigDict(extra="allow")

    name: str = "nextautodesk"
    version: str = ""
    api_version: str = ""
    uptime_s: float = 0.0
    display: str = ""
    screen: dict[str, int] = Field(default_factory=dict)
    desktop: dict[str, Any] = Field(default_factory=dict)
    actions: list[str] = Field(default_factory=list)
    providers: list[dict[str, Any]] = Field(default_factory=list)
    perception: dict[str, Any] = Field(default_factory=dict)
    features: dict[str, Any] = Field(default_factory=dict)
    executor: dict[str, Any] = Field(default_factory=dict)


class RootResponse(ApiModel):
    name: str = "NextAutoDesk"
    version: str = ""
    api_version: str = ""
    status: str = "ok"
    api: str = "/api/v1"
    docs: str | None = None
    healthz: str = "/healthz"
    readyz: str = "/readyz"


class LivenessResponse(ApiModel):
    status: Literal["ok"] = "ok"
    version: str = ""
    api_version: str = ""
    uptime_s: float = 0.0


class ReadinessResponse(ApiModel):
    ready: bool
    version: str = ""
    checks: dict[str, str] = Field(default_factory=dict)


class ConfigReloadResponse(ApiModel):
    reloaded: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


def parse_region(value: str | None) -> Rect | None:
    """Parse a compact ``x,y,width,height`` region query string."""

    if value is None or not value.strip():
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise InvalidArgument(
            "region must have the form 'x,y,width,height'",
            details={"region": value},
        )
    try:
        x, y, width, height = (int(part) for part in parts)
    except ValueError as exc:
        raise InvalidArgument(
            "region values must be integers",
            details={"region": value},
        ) from exc
    try:
        return Rect(x=x, y=y, width=width, height=height)
    except ValidationError as exc:
        raise InvalidArgument(
            "region width and height must be positive",
            details={"region": value},
        ) from exc


__all__ = [
    "SAFE_NAME_PATTERN",
    "ActionCatalogResponse",
    "ActionOptions",
    "ApiModel",
    "ConfigReloadResponse",
    "DesktopWaitRequest",
    "ErrorDetail",
    "ErrorResponse",
    "InfoResponse",
    "KeyboardHoldRequest",
    "KeyboardHotkeyRequest",
    "KeyboardKeyRequest",
    "KeyboardPressRequest",
    "KeyboardSequenceRequest",
    "KeyboardTypeRequest",
    "LivenessResponse",
    "MouseButtonRequest",
    "MouseClickRequest",
    "MouseDragRequest",
    "MouseHoldRequest",
    "MouseMoveRelativeRequest",
    "MouseMoveRequest",
    "MouseScrollRequest",
    "Point",
    "ReadinessResponse",
    "RootResponse",
    "ScreenshotListResponse",
    "ScreenshotRequest",
    "Size",
    "StrategyCatalogResponse",
    "TaskListResponse",
    "TaskStepRequest",
    "TaskSubmitRequest",
    "WindowListResponse",
    "WindowMoveRequest",
    "WindowResizeRequest",
    "parse_region",
]
