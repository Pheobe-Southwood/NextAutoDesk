"""Keyboard actions exposed to the HTTP/task layers.

Input actions are routed through the strategy layer, so the configured provider
(human_movement by default) owns typing rhythm, key hold timing and shortcut
pacing.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ..drivers.base import DriverError
from ..drivers.pyautogui_x11 import normalize_key_name
from ..errors import InvalidArgument
from ..operations import Operation
from ..strategies.base import OperationRequest
from .base import ActionContext, ActionHandler


class _KeyboardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str | None = Field(
        default=None, description="Strategy provider override for this action"
    )


def _check_key(key: str) -> str:
    try:
        return normalize_key_name(key)
    except DriverError as exc:
        raise InvalidArgument(str(exc), details={"key": key}) from exc


def _check_keys(keys: list[str]) -> list[str]:
    if not keys:
        raise InvalidArgument("at least one key is required")
    return [_check_key(key) for key in keys]


class KeyboardTypeParams(_KeyboardParams):
    text: str = Field(description="Text to type (Unicode supported)")


class KeyboardType(ActionHandler[KeyboardTypeParams]):
    name: ClassVar[str] = "keyboard.type"
    summary: ClassVar[str] = "Type a string of text"
    category: ClassVar[str] = "keyboard"
    params_model: ClassVar[type[BaseModel]] = KeyboardTypeParams
    example: ClassVar[dict[str, Any]] = {"text": "hello world"}

    def execute(self, params: KeyboardTypeParams, ctx: ActionContext) -> dict[str, Any]:
        ctx.run_strategy(
            Operation.KEYBOARD_TYPE_TEXT,
            OperationRequest(operation=Operation.KEYBOARD_TYPE_TEXT, text=params.text),
            strategy_override=params.strategy,
        )
        return {"typed": params.text, "length": len(params.text)}


class KeyboardPressParams(_KeyboardParams):
    key: str = Field(description="Key name, e.g. 'enter', 'a', 'f5', 'pageup'")


class KeyboardPress(ActionHandler[KeyboardPressParams]):
    name: ClassVar[str] = "keyboard.press"
    summary: ClassVar[str] = "Press and release a single key"
    category: ClassVar[str] = "keyboard"
    params_model: ClassVar[type[BaseModel]] = KeyboardPressParams
    example: ClassVar[dict[str, Any]] = {"key": "enter"}

    def execute(self, params: KeyboardPressParams, ctx: ActionContext) -> dict[str, Any]:
        key = _check_key(params.key)
        ctx.run_strategy(
            Operation.KEYBOARD_KEY_PRESS,
            OperationRequest(operation=Operation.KEYBOARD_KEY_PRESS, key=key),
            strategy_override=params.strategy,
        )
        return {"key": key}


class KeyboardHotkeyParams(_KeyboardParams):
    keys: list[str] = Field(
        min_length=1, description="Keys pressed together, e.g. ['ctrl', 'c']"
    )


class KeyboardHotkey(ActionHandler[KeyboardHotkeyParams]):
    name: ClassVar[str] = "keyboard.hotkey"
    summary: ClassVar[str] = "Press a key combination"
    category: ClassVar[str] = "keyboard"
    params_model: ClassVar[type[BaseModel]] = KeyboardHotkeyParams
    example: ClassVar[dict[str, Any]] = {"keys": ["ctrl", "c"]}

    def execute(self, params: KeyboardHotkeyParams, ctx: ActionContext) -> dict[str, Any]:
        keys = _check_keys(list(params.keys))
        ctx.run_strategy(
            Operation.KEYBOARD_HOTKEY,
            OperationRequest(operation=Operation.KEYBOARD_HOTKEY, keys=tuple(keys)),
            strategy_override=params.strategy,
        )
        return {"keys": keys}


class KeyboardSequenceParams(_KeyboardParams):
    keys: list[str] = Field(
        min_length=1, description="Keys pressed one after another, e.g. ['tab', 'enter']"
    )


class KeyboardSequence(ActionHandler[KeyboardSequenceParams]):
    name: ClassVar[str] = "keyboard.sequence"
    summary: ClassVar[str] = "Press several keys in sequence"
    category: ClassVar[str] = "keyboard"
    params_model: ClassVar[type[BaseModel]] = KeyboardSequenceParams
    example: ClassVar[dict[str, Any]] = {"keys": ["tab", "enter"]}

    def execute(self, params: KeyboardSequenceParams, ctx: ActionContext) -> dict[str, Any]:
        keys = _check_keys(list(params.keys))
        ctx.run_strategy(
            Operation.KEYBOARD_KEY_SEQUENCE,
            OperationRequest(operation=Operation.KEYBOARD_KEY_SEQUENCE, keys=tuple(keys)),
            strategy_override=params.strategy,
        )
        return {"keys": keys, "count": len(keys)}


class KeyboardKeyDownParams(_KeyboardParams):
    key: str = Field(description="Key name to press and keep held")


class KeyboardKeyDown(ActionHandler[KeyboardKeyDownParams]):
    name: ClassVar[str] = "keyboard.key_down"
    summary: ClassVar[str] = "Press and hold a single key"
    category: ClassVar[str] = "keyboard"
    params_model: ClassVar[type[BaseModel]] = KeyboardKeyDownParams
    example: ClassVar[dict[str, Any]] = {"key": "shift"}

    def execute(self, params: KeyboardKeyDownParams, ctx: ActionContext) -> dict[str, Any]:
        key = _check_key(params.key)
        ctx.run_strategy(
            Operation.KEYBOARD_KEY_DOWN,
            OperationRequest(operation=Operation.KEYBOARD_KEY_DOWN, key=key),
            strategy_override=params.strategy,
        )
        return {"key": key, "pressed": True}


class KeyboardKeyUpParams(_KeyboardParams):
    key: str = Field(description="Key name to release")


class KeyboardKeyUp(ActionHandler[KeyboardKeyUpParams]):
    name: ClassVar[str] = "keyboard.key_up"
    summary: ClassVar[str] = "Release a single key"
    category: ClassVar[str] = "keyboard"
    params_model: ClassVar[type[BaseModel]] = KeyboardKeyUpParams
    example: ClassVar[dict[str, Any]] = {"key": "shift"}

    def execute(self, params: KeyboardKeyUpParams, ctx: ActionContext) -> dict[str, Any]:
        key = _check_key(params.key)
        ctx.run_strategy(
            Operation.KEYBOARD_KEY_RELEASE,
            OperationRequest(operation=Operation.KEYBOARD_KEY_RELEASE, key=key),
            strategy_override=params.strategy,
        )
        return {"key": key, "pressed": False}


class KeyboardHoldParams(_KeyboardParams):
    key: str = Field(description="Key name to hold")
    hold_ms: float | None = Field(
        default=None, ge=0, le=600_000, description="Hold duration in milliseconds"
    )


class KeyboardHold(ActionHandler[KeyboardHoldParams]):
    name: ClassVar[str] = "keyboard.hold"
    summary: ClassVar[str] = "Hold a single key for a duration"
    category: ClassVar[str] = "keyboard"
    params_model: ClassVar[type[BaseModel]] = KeyboardHoldParams
    example: ClassVar[dict[str, Any]] = {"key": "shift", "hold_ms": 500}

    def execute(self, params: KeyboardHoldParams, ctx: ActionContext) -> dict[str, Any]:
        key = _check_key(params.key)
        ctx.run_strategy(
            Operation.KEYBOARD_KEY_HOLD,
            OperationRequest(
                operation=Operation.KEYBOARD_KEY_HOLD,
                key=key,
                hold_ms=params.hold_ms,
            ),
            strategy_override=params.strategy,
        )
        return {"key": key, "hold_ms": params.hold_ms}


ACTIONS: list[ActionHandler] = [
    KeyboardType(),
    KeyboardPress(),
    KeyboardHotkey(),
    KeyboardSequence(),
    KeyboardKeyDown(),
    KeyboardKeyUp(),
    KeyboardHold(),
]

__all__ = ["ACTIONS"]
