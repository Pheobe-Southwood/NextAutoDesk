"""Authentication and documentation-protection tests."""

from __future__ import annotations

from pathlib import Path

from test_api_support import API_KEY, make_config, serving


def test_missing_api_key_returns_401_with_www_authenticate(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.get("/api/v1/info")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "ApiKey"
    body = response.json()
    assert body["error"]["code"] == "AUTH_MISSING"
    assert body["error"]["message"]
    # Every error carries the same request id as the response header.
    assert body["error"]["request_id"] == response.headers["x-request-id"]


def test_invalid_api_key_returns_401(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.get("/api/v1/info", headers={"X-API-Key": "wrong-key"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID"
    assert response.headers["www-authenticate"] == "ApiKey"


def test_valid_api_key_grants_access(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.get("/api/v1/info", headers={"X-API-Key": API_KEY})
        second = client.get("/api/v1/info", headers={"X-API-Key": f" {API_KEY} "})

    assert response.status_code == 200
    assert response.json()["name"] == "nextautodesk"
    assert second.status_code == 200


def test_bearer_token_is_accepted(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.get(
            "/api/v1/info", headers={"Authorization": f"Bearer {API_KEY}"}
        )
        bad = client.get("/api/v1/info", headers={"Authorization": "Bearer nope"})
        wrong_scheme = client.get("/api/v1/info", headers={"Authorization": f"Basic {API_KEY}"})

    assert response.status_code == 200
    assert bad.status_code == 401
    assert wrong_scheme.status_code == 401


def test_health_probes_are_public(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        healthz = client.get("/healthz")
        readyz = client.get("/readyz")
        root = client.get("/")

    assert healthz.status_code == 200
    assert healthz.json()["status"] == "ok"
    assert readyz.status_code == 200
    assert readyz.json()["ready"] is True
    assert root.status_code == 200
    assert root.json()["api"] == "/api/v1"


def test_auth_can_be_disabled(tmp_path: Path) -> None:
    with serving(tmp_path, auth_enabled=False, api_keys=()) as (_env, client):
        response = client.get("/api/v1/info")

    assert response.status_code == 200


def test_docs_are_public_by_default(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        docs = client.get("/docs")
        schema = client.get("/openapi.json")

    assert docs.status_code == 200
    assert schema.status_code == 200


def test_docs_can_be_protected(tmp_path: Path) -> None:
    with serving(tmp_path, protect_docs=True) as (_env, client):
        docs_without = client.get("/docs")
        openapi_without = client.get("/openapi.json")
        docs_with = client.get("/docs", headers={"X-API-Key": API_KEY})
        openapi_with = client.get("/openapi.json", headers={"X-API-Key": API_KEY})

    assert docs_without.status_code == 401
    assert docs_without.headers["www-authenticate"] == "ApiKey"
    assert openapi_without.status_code == 401
    assert openapi_without.json()["error"]["code"] == "AUTH_MISSING"
    assert docs_with.status_code == 200
    assert openapi_with.status_code == 200


def test_request_id_is_echoed_and_reused_in_errors(tmp_path: Path) -> None:
    headers = {"X-API-Key": API_KEY, "X-Request-ID": "client-correlation-123"}
    with serving(tmp_path) as (_env, client):
        ok = client.get("/api/v1/info", headers=headers)
        error = client.get("/api/v1/tasks/does-not-exist", headers=headers)

    assert ok.headers["x-request-id"] == "client-correlation-123"
    assert error.headers["x-request-id"] == "client-correlation-123"
    assert error.json()["error"]["request_id"] == "client-correlation-123"


def test_non_ascii_api_key_is_compared_as_utf8_bytes(tmp_path: Path) -> None:
    """Regression: non-ASCII keys must not reach ``compare_digest(str, str)``.

    ASGI servers surface header values as latin-1 decoded strings (the test
    client cannot send raw non-ASCII header bytes), so build the same
    :class:`Headers` object the request would carry.
    """

    from starlette.datastructures import Headers

    from nextautodesk.api.security import check_api_key

    config = make_config(tmp_path, api_keys=("café-key",))
    accepted = Headers(raw=[(b"x-api-key", "café-key".encode("latin-1"))])
    rejected = Headers(raw=[(b"x-api-key", "café-wrong".encode("latin-1"))])
    assert check_api_key(accepted, config) is None
    error = check_api_key(rejected, config)
    assert error is not None
    assert error.code.value == "AUTH_INVALID"


def test_non_ascii_key_comparison_handles_untransportable_unicode(tmp_path: Path) -> None:
    """Keys outside latin-1 (not HTTP-transportable) still compare without error."""

    from nextautodesk.api.security import check_api_key

    config = make_config(tmp_path, api_keys=("密钥",))
    assert check_api_key({"X-API-Key": "密钥"}, config) is None
    error = check_api_key({"X-API-Key": "密钥-other"}, config)
    assert error is not None
    assert error.code.value == "AUTH_INVALID"
