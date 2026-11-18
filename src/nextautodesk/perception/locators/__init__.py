"""Built-in locators.

Locators are discovered by :class:`~nextautodesk.perception.registry.LocatorRegistry`
from this package and from the ``nextautodesk.locators`` entry-point group.
"""

from __future__ import annotations

from .template import OpenCVTemplateLocator
from .text import TesseractTextLocator


def builtin_locator_classes() -> tuple[type[OpenCVTemplateLocator], type[TesseractTextLocator]]:
    """Return the built-in locator classes (one instance is created per registry)."""

    return (OpenCVTemplateLocator, TesseractTextLocator)


__all__ = ["OpenCVTemplateLocator", "TesseractTextLocator", "builtin_locator_classes"]
