"""API key authentication.

Accepted credentials (when ``auth.enabled`` is true):

* the configured header (``auth.header``, default ``X-API-Key``), or
* ``Authorization: Bearer <key>``.

Comparison uses :func:`hmac.compare_digest` to avoid timing side channels.
Health probes (``/healthz``, ``/readyz``) and the root endpoint are public;
documentation endpoints are public unless ``auth.protect_docs`` is enabled
(enforced by :mod:`nextautodesk.api.middleware`, because the docs routes are
registered while the FastAPI app is constructed).
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from ..config import AppConfig
from ..errors import AuthError, ErrorCode, NextAutoDeskError

#: Schema used only for the OpenAPI security section / Swagger UI "Authorize" button.
#: The actual header name is configurable (``auth.header``).
api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    scheme_name="ApiKey",
    description=(
        "API key. The header name is configurable via auth.header; "
        "`Authorization: Bearer <key>` is also accepted."
    ),
)

WWW_AUTHENTICATE = "ApiKey"


class MissingAPIKey(AuthError):
    """No credential was supplied at all."""

    code = ErrorCode.AUTH_MISSING

    def __init__(self, message: str = "API key required", **kwargs: object) -> None:
        super().__init__(message, **kwargs)  # type: ignore[arg-type]


class InvalidAPIKey(AuthError):
    """A credential was supplied but does not match any configured key."""

    code = ErrorCode.AUTH_INVALID


def _bearer_token(headers: Mapping[str, str]) -> str | None:
    raw = headers.get("authorization") or headers.get("Authorization")
    if not raw:
        return None
    scheme, _, token = raw.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def extract_api_key(headers: Mapping[str, str], config: AppConfig) -> str | None:
    """Return the presented API key (configured header first, then bearer token)."""

    value = headers.get(config.auth.header)
    if value:
        return value.strip()
    return _bearer_token(headers)


def check_api_key(headers: Mapping[str, str], config: AppConfig) -> NextAutoDeskError | None:
    """Validate credentials without raising.

    Returns ``None`` when the request is authorised (or auth is disabled),
    otherwise the domain error to surface.
    """

    auth = config.auth
    if not auth.enabled:
        return None
    provided = extract_api_key(headers, config)
    if not provided:
        return MissingAPIKey(
            "API key required",
            details={"header": auth.header, "alternatives": ["Authorization: Bearer <key>"]},
        )
    # compare_digest rejects non-ASCII ``str`` inputs, so compare the UTF-8
    # bytes instead: configured keys may legitimately contain unicode, and an
    # arbitrary unicode header must produce 401 rather than a 500.
    provided_bytes = provided.encode("utf-8", "surrogatepass")
    matched = False
    for key in auth.api_keys:
        # compare_digest over every key: never short-circuit on a match so the
        # timing does not depend on the key's position in the list.
        if hmac.compare_digest(provided_bytes, key.encode("utf-8", "surrogatepass")):
            matched = True
    if not matched:
        return InvalidAPIKey(
            "invalid API key",
            details={"header": auth.header},
        )
    return None


def require_api_key(
    request: Request,
    _api_key: str | None = Security(api_key_header),
) -> None:
    """FastAPI dependency enforcing authentication for the ``/api/v1`` router."""

    ctx = getattr(request.app.state, "context", None)
    if ctx is None:  # pragma: no cover - the lifespan always installs the context
        raise RuntimeError("application context is not initialised")
    error = check_api_key(request.headers, ctx.config)
    if error is not None:
        raise error


__all__ = [
    "WWW_AUTHENTICATE",
    "InvalidAPIKey",
    "MissingAPIKey",
    "api_key_header",
    "check_api_key",
    "extract_api_key",
    "require_api_key",
]
