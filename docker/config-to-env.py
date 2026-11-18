#!/usr/bin/env python3
"""Translate the effective NextAutoDesk configuration (JSON on stdin) into shell
``export`` statements consumed by ``docker/entrypoint.sh``.

The entrypoint validates the merged configuration with
``python -m nextautodesk config validate --json`` and then evaluates this
script's output. That guarantees Xvfb, the XFCE session and the API all use the
same display/screen/data/server values even when only the YAML file (and not the
environment) customizes them.
"""

from __future__ import annotations

import json
import shlex
import sys


def main() -> int:
    try:
        config = json.load(sys.stdin)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        print(f"config-to-env: invalid JSON on stdin: {exc}", file=sys.stderr)
        return 2
    if not isinstance(config, dict):
        print("config-to-env: configuration JSON must be an object", file=sys.stderr)
        return 2

    desktop = config.get("desktop") or {}
    server = config.get("server") or {}
    storage = config.get("storage") or {}

    display = str(desktop.get("display") or ":99")
    try:
        width = int(desktop.get("width") or 1920)
        height = int(desktop.get("height") or 1080)
        depth = int(desktop.get("depth") or 24)
        dpi = int(desktop.get("dpi") or 96)
        port = int(server.get("port") or 8765)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        print(f"config-to-env: non-numeric desktop/server value: {exc}", file=sys.stderr)
        return 2

    display_number = display.lstrip(":").split(".")[0] or "99"

    values = {
        "NEXT_AUTODESK_DISPLAY": display,
        "NEXT_AUTODESK_SCREEN_WIDTH": str(width),
        "NEXT_AUTODESK_SCREEN_HEIGHT": str(height),
        "NEXT_AUTODESK_SCREEN_DEPTH": str(depth),
        "NEXT_AUTODESK_DATA_DIR": str(storage.get("data_dir") or "/data"),
        "NEXT_AUTODESK_HOST": str(server.get("host") or "0.0.0.0"),
        "NEXT_AUTODESK_PORT": str(port),
        "NAD_SCREEN_SIZE": f"{width}x{height}x{depth}",
        "NAD_DPI": str(dpi),
        "NAD_DISPLAY_NUMBER": display_number,
    }
    for name, value in values.items():
        print(f"export {name}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
