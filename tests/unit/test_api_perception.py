"""HTTP contract tests for the perception (``/elements``) and composite tiers.

The fake engine records invocations and returns canned perception payloads, so
these tests are headless and do not need OpenCV/Tesseract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_api_support import AUTH_HEADERS, serving

from nextautodesk.errors import ElementNotFound, PerceptionFailed, PerceptionUnavailable

MATCH_JSON: dict[str, Any] = {
    "box": {"x": 90, "y": 35, "width": 20, "height": 10},
    "center": {"x": 100.0, "y": 40.0},
    "confidence": 0.95,
    "locator": "text",
    "text": "Submit",
    "metadata": {"granularity": "line"},
}
FRAME_JSON: dict[str, Any] = {
    "region": {"x": 0, "y": 0, "width": 1280, "height": 720},
    "width": 1280,
    "height": 720,
    "captured_at": "2026-01-01T00:00:00Z",
    "screenshot": None,
}
ELEMENT_QUERY: dict[str, Any] = {
    "locator": "text",
    "text": {"text": "Submit", "match": "contains"},
}


def locate_output(
    matches: list[dict[str, Any]] | None = None, *, attempts: int = 1
) -> dict[str, Any]:
    return {
        "locator": "text",
        "matches": list(matches or []),
        "frame": dict(FRAME_JSON),
        "elapsed_ms": 4.5,
        "attempts": attempts,
    }


def ocr_output() -> dict[str, Any]:
    line = {
        "text": "HELLO",
        "box": {"x": 10, "y": 20, "width": 60, "height": 14},
        "center": {"x": 40.0, "y": 27.0},
        "confidence": 88.0,
        "words": [
            {
                "text": "HELLO",
                "box": {"x": 10, "y": 20, "width": 60, "height": 14},
                "center": {"x": 40.0, "y": 27.0},
                "confidence": 88.0,
            }
        ],
    }
    return {"lines": [line], "text": "HELLO", "frame": dict(FRAME_JSON), "elapsed_ms": 3.0}


def recorded(env: Any, name: str) -> dict[str, Any]:
    calls = [call for call in env.recorder.calls if call.name == name]
    assert calls, f"{name} was not invoked"
    return calls[-1].params


# ---------------------------------------------------------------------------
# locator discovery (read-only)
# ---------------------------------------------------------------------------
def test_elements_locators_lists_builtin_locators(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.get("/api/v1/elements/locators", headers=AUTH_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    names = [entry["name"] for entry in body["locators"]]
    assert names == ["template", "text"]
    kinds = {entry["name"]: entry["kind"] for entry in body["locators"]}
    assert kinds == {"template": "template", "text": "text"}
    for entry in body["locators"]:
        assert entry["supported_queries"]
        assert isinstance(entry["available"], bool)


def test_elements_locators_requires_api_key(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.get("/api/v1/elements/locators")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_MISSING"


# ---------------------------------------------------------------------------
# element.find / element.wait / element.ocr
# ---------------------------------------------------------------------------
def test_elements_find_returns_locate_result(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.handler("element.find").output = locate_output([MATCH_JSON])
        response = client.post(
            "/api/v1/elements/find",
            headers=AUTH_HEADERS,
            json={"query": ELEMENT_QUERY},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["locator"] == "text"
    assert body["matches"][0]["center"] == {"x": 100.0, "y": 40.0}
    assert body["frame"]["region"] == {"x": 0, "y": 0, "width": 1280, "height": 720}
    params = recorded(env, "element.find")
    assert params["query"]["text"]["text"] == "Submit"
    assert "timeout_ms" not in params


def test_elements_wait_returns_attempts(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.handler("element.wait").output = locate_output([MATCH_JSON], attempts=3)
        response = client.post(
            "/api/v1/elements/wait",
            headers=AUTH_HEADERS,
            json={"query": ELEMENT_QUERY, "timeout_ms": 1500, "poll_interval_ms": 50},
        )

    assert response.status_code == 200
    assert response.json()["attempts"] == 3
    params = recorded(env, "element.wait")
    assert params["timeout_ms"] == 1500
    assert params["poll_interval_ms"] == 50


def test_elements_ocr_returns_lines(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.handler("element.ocr").output = ocr_output()
        response = client.post(
            "/api/v1/elements/ocr",
            headers=AUTH_HEADERS,
            json={
                "region": {"x": 0, "y": 0, "width": 300, "height": 200},
                "languages": ["eng"],
                "psm": 6,
                "min_confidence": 40,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "HELLO"
    assert body["lines"][0]["box"]["width"] == 60
    assert body["lines"][0]["words"][0]["text"] == "HELLO"
    params = recorded(env, "element.ocr")
    assert params["languages"] == ["eng"]
    assert params["psm"] == 6
    assert params["region"] == {"x": 0, "y": 0, "width": 300, "height": 200}


def test_elements_request_validation(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        missing_spec = client.post(
            "/api/v1/elements/find",
            headers=AUTH_HEADERS,
            json={"query": {"locator": "auto"}},
        )
        both_specs = client.post(
            "/api/v1/elements/find",
            headers=AUTH_HEADERS,
            json={
                "query": {
                    "template": {"image_base64": "aGVsbG8="},
                    "text": {"text": "hello"},
                }
            },
        )
        unknown_field = client.post(
            "/api/v1/elements/find",
            headers=AUTH_HEADERS,
            json={"query": ELEMENT_QUERY, "bogus": True},
        )
        bad_psm = client.post(
            "/api/v1/elements/ocr",
            headers=AUTH_HEADERS,
            json={"psm": 42},
        )

    for response in (missing_spec, both_specs, unknown_field, bad_psm):
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# interaction.click / interaction.type (composite)
# ---------------------------------------------------------------------------
def test_interactions_click_executes_composite(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.handler("interaction.click").output = {
            "target": MATCH_JSON,
            "position": {"x": 100.0, "y": 40.0},
            "click": {"clicked": True},
            "attempts": 2,
        }
        response = client.post(
            "/api/v1/interactions/click",
            headers=AUTH_HEADERS,
            json={
                "query": ELEMENT_QUERY,
                "button": "right",
                "clicks": 2,
                "wait_timeout_ms": 1200,
                "strategy": "direct",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "interaction.click"
    assert body["success"] is True
    assert body["output"]["position"] == {"x": 100.0, "y": 40.0}
    assert body["output"]["target"]["center"] == {"x": 100.0, "y": 40.0}
    params = recorded(env, "interaction.click")
    assert params["button"] == "right"
    assert params["clicks"] == 2
    assert params["wait_timeout_ms"] == 1200
    # Strategy is an HTTP-level option: validated by the service and applied via
    # ActionContext.strategy_override, never forwarded as an action parameter.
    assert "strategy" not in params


def test_interactions_strategy_override_disabled_rejected(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.config.server.allow_strategy_override = False
        response = client.post(
            "/api/v1/interactions/click",
            headers=AUTH_HEADERS,
            json={"query": ELEMENT_QUERY, "strategy": "direct"},
        )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_interactions_type_executes_composite(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.handler("interaction.type").output = {
            "target": MATCH_JSON,
            "click": {"clicked": True},
            "typed": {"typed": "hello", "length": 5},
            "cleared": True,
            "submitted": True,
            "attempts": 1,
        }
        response = client.post(
            "/api/v1/interactions/type",
            headers=AUTH_HEADERS,
            json={
                "query": ELEMENT_QUERY,
                "text": "hello",
                "clear": True,
                "submit": True,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "interaction.type"
    assert body["output"]["typed"] == {"typed": "hello", "length": 5}
    assert body["output"]["cleared"] is True
    assert body["output"]["submitted"] is True
    params = recorded(env, "interaction.type")
    assert params["text"] == "hello"


def test_interactions_require_api_key(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.post(
            "/api/v1/interactions/click", json={"query": ELEMENT_QUERY}
        )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------
def test_element_error_codes_map_to_http_statuses(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.handler("element.find").error = ElementNotFound(
            "element not found", details={"query": "Submit"}
        )
        not_found = client.post(
            "/api/v1/elements/find", headers=AUTH_HEADERS, json={"query": ELEMENT_QUERY}
        )

        env.handler("element.find").error = PerceptionUnavailable("perception disabled")
        unavailable = client.post(
            "/api/v1/elements/find", headers=AUTH_HEADERS, json={"query": ELEMENT_QUERY}
        )

        env.handler("element.find").error = PerceptionFailed("opencv exploded")
        failed = client.post(
            "/api/v1/elements/find", headers=AUTH_HEADERS, json={"query": ELEMENT_QUERY}
        )

        env.handler("interaction.click").error = ElementNotFound("no target")
        interaction = client.post(
            "/api/v1/interactions/click",
            headers=AUTH_HEADERS,
            json={"query": ELEMENT_QUERY},
        )

    assert (not_found.status_code, not_found.json()["error"]["code"]) == (
        404,
        "ELEMENT_NOT_FOUND",
    )
    assert (unavailable.status_code, unavailable.json()["error"]["code"]) == (
        409,
        "PERCEPTION_UNAVAILABLE",
    )
    assert (failed.status_code, failed.json()["error"]["code"]) == (502, "PERCEPTION_FAILED")
    assert (interaction.status_code, interaction.json()["error"]["code"]) == (
        404,
        "ELEMENT_NOT_FOUND",
    )


# ---------------------------------------------------------------------------
# catalogue, info, OpenAPI
# ---------------------------------------------------------------------------
def test_actions_catalogue_groups_all_three_tiers(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.get("/api/v1/actions", headers=AUTH_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 35
    tiers = body["tiers"]
    assert set(tiers) == {"primitive", "perception", "composite"}
    assert sorted(tiers["perception"]) == ["element.find", "element.ocr", "element.wait"]
    assert sorted(tiers["composite"]) == ["interaction.click", "interaction.type"]
    assert len(tiers["primitive"]) == 30
    entries = {entry["name"]: entry for entry in body["actions"]}
    assert entries["element.find"]["tier"] == "perception"
    assert entries["interaction.type"]["tier"] == "composite"
    assert entries["mouse.move"]["tier"] == "primitive"


def test_info_exposes_perception_and_api_tiers(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.get("/api/v1/info", headers=AUTH_HEADERS)

    assert response.status_code == 200
    body = response.json()
    perception = body["perception"]
    assert perception["enabled"] is True
    assert [entry["name"] for entry in perception["locators"]] == ["template", "text"]
    features = body["features"]
    assert features["screen_element_perception"] is True
    assert features["api_tiers"] == ["primitive", "perception", "composite", "workflow"]


def test_openapi_includes_perception_and_composite_paths(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        schema = client.get("/openapi.json").json()

    paths = schema["paths"]
    for path in (
        "/api/v1/elements/locators",
        "/api/v1/elements/find",
        "/api/v1/elements/wait",
        "/api/v1/elements/ocr",
        "/api/v1/interactions/click",
        "/api/v1/interactions/type",
    ):
        assert path in paths, path
    assert paths["/api/v1/elements/find"]["post"]["tags"] == ["elements"]
    assert paths["/api/v1/interactions/click"]["post"]["tags"] == ["interactions"]


# ---------------------------------------------------------------------------
# hot reload
# ---------------------------------------------------------------------------
def _perception_overlay(tmp_path: Path, extra_lines: list[str]) -> Path:
    data_dir = tmp_path / "data"
    overlay = tmp_path / "perception-overlay.yml"
    overlay.write_text(
        "\n".join(
            [
                "auth:",
                "  enabled: true",
                "  api_keys: ['test-key']",
                "desktop:",
                "  width: 1280",
                "  height: 720",
                "storage:",
                f"  data_dir: '{data_dir}'",
                *extra_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return overlay


def test_config_reload_applies_perception_settings(tmp_path: Path) -> None:
    overlay = _perception_overlay(
        tmp_path,
        [
            "perception:",
            "  max_results: 5",
            "  locators:",
            "    text:",
            "      enabled: false",
        ],
    )
    with serving(tmp_path) as (env, client):
        env.config.source_path = str(overlay)
        response = client.post("/api/v1/config/reload", headers=AUTH_HEADERS)
        locators = client.get("/api/v1/elements/locators", headers=AUTH_HEADERS)

    assert response.status_code == 200, response.text
    applied = response.json()["config"]["perception"]
    assert applied["max_results"] == 5
    assert env.service.perception.settings.max_results == 5
    text_locator = next(
        entry for entry in locators.json()["locators"] if entry["name"] == "text"
    )
    assert text_locator["available"] is False
    assert text_locator["reason"] == "disabled in configuration"


def test_config_reload_rejects_invalid_perception_params(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        before = env.service.perception.settings
        overlay = _perception_overlay(
            tmp_path,
            [
                "perception:",
                "  locators:",
                "    template:",
                "      params:",
                "        threshold: 2.0",
            ],
        )
        env.config.source_path = str(overlay)
        response = client.post("/api/v1/config/reload", headers=AUTH_HEADERS)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "RELOAD_FAILED"
    assert "perception" in error["message"]
    assert env.service.perception.settings is before
