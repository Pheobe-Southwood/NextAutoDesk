"""Screen element perception layer.

Public entry points:

* :class:`~nextautodesk.perception.service.PerceptionService` - capture and
  locator orchestration owned by :class:`~nextautodesk.service.DesktopService`,
* :mod:`nextautodesk.perception.models` - transport-agnostic query/result models,
* :mod:`nextautodesk.perception.base` - ``Locator``/``TextExtractor`` contracts,
* :mod:`nextautodesk.perception.registry` - locator discovery and configuration.

The perception layer is read-only with respect to the desktop: it captures the
screen and understands it, but never generates input events.
"""

from __future__ import annotations

from .base import Frame, Locator, TextExtractor
from .models import (
    ElementMatch,
    ElementQuery,
    FrameInfo,
    LocateResult,
    LocatorAvailability,
    OcrLine,
    OcrResult,
    OcrWord,
    TemplateSpec,
    TextSpec,
)
from .registry import ENTRY_POINT_GROUP, LocatorRegistry
from .service import PerceptionService

__all__ = [
    "ENTRY_POINT_GROUP",
    "ElementMatch",
    "ElementQuery",
    "Frame",
    "FrameInfo",
    "LocateResult",
    "Locator",
    "LocatorAvailability",
    "LocatorRegistry",
    "OcrLine",
    "OcrResult",
    "OcrWord",
    "PerceptionService",
    "TemplateSpec",
    "TextExtractor",
    "TextSpec",
]
