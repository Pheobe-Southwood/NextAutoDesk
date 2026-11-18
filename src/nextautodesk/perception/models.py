"""Transport-agnostic domain models for the perception layer.

A perception *query* describes what to look for on screen; a *result* describes
where it was found. All models are Pydantic v2 with ``extra="forbid"`` so typos
in HTTP payloads or task steps fail fast.

Coordinate semantics: locators always return **frame-relative** coordinates
(origin at the top-left corner of the captured image). :class:`PerceptionService`
translates them to **absolute screen coordinates** by adding the capture region
offset, so every field documented as absolute here is ready to feed into mouse
actions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import Point, Rect, ScreenshotInfo

#: Maximum number of template scales accepted in one query/configuration.
MAX_TEMPLATE_SCALES = 8
MIN_TEMPLATE_SCALE = 0.1
MAX_TEMPLATE_SCALE = 4.0

#: Maximum number of OCR query characters echoed in error details.
MAX_QUERY_TEXT_ECHO = 120


class _PerceptionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemplateSpec(_PerceptionModel):
    """Image template used by the OpenCV template locator."""

    image_base64: str | None = Field(
        default=None,
        description=(
            "PNG/JPEG image bytes encoded as standard base64 or as a data URL "
            "('data:image/png;base64,...')."
        ),
    )
    screenshot: str | None = Field(
        default=None,
        description="Name of a screenshot previously stored through the screenshot store.",
    )
    threshold: float | None = Field(
        default=None, ge=0, le=1, description="Minimum normalized correlation (0..1)."
    )
    grayscale: bool | None = Field(
        default=None, description="Match on grayscale images (faster, colour-agnostic)."
    )
    scales: list[float] | None = Field(
        default=None,
        description=(
            "Template scales to try; each value must be within 0.1..4.0 "
            f"(at most {MAX_TEMPLATE_SCALES} entries)."
        ),
    )

    @model_validator(mode="after")
    def _validate_source(self) -> TemplateSpec:
        if (self.image_base64 is None) == (self.screenshot is None):
            raise ValueError("exactly one of template.image_base64 or template.screenshot is required")
        if self.image_base64 is not None and not self.image_base64.strip():
            raise ValueError("template.image_base64 must not be empty")
        if self.scales is not None:
            if len(self.scales) > MAX_TEMPLATE_SCALES:
                raise ValueError(f"template.scales accepts at most {MAX_TEMPLATE_SCALES} entries")
            for scale in self.scales:
                if not MIN_TEMPLATE_SCALE <= scale <= MAX_TEMPLATE_SCALE:
                    raise ValueError(
                        f"template.scales entries must be within "
                        f"{MIN_TEMPLATE_SCALE}..{MAX_TEMPLATE_SCALE}"
                    )
        return self


class TextSpec(_PerceptionModel):
    """Text query used by the Tesseract locator."""

    text: str = Field(min_length=1, max_length=1000, description="Text to look for.")
    match: Literal["contains", "exact", "regex"] = Field(
        default="contains", description="Match mode."
    )
    case_sensitive: bool = Field(default=False, description="Case-sensitive matching.")
    languages: list[str] | None = Field(
        default=None, description="Tesseract languages, e.g. ['eng', 'chi_sim']."
    )
    psm: int | None = Field(default=None, ge=0, le=13, description="Tesseract page segmentation mode.")
    min_confidence: float | None = Field(
        default=None, ge=0, le=100, description="Minimum Tesseract word confidence (0..100)."
    )
    granularity: Literal["line", "word"] | None = Field(
        default=None, description="Whether line boxes or word boxes are returned."
    )


class ElementQuery(_PerceptionModel):
    """One element lookup request."""

    locator: Literal["auto", "template", "text"] = Field(
        default="auto", description="Locator name; 'auto' infers it from the provided spec."
    )
    region: Rect | None = Field(
        default=None, description="Capture region; the whole screen when omitted."
    )
    template: TemplateSpec | None = Field(default=None, description="Template matching spec.")
    text: TextSpec | None = Field(default=None, description="OCR text spec.")
    max_results: int | None = Field(
        default=None, ge=1, le=100, description="Per-request result cap."
    )
    save_screenshot: bool = Field(
        default=False, description="Persist the captured frame in the screenshot store."
    )

    @model_validator(mode="after")
    def _validate_spec(self) -> ElementQuery:
        if (self.template is None) == (self.text is None):
            raise ValueError("exactly one of template or text must be provided")
        if self.locator == "template" and self.template is None:
            raise ValueError("locator 'template' requires a template spec")
        if self.locator == "text" and self.text is None:
            raise ValueError("locator 'text' requires a text spec")
        return self


class ElementMatch(_PerceptionModel):
    """One located element in absolute screen coordinates."""

    box: Rect = Field(description="Bounding box in absolute screen coordinates.")
    center: Point = Field(description="Box center in absolute screen coordinates.")
    confidence: float = Field(ge=0, le=1, description="Match confidence (0..1).")
    locator: str = Field(description="Locator that produced this match.")
    text: str | None = Field(default=None, description="Matched OCR text, when applicable.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Locator-specific details.")


class FrameInfo(_PerceptionModel):
    """Metadata about the captured frame a result was computed from."""

    region: Rect = Field(description="Capture region in absolute screen coordinates.")
    width: int = Field(ge=1, description="Captured image width in pixels.")
    height: int = Field(ge=1, description="Captured image height in pixels.")
    captured_at: datetime = Field(description="Capture timestamp (UTC).")
    screenshot: ScreenshotInfo | None = Field(
        default=None, description="Stored screenshot, when save_screenshot was requested."
    )


class LocateResult(_PerceptionModel):
    """Result of one element lookup."""

    locator: str
    matches: list[ElementMatch] = Field(default_factory=list)
    frame: FrameInfo
    elapsed_ms: float
    attempts: int = Field(default=1, ge=1)


class OcrWord(_PerceptionModel):
    """One OCR word in absolute screen coordinates."""

    text: str
    box: Rect
    center: Point
    confidence: float | None = Field(
        default=None, ge=0, le=100, description="Tesseract confidence (0..100)."
    )


class OcrLine(_PerceptionModel):
    """One OCR line in absolute screen coordinates."""

    text: str
    box: Rect
    center: Point
    confidence: float | None = Field(
        default=None, ge=0, le=100, description="Mean word confidence (0..100)."
    )
    words: list[OcrWord] = Field(default_factory=list)


class OcrResult(_PerceptionModel):
    """Result of a full-frame OCR extraction."""

    lines: list[OcrLine] = Field(default_factory=list)
    text: str = Field(description="Line texts joined with newlines.")
    frame: FrameInfo
    elapsed_ms: float


class LocatorAvailability(_PerceptionModel):
    """Runtime availability of one locator."""

    name: str
    kind: str = Field(description="Locator kind: 'template' or 'text'.")
    available: bool
    reason: str | None = Field(default=None, description="Why the locator is unavailable.")
    version: str | None = Field(default=None, description="Underlying engine version.")
    supported_queries: list[str] = Field(
        default_factory=list, description="Query kinds this locator can execute."
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def summarize_query(query: ElementQuery) -> dict[str, Any]:
    """Return a compact, redacted view of a query for error details.

    Template image bytes are never echoed: they can be several megabytes and
    error details may be persisted in task records. Only their size is reported.
    """

    summary: dict[str, Any] = {"locator": query.locator}
    if query.region is not None:
        summary["region"] = query.region.model_dump()
    if query.max_results is not None:
        summary["max_results"] = query.max_results
    if query.save_screenshot:
        summary["save_screenshot"] = True
    if query.template is not None:
        template: dict[str, Any] = {}
        if query.template.image_base64 is not None:
            template["image_base64"] = (
                f"<base64 omitted: {len(query.template.image_base64)} bytes>"
            )
        if query.template.screenshot is not None:
            template["screenshot"] = query.template.screenshot
        if query.template.threshold is not None:
            template["threshold"] = query.template.threshold
        if query.template.scales is not None:
            template["scales"] = query.template.scales
        summary["template"] = template
    if query.text is not None:
        summary["text"] = {
            "text": _truncate(query.text.text, MAX_QUERY_TEXT_ECHO),
            "match": query.text.match,
            "case_sensitive": query.text.case_sensitive,
        }
    return summary


__all__ = [
    "MAX_QUERY_TEXT_ECHO",
    "MAX_TEMPLATE_SCALE",
    "MAX_TEMPLATE_SCALES",
    "MIN_TEMPLATE_SCALE",
    "ElementMatch",
    "ElementQuery",
    "FrameInfo",
    "LocateResult",
    "LocatorAvailability",
    "OcrLine",
    "OcrResult",
    "OcrWord",
    "TemplateSpec",
    "TextSpec",
    "summarize_query",
]
