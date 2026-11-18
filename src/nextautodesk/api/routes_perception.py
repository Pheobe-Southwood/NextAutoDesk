"""HTTP routes for the perception and composite API tiers.

* ``/elements/*`` - read-only screen perception (tier ``perception``): locator
  discovery, element lookup/wait and full-frame OCR.
* ``/interactions/*`` - intent-level composite operations (tier ``composite``)
  that locate a target and then click/type through the primitive actions.

The router is mounted by :mod:`nextautodesk.api.routes` on the authenticated
``/api/v1`` router, so the API key dependency, error handlers and request-id
middleware apply unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from ..errors import ExecutorUnavailable
from ..models import ActionResult
from ..perception.models import LocateResult, OcrResult
from .schemas_perception import (
    ElementFindRequest,
    ElementOcrRequest,
    ElementWaitRequest,
    InteractionClickRequest,
    InteractionTypeRequest,
    LocatorsResponse,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..bootstrap import AppContext

perception_router = APIRouter()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _context(request: Request) -> AppContext:
    ctx = getattr(request.app.state, "context", None)
    if ctx is None:  # pragma: no cover - the lifespan always installs it
        raise ExecutorUnavailable("application context is not initialised")
    return ctx


def _execute(
    request: Request,
    action: str,
    params: dict[str, Any],
    *,
    strategy: str | None = None,
) -> ActionResult:
    from .routes import _execute_params

    return _execute_params(request, action, params, strategy=strategy)


def _read_output(
    request: Request,
    action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Execute a read-only element action and return its output payload."""

    return _execute(request, action, params).output


# ---------------------------------------------------------------------------
# perception tier: read-only screen understanding
# ---------------------------------------------------------------------------
@perception_router.get(
    "/elements/locators",
    response_model=LocatorsResponse,
    tags=["elements"],
    summary="List perception locators and their runtime availability",
)
def elements_locators(request: Request) -> LocatorsResponse:
    ctx = _context(request)
    perception = getattr(ctx.service, "perception", None)
    if perception is None:  # pragma: no cover - service always wires perception
        return LocatorsResponse(enabled=False, locators=[])
    return LocatorsResponse(
        enabled=bool(perception.enabled),
        locators=perception.locator_availability(),
    )


@perception_router.post(
    "/elements/find",
    response_model=LocateResult,
    tags=["elements"],
    summary="Locate screen elements by template image or OCR text",
)
def elements_find(request: Request, payload: ElementFindRequest) -> dict[str, Any]:
    return _read_output(request, "element.find", payload.model_dump(exclude_none=True))


@perception_router.post(
    "/elements/wait",
    response_model=LocateResult,
    tags=["elements"],
    summary="Wait for an element to appear (polling template matching or OCR)",
)
def elements_wait(request: Request, payload: ElementWaitRequest) -> dict[str, Any]:
    params = payload.model_dump(exclude_none=True)
    return _read_output(request, "element.wait", params)


@perception_router.post(
    "/elements/ocr",
    response_model=OcrResult,
    tags=["elements"],
    summary="Extract screen text with Tesseract OCR",
)
def elements_ocr(request: Request, payload: ElementOcrRequest) -> dict[str, Any]:
    return _read_output(request, "element.ocr", payload.model_dump(exclude_none=True))


# ---------------------------------------------------------------------------
# composite tier: intent-level operations built from primitives + perception
# ---------------------------------------------------------------------------
@perception_router.post(
    "/interactions/click",
    response_model=ActionResult,
    tags=["interactions"],
    summary="Locate an element by perception and click it",
)
def interactions_click(request: Request, payload: InteractionClickRequest) -> ActionResult:
    params = payload.model_dump(exclude_none=True)
    strategy = params.pop("strategy", None)
    return _execute(request, "interaction.click", params, strategy=strategy)


@perception_router.post(
    "/interactions/type",
    response_model=ActionResult,
    tags=["interactions"],
    summary="Locate an input element, focus it and type text",
)
def interactions_type(request: Request, payload: InteractionTypeRequest) -> ActionResult:
    params = payload.model_dump(exclude_none=True)
    strategy = params.pop("strategy", None)
    return _execute(request, "interaction.type", params, strategy=strategy)


__all__ = ["perception_router"]
