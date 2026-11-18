"""Direct (non-humanised) execution strategy.

Calls the driver primitives immediately, with no trajectory/behaviour modelling.
It is only used when explicitly selected (per operation or per request); the
default for every operation is ``human_movement``.

Holds and inter-key gaps are cancel-aware; everything else is a single primitive
call, which makes this provider useful for deterministic tests and for
workloads where latency matters more than natural motion.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...errors import StrategyExecutionFailed, TaskCancelled
from ...models import MouseButton, StrategyOutcome
from ...operations import ALL_OPERATIONS, Operation
from ..base import OperationRequest, StrategyContext, StrategyProvider


class DirectParams(BaseModel):
    """Validated provider parameters (merged from configuration)."""

    model_config = ConfigDict(extra="forbid")

    hold_default_ms: float = Field(
        default=100.0, ge=0, le=600_000, description="Default hold duration for mouse/key holds"
    )
    sequence_interval_ms: float = Field(
        default=120.0,
        ge=0,
        le=600_000,
        description="Gap between keys of a key sequence",
    )


def _require(request: OperationRequest, *names: str) -> None:
    missing = [name for name in names if getattr(request, name) is None]
    if missing:
        raise StrategyExecutionFailed(
            f"operation {request.operation.value} requires fields: {missing}",
            details={"operation": request.operation.value, "missing": missing},
        )


def _hold_seconds(request: OperationRequest, cfg: DirectParams) -> float:
    hold_ms = request.hold_ms if request.hold_ms is not None else cfg.hold_default_ms
    return max(0.0, float(hold_ms) / 1000.0)


class DirectProvider(StrategyProvider):
    """Immediate, non-humanised execution for every operation."""

    name: ClassVar[str] = "direct"
    version: ClassVar[str] = "0.1.0"
    description: ClassVar[str] = "Immediate driver calls without human-like pacing"
    supported: ClassVar[frozenset[Operation]] = frozenset(ALL_OPERATIONS)

    def validate_params(self, params: Mapping[str, Any]) -> None:
        try:
            DirectParams.model_validate(dict(params))
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
                for error in exc.errors()
            )
            raise ValueError(f"invalid direct parameters: {details}") from exc

    def execute(
        self,
        request: OperationRequest,
        params: Mapping[str, Any],
        ctx: StrategyContext,
    ) -> StrategyOutcome:
        self._ensure_supported(request.operation)
        cfg = DirectParams.model_validate(dict(params))
        drivers = ctx.drivers
        started = time.monotonic()
        events = 0
        operation = request.operation

        def check() -> None:
            ctx.check_cancelled()

        if operation is Operation.MOUSE_MOVE:
            _require(request, "x", "y")
            check()
            drivers.mouse.move_to(
                request.x, request.y, duration_s=(request.duration_ms or 0.0) / 1000.0
            )
            events += 1
        elif operation is Operation.MOUSE_MOVE_RELATIVE:
            _require(request, "dx", "dy")
            check()
            current = drivers.mouse.position()
            drivers.mouse.move_to(
                current.x + request.dx,
                current.y + request.dy,
                duration_s=(request.duration_ms or 0.0) / 1000.0,
            )
            events += 1
        elif operation in {
            Operation.MOUSE_CLICK_LEFT,
            Operation.MOUSE_CLICK_RIGHT,
            Operation.MOUSE_CLICK_MIDDLE,
        }:
            button: MouseButton = {
                Operation.MOUSE_CLICK_LEFT: "left",
                Operation.MOUSE_CLICK_RIGHT: "right",
                Operation.MOUSE_CLICK_MIDDLE: "middle",
            }[operation]
            events += self._click(drivers, button, max(1, int(request.clicks or 1)), check)
        elif operation is Operation.MOUSE_DOUBLE_CLICK:
            events += self._click(
                drivers, request.button or "left", 2, check
            )
        elif operation is Operation.MOUSE_DOWN:
            check()
            drivers.mouse.button_down(request.button or "left")
            events += 1
        elif operation is Operation.MOUSE_UP:
            check()
            drivers.mouse.button_up(request.button or "left")
            events += 1
        elif operation is Operation.MOUSE_HOLD:
            button = request.button or "left"
            check()
            drivers.mouse.button_down(button)
            events += 1
            try:
                ctx.sleep(_hold_seconds(request, cfg))
            except TaskCancelled:
                _release_mouse_quietly(drivers, button, ctx)
                raise
            drivers.mouse.button_up(button)
            events += 1
        elif operation is Operation.MOUSE_DRAG:
            _require(request, "x", "y")
            button = request.button or "left"
            check()
            drivers.mouse.button_down(button)
            events += 1
            try:
                drivers.mouse.move_to(
                    request.x,
                    request.y,
                    duration_s=(request.duration_ms or 0.0) / 1000.0,
                )
            finally:
                drivers.mouse.button_up(button)
            events += 2
        elif operation is Operation.MOUSE_SCROLL_VERTICAL:
            _require(request, "dy")
            check()
            drivers.mouse.scroll(0, int(request.dy))
            events += 1
        elif operation is Operation.MOUSE_SCROLL_HORIZONTAL:
            _require(request, "dx")
            check()
            drivers.mouse.scroll(int(request.dx), 0)
            events += 1
        elif operation is Operation.KEYBOARD_TYPE_TEXT:
            _require(request, "text")
            check()
            drivers.keyboard.type_text(request.text)
            events += len(request.text)
        elif operation is Operation.KEYBOARD_KEY_PRESS:
            _require(request, "key")
            check()
            drivers.keyboard.key_down(request.key)
            drivers.keyboard.key_up(request.key)
            events += 2
        elif operation is Operation.KEYBOARD_KEY_DOWN:
            _require(request, "key")
            check()
            drivers.keyboard.key_down(request.key)
            events += 1
        elif operation is Operation.KEYBOARD_KEY_RELEASE:
            _require(request, "key")
            check()
            drivers.keyboard.key_up(request.key)
            events += 1
        elif operation is Operation.KEYBOARD_KEY_HOLD:
            _require(request, "key")
            check()
            drivers.keyboard.key_down(request.key)
            events += 1
            try:
                ctx.sleep(_hold_seconds(request, cfg))
            except TaskCancelled:
                _release_key_quietly(drivers, request.key, ctx)
                raise
            drivers.keyboard.key_up(request.key)
            events += 1
        elif operation is Operation.KEYBOARD_HOTKEY:
            if not request.keys:
                raise StrategyExecutionFailed(
                    "keyboard_hotkey requires at least one key",
                    details={"operation": operation.value},
                )
            check()
            pressed: list[str] = []
            try:
                for key in request.keys:
                    drivers.keyboard.key_down(key)
                    pressed.append(key)
                    events += 1
                ctx.sleep(0.0)
            finally:
                for key in reversed(pressed):
                    try:
                        drivers.keyboard.key_up(key)
                    except Exception:  # pragma: no cover - best effort release
                        ctx.logger.exception("direct: failed to release hotkey key %r", key)
            events += len(pressed)
        elif operation is Operation.KEYBOARD_KEY_SEQUENCE:
            if not request.keys:
                raise StrategyExecutionFailed(
                    "keyboard_key_sequence requires at least one key",
                    details={"operation": operation.value},
                )
            interval_s = max(0.0, float(cfg.sequence_interval_ms) / 1000.0)
            for index, key in enumerate(request.keys):
                if index:
                    ctx.sleep(interval_s)
                check()
                drivers.keyboard.key_down(key)
                drivers.keyboard.key_up(key)
                events += 2
        else:  # pragma: no cover - supported covers all operations
            raise StrategyExecutionFailed(
                f"direct provider cannot dispatch {operation.value!r}",
                details={"operation": operation.value},
            )

        return self._outcome(
            request,
            started=started,
            engine="direct",
            details={
                "events": events,
                "hold_default_ms": cfg.hold_default_ms,
                "sequence_interval_ms": cfg.sequence_interval_ms,
            },
        )

    @staticmethod
    def _click(drivers, button: MouseButton, clicks: int, check) -> int:
        for index in range(clicks):
            if index:
                check()
            drivers.mouse.button_down(button)
            drivers.mouse.button_up(button)
        return clicks * 2


def _release_mouse_quietly(drivers, button: MouseButton, ctx: StrategyContext) -> None:
    try:
        drivers.mouse.button_up(button)
    except Exception:  # pragma: no cover - best effort during cancellation
        ctx.logger.exception("direct: failed to release mouse button %r", button)


def _release_key_quietly(drivers, key: str, ctx: StrategyContext) -> None:
    try:
        drivers.keyboard.key_up(key)
    except Exception:  # pragma: no cover - best effort during cancellation
        ctx.logger.exception("direct: failed to release key %r", key)


__all__ = ["DirectParams", "DirectProvider"]
