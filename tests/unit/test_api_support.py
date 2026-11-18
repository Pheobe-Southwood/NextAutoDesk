"""Headless test support for the API/task unit tests (no X server required).

This module is imported by ``test_api_*.py`` / ``test_tasks_*.py``; it is named
``test_api_*`` so it lives in the owning agent's write scope and is not collected
as a test module (it defines no ``test_*`` functions).

It wires a **real** :class:`DesktopService`/:class:`SerialExecutor`/task store
around *fake* drivers, strategies and action handlers, which gives high-fidelity
coverage of the API layer while staying independent from the real PyAutoGUI
drivers (they are implemented by another module owner).
"""

from __future__ import annotations

import base64
import os
import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from nextautodesk.actions.base import ActionContext, ActionHandler
from nextautodesk.actions.registry import ActionRegistry
from nextautodesk.bootstrap import AppContext
from nextautodesk.config import (
    AppConfig,
    AuthSettings,
    HealthSettings,
    LoggingSettings,
    TaskSettings,
    load_config,
)
from nextautodesk.drivers.base import DriverBundle, DriverError
from nextautodesk.errors import TaskCancelled, WindowNotFound
from nextautodesk.execution import SerialExecutor
from nextautodesk.models import (
    Point,
    ProviderAvailability,
    Rect,
    ScreenshotFormat,
    Size,
    WindowInfo,
    WindowSelector,
)
from nextautodesk.operations import ALL_OPERATIONS
from nextautodesk.service import DesktopService
from nextautodesk.storage import DataStore
from nextautodesk.tasks.manager import TaskManager
from nextautodesk.tasks.store import TaskStore

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
API_KEY = "test-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}

#: 1x1 transparent PNG.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32

#: Mutating actions exposed by the fake engine (mirrors the real action names).
FAKE_ACTION_NAMES: tuple[str, ...] = (
    "mouse.move",
    "mouse.move_relative",
    "mouse.click",
    "mouse.down",
    "mouse.up",
    "mouse.hold",
    "mouse.drag",
    "mouse.scroll",
    "keyboard.type",
    "keyboard.press",
    "keyboard.hotkey",
    "keyboard.sequence",
    "keyboard.key_down",
    "keyboard.key_up",
    "keyboard.hold",
    "desktop.wait",
    "mouse.position",
    "mouse.state",
    "screen.info",
    "screen.screenshot",
    "window.list",
    "window.active",
    "window.get",
    "window.activate",
    "window.move",
    "window.resize",
    "window.minimize",
    "window.maximize",
    "window.restore",
    "window.close",
    # perception + composite tiers
    "element.find",
    "element.wait",
    "element.ocr",
    "interaction.click",
    "interaction.type",
)

#: API tier of each fake action (mirrors the real action catalogue).
ACTION_TIERS: dict[str, str] = {
    "element.find": "perception",
    "element.wait": "perception",
    "element.ocr": "perception",
    "interaction.click": "composite",
    "interaction.type": "composite",
}

READ_ONLY_ACTIONS = frozenset(
    {
        "element.find",
        "element.ocr",
        "element.wait",
        "mouse.position",
        "mouse.state",
        "screen.info",
        "screen.screenshot",
        "window.list",
        "window.active",
        "window.get",
    }
)

#: Two fake top-level windows used by the window routes.
def default_windows() -> list[WindowInfo]:
    return [
        WindowInfo(
            id="0x3a00007",
            title="Terminal - nad",
            class_name="Xfce4-terminal",
            instance_name="xfce4-terminal",
            pid=1234,
            desktop=0,
            geometry=Rect(x=40, y=60, width=800, height=600),
            state="normal",
            active=True,
        ),
        WindowInfo(
            id="0x3a00008",
            title="Editor",
            class_name="Mousepad",
            instance_name="mousepad",
            pid=1235,
            desktop=0,
            geometry=Rect(x=200, y=120, width=640, height=480),
            state="normal",
            active=False,
        ),
    ]


# ---------------------------------------------------------------------------
# fake drivers
# ---------------------------------------------------------------------------
class FakeMouse:
    def __init__(self) -> None:
        self.point = Point(x=0, y=0)
        self.calls: list[tuple[Any, ...]] = []
        self.buttons: set[str] = set()
        self.fail = False

    def position(self) -> Point:
        if self.fail:
            raise DriverError("mouse probe failed", driver="fake")
        return self.point

    def move_to(self, x: float, y: float, duration_s: float = 0.0) -> None:
        self.calls.append(("move_to", x, y, duration_s))
        self.point = Point(x=x, y=y)

    def button_down(self, button: str) -> None:
        self.calls.append(("button_down", button))
        self.buttons.add(button)

    def button_up(self, button: str) -> None:
        self.calls.append(("button_up", button))
        self.buttons.discard(button)

    def scroll(self, dx: int, dy: int) -> None:
        self.calls.append(("scroll", dx, dy))

    def buttons_down(self) -> set[str]:
        return set(self.buttons)


class FakeKeyboard:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.held: set[str] = set()

    def key_down(self, key: str) -> None:
        self.calls.append(("key_down", key))
        self.held.add(key)

    def key_up(self, key: str) -> None:
        self.calls.append(("key_up", key))
        self.held.discard(key)

    def type_text(self, text: str) -> None:
        self.calls.append(("type_text", text))

    def char_down(self, char: str) -> None:
        self.calls.append(("char_down", char))

    def char_up(self, char: str) -> None:
        self.calls.append(("char_up", char))

    def supports_unicode(self) -> bool:
        return True


class FakeScreen:
    def __init__(self, size: Size | None = None) -> None:
        self._size = size or Size(width=1280, height=720)
        self.calls: list[tuple[Any, ...]] = []
        self.fail = False

    def size(self) -> Size:
        if self.fail:
            raise DriverError("screen probe failed", driver="fake")
        return self._size

    def screenshot(self, region: Rect | None = None, fmt: ScreenshotFormat = "png") -> bytes:
        self.calls.append(("screenshot", region, fmt))
        return PNG_BYTES if fmt == "png" else JPEG_BYTES


class FakeWindow:
    def __init__(self) -> None:
        self.windows = default_windows()
        self.calls: list[tuple[Any, ...]] = []

    # -- queries -----------------------------------------------------------
    def list_windows(self) -> list[WindowInfo]:
        self.calls.append(("list_windows",))
        return [window.model_copy(deep=True) for window in self.windows]

    def active_window(self) -> WindowInfo | None:
        self.calls.append(("active_window",))
        for window in self.windows:
            if window.active:
                return window.model_copy(deep=True)
        return None

    def _find(self, selector: WindowSelector) -> WindowInfo:
        for window in self.windows:
            if selector.id is not None and window.id.lower() == selector.id.lower():
                return window
            if selector.active and window.active:
                return window
            if selector.title is not None:
                if selector.title_regex:
                    if re.search(selector.title, window.title):
                        return window
                elif window.title == selector.title:
                    return window
            if selector.class_name is not None and window.class_name == selector.class_name:
                return window
            if (
                selector.instance_name is not None
                and window.instance_name == selector.instance_name
            ):
                return window
        raise WindowNotFound(
            "window not found",
            details={"selector": selector.model_dump(exclude_none=True)},
        )

    def get_window(self, selector: WindowSelector) -> WindowInfo:
        self.calls.append(("get_window", selector.model_dump(exclude_none=True)))
        return self._find(selector).model_copy(deep=True)

    # -- mutations ---------------------------------------------------------
    def activate(self, window: WindowInfo) -> None:
        self.calls.append(("activate", window.id))
        for item in self.windows:
            item.active = item.id == window.id

    def move(self, window: WindowInfo, x: int, y: int) -> None:
        self.calls.append(("move", window.id, x, y))
        self._find(WindowSelector(id=window.id)).geometry.x = x
        self._find(WindowSelector(id=window.id)).geometry.y = y

    def resize(self, window: WindowInfo, width: int, height: int) -> None:
        self.calls.append(("resize", window.id, width, height))
        self._find(WindowSelector(id=window.id)).geometry.width = width
        self._find(WindowSelector(id=window.id)).geometry.height = height

    def move_resize(self, window: WindowInfo, x: int, y: int, width: int, height: int) -> None:
        self.calls.append(("move_resize", window.id, x, y, width, height))

    def minimize(self, window: WindowInfo) -> None:
        self.calls.append(("minimize", window.id))
        self._find(WindowSelector(id=window.id)).state = "minimized"

    def maximize(self, window: WindowInfo) -> None:
        self.calls.append(("maximize", window.id))
        self._find(WindowSelector(id=window.id)).state = "maximized"

    def restore(self, window: WindowInfo) -> None:
        self.calls.append(("restore", window.id))
        self._find(WindowSelector(id=window.id)).state = "normal"

    def close(self, window: WindowInfo) -> None:
        self.calls.append(("close", window.id))

    def window_manager_name(self) -> str | None:
        return "xfwm4"


# ---------------------------------------------------------------------------
# fake strategies
# ---------------------------------------------------------------------------
class FakeStrategies:
    """Minimal stand-in for :class:`StrategyRegistry` (availability + views)."""

    def __init__(self, provider_names: tuple[str, ...] = ("human_movement", "direct")) -> None:
        self.provider_names = provider_names
        self.unavailable: dict[str, str] = {}

    def providers(self) -> dict[str, Any]:
        return {
            name: SimpleNamespace(
                name=name, supported=frozenset(ALL_OPERATIONS), version="0.0.0"
            )
            for name in self.provider_names
        }

    def provider_availability(self) -> list[ProviderAvailability]:
        return [
            ProviderAvailability(
                name=name,
                available=name not in self.unavailable,
                version="0.0.0",
                description="fake provider",
                supported_operations=list(ALL_OPERATIONS),
                reason=self.unavailable.get(name),
            )
            for name in sorted(self.provider_names)
        ]

    def operation_views(self) -> dict[str, Any]:
        from nextautodesk.models import OperationStrategyView

        return {
            operation.value: OperationStrategyView(
                operation=operation,
                provider="human_movement",
                enabled=True,
                available=True,
                params={},
                candidates=["human_movement"],
            )
            for operation in ALL_OPERATIONS
        }

    def resolve(self, operation: Any, override: str | None = None) -> Any:
        provider = override or "human_movement"
        return SimpleNamespace(provider=SimpleNamespace(name=provider), params={})


# ---------------------------------------------------------------------------
# fake action handlers
# ---------------------------------------------------------------------------
class _FakeParams(BaseModel):
    model_config = ConfigDict(extra="allow")


@dataclass
class ActionCall:
    name: str
    params: dict[str, Any]


class ActionRecorder:
    """Thread-safe record of action invocations (for assertions)."""

    def __init__(self) -> None:
        self.calls: list[ActionCall] = []
        self.events: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def record(self, name: str, params: Mapping[str, Any]) -> None:
        with self._lock:
            self.calls.append(ActionCall(name=name, params=dict(params)))

    def event(self, kind: str, name: str) -> None:
        with self._lock:
            self.events.append((kind, name))

    def names(self) -> list[str]:
        with self._lock:
            return [call.name for call in self.calls]

    def params_for(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            return [call.params for call in self.calls if call.name == name]

    def reset(self) -> None:
        with self._lock:
            self.calls.clear()
            self.events.clear()


class FakeActionHandler(ActionHandler):
    """Configurable action handler used by the fake engine."""

    name = "fake.action"
    summary = "fake action for tests"
    category = "test"
    tier = "primitive"
    mutating = True
    params_model = _FakeParams

    def __init__(
        self,
        name: str,
        recorder: ActionRecorder,
        *,
        mutating: bool = True,
        tier: str = "primitive",
        delay_s: float = 0.0,
        error: BaseException | None = None,
        output: Mapping[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.recorder = recorder
        self.mutating = mutating
        self.tier = tier
        self.delay_s = delay_s
        self.error = error
        self.output = dict(output) if output is not None else None

    def execute(self, params: _FakeParams, ctx: ActionContext) -> dict[str, Any]:
        self.recorder.record(self.name, params.model_dump())
        self.recorder.event("started", self.name)
        try:
            if self.delay_s:
                deadline = time.monotonic() + self.delay_s
                while time.monotonic() < deadline:
                    if ctx.cancel.is_set():
                        raise TaskCancelled(f"{self.name} cancelled")
                    time.sleep(0.005)
            if ctx.cancel.is_set():
                raise TaskCancelled(f"{self.name} cancelled")
            if self.error is not None:
                raise self.error
            if self.output is not None:
                return dict(self.output)
            return {"action": self.name, "params": params.model_dump(exclude_none=True)}
        finally:
            self.recorder.event("finished", self.name)


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------
@dataclass
class AppEnv:
    config: AppConfig
    service: DesktopService
    drivers: DriverBundle
    strategies: FakeStrategies
    recorder: ActionRecorder
    actions: ActionRegistry
    task_store: TaskStore
    manager: TaskManager
    context: AppContext
    handlers: dict[str, FakeActionHandler] = field(default_factory=dict)

    def handler(self, name: str) -> FakeActionHandler:
        return self.handlers[name]

    def close(self) -> None:
        try:
            self.task_store.close()
        finally:
            pass


def make_config(
    tmp_path: Path,
    *,
    auth_enabled: bool = True,
    api_keys: tuple[str, ...] = (API_KEY,),
    protect_docs: bool = False,
    max_body_bytes: int = 2_097_152,
    docs: bool = True,
    access_log: bool = True,
    log_format: str = "console",
    step_timeout_ms: int = 2000,
    default_timeout_ms: int = 5000,
    max_steps: int = 50,
    max_queue_size: int = 16,
    shutdown_grace_s: float = 1,
    min_free_disk_mb: int = 0,
    check_window_manager: bool = True,
    check_disk: bool = True,
) -> AppConfig:
    """Build a valid :class:`AppConfig` rooted in ``tmp_path``."""

    # The bundled default config intentionally ships no API keys; supply one
    # through the documented environment override so loading succeeds.
    inherited = os.environ.get("NEXT_AUTODESK_API_KEYS")
    bootstrap_keys = api_keys or ("placeholder-key",)
    os.environ["NEXT_AUTODESK_API_KEYS"] = ",".join(bootstrap_keys)
    try:
        base = load_config(None)
    finally:
        if inherited is None:
            os.environ.pop("NEXT_AUTODESK_API_KEYS", None)
        else:
            os.environ["NEXT_AUTODESK_API_KEYS"] = inherited
    data_dir = tmp_path / "data"
    storage = base.storage.model_copy(update={"data_dir": str(data_dir)})
    server = base.server.model_copy(
        update={
            "max_request_body_bytes": max_body_bytes,
            "docs_url": "/docs" if docs else None,
            "openapi_url": "/openapi.json" if docs else None,
        }
    )
    auth = AuthSettings(
        enabled=auth_enabled,
        header="X-API-Key",
        api_keys=list(api_keys),
        protect_docs=protect_docs,
    )
    desktop = base.desktop.model_copy(update={"width": 1280, "height": 720})
    tasks = TaskSettings(
        max_queue_size=max_queue_size,
        default_timeout_ms=default_timeout_ms,
        step_timeout_ms=step_timeout_ms,
        max_steps=max_steps,
        shutdown_grace_s=int(shutdown_grace_s),
        retention_days=7,
        max_records=1000,
    )
    health = HealthSettings(
        deep_timeout_ms=2000,
        check_window_manager=check_window_manager,
        check_disk=check_disk,
        min_free_disk_mb=min_free_disk_mb,
    )
    logging = LoggingSettings(
        level="WARNING", format=log_format if log_format in {"json", "console"} else "console",
        access_log=access_log,
    )
    return base.model_copy(
        update={
            "server": server,
            "auth": auth,
            "desktop": desktop,
            "storage": storage,
            "tasks": tasks,
            "health": health,
            "logging": logging,
        }
    )


def build_fake_service(config: AppConfig) -> tuple[DesktopService, DriverBundle, ActionRecorder]:
    store = DataStore(config)
    store.ensure_directories()
    drivers = DriverBundle(
        mouse=FakeMouse(),
        keyboard=FakeKeyboard(),
        screen=FakeScreen(Size(width=config.desktop.width, height=config.desktop.height)),
        window=FakeWindow(),
    )
    strategies = FakeStrategies()
    recorder = ActionRecorder()
    actions = ActionRegistry(
        [
            FakeActionHandler(
                name,
                recorder,
                mutating=name not in READ_ONLY_ACTIONS,
                tier=ACTION_TIERS.get(name, "primitive"),
            )
            for name in FAKE_ACTION_NAMES
        ]
    )
    executor = SerialExecutor(
        max_queue_size=config.tasks.max_queue_size,
        name="test",
    )
    service = DesktopService(
        config,
        store=store,
        drivers=drivers,
        strategies=strategies,  # type: ignore[arg-type]
        actions=actions,
        executor=executor,
    )
    return service, drivers, recorder


def make_env(tmp_path: Path, **config_kwargs: Any) -> AppEnv:
    """Build the complete runtime (service + task manager + context) with fakes."""

    config = make_config(tmp_path, **config_kwargs)
    service, drivers, recorder = build_fake_service(config)
    task_store = TaskStore(config)
    task_store.initialize()
    manager = TaskManager(service=service, store=task_store, config=config)
    context = AppContext(config=config, service=service, task_manager=manager)
    handlers = {
        name: service.actions.get(name)  # type: ignore[assignment]
        for name in FAKE_ACTION_NAMES
    }
    return AppEnv(
        config=config,
        service=service,
        drivers=drivers,
        strategies=service.strategies,  # type: ignore[arg-type]
        recorder=recorder,
        actions=service.actions,
        task_store=task_store,
        manager=manager,
        context=context,
        handlers=handlers,  # type: ignore[arg-type]
    )


@contextmanager
def serving(tmp_path: Path, **config_kwargs: Any) -> Iterator[tuple[AppEnv, TestClient]]:
    """Create an app + TestClient with lifespan managed (service/task manager started)."""

    from nextautodesk.api.app import create_app

    env = make_env(tmp_path, **config_kwargs)
    app = create_app(env.config, context=env.context)
    client = TestClient(app)
    try:
        with client:
            yield env, client
    finally:
        env.close()


@contextmanager
def running(tmp_path: Path, **config_kwargs: Any) -> Iterator[AppEnv]:
    """Start only the runtime (service + task manager) without an HTTP client."""

    env = make_env(tmp_path, **config_kwargs)
    env.service.start()
    try:
        env.manager.recover()
        env.manager.start()
        yield env
    finally:
        env.manager.shutdown()
        env.service.shutdown(grace_s=0.5)
        env.close()


def http_headers(env: AppEnv) -> dict[str, str]:
    """Valid API-key headers for ``env``."""

    keys = env.config.auth.api_keys or [API_KEY]
    return {"X-API-Key": keys[0]}


__all__ = [
    "ACTION_TIERS",
    "API_KEY",
    "AUTH_HEADERS",
    "JPEG_BYTES",
    "PNG_BYTES",
    "ActionCall",
    "ActionRecorder",
    "AppEnv",
    "FakeKeyboard",
    "FakeMouse",
    "FakeScreen",
    "FakeStrategies",
    "FakeWindow",
    "build_fake_service",
    "default_windows",
    "http_headers",
    "make_config",
    "make_env",
    "running",
    "serving",
]
