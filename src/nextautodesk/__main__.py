"""Command line entry point: ``python -m nextautodesk`` / ``nextautodesk``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config

LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextautodesk",
        description="NextAutoDesk headless GUI automation node",
    )
    parser.add_argument("--version", action="version", version=f"nextautodesk {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the HTTP API server (default)")
    serve.add_argument("--config", type=Path, default=None, help="path to the deployment YAML overlay")
    serve.add_argument("--host", default=None, help="override server.host")
    serve.add_argument("--port", type=int, default=None, help="override server.port")
    serve.add_argument("--log-level", default=None, help="override logging.level")

    check = sub.add_parser("config", help="configuration utilities")
    check_sub = check.add_subparsers(dest="config_command", required=True)
    validate = check_sub.add_parser("validate", help="validate the merged configuration and exit")
    validate.add_argument("--config", type=Path, default=None)
    validate.add_argument("--json", action="store_true", help="print the effective configuration")

    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    host = args.host or config.server.host
    port = int(args.port or config.server.port)
    if args.log_level:
        config.logging.level = args.log_level.upper()  # type: ignore[assignment]
    app = create_app(config)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,
        log_level=config.logging.level.lower(),
        access_log=False,  # structured access logging is handled by middleware
        timeout_graceful_shutdown=10,
    )
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    # Build the registry as well: it validates provider names, per-operation
    # parameters and extension discovery, so `config validate` catches the same
    # errors the runtime would hit on startup.
    try:
        from .perception.registry import LocatorRegistry
        from .strategies.registry import StrategyRegistry

        StrategyRegistry.build(config.strategies)
        # Locators are built as well so invalid perception parameters fail here
        # instead of at the first /elements call.
        LocatorRegistry.build(config.perception)
    except Exception as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(config.public_dict(), indent=2, ensure_ascii=False, default=str))
    else:
        print(f"configuration OK: version={config.version}")
        print(f"  server:    {config.server.host}:{config.server.port}")
        print(f"  desktop:   display={config.desktop.display} {config.desktop.width}x{config.desktop.height}x{config.desktop.depth}")
        print(f"  data:      {config.storage.data_dir}")
        print(f"  auth:      enabled={config.auth.enabled} keys={len(config.auth.api_keys)}")
        print(f"  providers: {', '.join(sorted(config.strategies.providers))}")
        for name in sorted(config.strategies.providers):
            params = ", ".join(sorted(config.strategies.provider_params(name)))
            print(f"    - {name}: {params or '(no params)'}")
        print(
            f"  perception: enabled={config.perception.enabled} "
            f"max_results={config.perception.max_results} "
            f"locators={', '.join(sorted(config.perception.locators))}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args([*(argv or sys.argv[1:]), "serve"])
    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "config":
        return _cmd_config(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
