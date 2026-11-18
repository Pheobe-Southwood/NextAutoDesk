"""Configuration model and loader.

Precedence (lowest to highest):

1. Package default: ``src/nextautodesk/config/default.yml``.
2. Optional deployment config file (``--config`` / ``NEXT_AUTODESK_CONFIG``).
3. A small set of environment variable overrides (documented below).

Nested dictionaries are merged recursively; lists are replaced.

Environment overrides
---------------------
``NEXT_AUTODESK_CONFIG``      Path to the deployment YAML file.
``NEXT_AUTODESK_HOST``        ``server.host``
``NEXT_AUTODESK_PORT``        ``server.port``
``NEXT_AUTODESK_API_KEYS``    Comma-separated API keys (replaces ``auth.api_keys``).
``NEXT_AUTODESK_AUTH_ENABLED`` ``auth.enabled`` (true/false)
``NEXT_AUTODESK_LOG_LEVEL``   ``logging.level``
``NEXT_AUTODESK_DATA_DIR``    ``storage.data_dir``
``NEXT_AUTODESK_DISPLAY``     ``desktop.display``
``NEXT_AUTODESK_SCREEN_WIDTH``/``_HEIGHT``/``_DEPTH``  desktop resolution
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import ConfigError
from .operations import ALL_OPERATIONS, Operation

CONFIG_ENV_VAR = "NEXT_AUTODESK_CONFIG"
PACKAGE_CONFIG_PACKAGE = "nextautodesk"
PACKAGE_CONFIG_NAME = "config/default.yml"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)


class CorsSettings(_Base):
    enabled: bool = False
    allow_origins: list[str] = Field(default_factory=list)


class ServerSettings(_Base):
    host: str = "0.0.0.0"
    port: int = Field(default=8765, ge=1, le=65535)
    sync_timeout_ms: int = Field(default=300_000, ge=100, le=3_600_000)
    allow_strategy_override: bool = True
    max_request_body_bytes: int = Field(default=2_097_152, ge=1024)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    docs_url: str | None = "/docs"
    openapi_url: str | None = "/openapi.json"

    @field_validator("host")
    @classmethod
    def _valid_host(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("server.host must be a non-empty host/IP string")
        return value


class AuthSettings(_Base):
    enabled: bool = True
    header: str = "X-API-Key"
    api_keys: list[str] = Field(default_factory=list)
    protect_docs: bool = False

    @model_validator(mode="after")
    def _keys_required_when_enabled(self) -> AuthSettings:
        if self.enabled and not self.api_keys:
            raise ValueError(
                "auth.enabled=true requires at least one key in auth.api_keys "
                "(or the NEXT_AUTODESK_API_KEYS environment variable)"
            )
        return self


class DesktopSettings(_Base):
    display: str = ":99"
    width: int = Field(default=1920, ge=320, le=7680)
    height: int = Field(default=1080, ge=200, le=4320)
    depth: int = Field(default=24, ge=8, le=32)
    dpi: int = Field(default=96, ge=48, le=400)
    require_window_manager: bool = True
    startup_timeout_s: int = Field(default=60, ge=1, le=600)

    @field_validator("display")
    @classmethod
    def _valid_display(cls, value: str) -> str:
        # The runtime entrypoint only supports local X displays of the form ":N"
        # (one ``.`` suffix is stripped there, which would silently change the
        # display); reject anything else at configuration time.
        value = value.strip()
        if not re.fullmatch(r":[0-9]+", value):
            raise ValueError("desktop.display must look like ':99'")
        return value


class KeyboardDriverSettings(_Base):
    unicode_mode: Literal["auto", "xlib", "xdotool"] = "auto"


class ScreenDriverSettings(_Base):
    jpeg_quality: int = Field(default=85, ge=10, le=100)


class WindowDriverSettings(_Base):
    backend: Literal["xdotool", "wmctrl"] = "xdotool"
    command_timeout_ms: int = Field(default=5000, ge=100, le=60_000)


class DriverSettings(_Base):
    """Backend selection and tool settings; pacing lives in the strategy layer."""

    keyboard: KeyboardDriverSettings = Field(default_factory=KeyboardDriverSettings)
    screen: ScreenDriverSettings = Field(default_factory=ScreenDriverSettings)
    window: WindowDriverSettings = Field(default_factory=WindowDriverSettings)


class ProviderSettings(_Base):
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


_LOCATOR_NAME_PATTERN = re.compile(r"^[a-z0-9_-]+$")


class LocatorSettings(_Base):
    """Per-locator enable flag and free-form locator parameters."""

    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class PerceptionSettings(_Base):
    """Screen perception settings (hot-reloadable)."""

    enabled: bool = True
    max_results: int = Field(default=20, ge=1, le=100)
    locators: dict[str, LocatorSettings] = Field(default_factory=dict)

    @field_validator("locators")
    @classmethod
    def _validate_locator_names(
        cls, value: dict[str, LocatorSettings]
    ) -> dict[str, LocatorSettings]:
        for name in value:
            if not _LOCATOR_NAME_PATTERN.fullmatch(name):
                raise ValueError(
                    f"locator name {name!r} must match {_LOCATOR_NAME_PATTERN.pattern}"
                )
        return value


class OperationStrategyConfig(_Base):
    provider: str = "human_movement"
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _provider_name(cls, value: str) -> str:
        value = value.strip()
        if not value or not all(ch.isalnum() or ch in "_-" for ch in value):
            raise ValueError("provider names contain only letters, digits, '_' and '-'")
        return value


class StrategiesSettings(_Base):
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    operations: dict[str, OperationStrategyConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_operations(self) -> StrategiesSettings:
        expected = {op.value for op in ALL_OPERATIONS}
        missing = expected - set(self.operations)
        unknown = set(self.operations) - expected
        if missing:
            raise ValueError(f"strategies.operations is missing entries: {sorted(missing)}")
        if unknown:
            raise ValueError(f"strategies.operations has unknown entries: {sorted(unknown)}")
        for name, cfg in self.operations.items():
            if cfg.provider not in self.providers:
                raise ValueError(
                    f"strategies.operations.{name} references unknown provider "
                    f"{cfg.provider!r} (declare it under strategies.providers)"
                )
        return self

    def operation(self, operation: Operation) -> OperationStrategyConfig:
        return self.operations[operation.value]

    def provider_params(self, provider: str) -> dict[str, Any]:
        settings = self.providers.get(provider)
        return dict(settings.params) if settings else {}


class TaskSettings(_Base):
    max_queue_size: int = Field(default=256, ge=1, le=10_000)
    default_timeout_ms: int = Field(default=300_000, ge=100, le=7_200_000)
    step_timeout_ms: int = Field(default=120_000, ge=100, le=3_600_000)
    max_steps: int = Field(default=200, ge=1, le=2_000)
    retention_days: int = Field(default=7, ge=0, le=3650)
    max_records: int = Field(default=20_000, ge=100, le=1_000_000)
    shutdown_grace_s: int = Field(default=10, ge=0, le=120)


class StorageSettings(_Base):
    data_dir: str = "/data"
    screenshots_dir: str = "screenshots"
    database: str = "nextautodesk.db"
    logs_dir: str = "logs"
    max_screenshots: int = Field(default=2000, ge=1, le=1_000_000)
    screenshot_retention_days: int = Field(default=14, ge=0, le=3650)

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else Path(self.data_dir) / path

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def screenshots_path(self) -> Path:
        return self._resolve(self.screenshots_dir)

    @property
    def logs_path(self) -> Path:
        return self._resolve(self.logs_dir)

    @property
    def database_path(self) -> Path:
        return self._resolve(self.database)


class LoggingSettings(_Base):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["json", "console"] = "json"
    access_log: bool = True
    max_bytes: int = Field(default=10_485_760, ge=65_536, le=1_073_741_824)
    backup_count: int = Field(default=5, ge=1, le=50)


class HealthSettings(_Base):
    deep_timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    check_window_manager: bool = True
    check_disk: bool = True
    min_free_disk_mb: int = Field(default=256, ge=0, le=1_048_576)


class AppConfig(_Base):
    """Root configuration object."""

    _source_path: str | None = PrivateAttr(default=None)

    version: int = Field(default=1, ge=1)
    server: ServerSettings = Field(default_factory=ServerSettings)
    auth: AuthSettings = Field(default_factory=lambda: AuthSettings(enabled=False))
    desktop: DesktopSettings = Field(default_factory=DesktopSettings)
    driver: DriverSettings = Field(default_factory=DriverSettings)
    strategies: StrategiesSettings = Field(default_factory=StrategiesSettings)
    perception: PerceptionSettings = Field(default_factory=PerceptionSettings)
    tasks: TaskSettings = Field(default_factory=TaskSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)

    @model_validator(mode="after")
    def _storage_paths(self) -> AppConfig:
        # Validate that derived paths do not escape the data dir accidentally.
        data = self.storage.data_path
        for label, path in (
            ("screenshots_dir", self.storage.screenshots_path),
            ("logs_dir", self.storage.logs_path),
            ("database", self.storage.database_path),
        ):
            try:
                path.resolve().relative_to(data.resolve())
            except ValueError as exc:
                raise ValueError(f"storage.{label} must live under storage.data_dir") from exc
        return self

    # -- helpers -----------------------------------------------------------
    @property
    def source_path(self) -> str | None:
        """Path of the deployment overlay that produced this configuration, if any."""

        return self._source_path

    @source_path.setter
    def source_path(self, value: str | None) -> None:
        self._source_path = value

    def public_dict(self) -> dict[str, Any]:
        """Configuration with secrets removed (safe to expose over HTTP)."""

        payload = self.model_dump(mode="json")
        keys = payload.get("auth", {}).get("api_keys") or []
        payload["auth"]["api_keys"] = ["***" for _ in keys]
        return payload


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"Environment variable {name} must be a boolean, got {raw!r}")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def package_default_config_path() -> Path:
    """Absolute path to the bundled default configuration."""

    import importlib.resources

    resource = importlib.resources.files(PACKAGE_CONFIG_PACKAGE).joinpath(PACKAGE_CONFIG_NAME)
    with importlib.resources.as_file(resource) as path:
        return Path(path)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration file {path} must contain a YAML mapping at its root")
    return data


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(data)
    env = os.environ

    def _set(section: str, key: str, value: Any) -> None:
        data.setdefault(section, {})
        if not isinstance(data[section], dict):
            raise ConfigError(f"Configuration section {section!r} must be a mapping")
        data[section][key] = value

    if (raw := env.get("NEXT_AUTODESK_HOST")) is not None:
        _set("server", "host", raw.strip())
    if (raw := env.get("NEXT_AUTODESK_PORT")) is not None:
        try:
            _set("server", "port", int(raw))
        except ValueError as exc:
            raise ConfigError(f"NEXT_AUTODESK_PORT must be an integer, got {raw!r}") from exc
    if (raw := env.get("NEXT_AUTODESK_API_KEYS")) is not None:
        keys = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
        _set("auth", "api_keys", keys)
    if (value := _env_bool("NEXT_AUTODESK_AUTH_ENABLED")) is not None:
        _set("auth", "enabled", value)
    if (raw := env.get("NEXT_AUTODESK_LOG_LEVEL")) is not None:
        _set("logging", "level", raw.strip().upper())
    if (raw := env.get("NEXT_AUTODESK_DATA_DIR")) is not None:
        _set("storage", "data_dir", raw.strip())
    if (raw := env.get("NEXT_AUTODESK_DISPLAY")) is not None:
        _set("desktop", "display", raw.strip())
    for env_name, field in (
        ("NEXT_AUTODESK_SCREEN_WIDTH", "width"),
        ("NEXT_AUTODESK_SCREEN_HEIGHT", "height"),
        ("NEXT_AUTODESK_SCREEN_DEPTH", "depth"),
    ):
        if (raw := env.get(env_name)) is not None:
            try:
                _set("desktop", field, int(raw))
            except ValueError as exc:
                raise ConfigError(f"{env_name} must be an integer, got {raw!r}") from exc
    return data


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load, merge and validate the configuration.

    Raises:
        ConfigError: when a file is unreadable/invalid or the merged mapping does
            not satisfy :class:`AppConfig` validation.
    """

    default_path = package_default_config_path()
    merged = _load_yaml(default_path)

    explicit = config_path or os.environ.get(CONFIG_ENV_VAR)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"Configuration file not found: {path}")
        merged = _deep_merge(merged, _load_yaml(path))

    merged = _apply_env_overrides(merged)

    try:
        config = AppConfig.model_validate(merged)
        config.source_path = str(Path(explicit).expanduser()) if explicit else None
        return config
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"Invalid configuration: {details}") from exc


__all__ = [
    "CONFIG_ENV_VAR",
    "AppConfig",
    "AuthSettings",
    "ConfigError",
    "DesktopSettings",
    "DriverSettings",
    "HealthSettings",
    "LocatorSettings",
    "LoggingSettings",
    "PerceptionSettings",
    "ServerSettings",
    "StorageSettings",
    "StrategiesSettings",
    "TaskSettings",
    "load_config",
    "package_default_config_path",
]
