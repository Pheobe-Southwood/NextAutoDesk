"""Canonical operation identifiers used by the strategy layer.

Every operation in this enumeration is independently configurable: the
configuration file maps an operation to a *strategy provider* plus parameters.
The values are stable and are part of the public configuration/API contract.
"""

from __future__ import annotations

from enum import StrEnum


class Operation(StrEnum):
    """A single mouse/keyboard action that has its own strategy configuration."""

    # --- mouse -------------------------------------------------------------
    MOUSE_MOVE = "mouse_move"
    MOUSE_MOVE_RELATIVE = "mouse_move_relative"
    MOUSE_CLICK_LEFT = "mouse_click_left"
    MOUSE_CLICK_RIGHT = "mouse_click_right"
    MOUSE_CLICK_MIDDLE = "mouse_click_middle"
    MOUSE_DOUBLE_CLICK = "mouse_double_click"
    MOUSE_DOWN = "mouse_down"
    MOUSE_UP = "mouse_up"
    MOUSE_HOLD = "mouse_hold"
    MOUSE_DRAG = "mouse_drag"
    MOUSE_SCROLL_VERTICAL = "mouse_scroll_vertical"
    MOUSE_SCROLL_HORIZONTAL = "mouse_scroll_horizontal"

    # --- keyboard ----------------------------------------------------------
    KEYBOARD_TYPE_TEXT = "keyboard_type_text"
    KEYBOARD_KEY_PRESS = "keyboard_key_press"
    KEYBOARD_KEY_DOWN = "keyboard_key_down"
    KEYBOARD_KEY_RELEASE = "keyboard_key_release"
    KEYBOARD_KEY_HOLD = "keyboard_key_hold"
    KEYBOARD_HOTKEY = "keyboard_hotkey"
    KEYBOARD_KEY_SEQUENCE = "keyboard_key_sequence"


ALL_OPERATIONS: tuple[Operation, ...] = tuple(Operation)
