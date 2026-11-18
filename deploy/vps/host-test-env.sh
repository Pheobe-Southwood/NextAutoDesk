#!/usr/bin/env bash
# Prepare (or refresh) the Python test environment used to validate this
# repository *on the build/deployment host* (for example the reference VPS).
#
# The environment is created outside the repository (default /root/nad-test) and
# contains:
#   - a Python virtualenv with the runtime dependencies from pyproject.toml,
#     the dev tools (pytest, ruff, ...) and the optional strategy extras,
#   - the OS packages needed by the unit tests: Tesseract + English language
#     data (OCR tests really run), DejaVu fonts (OCR test fixtures) and the
#     C toolchain required to compile the optional ``evdev`` strategy extra.
#
# The project itself is NOT pip-installed: pytest is configured with
# ``pythonpath = ["src"]``, so a synced checkout is enough and the tests always
# exercise the working tree.
#
# Typical use on the VPS (root). Add --pip-index-url when direct PyPI access is
# slow on the host (an index mirror makes the install minutes faster):
#   ./deploy/vps/host-test-env.sh --root /root/nad-test --repo /root/nad-test/repo \
#       --pip-index-url https://<pypi-mirror>/simple
#   /root/nad-test/venv/bin/ruff check src tests
#   /root/nad-test/venv/bin/pytest tests/unit -q
#
# The script is idempotent and safe to re-run after pyproject.toml changes: it
# only installs missing packages and refreshes the dependency set.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

ROOT="/root/nad-test"
REPO="${REPO_ROOT}"
SKIP_APT=false
CHECK_ONLY=false
WITH_OPTIONAL_STRATEGIES=true
PIP_INDEX_URL="${PIP_INDEX_URL:-}"
APT_PACKAGES=(
    python3-venv
    python3-dev
    gcc
    tesseract-ocr
    tesseract-ocr-eng
    fonts-dejavu-core
    ca-certificates
)

usage() {
    awk 'NR > 1 && $0 == "set -euo pipefail" {exit} NR > 1 {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]}"
}

die() {
    printf 'host-test-env: error: %s\n' "$*" >&2
    exit 1
}

log() {
    printf 'host-test-env: %s\n' "$*"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)
            [[ $# -ge 2 ]] || die "--root requires a value"
            ROOT="$2"
            shift 2
            ;;
        --repo)
            [[ $# -ge 2 ]] || die "--repo requires a value"
            REPO="$2"
            shift 2
            ;;
        --skip-apt)
            SKIP_APT=true
            shift
            ;;
        --no-optional-strategies)
            WITH_OPTIONAL_STRATEGIES=false
            shift
            ;;
        --check)
            CHECK_ONLY=true
            shift
            ;;
        --pip-index-url)
            [[ $# -ge 2 ]] || die "--pip-index-url requires a value"
            PIP_INDEX_URL="$2"
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1 (try --help)"
            ;;
    esac
done

[[ -f "${REPO}/pyproject.toml" ]] || die "${REPO}/pyproject.toml not found (pass --repo <checkout>)"
[[ -f "${REPO}/docker/gen-requirements.py" ]] \
    || die "${REPO}/docker/gen-requirements.py not found; is --repo a full checkout?"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v apt-get >/dev/null 2>&1 || die "apt-get is required (Ubuntu/Debian host)"

if [[ "$(id -u)" -ne 0 && "${CHECK_ONLY}" != "true" ]]; then
    die "run as root (apt-get install and /root layout); use --check to inspect an existing environment"
fi

# ---------------------------------------------------------------------------
# Host safety: do not start a heavy pip install when the host is out of memory.
# The deployment container is usually running while this command is used; the
# test environment only needs a few hundred MB.
# ---------------------------------------------------------------------------
available_mb="$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)"
[[ -n "${available_mb}" ]] || available_mb=0
free_disk_mb="$(df -Pm / | awk 'NR==2 {print $4}')"
log "host: $(nproc) vCPU, ${available_mb} MB RAM available, ${free_disk_mb} MB free disk on /"
if ((available_mb < 250)); then
    die "only ${available_mb} MB RAM available; stop the deployment container or wait before installing"
elif ((available_mb < 500)); then
    log "warning: low available memory (${available_mb} MB); continuing carefully"
fi

# ---------------------------------------------------------------------------
# 1. OS packages
# ---------------------------------------------------------------------------
pkg_installed() {
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

missing=()
for package in "${APT_PACKAGES[@]}"; do
    pkg_installed "${package}" || missing+=("${package}")
done

if ((${#missing[@]} > 0)); then
    log "missing OS packages: ${missing[*]}"
    if [[ "${SKIP_APT}" == "true" ]]; then
        die "--skip-apt given but packages are missing: ${missing[*]}"
    fi
    if [[ "${CHECK_ONLY}" == "true" ]]; then
        die "--check: packages missing: ${missing[*]}"
    fi
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${missing[@]}"
else
    log "OS packages present (tesseract, DejaVu fonts, python3-venv/dev, gcc)"
fi

if ! command -v tesseract >/dev/null 2>&1; then
    die "tesseract binary is missing after apt install"
fi
if [[ ! -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf ]]; then
    die "DejaVuSans.ttf not found; OCR text fixtures would be skipped"
fi

# ---------------------------------------------------------------------------
# 2. Virtualenv
# ---------------------------------------------------------------------------
venv="${ROOT}/venv"
python_bin="${venv}/bin/python"
if [[ ! -x "${python_bin}" ]]; then
    if [[ "${CHECK_ONLY}" == "true" ]]; then
        die "--check: no virtualenv at ${venv}"
    fi
    log "creating virtualenv at ${venv}"
    mkdir -p "${ROOT}"
    python3 -m venv "${venv}"
fi

if [[ "${CHECK_ONLY}" != "true" ]]; then
    log "upgrading pip tooling"
    pip_upgrade_args=(--quiet --disable-pip-version-check)
    if [[ -n "${PIP_INDEX_URL}" ]]; then
        pip_upgrade_args+=(--index-url "${PIP_INDEX_URL}")
    fi
    "${python_bin}" -m pip install "${pip_upgrade_args[@]}" \
        --upgrade "pip==26.3" "setuptools==84.2.1" "wheel==0.48.0"
fi

# ---------------------------------------------------------------------------
# 3. Dependencies from pyproject.toml (single source of truth)
# ---------------------------------------------------------------------------
requirements="${ROOT}/requirements.txt"
if [[ "${CHECK_ONLY}" != "true" ]]; then
    log "generating ${requirements} from ${REPO}/pyproject.toml"
    "${python_bin}" - "${REPO}/pyproject.toml" "${requirements}" "${WITH_OPTIONAL_STRATEGIES}" <<'PY'
import sys
import tomllib
from pathlib import Path

pyproject, output, with_optional = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == "true"
with pyproject.open("rb") as handle:
    project = tomllib.load(handle)["project"]

extras = project.get("optional-dependencies", {})
requirements: list[str] = list(project.get("dependencies", []))
names = ["dev"]
if with_optional:
    names = ["dev", "humanmove_mouse", "windmouse"]
for extra in names:
    if extra not in extras:
        raise SystemExit(f"error: pyproject.toml has no optional-dependency group {extra!r}")
    requirements.extend(extras[extra])

seen: set[str] = set()
unique = [item for item in requirements if not (item in seen or seen.add(item))]
output.write_text("\n".join(unique) + "\n", encoding="utf-8")
print(f"  {len(unique)} requirement(s) -> {output}")
for item in unique:
    print(f"    {item}")
PY

    log "installing test dependencies into ${venv} (may take a few minutes)"
    pip_args=(--disable-pip-version-check --no-cache-dir)
    if [[ -n "${PIP_INDEX_URL}" ]]; then
        log "using pip index: ${PIP_INDEX_URL}"
        pip_args+=(--index-url "${PIP_INDEX_URL}")
    fi
    "${python_bin}" -m pip install "${pip_args[@]}" -r "${requirements}"
fi

# ---------------------------------------------------------------------------
# 4. Report
# ---------------------------------------------------------------------------
log "environment ready at ${ROOT}"
"${python_bin}" --version | sed 's/^/  /'
"${venv}/bin/ruff" --version 2>/dev/null | sed 's/^/  /' || true
"${python_bin}" -m pytest --version 2>/dev/null | sed 's/^/  /' || true
tesseract --version 2>/dev/null | head -n 1 | sed 's/^/  /'
"${python_bin}" - <<'PY' || true
import importlib
for module in ("cv2", "pytesseract", "PIL", "evdev", "humanmouse", "windmouse", "fastapi"):
    try:
        importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 - report only
        print(f"  optional module {module}: unavailable ({exc.__class__.__name__})")
    else:
        print(f"  optional module {module}: available")
PY
log "run from a synced checkout: ${venv}/bin/ruff check src tests && ${venv}/bin/pytest tests/unit -q"
