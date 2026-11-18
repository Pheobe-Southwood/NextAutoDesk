"""Locator layer contracts.

A *locator* answers one question: given an already captured :class:`Frame` and
a query, where is the element? Locators never capture or encode screenshots
themselves, never touch HTTP or storage, and only return frame-relative
coordinates; :class:`~nextautodesk.perception.service.PerceptionService` owns
capture, persistence and coordinate translation.

New locators can be discovered from the ``nextautodesk.locators`` entry-point
group without changing the service.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from ..models import Rect, Size
from .models import ElementMatch, ElementQuery, LocatorAvailability, OcrLine


@dataclass(frozen=True, slots=True)
class Frame:
    """One captured screen frame handed to locators.

    ``image`` is a loaded ``PIL.Image.Image`` in RGB mode, ``png`` the original
    encoded bytes (persisted verbatim when a caller asks to save the capture) and
    ``region`` the absolute screen region the image was captured from.
    """

    image: Any
    png: bytes
    region: Rect
    screen: Size
    captured_at: datetime


class Locator(ABC):
    """Base class for screen element locators."""

    name: ClassVar[str]
    kind: ClassVar[Literal["template", "text"]]
    #: Query kinds handled by this locator, e.g. ``frozenset({"template"})``.
    supported_queries: ClassVar[frozenset[str]]

    def availability(self) -> LocatorAvailability:
        """Report whether the locator can run in this environment."""

        return LocatorAvailability(
            name=self.name,
            kind=self.kind,
            available=True,
            supported_queries=sorted(self.supported_queries),
        )

    def validate_params(self, params: Mapping[str, Any]) -> None:
        """Validate merged parameters (raise ``ValueError`` on invalid values)."""

    @abstractmethod
    def execute(
        self,
        query: ElementQuery,
        frame: Frame,
        params: dict[str, Any],
    ) -> list[ElementMatch]:
        """Locate element matches in ``frame`` and return frame-relative boxes."""


@runtime_checkable
class TextExtractor(Protocol):
    """A locator that can extract all OCR lines from a frame."""

    def extract_text(
        self,
        frame: Frame,
        params: dict[str, Any],
        *,
        languages: list[str] | None = None,
        psm: int | None = None,
        min_confidence: float | None = None,
    ) -> list[OcrLine]:
        """Extract OCR lines (frame-relative coordinates) from ``frame``."""


__all__ = ["Frame", "Locator", "TextExtractor"]
