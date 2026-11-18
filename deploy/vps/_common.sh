#!/usr/bin/env bash
# Shared helpers for the NextAutoDesk deployment scripts.
#
# Source this file from a script; never execute it directly.
#
#   # shellcheck source=deploy/vps/_common.sh
#   source "${SCRIPT_DIR}/_common.sh"
#
# The image tag is derived from ``pyproject.toml`` so the version lives in one
# place. Docker tags cannot contain the characters used by PEP 440 pre-release
# spellings, therefore ``1.2.3a1`` becomes ``1.2.3-alpha1`` (a -> alpha,
# b -> beta, rc -> rc). Everything else is kept as-is.
#
# Every function is pure: it reads a file and prints the result on stdout. No
# network, no Docker, no side effects.

# Print the PEP 440 project version from a pyproject.toml path.
nad_project_version() {
    local pyproject="${1:?nad_project_version: pyproject.toml path required}"
    python3 - "${pyproject}" <<'PY'
import sys
import tomllib
from pathlib import Path

path = Path(sys.argv[1])
try:
    with path.open("rb") as handle:
        project = tomllib.load(handle).get("project", {})
except OSError as exc:
    raise SystemExit(f"error: cannot read {path}: {exc}")
version = project.get("version")
if not isinstance(version, str) or not version.strip():
    raise SystemExit(f"error: {path} has no project.version string")
print(version.strip())
PY
}

# Convert a PEP 440 version string to the Docker tag style used by this project:
#   1.2.3a1 -> 1.2.3-alpha1   1.2.3b2 -> 1.2.3-beta2
#   1.2.3rc1 -> 1.2.3-rc1     1.2.3 -> 1.2.3
# A local version separator (``+``) becomes ``-``; other unsafe characters are
# replaced with ``-`` so the result is always a valid Docker tag.
nad_docker_tag() {
    local version="${1:?nad_docker_tag: version required}"
    python3 - "${version}" <<'PY'
import re
import sys

tag = sys.argv[1].strip().replace("+", "-")
tag = re.sub(
    r"(?<=\d)(a|b|rc)(\d+)$",
    lambda match: {"a": "-alpha", "b": "-beta", "rc": "-rc"}[match.group(1)]
    + match.group(2),
    tag,
)
tag = re.sub(r"[^A-Za-z0-9_.-]", "-", tag)
print(tag)
PY
}

# Print ``nextautodesk:<tag>`` for a repository root (reads its pyproject.toml).
nad_default_image() {
    local repo_root="${1:?nad_default_image: repo root required}"
    local pyproject="${repo_root}/pyproject.toml"
    [[ -f "${pyproject}" ]] || {
        printf 'error: %s not found\n' "${pyproject}" >&2
        return 1
    }
    printf 'nextautodesk:%s\n' "$(nad_docker_tag "$(nad_project_version "${pyproject}")")"
}

# Print only the tag for a repository root (reads its pyproject.toml).
nad_default_tag() {
    local image
    image="$(nad_default_image "${1:?nad_default_tag: repo root required}")" || return 1
    printf '%s\n' "${image#nextautodesk:}"
}
