"""Health checking and runtime metrics.

``build_health_report`` aggregates cheap probes into a :class:`HealthReport`:

* **api** - the HTTP process itself,
* **executor** - the serial desktop worker,
* **desktop** - X11 display reachable (screen size + pointer probe, deep mode only),
* **window_manager** - EWMH window manager detected (deep mode only),
* **storage** - data directory writable and enough disk free,
* **database** - task store reachable (when a task store is supplied),
* **providers** - strategy providers availability (deep mode only),
* **perception** - locator availability without capturing the screen (deep mode only).

The aggregate status is ``unhealthy`` when any check is in ``error``, ``degraded``
when any check is ``degraded``/``unknown``, otherwise ``ok``.

``/healthz`` (liveness) deliberately does **not** call this function: it must be
extremely cheap and never touch the X display.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import API_VERSION, __version__
from .models import HealthCheck, HealthReport

LOGGER = logging.getLogger(__name__)

_MB = 1024.0 * 1024.0
_CGROUP_V2_MEMORY_CURRENT = Path("/sys/fs/cgroup/memory.current")
_CGROUP_V2_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")


def _mb(value: float | int) -> float:
    return round(float(value) / _MB, 3)


def _memory_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        import psutil

        process = psutil.Process(os.getpid())
        rss = process.memory_info().rss
        info["process_rss_bytes"] = rss
        info["process_rss_mb"] = _mb(rss)
        with contextlib.suppress(Exception):  # pragma: no cover - platform dependent
            info["system_percent"] = psutil.virtual_memory().percent
    except Exception:  # pragma: no cover - psutil should be installed
        LOGGER.debug("psutil unavailable; memory metrics omitted", exc_info=True)
    # cgroup v2 (containers) - useful when the process RSS under-reports pressure.
    try:
        current = _CGROUP_V2_MEMORY_CURRENT.read_text(encoding="ascii").strip()
        info["cgroup_current_bytes"] = int(current)
        info["cgroup_current_mb"] = _mb(int(current))
        raw_max = _CGROUP_V2_MEMORY_MAX.read_text(encoding="ascii").strip()
        if raw_max.isdigit():
            info["cgroup_limit_bytes"] = int(raw_max)
            info["cgroup_limit_mb"] = _mb(int(raw_max))
            if int(raw_max) > 0:
                info["cgroup_used_percent"] = round(int(current) / int(raw_max) * 100.0, 2)
    except (OSError, ValueError):  # pragma: no cover - no cgroup on the host
        pass
    return info


def _cpu_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        import psutil

        info["percent"] = psutil.cpu_percent(interval=None)
        info["count"] = psutil.cpu_count(logical=True)
    except Exception:  # pragma: no cover - psutil should be installed
        pass
    try:
        load1, load5, load15 = os.getloadavg()
        info["loadavg"] = [round(load1, 3), round(load5, 3), round(load15, 3)]
    except (OSError, AttributeError):  # pragma: no cover - non-POSIX
        pass
    return info


def _disk_info(service: Any) -> dict[str, Any]:
    try:
        usage = service.store.disk_usage()
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "total_mb": _mb(usage.get("total", 0)),
        "used_mb": _mb(usage.get("used", 0)),
        "free_mb": _mb(usage.get("free", 0)),
        "path": str(getattr(service.store, "root", "")),
    }


def _screenshot_count(service: Any) -> int:
    try:
        return int(service.store.screenshots.count())
    except Exception:  # pragma: no cover - defensive
        return 0


def _task_counts(task_store: Any) -> dict[str, Any]:
    if task_store is None:
        return {}
    try:
        counts = task_store.counts()
        return {str(key): int(value) for key, value in counts.items()}
    except Exception:  # pragma: no cover - defensive
        LOGGER.debug("cannot read task counts", exc_info=True)
        return {}


def _check_desktop(service: Any, *, deep: bool) -> HealthCheck:
    started = bool(getattr(service, "started", False))
    if not deep:
        if started:
            return HealthCheck(status="ok", message="service started (shallow probe)")
        return HealthCheck(status="error", message="service is not started")
    if not started:
        return HealthCheck(status="error", message="desktop service is not started")
    started_at = time.monotonic()
    try:
        drivers = service.drivers
        drivers.probe()
        screen = drivers.screen.size()
        point = drivers.mouse.position()
        duration_ms = round((time.monotonic() - started_at) * 1000.0, 3)
        return HealthCheck(
            status="ok",
            message="X11 desktop reachable",
            details={
                "screen": {"width": screen.width, "height": screen.height},
                "pointer": {"x": point.x, "y": point.y},
                "probe_ms": duration_ms,
            },
        )
    except Exception as exc:
        return HealthCheck(
            status="error",
            message=f"desktop probe failed: {type(exc).__name__}: {exc}",
            details={"display": getattr(service, "config", None).desktop.display
                     if getattr(service, "config", None) is not None else None},
        )


def _check_window_manager(service: Any, *, deep: bool) -> HealthCheck:
    config = getattr(service, "config", None)
    if config is not None and not config.health.check_window_manager:
        return HealthCheck(status="ok", message="window manager check disabled by configuration")
    if not deep:
        return HealthCheck(status="unknown", message="skipped in shallow mode")
    try:
        name = service.drivers.window.window_manager_name()
    except Exception as exc:
        return HealthCheck(
            status="error",
            message=f"window manager probe failed: {type(exc).__name__}: {exc}",
        )
    if name:
        return HealthCheck(status="ok", message=f"window manager detected: {name}", details={"name": name})
    return HealthCheck(
        status="degraded",
        message="no EWMH window manager detected; window operations may be unreliable",
    )


def _check_executor(service: Any) -> HealthCheck:
    try:
        stats = service.executor.stats()
    except Exception as exc:  # pragma: no cover - defensive
        return HealthCheck(status="error", message=f"executor stats failed: {exc}")
    status = "ok" if stats.state == "running" else "error"
    message = "serial executor running" if status == "ok" else "serial executor is stopped"
    return HealthCheck(status=status, message=message, details=stats.model_dump(mode="json"))


def _check_storage(service: Any) -> HealthCheck:
    config = getattr(service, "config", None)
    details: dict[str, Any] = {}
    try:
        writable = bool(service.store.writable())
    except Exception as exc:  # pragma: no cover - defensive
        return HealthCheck(status="error", message=f"storage probe failed: {exc}")
    details["writable"] = writable
    if not writable:
        return HealthCheck(status="error", message="data directory is not writable", details=details)
    check_disk = bool(getattr(getattr(config, "health", None), "check_disk", True))
    min_free_mb = int(getattr(getattr(config, "health", None), "min_free_disk_mb", 0))
    if not check_disk:
        return HealthCheck(status="ok", message="storage writable (disk check disabled)", details=details)
    try:
        usage = service.store.disk_usage()
        free_mb = _mb(usage.get("free", 0))
        details.update({"free_mb": free_mb, "min_free_mb": min_free_mb})
        if free_mb < min_free_mb:
            return HealthCheck(
                status="degraded",
                message=f"low disk space: {free_mb:.1f} MiB free (< {min_free_mb} MiB)",
                details=details,
            )
        return HealthCheck(status="ok", message="storage writable, disk space sufficient", details=details)
    except Exception as exc:  # pragma: no cover - defensive
        details["disk_error"] = str(exc)
        return HealthCheck(status="degraded", message="storage writable but disk usage unavailable", details=details)


def _check_database(task_store: Any) -> HealthCheck:
    if task_store is None:
        return HealthCheck(status="unknown", message="task store is not attached")
    try:
        counts = task_store.counts()
    except Exception as exc:
        return HealthCheck(status="error", message=f"task database unavailable: {exc}")
    return HealthCheck(status="ok", message="task database reachable", details={"counts": counts})


def _check_providers(service: Any, *, deep: bool) -> HealthCheck:
    if not deep:
        return HealthCheck(status="unknown", message="skipped in shallow mode")
    try:
        availability = service.strategies.provider_availability()
    except Exception as exc:  # pragma: no cover - defensive
        return HealthCheck(status="error", message=f"provider probe failed: {exc}")
    if not availability:
        return HealthCheck(status="error", message="no strategy providers registered")
    unavailable = [
        {"name": item.name, "reason": item.reason}
        for item in availability
        if not item.available
    ]
    if unavailable:
        return HealthCheck(
            status="degraded",
            message=f"{len(unavailable)} strategy provider(s) unavailable",
            details={"available": len(availability) - len(unavailable), "unavailable": unavailable},
        )
    return HealthCheck(
        status="ok",
        message=f"{len(availability)} strategy provider(s) available",
        details={"available": len(availability)},
    )


def _check_perception(service: Any, *, deep: bool) -> HealthCheck:
    """Report perception locator availability.

    This check never captures the screen: it only asks the locators whether
    their dependencies are installed and whether they are enabled.
    """

    if not deep:
        return HealthCheck(status="unknown", message="skipped in shallow mode")
    perception = getattr(service, "perception", None)
    if perception is None:
        return HealthCheck(
            status="degraded", message="screen perception service is not wired"
        )
    try:
        locators = perception.locator_availability()
    except Exception as exc:  # pragma: no cover - defensive
        return HealthCheck(
            status="error",
            message=f"perception availability probe failed: {type(exc).__name__}: {exc}",
        )
    details = {
        "enabled": bool(perception.enabled),
        "locators": [
            {"name": item.name, "available": item.available, "reason": item.reason}
            for item in locators
        ],
    }
    if not perception.enabled:
        return HealthCheck(
            status="degraded",
            message="screen perception is disabled by configuration",
            details=details,
        )
    if not locators:
        return HealthCheck(
            status="error", message="no perception locators are registered", details=details
        )
    available = [item.name for item in locators if item.available]
    if not available:
        return HealthCheck(
            status="error",
            message="no perception locator is available",
            details=details,
        )
    return HealthCheck(
        status="ok",
        message=f"{len(available)} of {len(locators)} perception locator(s) available",
        details={**details, "available": available},
    )


def _aggregate(checks: dict[str, HealthCheck]) -> str:
    statuses = {check.status for check in checks.values()}
    if "error" in statuses:
        return "unhealthy"
    if statuses & {"degraded", "unknown"}:
        return "degraded"
    return "ok"


_DEEP_CHECK_NAMES = ("desktop", "window_manager", "providers", "perception")


def _deep_checks_with_timeout(service: Any) -> dict[str, HealthCheck]:
    """Run the deep probes under the configured overall deadline.

    Driver probes talk to X11 and a misbehaving driver could block indefinitely;
    ``health.deep_timeout_ms`` bounds the *whole* deep probe set so ``GET
    /health`` stays responsive. A probe that misses the deadline is reported as
    ``degraded`` (with the pending probe names in ``details``) instead of
    blocking the request.
    """

    config = getattr(service, "config", None)
    health_settings = getattr(config, "health", None)
    timeout_ms = int(getattr(health_settings, "deep_timeout_ms", 5000) or 5000)
    timeout_s = max(0.001, timeout_ms / 1000.0)
    results: dict[str, HealthCheck] = {}

    def probe() -> None:
        results["desktop"] = _check_desktop(service, deep=True)
        results["window_manager"] = _check_window_manager(service, deep=True)
        results["providers"] = _check_providers(service, deep=True)
        results["perception"] = _check_perception(service, deep=True)

    worker = threading.Thread(
        target=probe,
        name="nextautodesk-health-deep",
        daemon=True,
    )
    started = time.monotonic()
    worker.start()
    worker.join(timeout_s)
    elapsed_ms = round((time.monotonic() - started) * 1000.0, 3)
    if worker.is_alive():
        completed = [name for name in _DEEP_CHECK_NAMES if name in results]
        pending = [name for name in _DEEP_CHECK_NAMES if name not in results]
        details: dict[str, Any] = {
            "timeout_ms": timeout_ms,
            "elapsed_ms": elapsed_ms,
            "timed_out": True,
            "completed": completed,
            "pending": pending,
        }
        for name in pending:
            results[name] = HealthCheck(
                status="degraded",
                message=f"deep probe timed out after {timeout_ms}ms",
                details=dict(details),
            )
    for name in _DEEP_CHECK_NAMES:
        results.setdefault(
            name,
            HealthCheck(
                status="degraded",
                message="deep probe did not complete",
                details={"timeout_ms": timeout_ms, "elapsed_ms": elapsed_ms},
            ),
        )
    return results


def build_health_report(service: Any, *, deep: bool = True, task_store: Any = None) -> HealthReport:
    """Build the aggregated health report for ``service``.

    Parameters
    ----------
    service:
        The :class:`~nextautodesk.service.DesktopService` (or a compatible fake).
    deep:
        When false, only in-process state is inspected: no X11/strategy probe is
        performed, which keeps ``GET /health?deep=false`` cheap.
    task_store:
        Optional :class:`~nextautodesk.tasks.store.TaskStore` used for the
        database check and task counts.
    """

    started = time.monotonic()
    checks: dict[str, HealthCheck] = {
        "api": HealthCheck(status="ok", message="HTTP API is serving requests"),
        "executor": _check_executor(service),
    }
    if deep:
        deep_checks = _deep_checks_with_timeout(service)
        checks["desktop"] = deep_checks["desktop"]
    else:
        # Deep probes are skipped entirely (not reported as "unknown") in shallow
        # mode so that a cheap ``/health?deep=false`` probe can still aggregate "ok".
        checks["desktop"] = _check_desktop(service, deep=False)
    checks["storage"] = _check_storage(service)
    if task_store is not None:
        checks["database"] = _check_database(task_store)
    if deep:
        checks["window_manager"] = deep_checks["window_manager"]
        checks["providers"] = deep_checks["providers"]
        checks["perception"] = deep_checks["perception"]
    disk = _disk_info(service)
    runtime: dict[str, Any] = {
        "version": __version__,
        "api_version": API_VERSION,
        "uptime_s": round(float(getattr(service, "uptime_s", 0.0)), 3),
        "memory": _memory_info(),
        "disk": disk,
        "cpu": _cpu_info(),
        "screenshots_count": _screenshot_count(service),
        "tasks": _task_counts(task_store),
        "checks_duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        "deep": deep,
    }
    return HealthReport(
        status=_aggregate(checks),  # type: ignore[arg-type]
        version=__version__,
        api_version=API_VERSION,
        uptime_s=runtime["uptime_s"],
        checks=checks,
        runtime=runtime,
    )


__all__ = ["build_health_report"]
