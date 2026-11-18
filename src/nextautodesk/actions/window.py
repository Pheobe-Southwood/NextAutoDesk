"""Window management actions (EWMH via the window driver).

Window queries are read-only and bypass the serial executor. Mutations run on
the serial executor and wait (briefly) for the window manager to apply the
change so callers observe deterministic results.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import NextAutoDeskError
from ..models import Rect, WindowInfo, WindowSelector
from .base import ActionContext, ActionHandler

_SELECTOR_FIELDS = ("id", "title", "class_name", "instance_name", "active")


class _SelectorParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, description="Window id, e.g. '0x3a00007'")
    title: str | None = Field(default=None, description="Exact title (or regex when title_regex)")
    title_regex: bool = Field(default=False, description="Treat title as a regular expression")
    class_name: str | None = Field(default=None, description="WM_CLASS class name")
    instance_name: str | None = Field(default=None, description="WM_CLASS instance name")
    active: bool = Field(default=False, description="Select the currently active window")

    @model_validator(mode="after")
    def _has_selector(self) -> _SelectorParams:
        if not any(getattr(self, field) for field in _SELECTOR_FIELDS):
            raise ValueError(
                "at least one selector is required (id/title/class_name/instance_name/active)"
            )
        return self

    def selector(self) -> WindowSelector:
        return WindowSelector(
            id=self.id,
            title=self.title,
            title_regex=self.title_regex,
            class_name=self.class_name,
            instance_name=self.instance_name,
            active=self.active,
        )


class _GeometryParams(_SelectorParams):
    x: int | None = Field(default=None, description="Target X position")
    y: int | None = Field(default=None, description="Target Y position")
    width: int | None = Field(default=None, ge=1, description="Target width")
    height: int | None = Field(default=None, ge=1, description="Target height")


def _dump(window: WindowInfo | None) -> dict[str, Any] | None:
    return None if window is None else window.model_dump(mode="json")


def _resolve(ctx: ActionContext, params: _SelectorParams) -> WindowInfo:
    return ctx.drivers.window.get_window(params.selector())


def _refetch(ctx: ActionContext, window: WindowInfo) -> WindowInfo:
    try:
        return ctx.drivers.window.get_window(WindowSelector(id=window.id))
    except NextAutoDeskError:
        return window


def _await_state(
    ctx: ActionContext,
    window: WindowInfo,
    expected: frozenset[str],
    *,
    timeout_s: float = 2.0,
) -> WindowInfo:
    deadline = time.monotonic() + timeout_s
    current = _refetch(ctx, window)
    while current.state not in expected and time.monotonic() < deadline:
        ctx.sleep(0.05)
        current = _refetch(ctx, window)
    return current


def _await_geometry(
    ctx: ActionContext,
    window: WindowInfo,
    *,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    initial: Rect | None = None,
    timeout_s: float = 2.0,
) -> WindowInfo:
    """Wait for the window manager to settle after a move/resize.

    Exact equality with the requested value is often unattainable: several WMs
    report the *client* geometry inset by the decoration while ``wmctrl``
    commands address the frame, and terminals snap to their character grid via
    ``WM_NORMAL_HINTS``. Waiting for an exact match therefore stalled for the
    whole timeout on every operation. Return immediately on an exact match, or
    once the geometry has changed from its pre-command value and then stayed
    stable for a few polls.
    """

    deadline = time.monotonic() + timeout_s
    before = initial if initial is not None else window.geometry
    initial_key = (before.x, before.y, before.width, before.height)
    current = _refetch(ctx, window)
    last_key: tuple[int, int, int, int] | None = None
    stable = 0
    while time.monotonic() < deadline:
        geometry = current.geometry
        key = (geometry.x, geometry.y, geometry.width, geometry.height)
        if (
            (x is None or geometry.x == x)
            and (y is None or geometry.y == y)
            and (width is None or geometry.width == width)
            and (height is None or geometry.height == height)
        ):
            return current
        if key == last_key:
            stable += 1
            if stable >= 2 and key != initial_key:
                return current
        else:
            stable = 0
            last_key = key
        ctx.sleep(0.05)
        current = _refetch(ctx, window)
    return current


class WindowListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WindowList(ActionHandler[WindowListParams]):
    name: ClassVar[str] = "window.list"
    summary: ClassVar[str] = "List top-level windows"
    category: ClassVar[str] = "window"
    params_model: ClassVar[type[BaseModel]] = WindowListParams
    mutating: ClassVar[bool] = False

    def execute(self, params: WindowListParams, ctx: ActionContext) -> dict[str, Any]:
        del params
        windows = ctx.drivers.window.list_windows()
        return {"windows": [_dump(window) for window in windows], "count": len(windows)}


class WindowActiveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WindowActive(ActionHandler[WindowActiveParams]):
    name: ClassVar[str] = "window.active"
    summary: ClassVar[str] = "Get the currently active window"
    category: ClassVar[str] = "window"
    params_model: ClassVar[type[BaseModel]] = WindowActiveParams
    mutating: ClassVar[bool] = False

    def execute(self, params: WindowActiveParams, ctx: ActionContext) -> dict[str, Any]:
        del params
        return {"window": _dump(ctx.drivers.window.active_window())}


class WindowGetParams(_SelectorParams):
    pass


class WindowGet(ActionHandler[WindowGetParams]):
    name: ClassVar[str] = "window.get"
    summary: ClassVar[str] = "Get one window by selector"
    category: ClassVar[str] = "window"
    params_model: ClassVar[type[BaseModel]] = WindowGetParams
    mutating: ClassVar[bool] = False

    def execute(self, params: WindowGetParams, ctx: ActionContext) -> dict[str, Any]:
        return {"window": _dump(_resolve(ctx, params))}


class WindowActivateParams(_SelectorParams):
    pass


class WindowActivate(ActionHandler[WindowActivateParams]):
    name: ClassVar[str] = "window.activate"
    summary: ClassVar[str] = "Activate (focus) a window"
    category: ClassVar[str] = "window"
    params_model: ClassVar[type[BaseModel]] = WindowActivateParams

    def execute(self, params: WindowActivateParams, ctx: ActionContext) -> dict[str, Any]:
        window = _resolve(ctx, params)
        ctx.drivers.window.activate(window)
        return {"window": _dump(_await_state(ctx, window, frozenset({"normal", "maximized", "fullscreen"})))}


class WindowMoveParams(_SelectorParams):
    x: int = Field(description="Target X position")
    y: int = Field(description="Target Y position")


class WindowMove(ActionHandler[WindowMoveParams]):
    name: ClassVar[str] = "window.move"
    summary: ClassVar[str] = "Move a window"
    category: ClassVar[str] = "window"
    params_model: ClassVar[type[BaseModel]] = WindowMoveParams

    def execute(self, params: WindowMoveParams, ctx: ActionContext) -> dict[str, Any]:
        window = _resolve(ctx, params)
        before = window.geometry.model_copy(deep=True)
        ctx.drivers.window.move(window, params.x, params.y)
        return {
            "window": _dump(
                _await_geometry(ctx, window, x=params.x, y=params.y, initial=before)
            )
        }


class WindowResizeParams(_SelectorParams):
    width: int = Field(ge=1, description="Target width")
    height: int = Field(ge=1, description="Target height")


class WindowResize(ActionHandler[WindowResizeParams]):
    name: ClassVar[str] = "window.resize"
    summary: ClassVar[str] = "Resize a window"
    category: ClassVar[str] = "window"
    params_model: ClassVar[type[BaseModel]] = WindowResizeParams

    def execute(self, params: WindowResizeParams, ctx: ActionContext) -> dict[str, Any]:
        window = _resolve(ctx, params)
        before = window.geometry.model_copy(deep=True)
        ctx.drivers.window.resize(window, params.width, params.height)
        return {
            "window": _dump(
                _await_geometry(
                    ctx,
                    window,
                    width=params.width,
                    height=params.height,
                    initial=before,
                )
            )
        }


class WindowMinimizeParams(_SelectorParams):
    pass


class WindowMinimize(ActionHandler[WindowMinimizeParams]):
    name: ClassVar[str] = "window.minimize"
    summary: ClassVar[str] = "Minimize a window"
    category: ClassVar[str] = "window"
    params_model: ClassVar[type[BaseModel]] = WindowMinimizeParams

    def execute(self, params: WindowMinimizeParams, ctx: ActionContext) -> dict[str, Any]:
        window = _resolve(ctx, params)
        ctx.drivers.window.minimize(window)
        return {"window": _dump(_await_state(ctx, window, frozenset({"minimized"})))}


class WindowMaximizeParams(_SelectorParams):
    pass


class WindowMaximize(ActionHandler[WindowMaximizeParams]):
    name: ClassVar[str] = "window.maximize"
    summary: ClassVar[str] = "Maximize a window"
    category: ClassVar[str] = "window"
    params_model: ClassVar[type[BaseModel]] = WindowMaximizeParams

    def execute(self, params: WindowMaximizeParams, ctx: ActionContext) -> dict[str, Any]:
        window = _resolve(ctx, params)
        ctx.drivers.window.maximize(window)
        return {"window": _dump(_await_state(ctx, window, frozenset({"maximized"})))}


class WindowRestoreParams(_SelectorParams):
    pass


class WindowRestore(ActionHandler[WindowRestoreParams]):
    name: ClassVar[str] = "window.restore"
    summary: ClassVar[str] = "Restore a minimized/maximized window"
    category: ClassVar[str] = "window"
    params_model: ClassVar[type[BaseModel]] = WindowRestoreParams

    def execute(self, params: WindowRestoreParams, ctx: ActionContext) -> dict[str, Any]:
        window = _resolve(ctx, params)
        ctx.drivers.window.restore(window)
        return {"window": _dump(_await_state(ctx, window, frozenset({"normal"})))}


class WindowCloseParams(_SelectorParams):
    pass


class WindowClose(ActionHandler[WindowCloseParams]):
    name: ClassVar[str] = "window.close"
    summary: ClassVar[str] = "Ask a window to close"
    category: ClassVar[str] = "window"
    params_model: ClassVar[type[BaseModel]] = WindowCloseParams

    def execute(self, params: WindowCloseParams, ctx: ActionContext) -> dict[str, Any]:
        window = _resolve(ctx, params)
        ctx.drivers.window.close(window)
        deadline = time.monotonic() + 2.0
        closed = False
        while time.monotonic() < deadline:
            ctx.sleep(0.05)
            try:
                ctx.drivers.window.get_window(WindowSelector(id=window.id))
            except NextAutoDeskError:
                closed = True
                break
        return {"window": _dump(window), "closed": closed}


ACTIONS: list[ActionHandler] = [
    WindowList(),
    WindowActive(),
    WindowGet(),
    WindowActivate(),
    WindowMove(),
    WindowResize(),
    WindowMinimize(),
    WindowMaximize(),
    WindowRestore(),
    WindowClose(),
]

__all__ = ["ACTIONS"]
