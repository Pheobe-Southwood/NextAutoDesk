"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .. import API_VERSION, __version__
from ..bootstrap import AppContext, build_context
from ..config import AppConfig, load_config

LOGGER = logging.getLogger(__name__)


def create_app(config: AppConfig | None = None, *, context: AppContext | None = None) -> FastAPI:
    """Create the HTTP application.

    Parameters
    ----------
    config:
        Configuration used when constructing the runtime context inside the
        application lifespan.
    context:
        Pre-built context (used by tests). When provided, ``config`` is ignored
        except for HTTP-surface settings read at construction time.
    """

    cfg = config or (context.config if context is not None else load_config())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = context
        if ctx is None:
            ctx = build_context(cfg)
        app.state.context = ctx
        ctx.start()
        try:
            yield
        finally:
            ctx.shutdown()

    app = FastAPI(
        title="NextAutoDesk",
        version=__version__,
        description=(
            "Headless X11/XFCE GUI automation node: human-like mouse & keyboard "
            "policies, screen element perception (template matching + OCR), "
            "screenshots, window management and serial task execution."
        ),
        lifespan=lifespan,
        docs_url=cfg.server.docs_url,
        openapi_url=cfg.server.openapi_url,
        redoc_url=None,
        openapi_tags=[
            {"name": "meta", "description": "Health, capabilities, catalogues and configuration."},
            {"name": "mouse", "description": "Pointer movement, buttons, drag and scroll."},
            {"name": "keyboard", "description": "Key presses, holds, text and shortcuts."},
            {"name": "screen", "description": "Screen probes, screenshots and screenshot files."},
            {"name": "windows", "description": "Window enumeration and manipulation."},
            {"name": "desktop", "description": "Timing helpers for composed operations."},
            {
                "name": "elements",
                "description": (
                    "Read-only screen perception (tier 'perception'): locator discovery, "
                    "template matching, OCR text lookup and OCR extraction."
                ),
            },
            {
                "name": "interactions",
                "description": (
                    "Intent-level composite operations (tier 'composite') built from "
                    "primitives + perception: locate-then-click and locate-then-type."
                ),
            },
            {"name": "tasks", "description": "Composed, queued and cancellable operations."},
            {"name": "system", "description": "Service health and readiness probes (no auth)."},
        ],
    )

    from .errors_handlers import register_exception_handlers
    from .middleware import RequestContextMiddleware
    from .routes import api_router, public_router

    register_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware, config=cfg)
    if cfg.server.cors.enabled:
        # Added last so it wraps the request-context middleware: even 401/413
        # responses produced by middleware carry the CORS headers.
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.server.cors.allow_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[cfg.auth.header, "Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
            max_age=600,
        )
    app.include_router(public_router)
    app.include_router(api_router, prefix=f"/api/{API_VERSION}")
    return app


app = create_app

__all__ = ["app", "create_app"]
