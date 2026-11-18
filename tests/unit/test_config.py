"""Unit tests for configuration loading, merging and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from nextautodesk.config import AppConfig, load_config
from nextautodesk.errors import ConfigError


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_default_config_loads_with_env_key(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "key-one,key-two")
    monkeypatch.setenv("NEXT_AUTODESK_DATA_DIR", str(tmp_path))
    config = load_config()
    assert config.version == 1
    assert config.auth.enabled is True
    assert config.auth.api_keys == ["key-one", "key-two"]
    assert config.storage.data_dir == str(tmp_path)
    # every operation has an independent strategy entry
    assert len(config.strategies.operations) == 19
    for operation, entry in config.strategies.operations.items():
        assert entry.provider in config.strategies.providers, operation
    # perception ships enabled with both built-in locators configured
    assert config.perception.enabled is True
    assert config.perception.max_results == 20
    assert set(config.perception.locators) == {"template", "text"}
    template_params = config.perception.locators["template"].params
    assert template_params["threshold"] == 0.8
    assert template_params["scales"] == [1.0]
    text_params = config.perception.locators["text"].params
    assert text_params["languages"] == ["eng", "chi_sim"]
    assert text_params["psm"] == 6


def test_auth_enabled_without_keys_is_rejected(monkeypatch):
    monkeypatch.delenv("NEXT_AUTODESK_API_KEYS", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    assert "auth.enabled" in str(excinfo.value)


def test_overlay_merge_and_source_path(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    overlay = _write(
        tmp_path / "overlay.yml",
        """
server:
  port: 9123
strategies:
  operations:
    mouse_move:
      provider: direct
      params: {foo: 1}
""",
    )
    config = load_config(overlay)
    assert config.server.port == 9123
    # untouched defaults survive the deep merge
    assert config.auth.enabled is True
    assert config.strategies.operations["mouse_move"].provider == "direct"
    assert config.strategies.operations["mouse_move"].params == {"foo": 1}
    assert config.strategies.operations["keyboard_type_text"].provider == "human_movement"
    assert config.source_path == str(overlay)


def test_unknown_provider_reference_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    overlay = _write(
        tmp_path / "bad.yml",
        "strategies:\n  operations:\n    mouse_move:\n      provider: does_not_exist\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(overlay)
    assert "unknown provider" in str(excinfo.value)


def test_missing_operation_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    overlay = _write(
        tmp_path / "missing.yml",
        "strategies:\n  operations:\n    mouse_move:\n      provider: direct\n",
    )
    config = load_config(overlay)
    # missing entries come from the default file, so this must NOT remove other entries
    assert len(config.strategies.operations) == 19


def test_invalid_unknown_key_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    overlay = _write(tmp_path / "extra.yml", "server:\n  nonsense: true\n")
    with pytest.raises(ConfigError):
        load_config(overlay)


def test_storage_escape_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    overlay = _write(
        tmp_path / "escape.yml",
        f"storage:\n  data_dir: {tmp_path / 'data'}\n  screenshots_dir: /etc\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(overlay)
    assert "storage.screenshots_dir" in str(excinfo.value)


def test_env_overrides_take_precedence(monkeypatch):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "env-key")
    monkeypatch.setenv("NEXT_AUTODESK_HOST", "127.0.0.1")
    monkeypatch.setenv("NEXT_AUTODESK_PORT", "7000")
    monkeypatch.setenv("NEXT_AUTODESK_LOG_LEVEL", "debug")
    monkeypatch.setenv("NEXT_AUTODESK_AUTH_ENABLED", "true")
    monkeypatch.setenv("NEXT_AUTODESK_DISPLAY", ":77")
    monkeypatch.setenv("NEXT_AUTODESK_SCREEN_WIDTH", "1280")
    monkeypatch.setenv("NEXT_AUTODESK_SCREEN_HEIGHT", "720")
    config = load_config()
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 7000
    assert config.logging.level == "DEBUG"
    assert config.desktop.display == ":77"
    assert (config.desktop.width, config.desktop.height) == (1280, 720)


def test_invalid_env_bool_rejected(monkeypatch):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    monkeypatch.setenv("NEXT_AUTODESK_AUTH_ENABLED", "maybe")
    with pytest.raises(ConfigError):
        load_config()


def test_public_dict_masks_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "secret-a,secret-b")
    monkeypatch.setenv("NEXT_AUTODESK_DATA_DIR", str(tmp_path))
    config = load_config()
    public = config.public_dict()
    assert public["auth"]["api_keys"] == ["***", "***"]
    assert "secret-a" not in str(public)


def test_appconfig_rejects_unknown_fields():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"version": 1, "wat": 1})


def test_perception_overlay_merge(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    overlay = _write(
        tmp_path / "perception.yml",
        """
perception:
  max_results: 5
  locators:
    template:
      enabled: false
      params:
        threshold: 0.95
        scales: [0.75, 1.0]
""",
    )
    config = load_config(overlay)
    assert config.perception.max_results == 5
    template = config.perception.locators["template"]
    assert template.enabled is False
    assert template.params["threshold"] == 0.95
    assert template.params["scales"] == [0.75, 1.0]
    # untouched locator defaults survive the deep merge
    assert config.perception.locators["text"].params["languages"] == ["eng", "chi_sim"]


def test_invalid_perception_locator_name_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    overlay = _write(
        tmp_path / "bad-locator.yml",
        "perception:\n  locators:\n    NotAValidName:\n      enabled: true\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(overlay)
    assert "locator name" in str(excinfo.value)


def test_invalid_perception_max_results_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    overlay = _write(tmp_path / "bad-limit.yml", "perception:\n  max_results: 0\n")
    with pytest.raises(ConfigError):
        load_config(overlay)


def test_config_validate_cli_checks_perception_locators(monkeypatch, tmp_path, capsys):
    from nextautodesk.__main__ import main

    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    monkeypatch.setenv("NEXT_AUTODESK_DATA_DIR", str(tmp_path / "data"))
    assert main(["config", "validate"]) == 0
    output = capsys.readouterr().out
    assert "perception:" in output
    assert "template" in output and "text" in output

    overlay = _write(
        tmp_path / "bad-params.yml",
        """
perception:
  locators:
    template:
      params:
        threshold: 2.0
""",
    )
    assert main(["config", "validate", "--config", str(overlay)]) == 2
    assert "configuration error" in capsys.readouterr().err
