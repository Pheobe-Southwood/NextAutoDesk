"""Mouse actions exposed to the HTTP/task layers.

Every input action is executed through the strategy layer
(:meth:`ActionContext.run_strategy`) so the configured behaviour provider decides
how the pointer moves and how clicks are paced. Optional coordinates compose two
strategy operations (move, then the requested gesture).
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import MouseButton
from ..operations import Operation
from ..strategies.base import OperationRequest
from .base import ActionContext, ActionHandler

_CLICK_OPERATIONS: dict[str, Operation] = {
    "left": Operation.MOUSE_CLICK_LEFT,
    "right": Operation.MOUSE_CLICK_RIGHT,
    "middle": Operation.MOUSE_CLICK_MIDDLE,
}


class _MouseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str | None = Field(
        default=None, description="Strategy provider override for this action"
    )


class _PositionalParams(_MouseParams):
    x: float | None = Field(default=None, description="Optional absolute X coordinate")
    y: float | None = Field(default=None, description="Optional absolute Y coordinate")

    @model_validator(mode="after")
    def _both_coordinates(self) -> _PositionalParams:
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be provided together")
        return self


def _position(ctx: ActionContext) -> dict[str, float]:
    point = ctx.drivers.mouse.position()
    return {"x": float(point.x), "y": float(point.y)}


def _move_if_requested(
    params: _PositionalParams,
    ctx: ActionContext,
    *,
    duration_ms: float | None = None,
) -> None:
    if params.x is None or params.y is None:
        return
    ctx.run_strategy(
        Operation.MOUSE_MOVE,
        OperationRequest(
            operation=Operation.MOUSE_MOVE,
            x=params.x,
            y=params.y,
            duration_ms=duration_ms,
        ),
        strategy_override=params.strategy,
    )


class MouseMoveParams(_MouseParams):
    x: float = Field(description="Target X coordinate in desktop pixels")
    y: float = Field(description="Target Y coordinate in desktop pixels")
    duration_ms: float | None = Field(
        default=None, ge=0, le=600_000, description="Optional movement duration hint"
    )


class MouseMove(ActionHandler[MouseMoveParams]):
    name: ClassVar[str] = "mouse.move"
    summary: ClassVar[str] = "Move the pointer to an absolute position"
    category: ClassVar[str] = "mouse"
    params_model: ClassVar[type[BaseModel]] = MouseMoveParams
    example: ClassVar[dict[str, Any]] = {"x": 640, "y": 360}

    def execute(self, params: MouseMoveParams, ctx: ActionContext) -> dict[str, Any]:
        ctx.run_strategy(
            Operation.MOUSE_MOVE,
            OperationRequest(
                operation=Operation.MOUSE_MOVE,
                x=params.x,
                y=params.y,
                duration_ms=params.duration_ms,
            ),
            strategy_override=params.strategy,
        )
        return {
            "position": _position(ctx),
            "requested": {"x": params.x, "y": params.y},
            "duration_ms": params.duration_ms,
        }


class MouseMoveRelativeParams(_MouseParams):
    dx: float = Field(description="Relative X offset in pixels")
    dy: float = Field(description="Relative Y offset in pixels")
    duration_ms: float | None = Field(default=None, ge=0, le=600_000)

    @model_validator(mode="after")
    def _movement(self) -> MouseMoveRelativeParams:
        if self.dx == 0 and self.dy == 0:
            raise ValueError("dx or dy must be non-zero")
        return self


class MouseMoveRelative(ActionHandler[MouseMoveRelativeParams]):
    name: ClassVar[str] = "mouse.move_relative"
    summary: ClassVar[str] = "Move the pointer by a relative offset"
    category: ClassVar[str] = "mouse"
    params_model: ClassVar[type[BaseModel]] = MouseMoveRelativeParams
    example: ClassVar[dict[str, Any]] = {"dx": 120, "dy": -40}

    def execute(self, params: MouseMoveRelativeParams, ctx: ActionContext) -> dict[str, Any]:
        ctx.run_strategy(
            Operation.MOUSE_MOVE_RELATIVE,
            OperationRequest(
                operation=Operation.MOUSE_MOVE_RELATIVE,
                dx=params.dx,
                dy=params.dy,
                duration_ms=params.duration_ms,
            ),
            strategy_override=params.strategy,
        )
        return {"position": _position(ctx), "delta": {"dx": params.dx, "dy": params.dy}}


class MouseClickParams(_PositionalParams):
    button: MouseButton = Field(default="left", description="Button: left, right or middle")
    clicks: int = Field(default=1, ge=1, le=3, description="Number of clicks (2 = double click)")
    interval_ms: float | None = Field(
        default=None,
        ge=0,
        le=600_000,
        description="Optional gap between clicks when clicks > 1",
    )



class MouseClick(ActionHandler[MouseClickParams]):
    name: ClassVar[str] = "mouse.click"
    summary: ClassVar[str] = "Click a mouse button, optionally at a position"
    category: ClassVar[str] = "mouse"
    params_model: ClassVar[type[BaseModel]] = MouseClickParams
    example: ClassVar[dict[str, Any]] = {"x": 320, "y": 240, "button": "left"}

    def execute(self, params: MouseClickParams, ctx: ActionContext) -> dict[str, Any]:
        _move_if_requested(params, ctx)
        button: MouseButton = params.button
        operation = _CLICK_OPERATIONS[params.button]
        if params.interval_ms is not None and params.clicks > 1:
            for index in range(params.clicks):
                if index:
                    ctx.sleep(params.interval_ms / 1000.0)
                ctx.run_strategy(
                    operation,
                    OperationRequest(operation=operation, button=button),
                    strategy_override=params.strategy,
                )
        elif params.clicks > 2:
            ctx.run_strategy(
                operation,
                OperationRequest(
                    operation=operation, button=button, clicks=params.clicks
                ),
                strategy_override=params.strategy,
            )
        elif params.clicks == 2:
            ctx.run_strategy(
                Operation.MOUSE_DOUBLE_CLICK,
                OperationRequest(
                    operation=Operation.MOUSE_DOUBLE_CLICK, button=button
                ),
                strategy_override=params.strategy,
            )
        else:
            ctx.run_strategy(
                operation,
                OperationRequest(operation=operation, button=button),
                strategy_override=params.strategy,
            )
        return {
            "position": _position(ctx),
            "button": params.button,
            "clicks": params.clicks,
        }


class MouseDownParams(_PositionalParams):
    button: MouseButton = Field(default="left", description="Button: left, right or middle")



class MouseDown(ActionHandler[MouseDownParams]):
    name: ClassVar[str] = "mouse.down"
    summary: ClassVar[str] = "Press and hold a mouse button"
    category: ClassVar[str] = "mouse"
    params_model: ClassVar[type[BaseModel]] = MouseDownParams
    example: ClassVar[dict[str, Any]] = {"button": "left"}

    def execute(self, params: MouseDownParams, ctx: ActionContext) -> dict[str, Any]:
        _move_if_requested(params, ctx)
        button: MouseButton = params.button
        ctx.run_strategy(
            Operation.MOUSE_DOWN,
            OperationRequest(operation=Operation.MOUSE_DOWN, button=button),
            strategy_override=params.strategy,
        )
        return {"position": _position(ctx), "button": params.button, "pressed": True}


class MouseUpParams(_PositionalParams):
    button: MouseButton = Field(default="left", description="Button: left, right or middle")



class MouseUp(ActionHandler[MouseUpParams]):
    name: ClassVar[str] = "mouse.up"
    summary: ClassVar[str] = "Release a mouse button"
    category: ClassVar[str] = "mouse"
    params_model: ClassVar[type[BaseModel]] = MouseUpParams
    example: ClassVar[dict[str, Any]] = {"button": "left"}

    def execute(self, params: MouseUpParams, ctx: ActionContext) -> dict[str, Any]:
        _move_if_requested(params, ctx)
        button: MouseButton = params.button
        ctx.run_strategy(
            Operation.MOUSE_UP,
            OperationRequest(operation=Operation.MOUSE_UP, button=button),
            strategy_override=params.strategy,
        )
        return {"position": _position(ctx), "button": params.button, "pressed": False}


class MouseHoldParams(_PositionalParams):
    button: MouseButton = Field(default="left", description="Button: left, right or middle")
    hold_ms: float | None = Field(
        default=None, ge=0, le=600_000, description="Hold duration in milliseconds"
    )



class MouseHold(ActionHandler[MouseHoldParams]):
    name: ClassVar[str] = "mouse.hold"
    summary: ClassVar[str] = "Hold a mouse button for a duration"
    category: ClassVar[str] = "mouse"
    params_model: ClassVar[type[BaseModel]] = MouseHoldParams
    example: ClassVar[dict[str, Any]] = {"button": "left", "hold_ms": 500}

    def execute(self, params: MouseHoldParams, ctx: ActionContext) -> dict[str, Any]:
        _move_if_requested(params, ctx)
        button: MouseButton = params.button
        ctx.run_strategy(
            Operation.MOUSE_HOLD,
            OperationRequest(
                operation=Operation.MOUSE_HOLD,
                button=button,
                hold_ms=params.hold_ms,
            ),
            strategy_override=params.strategy,
        )
        return {"position": _position(ctx), "button": params.button, "hold_ms": params.hold_ms}


class MouseDragParams(_MouseParams):
    from_x: float | None = Field(default=None, description="Optional start X coordinate")
    from_y: float | None = Field(default=None, description="Optional start Y coordinate")
    to_x: float = Field(description="Target X coordinate")
    to_y: float = Field(description="Target Y coordinate")
    button: MouseButton = Field(default="left", description="Button: left, right or middle")
    duration_ms: float | None = Field(default=None, ge=0, le=600_000)

    @model_validator(mode="after")
    def _validate(self) -> MouseDragParams:
        if (self.from_x is None) != (self.from_y is None):
            raise ValueError("from_x and from_y must be provided together")
        return self


class MouseDrag(ActionHandler[MouseDragParams]):
    name: ClassVar[str] = "mouse.drag"
    summary: ClassVar[str] = "Drag the pointer with a held button"
    category: ClassVar[str] = "mouse"
    params_model: ClassVar[type[BaseModel]] = MouseDragParams
    example: ClassVar[dict[str, Any]] = {
        "from_x": 100,
        "from_y": 120,
        "to_x": 400,
        "to_y": 300,
    }

    def execute(self, params: MouseDragParams, ctx: ActionContext) -> dict[str, Any]:
        if params.from_x is not None and params.from_y is not None:
            ctx.run_strategy(
                Operation.MOUSE_MOVE,
                OperationRequest(
                    operation=Operation.MOUSE_MOVE, x=params.from_x, y=params.from_y
                ),
                strategy_override=params.strategy,
            )
        button: MouseButton = params.button
        ctx.run_strategy(
            Operation.MOUSE_DRAG,
            OperationRequest(
                operation=Operation.MOUSE_DRAG,
                x=params.to_x,
                y=params.to_y,
                button=button,
                duration_ms=params.duration_ms,
            ),
            strategy_override=params.strategy,
        )
        return {
            "position": _position(ctx),
            "to": {"x": params.to_x, "y": params.to_y},
            "button": params.button,
        }


class MouseScrollParams(_PositionalParams):
    dx: int = Field(default=0, description="Horizontal detents (positive scrolls right)")
    dy: int = Field(default=0, description="Vertical detents (positive scrolls down)")

    @model_validator(mode="after")
    def _movement(self) -> MouseScrollParams:
        if self.dx == 0 and self.dy == 0:
            raise ValueError("dx or dy must be non-zero")
        return self


class MouseScroll(ActionHandler[MouseScrollParams]):
    name: ClassVar[str] = "mouse.scroll"
    summary: ClassVar[str] = "Scroll the wheel vertically and/or horizontally"
    category: ClassVar[str] = "mouse"
    params_model: ClassVar[type[BaseModel]] = MouseScrollParams
    example: ClassVar[dict[str, Any]] = {"dy": -3}

    def execute(self, params: MouseScrollParams, ctx: ActionContext) -> dict[str, Any]:
        _move_if_requested(params, ctx)
        if params.dy:
            ctx.run_strategy(
                Operation.MOUSE_SCROLL_VERTICAL,
                OperationRequest(
                    operation=Operation.MOUSE_SCROLL_VERTICAL, dy=float(params.dy)
                ),
                strategy_override=params.strategy,
            )
        if params.dx:
            ctx.run_strategy(
                Operation.MOUSE_SCROLL_HORIZONTAL,
                OperationRequest(
                    operation=Operation.MOUSE_SCROLL_HORIZONTAL, dx=float(params.dx)
                ),
                strategy_override=params.strategy,
            )
        return {
            "position": _position(ctx),
            "dx": params.dx,
            "dy": params.dy,
            "detents": abs(params.dx) + abs(params.dy),
        }


class MousePositionParams(_MouseParams):
    pass


class MousePosition(ActionHandler[MousePositionParams]):
    name: ClassVar[str] = "mouse.position"
    summary: ClassVar[str] = "Get the current pointer position"
    category: ClassVar[str] = "mouse"
    params_model: ClassVar[type[BaseModel]] = MousePositionParams
    mutating: ClassVar[bool] = False

    def execute(self, params: MousePositionParams, ctx: ActionContext) -> dict[str, Any]:
        del params
        return _position(ctx)


class MouseStateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MouseState(ActionHandler[MouseStateParams]):
    name: ClassVar[str] = "mouse.state"
    summary: ClassVar[str] = "Get the pointer position and currently pressed buttons"
    category: ClassVar[str] = "mouse"
    params_model: ClassVar[type[BaseModel]] = MouseStateParams
    mutating: ClassVar[bool] = False

    def execute(self, params: MouseStateParams, ctx: ActionContext) -> dict[str, Any]:
        del params
        point = ctx.drivers.mouse.position()
        buttons = sorted(ctx.drivers.mouse.buttons_down())
        return {
            "position": {"x": float(point.x), "y": float(point.y)},
            "buttons": buttons,
        }


ACTIONS: list[ActionHandler] = [
    MouseMove(),
    MouseMoveRelative(),
    MouseClick(),
    MouseDown(),
    MouseUp(),
    MouseHold(),
    MouseDrag(),
    MouseScroll(),
    MousePosition(),
    MouseState(),
]

__all__ = ["ACTIONS"]
