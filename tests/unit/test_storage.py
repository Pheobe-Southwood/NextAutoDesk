"""Unit tests for storage layout and the screenshot store."""

from __future__ import annotations

import time

import pytest

from nextautodesk.config import load_config
from nextautodesk.errors import ScreenshotNotFound, StorageError
from nextautodesk.storage import DataStore


@pytest.fixture()
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXT_AUTODESK_API_KEYS", "k")
    monkeypatch.setenv("NEXT_AUTODESK_DATA_DIR", str(tmp_path / "data"))
    return load_config()


def test_directories_created(config):
    store = DataStore(config)
    store.ensure_directories()
    assert store.root.is_dir()
    assert store.screenshots_dir.is_dir()
    assert store.logs_dir.is_dir()
    assert store.writable() is True


def test_screenshot_roundtrip(config):
    store = DataStore(config)
    store.ensure_directories()
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    info = store.screenshots.save(png, fmt="png", width=1920, height=1080)
    assert info.name.endswith(".png")
    assert info.width == 1920
    assert store.screenshots.get(info.name)[0].read_bytes() == png
    listed = store.screenshots.list()
    assert [item.name for item in listed] == [info.name]
    store.screenshots.delete(info.name)
    assert store.screenshots.list() == []
    with pytest.raises(ScreenshotNotFound):
        store.screenshots.get(info.name)


def test_screenshot_name_traversal_rejected(config):
    store = DataStore(config)
    store.ensure_directories()
    for bad in ("../evil.png", "/etc/passwd", "..", "a/b.png", ""):
        with pytest.raises(StorageError):
            store.screenshots.path_for(bad)


def test_screenshot_custom_name_and_format(config):
    store = DataStore(config)
    store.ensure_directories()
    info = store.screenshots.save(b"jpegdata", fmt="jpeg", name="my-shot")
    assert info.name == "my-shot.jpg"
    assert info.format == "jpeg"


def test_screenshot_name_extension_must_match_format(config):
    store = DataStore(config)
    store.ensure_directories()
    for fmt, name in (
        ("jpeg", "shot.png"),
        ("png", "shot.jpg"),
        ("png", "shot.jpeg"),
        ("jpeg", "shot.txt"),
    ):
        with pytest.raises(StorageError):
            store.screenshots.save(b"data", fmt=fmt, name=name)
    # compatible explicit extensions are accepted unchanged
    assert store.screenshots.save(b"data", fmt="jpeg", name="shot.JPG").name == "shot.JPG"
    assert store.screenshots.save(b"data", fmt="png", name="shot.png").name == "shot.png"


def test_screenshot_retention_ignores_inflight_tmp_files(config):
    import os

    config.storage.max_screenshots = 1
    config.storage.screenshot_retention_days = 0
    store = DataStore(config)
    store.ensure_directories()
    saved = store.screenshots.save(b"keep", fmt="png")
    in_flight = store.screenshots.directory / "in-flight.png.tmp"
    in_flight.write_bytes(b"partial")
    old_time = time.time() - 60
    os.utime(in_flight, (old_time, old_time))
    latest = store.screenshots.save(b"new", fmt="png")  # triggers retention
    assert in_flight.exists()
    remaining = [item.name for item in store.screenshots.list()]
    # Regression: with max_screenshots=1 and coarse mtimes, a save used to be
    # able to delete the file it had just written and then fail its own stat.
    assert latest.name in remaining
    assert saved.name not in remaining


def test_screenshot_retention_by_count(config):
    config.storage.max_screenshots = 3
    config.storage.screenshot_retention_days = 0
    store = DataStore(config)
    store.ensure_directories()
    names = [store.screenshots.save(b"x", fmt="png").name for _ in range(6)]
    remaining = [item.name for item in store.screenshots.list()]
    assert len(remaining) <= 3
    assert names[-1] in remaining


def test_screenshot_retention_by_age(config):
    config.storage.max_screenshots = 100
    config.storage.screenshot_retention_days = 1
    store = DataStore(config)
    store.ensure_directories()
    old = store.screenshots.save(b"x", fmt="png")
    path = store.screenshots.path_for(old.name)
    old_time = time.time() - 5 * 86400
    import os

    os.utime(path, (old_time, old_time))
    store.screenshots.save(b"y", fmt="png")  # triggers retention
    assert old.name not in [item.name for item in store.screenshots.list()]
