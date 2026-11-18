"""HTTP routes.

Two routers are exported and mounted by :func:`nextautodesk.api.app.create_app`:

* ``public_router`` - ``/``, ``/healthz``, ``/readyz`` (no authentication),
* ``api_router`` - everything under ``/api/v1`` (API key required).

Mutating desktop actions go through the serial executor via
:meth:`DesktopService.execute_action`; read-only queries (mouse position, screen
size, window enumeration, screenshots) bypass the queue by design so monitoring
stays responsive while a long task is running.

Read-only responses are domain models (``Point``, ``Size``, ``WindowInfo`` ...);
mutating actions return the :class:`ActionResult` produced by the action layer.
Task submissions return ``202 Accepted`` with the persisted ``TaskRecord``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .. import API_VERSION, __version__
from ..config import load_config
from ..errors import (
    ConfigError,
    ExecutorUnavailable,
    ReloadError,
    WindowNotFound,
)
from ..health import build_health_report
from ..logging_setup import setup_logging
from ..models import (
    ActionResult,
    HealthReport,
    MouseState,
    Point,
    ScreenshotFormat,
    Size,
    TaskRecord,
    TaskStatus,
    WindowInfo,
    WindowSelector,
)
from .routes_perception import perception_router
from .schemas import (
    SAFE_NAME_PATTERN,
    ActionCatalogResponse,
    ActionOptions,
    ConfigReloadResponse,
    DesktopWaitRequest,
    InfoResponse,
    KeyboardHoldRequest,
    KeyboardHotkeyRequest,
    KeyboardKeyRequest,
    KeyboardPressRequest,
    KeyboardSequenceRequest,
    KeyboardTypeRequest,
    LivenessResponse,
    MouseButtonRequest,
    MouseClickRequest,
    MouseDragRequest,
    MouseHoldRequest,
    MouseMoveRelativeRequest,
    MouseMoveRequest,
    MouseScrollRequest,
    ReadinessResponse,
    RootResponse,
    ScreenshotListResponse,
    ScreenshotRequest,
    StrategyCatalogResponse,
    TaskListResponse,
    TaskSubmitRequest,
    WindowListResponse,
    WindowMoveRequest,
    WindowResizeRequest,
    parse_region,
)
from .security import require_api_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..bootstrap import AppContext
    from ..tasks.manager import TaskManager

LOGGER = logging.getLogger(__name__)

#: X11 window identifiers are hex (``0x3a00007``) or decimal strings.
_WINDOW_ID_PATTERN = r"^(0x)?[0-9A-Fa-f]{1,16}$"
_TASK_ID_PATTERN = r"^[0-9A-Za-z_-]{1,64}$"
public_router = APIRouter(tags=["system"])
api_router = APIRouter(
    dependencies=[Depends(require_api_key)],
    responses={
        401: {"description": "Missing or invalid API key"},
        404: {"description": "Resource not found"},
        422: {"description": "Request validation failed"},
    },
)
# Perception (read-only) and composite interaction tiers.
api_router.include_router(perception_router)

_MEDIA_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _context(request: Request) -> AppContext:
    ctx = getattr(request.app.state, "context", None)
    if ctx is None:  # pragma: no cover - the lifespan always installs it
        raise ExecutorUnavailable("application context is not initialised")
    return ctx


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _split_options(payload: ActionOptions) -> tuple[dict[str, Any], str | None, int | None]:
    data = payload.model_dump(exclude_none=True)
    strategy = data.pop("strategy", None)
    timeout_ms = data.pop("timeout_ms", None)
    return data, strategy, timeout_ms


def _execute_payload(request: Request, action: str, payload: ActionOptions) -> ActionResult:
    params, strategy, timeout_ms = _split_options(payload)
    return _execute_params(
        request, action, params, strategy=strategy, timeout_ms=timeout_ms
    )


def _execute_params(
    request: Request,
    action: str,
    params: dict[str, Any],
    *,
    strategy: str | None = None,
    timeout_ms: int | None = None,
) -> ActionResult:
    ctx = _context(request)
    request.state.action = action
    return ctx.service.execute_action(
        action,
        params,
        strategy=strategy,
        timeout_ms=timeout_ms,
        request_id=_request_id(request),
    )


def _task_manager(request: Request) -> TaskManager:
    ctx = _context(request)
    manager = ctx.task_manager
    if manager is None:
        raise ExecutorUnavailable(
            "task subsystem is unavailable (task manager is not initialised)"
        )
    return manager


def _screenshots_total(store: Any) -> int:
    try:
        return int(store.screenshots.count())
    except Exception:  # pragma: no cover - defensive
        return 0


#: Keys that are secret in their entirety (``api_key``, ``auth`` ...).
_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "key",
        "keys",
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwd",
        "credential",
        "credentials",
        "auth",
        "authorization",
        "apikey",
        "api_key",
        "api_keys",
    }
)

#: Tokens that unambiguously mark a value as secret wherever they appear.
#: ``key``/``auth`` are intentionally absent: they occur in structural names
#: such as ``keyboard_key_press`` (an operation id) that must not be redacted.
_SENSITIVE_TOKENS = frozenset(
    {
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwd",
        "credential",
        "credentials",
    }
)

_SENSITIVE_SUFFIXES = (
    "_key",
    "_keys",
    "_token",
    "_tokens",
    "_secret",
    "_secrets",
    "_password",
    "_passwd",
    "_credential",
    "_credentials",
)

#: Redaction marker used for provider/operation parameters that look secret.
REDACTED = "***"


def _is_sensitive_key(name: str) -> bool:
    """Return True when a dictionary key names a secret-like value."""

    normalised = name.strip().lower()
    if normalised in _SENSITIVE_EXACT_KEYS:
        return True
    tokens = [token for token in re.split(r"[^a-z0-9]+", normalised) if token]
    if any(token in _SENSITIVE_TOKENS for token in tokens):
        return True
    return normalised.endswith(_SENSITIVE_SUFFIXES)


def _redact_sensitive(value: Any) -> Any:
    """Recursively replace secret-looking values with :data:`REDACTED`.

    Provider and operation ``params`` are free-form dictionaries supplied by the
    deployment, so they may contain API tokens, passwords or credentials. They
    must never leak through the read-only meta endpoints.
    """

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if (
                isinstance(key, str)
                and _is_sensitive_key(key)
                and item not in (None, "", [], {})
            ):
                redacted[key] = REDACTED
            else:
                redacted[key] = _redact_sensitive(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    return value


def _redacted_config(config: Any) -> dict[str, Any]:
    """Public configuration with secrets masked recursively."""

    payload = config.public_dict()
    strategies = payload.get("strategies")
    if isinstance(strategies, Mapping):
        payload["strategies"] = _redact_sensitive(strategies)
    return payload


#: Settings consumed while the FastAPI app / ASGI server is being constructed:
#: changing them via a hot reload would leave the running app on stale values.
_APP_CONSTRUCTION_SERVER_FIELDS = ("host", "port", "docs_url", "openapi_url", "cors")


def _non_hot_reloadable_fields(old: Any, new: Any) -> list[str]:
    """List settings that cannot be applied to the already-running process."""

    fields: list[str] = []
    for section in ("desktop", "driver", "storage"):
        if getattr(old, section).model_dump() != getattr(new, section).model_dump():
            fields.append(section)
    old_server = old.server
    new_server = new.server
    for name in _APP_CONSTRUCTION_SERVER_FIELDS:
        if getattr(old_server, name) != getattr(new_server, name):
            fields.append(f"server.{name}")
    return fields


# ---------------------------------------------------------------------------
# public system routes (no authentication)
# ---------------------------------------------------------------------------
@public_router.get("/", response_model=RootResponse, summary="Service banner")
def root(request: Request) -> RootResponse:
    ctx = getattr(request.app.state, "context", None)
    config = ctx.config if ctx is not None else None
    return RootResponse(
        name="NextAutoDesk",
        version=__version__,
        api_version=API_VERSION,
        status="ok",
        api=f"/api/{API_VERSION}",
        docs=getattr(config.server, "docs_url", None) if config is not None else None,
    )


@public_router.get(
    "/healthz",
    response_model=LivenessResponse,
    summary="Liveness probe (cheap, never touches the X display)",
)
def healthz(request: Request) -> LivenessResponse:
    ctx = getattr(request.app.state, "context", None)
    uptime = float(ctx.uptime_s) if ctx is not None else 0.0
    return LivenessResponse(
        status="ok",
        version=__version__,
        api_version=API_VERSION,
        uptime_s=round(uptime, 3),
    )


@public_router.get(
    "/readyz",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "Service is not ready"}},
    summary="Readiness probe (desktop + executor + storage)",
)
def readyz(request: Request) -> Any:
    ctx = getattr(request.app.state, "context", None)
    if ctx is None:
        return JSONResponse(
            status_code=503,
            content=ReadinessResponse(
                ready=False, version=__version__, checks={"context": "not_initialised"}
            ).model_dump(mode="json"),
        )
    checks: dict[str, str] = {}
    ready = True
    try:
        if not getattr(ctx.service, "started", False):
            checks["desktop"] = "not_started"
            ready = False
        else:
            ctx.service.drivers.probe()
            checks["desktop"] = "ok"
    except Exception as exc:
        checks["desktop"] = f"error: {type(exc).__name__}: {exc}"
        ready = False
    try:
        stats = ctx.service.executor.stats()
        checks["executor"] = stats.state
        ready = ready and stats.state == "running"
    except Exception as exc:
        checks["executor"] = f"error: {type(exc).__name__}: {exc}"
        ready = False
    try:
        writable = bool(ctx.service.store.writable())
        checks["storage"] = "ok" if writable else "error: data directory not writable"
        ready = ready and writable
    except Exception as exc:
        checks["storage"] = f"error: {type(exc).__name__}: {exc}"
        ready = False
    payload = ReadinessResponse(ready=ready, version=__version__, checks=checks)
    return JSONResponse(
        status_code=200 if ready else 503,
        content=payload.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------
@api_router.get("/info", response_model=InfoResponse, tags=["meta"], summary="Node capabilities")
def info(request: Request) -> dict[str, Any]:
    ctx = _context(request)
    payload = dict(ctx.service.capabilities())
    payload["uptime_s"] = round(float(ctx.uptime_s), 3)
    return payload


@api_router.get(
    "/health",
    response_model=HealthReport,
    tags=["meta"],
    summary="Aggregated health report",
)
def health(
    request: Request,
    deep: bool = Query(
        default=True,
        description="Run deep probes (X11 display, window manager, strategy providers).",
    ),
) -> HealthReport:
    ctx = _context(request)
    task_store = getattr(ctx.task_manager, "store", None) if ctx.task_manager is not None else None
    return build_health_report(ctx.service, deep=deep, task_store=task_store)


@api_router.get(
    "/actions",
    response_model=ActionCatalogResponse,
    tags=["meta"],
    summary="Action catalogue with parameter JSON schemas",
)
def actions(request: Request) -> ActionCatalogResponse:
    ctx = _context(request)
    catalog = ctx.service.action_catalog()
    tiers: dict[str, list[str]] = {}
    for entry in catalog:
        tiers.setdefault(entry.tier, []).append(entry.name)
    return ActionCatalogResponse(actions=catalog, count=len(catalog), tiers=tiers)


@api_router.get(
    "/strategies",
    response_model=StrategyCatalogResponse,
    tags=["meta"],
    summary="Effective strategy configuration and provider availability",
)
def strategies(request: Request) -> StrategyCatalogResponse:
    ctx = _context(request)
    payload = {
        "operations": {
            name: view.model_dump(mode="json")
            for name, view in ctx.service.strategy_views().items()
        },
        "providers": [
            provider.model_dump(mode="json")
            for provider in ctx.service.strategies.provider_availability()
        ],
    }
    redacted = StrategyCatalogResponse.model_validate(_redact_sensitive(payload))
    return redacted


@api_router.get("/config", tags=["meta"], summary="Effective configuration (secrets masked)")
def config(request: Request) -> dict[str, Any]:
    ctx = _context(request)
    return _redacted_config(ctx.config)


@api_router.post(
    "/config/reload",
    response_model=ConfigReloadResponse,
    tags=["meta"],
    summary="Reload the configuration file and apply hot-reloadable settings",
)
def reload_config(request: Request) -> ConfigReloadResponse:
    ctx = _context(request)
    source = ctx.config.source_path
    try:
        new_config = load_config(source)
    except ConfigError as exc:
        raise ReloadError(
            f"configuration reload failed: {exc}",
            details={"path": source},
        ) from exc
    incompatible = _non_hot_reloadable_fields(ctx.config, new_config)
    if incompatible:
        raise ReloadError(
            "configuration change requires a container restart",
            details={
                "fields": incompatible,
                "hint": "these settings are consumed while the app/server is constructed",
            },
        )
    applied = ctx.service.reload_config(new_config)
    # Keep the live context (auth, middleware, health) in sync with the service.
    ctx.config = applied
    # Behavioural settings declared hot-reloadable by the architecture: task
    # limits/timeouts/retention and structured logging re-apply immediately.
    if ctx.task_manager is not None:
        ctx.task_manager.config = applied
        task_store = getattr(ctx.task_manager, "store", None)
        if task_store is not None and hasattr(task_store, "config"):
            task_store.config = applied
    setup_logging(applied)
    request.state.action = "config.reload"
    LOGGER.info("configuration reloaded via API (source=%s)", source)
    return ConfigReloadResponse(reloaded=True, config=_redacted_config(applied))


# ---------------------------------------------------------------------------
# mouse
# ---------------------------------------------------------------------------
@api_router.post(
    "/mouse/move",
    response_model=ActionResult,
    tags=["mouse"],
    summary="Move the pointer to absolute coordinates",
)
def mouse_move(request: Request, payload: MouseMoveRequest) -> ActionResult:
    return _execute_payload(request, "mouse.move", payload)


@api_router.post(
    "/mouse/move-relative",
    response_model=ActionResult,
    tags=["mouse"],
    summary="Move the pointer by a relative offset",
)
def mouse_move_relative(
    request: Request, payload: MouseMoveRelativeRequest
) -> ActionResult:
    return _execute_payload(request, "mouse.move_relative", payload)


@api_router.post(
    "/mouse/click",
    response_model=ActionResult,
    tags=["mouse"],
    summary="Click a mouse button (optionally moving to x/y first)",
)
def mouse_click(request: Request, payload: MouseClickRequest) -> ActionResult:
    return _execute_payload(request, "mouse.click", payload)


@api_router.post(
    "/mouse/down",
    response_model=ActionResult,
    tags=["mouse"],
    summary="Press and hold a mouse button",
)
def mouse_down(request: Request, payload: MouseButtonRequest) -> ActionResult:
    return _execute_payload(request, "mouse.down", payload)


@api_router.post(
    "/mouse/up",
    response_model=ActionResult,
    tags=["mouse"],
    summary="Release a mouse button",
)
def mouse_up(request: Request, payload: MouseButtonRequest) -> ActionResult:
    return _execute_payload(request, "mouse.up", payload)


@api_router.post(
    "/mouse/hold",
    response_model=ActionResult,
    tags=["mouse"],
    summary="Hold a mouse button for a duration",
)
def mouse_hold(request: Request, payload: MouseHoldRequest) -> ActionResult:
    return _execute_payload(request, "mouse.hold", payload)


@api_router.post(
    "/mouse/drag",
    response_model=ActionResult,
    tags=["mouse"],
    summary="Drag with a held mouse button",
)
def mouse_drag(request: Request, payload: MouseDragRequest) -> ActionResult:
    return _execute_payload(request, "mouse.drag", payload)


@api_router.post(
    "/mouse/scroll",
    response_model=ActionResult,
    tags=["mouse"],
    summary="Scroll the wheel",
)
def mouse_scroll(request: Request, payload: MouseScrollRequest) -> ActionResult:
    return _execute_payload(request, "mouse.scroll", payload)


@api_router.get(
    "/mouse/position",
    response_model=Point,
    tags=["mouse"],
    summary="Current pointer coordinates",
)
def mouse_position(request: Request) -> Point:
    return _context(request).service.mouse_position()


@api_router.get(
    "/mouse/state",
    response_model=MouseState,
    tags=["mouse"],
    summary="Pointer position and pressed mouse buttons",
)
def mouse_state(request: Request) -> dict[str, Any]:
    drivers = _context(request).service.drivers
    point = drivers.mouse.position()
    return {
        "position": {"x": float(point.x), "y": float(point.y)},
        "buttons": sorted(drivers.mouse.buttons_down()),
    }


# ---------------------------------------------------------------------------
# keyboard
# ---------------------------------------------------------------------------
@api_router.post(
    "/keyboard/type",
    response_model=ActionResult,
    tags=["keyboard"],
    summary="Type text character by character",
)
def keyboard_type(request: Request, payload: KeyboardTypeRequest) -> ActionResult:
    return _execute_payload(request, "keyboard.type", payload)


@api_router.post(
    "/keyboard/press",
    response_model=ActionResult,
    tags=["keyboard"],
    summary="Press and release a single key",
)
def keyboard_press(request: Request, payload: KeyboardPressRequest) -> ActionResult:
    return _execute_payload(request, "keyboard.press", payload)


@api_router.post(
    "/keyboard/hotkey",
    response_model=ActionResult,
    tags=["keyboard"],
    summary="Press a key combination",
)
def keyboard_hotkey(request: Request, payload: KeyboardHotkeyRequest) -> ActionResult:
    return _execute_payload(request, "keyboard.hotkey", payload)


@api_router.post(
    "/keyboard/sequence",
    response_model=ActionResult,
    tags=["keyboard"],
    summary="Press several keys in sequence",
)
def keyboard_sequence(request: Request, payload: KeyboardSequenceRequest) -> ActionResult:
    return _execute_payload(request, "keyboard.sequence", payload)


@api_router.post(
    "/keyboard/key/down",
    response_model=ActionResult,
    tags=["keyboard"],
    summary="Press and hold a single key",
)
def keyboard_key_down(request: Request, payload: KeyboardKeyRequest) -> ActionResult:
    return _execute_payload(request, "keyboard.key_down", payload)


@api_router.post(
    "/keyboard/key/up",
    response_model=ActionResult,
    tags=["keyboard"],
    summary="Release a single key",
)
def keyboard_key_up(request: Request, payload: KeyboardKeyRequest) -> ActionResult:
    return _execute_payload(request, "keyboard.key_up", payload)


@api_router.post(
    "/keyboard/hold",
    response_model=ActionResult,
    tags=["keyboard"],
    summary="Hold a key for a duration",
)
def keyboard_hold(request: Request, payload: KeyboardHoldRequest) -> ActionResult:
    return _execute_payload(request, "keyboard.hold", payload)


# ---------------------------------------------------------------------------
# desktop
# ---------------------------------------------------------------------------
@api_router.post(
    "/desktop/wait",
    response_model=ActionResult,
    tags=["desktop"],
    summary="Cancel-aware sleep (useful between task steps)",
)
def desktop_wait(request: Request, payload: DesktopWaitRequest) -> ActionResult:
    return _execute_params(request, "desktop.wait", payload.model_dump(exclude_none=True))


# ---------------------------------------------------------------------------
# screen
# ---------------------------------------------------------------------------
@api_router.get(
    "/screen/size",
    response_model=Size,
    tags=["screen"],
    summary="Screen resolution",
)
def screen_size(request: Request) -> Size:
    return _context(request).service.screen_size()


@api_router.get(
    "/screen/screenshot",
    response_class=Response,
    tags=["screen"],
    responses={
        200: {
            "content": {"image/png": {}, "image/jpeg": {}},
            "description": "Screenshot image bytes",
        }
    },
    summary="Capture a screenshot and return the image",
)
def screen_screenshot(
    request: Request,
    region: Annotated[
        str | None,
        Query(description="Optional crop region 'x,y,width,height'", examples=["0,0,640,480"]),
    ] = None,
    fmt: Annotated[
        ScreenshotFormat,
        Query(alias="format", description="Image format (png or jpeg)"),
    ] = "png",
) -> Response:
    ctx = _context(request)
    rect = parse_region(region)
    data = ctx.service.drivers.screen.screenshot(rect, fmt)
    extension = "jpg" if fmt == "jpeg" else fmt
    return Response(
        content=data,
        media_type=_MEDIA_TYPES.get(fmt, "application/octet-stream"),
        headers={
            "Content-Disposition": f'inline; filename="screenshot.{extension}"',
            "Cache-Control": "no-store",
        },
    )


@api_router.post(
    "/screen/screenshot",
    tags=["screen"],
    summary="Capture a screenshot as JSON metadata (optionally save it)",
    response_model=None,
)
def screen_screenshot_json(request: Request, payload: ScreenshotRequest) -> dict[str, Any]:
    ctx = _context(request)
    params = payload.model_dump(exclude_none=True)
    params.pop("inline", None)
    request.state.action = "screen.screenshot"
    result = ctx.service.execute_action(
        "screen.screenshot",
        params,
        request_id=_request_id(request),
    )
    output = dict(result.output)
    if not payload.inline:
        output.pop("image_base64", None)
    return output


@api_router.get(
    "/screen/screenshots",
    response_model=ScreenshotListResponse,
    tags=["screen"],
    summary="List saved screenshots",
)
def screen_screenshots(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ScreenshotListResponse:
    ctx = _context(request)
    store = ctx.service.store
    items = store.screenshots.list(limit=limit, offset=offset)
    return ScreenshotListResponse(
        items=items,
        total=_screenshots_total(store),
        limit=limit,
        offset=offset,
    )


@api_router.get(
    "/screen/screenshots/{name}",
    tags=["screen"],
    summary="Download a saved screenshot",
    response_class=FileResponse,
)
def screen_screenshot_file(
    request: Request,
    name: str = Path(pattern=SAFE_NAME_PATTERN, description="Screenshot file name"),
) -> FileResponse:
    ctx = _context(request)
    path, info = ctx.service.store.screenshots.get(name)
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(info.format.lower(), "application/octet-stream"),
        filename=name,
        headers={"Cache-Control": "no-store"},
    )


@api_router.delete(
    "/screen/screenshots/{name}",
    status_code=204,
    tags=["screen"],
    summary="Delete a saved screenshot",
)
def screen_screenshot_delete(
    request: Request,
    name: str = Path(pattern=SAFE_NAME_PATTERN, description="Screenshot file name"),
) -> Response:
    ctx = _context(request)
    ctx.service.store.screenshots.delete(name)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------
@api_router.get(
    "/windows",
    response_model=WindowListResponse,
    tags=["windows"],
    summary="List top-level windows",
)
def windows(request: Request) -> WindowListResponse:
    ctx = _context(request)
    items = ctx.service.drivers.window.list_windows()
    return WindowListResponse(windows=items, count=len(items))


@api_router.get(
    "/windows/active",
    response_model=WindowInfo,
    tags=["windows"],
    summary="Currently active window",
)
def windows_active(request: Request) -> WindowInfo:
    ctx = _context(request)
    window = ctx.service.drivers.window.active_window()
    if window is None:
        raise WindowNotFound("no active window", details={})
    return window


@api_router.get(
    "/windows/{window_id}",
    response_model=WindowInfo,
    tags=["windows"],
    summary="Window details by id",
)
def windows_get(
    request: Request,
    window_id: str = Path(pattern=_WINDOW_ID_PATTERN, description="X11 window id (hex)"),
) -> WindowInfo:
    ctx = _context(request)
    return ctx.service.drivers.window.get_window(WindowSelector(id=window_id))


@api_router.post(
    "/windows/{window_id}/activate",
    response_model=ActionResult,
    tags=["windows"],
    summary="Activate (focus) a window",
)
def windows_activate(
    request: Request,
    window_id: str = Path(pattern=_WINDOW_ID_PATTERN),
) -> ActionResult:
    return _execute_params(request, "window.activate", {"id": window_id})


@api_router.post(
    "/windows/{window_id}/move",
    response_model=ActionResult,
    tags=["windows"],
    summary="Move a window",
)
def windows_move(
    request: Request,
    payload: WindowMoveRequest,
    window_id: str = Path(pattern=_WINDOW_ID_PATTERN),
) -> ActionResult:
    return _execute_params(request, "window.move", {"id": window_id, **payload.model_dump()})


@api_router.post(
    "/windows/{window_id}/resize",
    response_model=ActionResult,
    tags=["windows"],
    summary="Resize a window",
)
def windows_resize(
    request: Request,
    payload: WindowResizeRequest,
    window_id: str = Path(pattern=_WINDOW_ID_PATTERN),
) -> ActionResult:
    return _execute_params(request, "window.resize", {"id": window_id, **payload.model_dump()})


@api_router.post(
    "/windows/{window_id}/minimize",
    response_model=ActionResult,
    tags=["windows"],
    summary="Minimize a window",
)
def windows_minimize(
    request: Request,
    window_id: str = Path(pattern=_WINDOW_ID_PATTERN),
) -> ActionResult:
    return _execute_params(request, "window.minimize", {"id": window_id})


@api_router.post(
    "/windows/{window_id}/maximize",
    response_model=ActionResult,
    tags=["windows"],
    summary="Maximize a window",
)
def windows_maximize(
    request: Request,
    window_id: str = Path(pattern=_WINDOW_ID_PATTERN),
) -> ActionResult:
    return _execute_params(request, "window.maximize", {"id": window_id})


@api_router.post(
    "/windows/{window_id}/restore",
    response_model=ActionResult,
    tags=["windows"],
    summary="Restore a minimized/maximized window",
)
def windows_restore(
    request: Request,
    window_id: str = Path(pattern=_WINDOW_ID_PATTERN),
) -> ActionResult:
    return _execute_params(request, "window.restore", {"id": window_id})


@api_router.post(
    "/windows/{window_id}/close",
    response_model=ActionResult,
    tags=["windows"],
    summary="Close a window",
)
def windows_close(
    request: Request,
    window_id: str = Path(pattern=_WINDOW_ID_PATTERN),
) -> ActionResult:
    return _execute_params(request, "window.close", {"id": window_id})


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------
@api_router.post(
    "/tasks",
    response_model=TaskRecord,
    status_code=202,
    tags=["tasks"],
    summary="Submit a composed task (queued, executed serially)",
)
def tasks_submit(request: Request, payload: TaskSubmitRequest) -> TaskRecord:
    manager = _task_manager(request)
    specs = [step.to_spec() for step in payload.steps]
    record = manager.submit(
        specs,
        name=payload.name,
        timeout_ms=payload.timeout_ms,
        metadata=payload.metadata,
        client_request_id=payload.client_request_id,
    )
    request.state.task_id = record.id
    return record


@api_router.get(
    "/tasks",
    response_model=TaskListResponse,
    tags=["tasks"],
    summary="List tasks (newest first)",
)
def tasks_list(
    request: Request,
    status: Annotated[
        TaskStatus | None, Query(description="Filter by task status")
    ] = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> TaskListResponse:
    manager = _task_manager(request)
    items, total = manager.list(status=status, limit=limit, offset=offset)
    return TaskListResponse(items=items, total=total, limit=limit, offset=offset)


@api_router.get(
    "/tasks/{task_id}",
    response_model=TaskRecord,
    tags=["tasks"],
    summary="Task status, step results and error details",
)
def tasks_get(
    request: Request,
    task_id: str = Path(pattern=_TASK_ID_PATTERN, description="Task id returned by POST /tasks"),
) -> TaskRecord:
    manager = _task_manager(request)
    request.state.task_id = task_id
    return manager.get(task_id)


@api_router.post(
    "/tasks/{task_id}/cancel",
    response_model=TaskRecord,
    tags=["tasks"],
    summary="Request cooperative cancellation of a queued/running task",
)
def tasks_cancel(
    request: Request,
    task_id: str = Path(pattern=_TASK_ID_PATTERN, description="Task id returned by POST /tasks"),
) -> TaskRecord:
    manager = _task_manager(request)
    request.state.task_id = task_id
    return manager.cancel(task_id)


__all__ = ["api_router", "public_router"]
