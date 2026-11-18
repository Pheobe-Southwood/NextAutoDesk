"""Built-in strategy providers.

The provider modules shipped with the package are always importable; they import
their optional third-party packages lazily and report ``available=False`` when
those extras are missing. A provider *module* that cannot be imported therefore
indicates a broken installation and is treated as a startup error.
"""

from __future__ import annotations

import importlib
import logging
from typing import ClassVar

from ...errors import ConfigError
from ..base import StrategyProvider

LOGGER = logging.getLogger(__name__)

#: Provider modules shipped with the package, in discovery order.
PROVIDER_MODULES: tuple[str, ...] = (
    "human_movement",
    "direct",
    "human_move_mouse",
    "windmouse",
    "clack",
)


def builtin_provider_classes() -> list[type[StrategyProvider]]:
    classes: list[type[StrategyProvider]] = []
    for module_name in PROVIDER_MODULES:
        full_name = f"{__name__}.{module_name}"
        try:
            module = importlib.import_module(full_name)
        except ImportError as exc:
            raise ConfigError(
                f"built-in strategy provider module {full_name!r} failed to import: {exc}"
            ) from exc
        found = False
        for attribute_name in dir(module):
            candidate = getattr(module, attribute_name)
            if (
                isinstance(candidate, type)
                and issubclass(candidate, StrategyProvider)
                and candidate is not StrategyProvider
                and getattr(candidate, "__module__", "") == module.__name__
            ):
                classes.append(candidate)
                found = True
        if not found:
            LOGGER.warning("Strategy provider module %s exports no StrategyProvider", full_name)
    return classes


__all__: ClassVar = ["PROVIDER_MODULES", "builtin_provider_classes"]
