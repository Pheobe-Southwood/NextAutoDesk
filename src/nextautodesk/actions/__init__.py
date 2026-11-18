"""Built-in action modules and the default registry factory."""

from __future__ import annotations

from .base import ActionContext, ActionHandler, ActionInfo
from .registry import ActionRegistry


def default_action_registry() -> ActionRegistry:
    return ActionRegistry.build_default()


__all__ = ["ActionContext", "ActionHandler", "ActionInfo", "ActionRegistry", "default_action_registry"]
