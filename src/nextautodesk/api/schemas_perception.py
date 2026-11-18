"""HTTP request/response schemas for the perception and composite tiers.

Request models mirror the action parameter models field-for-field and reject
unknown fields, so a typo fails fast with ``422 VALIDATION_ERROR``. The
read-only ``/elements/*`` calls have no execution timeout: they either answer or
raise promptly. ``element.wait`` has its own concrete ``timeout_ms``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from ..models import Rect
from ..perception.models import ElementQuery, LocatorAvailability
from .schemas import ApiModel


class LocatorsResponse(ApiModel):
    """Availability of the perception locators (discovery endpoint)."""

    enabled: bool = Field(description="True when the perception layer is enabled.")
    locators: list[LocatorAvailability] = Field(default_factory=list)


class ElementFindRequest(ApiModel):
    """Body of ``POST /elements/find``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": {
                        "locator": "template",
                        "template": {
                            "image_base64": "<base64-encoded PNG>",
                            "threshold": 0.8,
                        },
                    }
                }
            ]
        },
    )

    query: ElementQuery = Field(description="Element query (template image or OCR text).")


class ElementWaitRequest(ApiModel):
    """Body of ``POST /elements/wait``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": {"locator": "text", "text": {"text": "Sign in"}},
                    "timeout_ms": 10000,
                    "poll_interval_ms": 250,
                }
            ]
        },
    )

    query: ElementQuery = Field(description="Element query (template image or OCR text).")
    timeout_ms: int = Field(
        default=5000, ge=1, le=600_000, description="Maximum wait time in milliseconds."
    )
    poll_interval_ms: int = Field(
        default=200, ge=10, le=5000, description="Pause between locate attempts in milliseconds."
    )


class ElementOcrRequest(ApiModel):
    """Body of ``POST /elements/ocr``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"region": {"x": 0, "y": 0, "width": 800, "height": 600}, "languages": ["eng"]}
            ]
        },
    )

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


class InteractionClickRequest(ApiModel):
    """Body of ``POST /interactions/click``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": {"locator": "text", "text": {"text": "Submit"}},
                    "button": "left",
                    "wait_timeout_ms": 5000,
                }
            ]
        },
    )

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


class InteractionTypeRequest(ApiModel):
    """Body of ``POST /interactions/type``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": {"locator": "text", "text": {"text": "Username"}},
                    "text": "demo user",
                    "clear": True,
                    "submit": True,
                }
            ]
        },
    )

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


__all__ = [
    "ElementFindRequest",
    "ElementOcrRequest",
    "ElementWaitRequest",
    "InteractionClickRequest",
    "InteractionTypeRequest",
    "LocatorsResponse",
]
