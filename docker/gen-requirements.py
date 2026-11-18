#!/usr/bin/env python3
"""Generate the runtime pip requirements file from ``pyproject.toml``.

The Docker image keeps PyPI dependency versions in a single place
(``pyproject.toml``) instead of duplicating them in the Dockerfile. This helper
reads the project dependencies (and, optionally, the strategy extras) and writes
a plain ``requirements.txt`` that pip can install before the application itself
is copied into the image.

Usage::

    gen-requirements.py <pyproject.toml> <output-requirements.txt> [true|false]

The last argument controls whether the optional strategy extras are included
(``WITH_OPTIONAL_STRATEGIES`` build argument); it defaults to ``true``.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

# Extras installed by default in the runtime image (WITH_OPTIONAL_STRATEGIES).
OPTIONAL_STRATEGIES: tuple[str, ...] = ("humanmove_mouse", "windmouse")

_TRUE = {"1", "true", "yes", "on"}


def _truthy(value: str) -> bool:
    return value.strip().lower() in _TRUE


def build_requirements(pyproject: Path, with_optional: bool) -> list[str]:
    with pyproject.open("rb") as handle:
        project = tomllib.load(handle)["project"]

    requirements: list[str] = list(project.get("dependencies", []))
    if with_optional:
        extras = project.get("optional-dependencies", {})
        for extra in OPTIONAL_STRATEGIES:
            if extra not in extras:
                raise SystemExit(
                    f"pyproject.toml has no optional-dependency group {extra!r} "
                    f"(expected one of: {', '.join(OPTIONAL_STRATEGIES)})"
                )
            requirements.extend(extras[extra])

    # De-duplicate while preserving order (an extra may repeat a base dependency).
    seen: set[str] = set()
    unique: list[str] = []
    for requirement in requirements:
        key = requirement.strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(
            f"usage: {Path(sys.argv[0]).name} <pyproject.toml> <output.txt> [true|false]",
            file=sys.stderr,
        )
        return 2

    pyproject = Path(argv[1])
    output = Path(argv[2])
    with_optional = _truthy(argv[3]) if len(argv) == 4 else True

    if not pyproject.is_file():
        print(f"error: {pyproject} does not exist", file=sys.stderr)
        return 2

    requirements = build_requirements(pyproject, with_optional)
    output.write_text("\n".join(requirements) + "\n", encoding="utf-8")

    print(
        f"wrote {len(requirements)} requirement(s) to {output} "
        f"(optional strategy extras: {'yes' if with_optional else 'no'})"
    )
    for requirement in requirements:
        print(f"  {requirement}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
