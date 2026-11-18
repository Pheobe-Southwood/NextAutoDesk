"""Read-only element perception actions (API tier: ``perception``).

``element.find`` / ``element.wait`` / ``element.ocr`` look at the screen and
return what they see. They never generate input events, so they are marked
``mutating=False`` and bypass the serial desktop queue;
monitoring calls therefore stay responsive while a long task is running.

The heavy lifting (capture, locator dispatch, OCR) lives in
:class:`~nextautodesk.perception.service.PerceptionService`; these handlers are
the thin action/HTTP contract around it.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ..errors import ElementNotFound, PerceptionUnavailable
from ..models import Rect
from ..perception.models import ElementQuery, summarize_query
from ..perception.service import PerceptionService
from .base import ActionContext, ActionHandler


class ElementFindParams(BaseModel):
    """Parameters of ``element.find``."""

    model_config = ConfigDict(extra="forbid")

    query: ElementQuery = Field(description="Element query (template image or OCR text).")


class ElementWaitParams(BaseModel):
    """Parameters of ``element.wait``."""

    model_config = ConfigDict(extra="forbid")

    query: ElementQuery = Field(description="Element query (template image or OCR text).")
    timeout_ms: int = Field(
        default=5000, ge=1, le=600_000, description="Maximum wait time in milliseconds."
    )
    poll_interval_ms: int = Field(
        default=200, ge=10, le=5000, description="Pause between locate attempts in milliseconds."
    )


class ElementOcrParams(BaseModel):
    """Parameters of ``element.ocr``."""

    model_config = ConfigDict(extra="forbid")

    region: Rect | None = Field(
        default=None, description="Optional capture region; the whole screen when omitted."
    )
    languages: list[str] | None = Field(
        default=None, description="Tesseract languages, e.g. ['eng', 'chi_sim']."
    )
    psm: int | None = Field(default=None, ge=0, le=13, description="Tesseract page segmentation mode.")
    min_confidence: float | None = Field(
        default=None, ge=0, le=100, description="Minimum Tesseract word confidence (0..100)."
    )
    save_screenshot: bool = Field(
        default=False, description="Persist the captured frame in the screenshot store."
    )


def _perception(ctx: ActionContext) -> PerceptionService:
    if ctx.perception is None:
        raise PerceptionUnavailable("screen perception is not available in this runtime")
    return ctx.perception


class ElementFind(ActionHandler[ElementFindParams]):
    """Locate elements matching a query once."""

    name: ClassVar[str] = "element.find"
    summary: ClassVar[str] = "Locate screen elements by template image or OCR text"
    category: ClassVar[str] = "elements"
    tier: ClassVar[str] = "perception"
    params_model: ClassVar[type[BaseModel]] = ElementFindParams
    mutating: ClassVar[bool] = False
    example: ClassVar[dict[str, Any]] = {
        "query": {
            "locator": "template",
            "template": {
                "image_base64": "<base64-encoded PNG>",
                "threshold": 0.8,
                "scales": [0.75, 1.0, 1.25],
            },
        }
    }

    def execute(self, params: ElementFindParams, ctx: ActionContext) -> dict[str, Any]:
        perception = _perception(ctx)
        ctx.check_cancelled()
        result = perception.locate(params.query, cancel=ctx.cancel)
        ctx.check_cancelled()
        return result.model_dump(mode="json")


class ElementWait(ActionHandler[ElementWaitParams]):
    """Poll for an element until it appears or the timeout expires."""

    name: ClassVar[str] = "element.wait"
    summary: ClassVar[str] = "Wait for a screen element to appear (polling template/OCR)"
    category: ClassVar[str] = "elements"
    tier: ClassVar[str] = "perception"
    params_model: ClassVar[type[BaseModel]] = ElementWaitParams
    mutating: ClassVar[bool] = False
    example: ClassVar[dict[str, Any]] = {
        "query": {"locator": "text", "text": {"text": "Sign in", "match": "contains"}},
        "timeout_ms": 10000,
        "poll_interval_ms": 250,
    }

    def execute(self, params: ElementWaitParams, ctx: ActionContext) -> dict[str, Any]:
        perception = _perception(ctx)
        started = time.monotonic()
        deadline = started + params.timeout_ms / 1000.0
        # Polling with save_screenshot=true would persist a screenshot on every
        # attempt; polling is cheap, so persist the frame only once, after the
        # element has been found (one extra locate with saving enabled).
        poll_query = params.query
        if params.query.save_screenshot:
            poll_query = params.query.model_copy(update={"save_screenshot": False})
        attempts = 0
        found = None
        while True:
            ctx.check_cancelled()
            attempts += 1
            result = perception.locate(poll_query, cancel=ctx.cancel)
            if result.matches:
                if params.query.save_screenshot:
                    persisted = perception.locate(params.query, cancel=ctx.cancel)
                    if persisted.matches:
                        result = persisted
                found = result
                break
            if time.monotonic() >= deadline:
                raise ElementNotFound(
                    f"element not found within {params.timeout_ms} ms",
                    details={
                        "query": summarize_query(params.query),
                        "attempts": attempts,
                        "timeout_ms": params.timeout_ms,
                    },
                )
            ctx.sleep(params.poll_interval_ms / 1000.0)
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 3)
        matched = found.model_copy(update={"attempts": attempts, "elapsed_ms": elapsed_ms})
        return matched.model_dump(mode="json")


class ElementOcr(ActionHandler[ElementOcrParams]):
    """Extract all text (lines and words) from the screen or a region."""

    name: ClassVar[str] = "element.ocr"
    summary: ClassVar[str] = "Extract screen text with Tesseract OCR"
    category: ClassVar[str] = "elements"
    tier: ClassVar[str] = "perception"
    params_model: ClassVar[type[BaseModel]] = ElementOcrParams
    mutating: ClassVar[bool] = False
    example: ClassVar[dict[str, Any]] = {
        "region": {"x": 0, "y": 0, "width": 800, "height": 600},
        "languages": ["eng"],
        "psm": 6,
    }

    def execute(self, params: ElementOcrParams, ctx: ActionContext) -> dict[str, Any]:
        perception = _perception(ctx)
        ctx.check_cancelled()
        result = perception.ocr(
            region=params.region,
            languages=params.languages,
            psm=params.psm,
            min_confidence=params.min_confidence,
            save_screenshot=params.save_screenshot,
            cancel=ctx.cancel,
        )
        ctx.check_cancelled()
        return result.model_dump(mode="json")


ACTIONS: list[ActionHandler] = [ElementFind(), ElementWait(), ElementOcr()]

__all__ = [
    "ACTIONS",
    "ElementFind",
    "ElementFindParams",
    "ElementOcr",
    "ElementOcrParams",
    "ElementWait",
    "ElementWaitParams",
]
