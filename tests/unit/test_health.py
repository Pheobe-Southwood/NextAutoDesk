"""Health deep-probe timeout behaviour (task-5 audit)."""

from __future__ import annotations

import time
from pathlib import Path

from test_api_support import AUTH_HEADERS, serving


def test_deep_health_probes_are_bounded_by_deep_timeout_ms(tmp_path: Path) -> None:
    with serving(tmp_path) as (env, client):
        env.config.health.deep_timeout_ms = 100
        original_size = env.drivers.screen.size

        def slow_size():  # type: ignore[no-untyped-def]
            time.sleep(0.5)
            return original_size()

        env.drivers.screen.size = slow_size  # type: ignore[method-assign]
        started = time.monotonic()
        response = client.get("/api/v1/health", headers=AUTH_HEADERS)
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 0.45, f"deep probes blocked the request for {elapsed:.3f}s"
    report = response.json()
    assert report["status"] == "degraded"
    for name in ("desktop", "window_manager", "providers", "perception"):
        check = report["checks"][name]
        assert check["status"] == "degraded", check
        assert check["details"]["timed_out"] is True
        assert check["details"]["timeout_ms"] == 100
    assert report["runtime"]["deep"] is True
