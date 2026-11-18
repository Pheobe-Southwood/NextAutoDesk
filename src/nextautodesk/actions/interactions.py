"""Composite interaction actions (API tier: ``composite``).

``interaction.click`` and ``interaction.type`` combine perception with the
existing primitive actions: they locate a target with ``element.wait`` /
``element.find`` and then delegate the actual input to ``mouse.click``,
``keyboard.hotkey``, ``keyboard.type`` and ``keyboard.press`` through
:meth:`ActionContext.invoke`.

No mouse/keyboard/driver logic is duplicated here, so strategy configuration,
human-like pacing, cancellation and strategy overrides keep working exactly as
they do for the primitive actions. Both actions are mutating, so they run on
the serial desktop queue.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..errors import ElementNotFound
from ..models import Point
from ..perception.models import ElementQuery, summarize_query
from .base import ActionContext, ActionHandler


class InteractionClickParams(BaseModel):
    """Parameters of ``interaction.click``."""

    model_config = ConfigDict(extra="forbid")

    query: ElementQuery = Field(description="Element query used to locate the click target.")
    button: Literal["left", "right", "middle"] = Field(
        default="left", description="Mouse button to click."
    )
    clicks: int = Field(default=1, ge=1, le=3, description="Number of clicks.")
    interval_ms: float | None = Field(
        default=None, ge=0, le=60_000, description="Pause between clicks when clicks > 1."
    )
    offset_x: float = Field(
        default=0, ge=-10_000, le=10_000, description="X offset from the element center."
    )
    offset_y: float = Field(
        default=0, ge=-10_000, le=10_000, description="Y offset from the element center."
    )
    wait_timeout_ms: int = Field(
        default=2000, ge=0, le=600_000, description="Wait for the element; 0 = locate once."
    )
    strategy: str | None = Field(
        default=None, description="Strategy provider override forwarded to the nested actions."
    )


class InteractionTypeParams(BaseModel):
    """Parameters of ``interaction.type``."""

    model_config = ConfigDict(extra="forbid")

    query: ElementQuery = Field(description="Element query used to locate the input field.")
    text: str = Field(min_length=1, max_length=100_000, description="Text to type.")
    clear: bool = Field(
        default=False, description="Select and replace existing content (ctrl+a) before typing."
    )
    submit: bool = Field(default=False, description="Press Enter after typing.")
    wait_timeout_ms: int = Field(
        default=2000, ge=0, le=600_000, description="Wait for the element; 0 = locate once."
    )
    strategy: str | None = Field(
        default=None, description="Strategy provider override forwarded to the nested actions."
    )


def _locate_target(
    ctx: ActionContext,
    query: ElementQuery,
    wait_timeout_ms: int,
    action: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Locate the target through the perception actions and pick the best match."""

    if wait_timeout_ms > 0:
        located = ctx.invoke(
            "element.wait",
            {"query": query, "timeout_ms": wait_timeout_ms},
        )
    else:
        located = ctx.invoke("element.find", {"query": query})
    matches = located.get("matches") or []
    if not matches:
        raise ElementNotFound(
            f"{action}: no element matched the query",
            details={"action": action, "query": summarize_query(query)},
        )
    match = max(matches, key=lambda item: float(item.get("confidence", 0.0)))
    return located, match


def _click_target(
    ctx: ActionContext,
    match: dict[str, Any],
    *,
    button: str,
    clicks: int,
    interval_ms: float | None,
    offset_x: float,
    offset_y: float,
    strategy: str | None,
) -> tuple[Point, dict[str, Any]]:
    """Click on the element center plus the requested offset via ``mouse.click``."""

    center = match.get("center") or {}
    position = Point(
        x=float(center.get("x", 0.0)) + offset_x,
        y=float(center.get("y", 0.0)) + offset_y,
    )
    output = ctx.invoke(
        "mouse.click",
        {
            "x": position.x,
            "y": position.y,
            "button": button,
            "clicks": clicks,
            "interval_ms": interval_ms,
            "strategy": strategy,
        },
    )
    return position, output


def _attempts(located: dict[str, Any]) -> int:
    return int(located.get("attempts", 1) or 1)


class InteractionClick(ActionHandler[InteractionClickParams]):
    """Locate an element and click it."""

    name: ClassVar[str] = "interaction.click"
    summary: ClassVar[str] = "Locate an element by perception and click it"
    category: ClassVar[str] = "interactions"
    tier: ClassVar[str] = "composite"
    params_model: ClassVar[type[BaseModel]] = InteractionClickParams
    example: ClassVar[dict[str, Any]] = {
        "query": {"locator": "text", "text": {"text": "Submit", "match": "contains"}},
        "button": "left",
        "wait_timeout_ms": 5000,
    }

    def execute(self, params: InteractionClickParams, ctx: ActionContext) -> dict[str, Any]:
        located, match = _locate_target(ctx, params.query, params.wait_timeout_ms, self.name)
        position, click_output = _click_target(
            ctx,
            match,
            button=params.button,
            clicks=params.clicks,
            interval_ms=params.interval_ms,
            offset_x=params.offset_x,
            offset_y=params.offset_y,
            strategy=params.strategy,
        )
        return {
            "target": match,
            "position": {"x": position.x, "y": position.y},
            "click": click_output,
            "attempts": _attempts(located),
        }


class InteractionType(ActionHandler[InteractionTypeParams]):
    """Locate an input element, click it and type text into it."""

    name: ClassVar[str] = "interaction.type"
    summary: ClassVar[str] = "Locate an input element, focus it and type text"
    category: ClassVar[str] = "interactions"
    tier: ClassVar[str] = "composite"
    params_model: ClassVar[type[BaseModel]] = InteractionTypeParams
    example: ClassVar[dict[str, Any]] = {
        "query": {"locator": "text", "text": {"text": "Username", "match": "contains"}},
        "text": "demo user",
        "clear": True,
        "submit": True,
    }

    def execute(self, params: InteractionTypeParams, ctx: ActionContext) -> dict[str, Any]:
        located, match = _locate_target(ctx, params.query, params.wait_timeout_ms, self.name)
        _, click_output = _click_target(
            ctx,
            match,
            button="left",
            clicks=1,
            interval_ms=None,
            offset_x=0.0,
            offset_y=0.0,
            strategy=params.strategy,
        )
        if params.clear:
            ctx.invoke(
                "keyboard.hotkey",
                {"keys": ["ctrl", "a"], "strategy": params.strategy},
            )
        typed = ctx.invoke(
            "keyboard.type",
            {"text": params.text, "strategy": params.strategy},
        )
        if params.submit:
            ctx.invoke(
                "keyboard.press",
                {"key": "enter", "strategy": params.strategy},
            )
        return {
            "target": match,
            "click": click_output,
            "typed": typed,
            "cleared": params.clear,
            "submitted": params.submit,
            "attempts": _attempts(located),
        }


ACTIONS: list[ActionHandler] = [InteractionClick(), InteractionType()]

__all__ = [
    "ACTIONS",
    "InteractionClick",
    "InteractionClickParams",
    "InteractionType",
    "InteractionTypeParams",
]
