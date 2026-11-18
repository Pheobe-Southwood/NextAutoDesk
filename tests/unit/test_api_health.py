"""Health aggregation, liveness and readiness tests."""

from __future__ import annotations

from pathlib import Path

from test_api_support import AUTH_HEADERS, serving

from nextautodesk.health import build_health_report

EXPECTED_DEEP_CHECKS = {
    "api",
    "executor",
    "desktop",
    "window_manager",
    "storage",
    "database",
    "providers",
    "perception",
}


def test_health_report_ok(tmp_path: Path) -> None:
    with serving(tmp_path) as (_env, client):
        response = client.get("/api/v1/health", headers=AUTH_HEADERS)

    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "ok"
    assert set(report["checks"]) == EXPECTED_DEEP_CHECKS
    for check in report["checks"].values():
        assert check["status"] == "ok", check
    assert report["checks"]["desktop"]["details"]["screen"] == {"width": 1280, "height": 720}
    assert report["checks"]["database"]["details"]["counts"]["total"] >= 0

    runtime = report["runtime"]
    assert runtime["version"]
    assert runtime["api_version"] == "v1"
    assert runtime["uptime_s"] >= 0
    assert runtime["memory"]["process_rss_bytes"] > 0
    assert runtime["cpu"]["count"] >= 1
    assert runtime["disk"]["free_mb"] > 0
    assert runtime["screenshots_count"] == 0
    assert "total" in runtime["tasks"]


def test_shallow_health_skips_deep_probes(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.drivers.screen.fail = True
        response = client.get(
            "/api/v1/health", headers=AUTH_HEADERS, params={"deep": "false"}
        )

    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "ok"
    assert "window_manager" not in report["checks"]
    assert "providers" not in report["checks"]
    assert "perception" not in report["checks"]
    assert report["runtime"]["deep"] is False


def test_health_reports_unhealthy_when_desktop_probe_fails(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.drivers.screen.fail = True
        response = client.get("/api/v1/health", headers=AUTH_HEADERS)
        liveness = client.get("/healthz")
        ready = client.get("/readyz")

    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "unhealthy"
    assert report["checks"]["desktop"]["status"] == "error"
    assert "probe failed" in report["checks"]["desktop"]["message"]
    # Liveness never touches the display, so it must stay green.
    assert liveness.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["ready"] is False
    assert ready.json()["checks"]["desktop"].startswith("error:")


def test_health_reports_degraded_when_provider_unavailable(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.strategies.unavailable["direct"] = "optional package is missing"
        response = client.get("/api/v1/health", headers=AUTH_HEADERS)

    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "degraded"
    providers = report["checks"]["providers"]
    assert providers["status"] == "degraded"
    assert providers["details"]["unavailable"][0]["name"] == "direct"


def test_readyz_not_ready_when_service_not_started(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.service._started = False  # simulate a service that failed to start / stopped
        try:
            ready = client.get("/readyz")
            liveness = client.get("/healthz")
        finally:
            env.service._started = True

    assert ready.status_code == 503
    assert ready.json()["checks"]["desktop"] == "not_started"
    assert liveness.status_code == 200


def test_build_health_report_without_task_store_omits_database_check(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, _client):
        report = build_health_report(env.service)

    assert "database" not in report.checks
    assert report.status == "ok"
    assert report.checks["api"].status == "ok"
