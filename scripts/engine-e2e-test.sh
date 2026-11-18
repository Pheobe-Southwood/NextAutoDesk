#!/usr/bin/env bash
# Run the core-engine real-X11 integration suite against the final Docker image.
#
# The suite (tests/unit/test_engine_integration.py) exercises the drivers and the
# human-movement strategy provider against a live Xvfb + XFCE + xfwm4 desktop:
# screen size/screenshot, mouse move/click/scroll, Unicode typing into a real
# xfce4-terminal, window list/activate/move/resize/minimize/restore/close.
#
# The runtime image intentionally does not ship pytest, so this script installs
# pytest into a throw-away directory inside the container (never into the image),
# mounts the repository read-only, and reports the raw pytest output.
#
# Usage:
#   ./scripts/engine-e2e-test.sh [--image TAG] [--port PORT] [--keep] [--timeout SECONDS]
#   (default image: nextautodesk:<version from pyproject.toml>)
#
# Exit codes: 0 = suite passed, 1 = suite failed, 2 = setup/usage error.

set -euo pipefail

readonly DOCKER="${DOCKER:-docker}"
readonly REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=deploy/vps/_common.sh
source "${REPO_ROOT}/deploy/vps/_common.sh"

IMAGE="${IMAGE:-$(nad_default_image "${REPO_ROOT}")}"
NAME="${NAME:-nad-engine-e2e-$$}"
HOST_PORT="${HOST_PORT:-18766}"
STARTUP_TIMEOUT="${STARTUP_TIMEOUT:-180}"
KEEP=false
API_KEY="${NEXT_AUTODESK_API_KEYS:-test-key}"

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed -e 's/^# \{0,1\}//'
}

die() {
    printf 'engine-e2e-test: error: %s\n' "$*" >&2
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --image)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            IMAGE="$2"
            shift 2
            ;;
        --port)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            HOST_PORT="$2"
            shift 2
            ;;
        --timeout)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            STARTUP_TIMEOUT="$2"
            shift 2
            ;;
        --keep)
            KEEP=true
            shift
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

"${DOCKER}" image inspect "${IMAGE}" >/dev/null 2>&1 \
    || die "image '${IMAGE}' not found; build it with ./scripts/build-image.sh"

[[ -f "${REPO_ROOT}/tests/unit/test_engine_integration.py" ]] \
    || die "tests/unit/test_engine_integration.py not found under ${REPO_ROOT}"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/nad-engine-e2e.XXXXXX")"
DATA_DIR="${WORK_DIR}/data"
mkdir -p "${DATA_DIR}"

# The repository checkout may contain files with restrictive host modes; the
# container runs as the non-root "nad" uid, so stage a readable copy of just the
# test tree + pytest config and mount that instead of the live checkout.
STAGE_DIR="${WORK_DIR}/repo"
mkdir -p "${STAGE_DIR}"
cp -a "${REPO_ROOT}/pyproject.toml" "${STAGE_DIR}/"
cp -a "${REPO_ROOT}/tests" "${STAGE_DIR}/tests"
chmod -R a+rX "${STAGE_DIR}"

if [[ "$(id -u)" -eq 0 ]]; then
    chown -R 1000:1000 "${DATA_DIR}"
else
    chmod 0777 "${DATA_DIR}"
fi

BASE_URL="http://127.0.0.1:${HOST_PORT}"
PASSED=false

cleanup() {
    local status=$?
    if [[ "${PASSED}" != "true" || "${KEEP}" == "true" ]]; then
        printf '\nengine-e2e-test: container kept for debugging: %s\n' "${NAME}" >&2
        printf '  docker logs %s\n  docker exec -it %s bash\n' "${NAME}" "${NAME}" >&2
        printf '  data dir: %s\n' "${DATA_DIR}" >&2
        return
    fi
    "${DOCKER}" rm -f "${NAME}" >/dev/null 2>&1 || true
    rm -rf "${WORK_DIR}"
    return "${status}"
}
trap cleanup EXIT

echo "engine-e2e-test: image=${IMAGE} container=${NAME} repo=${REPO_ROOT}"

# --- start -----------------------------------------------------------------
if "${DOCKER}" inspect "${NAME}" >/dev/null 2>&1; then
    die "a container named '${NAME}' already exists"
fi

"${DOCKER}" run -d --name "${NAME}" \
    --hostname nextautodesk \
    --shm-size=256m \
    --memory=2g --memory-swap=2g \
    --pids-limit=512 \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --stop-timeout=45 \
    -e "NEXT_AUTODESK_API_KEYS=${API_KEY}" \
    -e "NEXT_AUTODESK_LOG_LEVEL=INFO" \
    -p "127.0.0.1:${HOST_PORT}:8765" \
    -v "${DATA_DIR}:/data" \
    -v "${STAGE_DIR}:/repo:ro" \
    "${IMAGE}" >/dev/null

started_at="$(date +%s)"
echo "engine-e2e-test: waiting for /healthz (timeout ${STARTUP_TIMEOUT}s)"
while true; do
    if ! "${DOCKER}" inspect -f '{{.State.Running}}' "${NAME}" 2>/dev/null | grep -qx 'true'; then
        echo "engine-e2e-test: container exited before becoming healthy" >&2
        "${DOCKER}" logs --tail 120 "${NAME}" >&2 || true
        exit 1
    fi
    if [[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "${BASE_URL}/healthz" 2>/dev/null || true)" == "200" ]]; then
        break
    fi
    if (( $(date +%s) - started_at > STARTUP_TIMEOUT )); then
        echo "engine-e2e-test: API not healthy after ${STARTUP_TIMEOUT}s" >&2
        "${DOCKER}" logs --tail 120 "${NAME}" >&2 || true
        "${DOCKER}" exec "${NAME}" supervisorctl -c /opt/nad/supervisord.conf status >&2 2>&1 || true
        exit 1
    fi
    sleep 0.5
done
echo "engine-e2e-test: API healthy after $(( $(date +%s) - started_at ))s"

# --- evidence: desktop + Xlib ----------------------------------------------
echo
echo "== desktop =="
"${DOCKER}" exec "${NAME}" sh -c \
    'xdpyinfo | grep -E "dimensions|depth of root" | head -2; wmctrl -m | head -2' 2>/dev/null \
    | sed 's/^/  | /' || true
echo
echo "== Xlib version =="
"${DOCKER}" exec "${NAME}" /opt/venv/bin/python -c \
    'import Xlib; print("Xlib.__version__ =", Xlib.__version__); print("Xlib package:", Xlib.__file__)' 2>&1 \
    | sed 's/^/  | /' || true

# --- install pytest into a throw-away in-container directory ---------------
echo
echo "== install pytest into the disposable container (image stays runtime-only) =="
if ! "${DOCKER}" exec "${NAME}" /opt/venv/bin/pip install \
    --no-cache-dir --disable-pip-version-check --target /tmp/pytest-deps pytest 2>&1 \
    | tail -5 | sed 's/^/  | /'; then
    echo "engine-e2e-test: could not install pytest inside the container (no egress?)." >&2
    echo "  Hint: run '${DOCKER} exec ${NAME} /opt/venv/bin/pip install --target /tmp/pytest-deps pytest' manually," >&2
    echo "  or copy wheels in with ${DOCKER} cp." >&2
    exit 1
fi

# --- run the suite ----------------------------------------------------------
echo
echo "== pytest tests/unit/test_engine_integration.py -q -m integration =="
pytest_out="$("${DOCKER}" exec \
    -e "DISPLAY=:99" \
    -e "NEXT_AUTODESK_API_KEYS=${API_KEY}" \
    -e "PYTHONPATH=/tmp/pytest-deps" \
    -w /repo \
    "${NAME}" /opt/venv/bin/python -m pytest \
    tests/unit/test_engine_integration.py -q -m integration -p no:cacheprovider 2>&1)" || true
printf '%s\n' "${pytest_out}" | sed 's/^/  | /'

# --- verdict ----------------------------------------------------------------
echo
summary_line="$(printf '%s\n' "${pytest_out}" | grep -E '[0-9]+ (passed|failed|error|skipped)' | tail -1 || true)"
if grep -qE '[0-9]+ passed' <<<"${pytest_out}" \
    && ! grep -qE '[0-9]+ (failed|error|skipped)' <<<"${pytest_out}"; then
    PASSED=true
    echo "engine-e2e-test: PASS (${summary_line:-tests passed})"
    exit 0
fi
echo "engine-e2e-test: FAIL" >&2
if grep -qE 'skipped' <<<"${pytest_out}" && ! grep -qE '[0-9]+ passed' <<<"${pytest_out}"; then
    echo "  The module skipped itself: the container's X desktop was not reachable." >&2
    echo "  Check: ${DOCKER} exec ${NAME} supervisorctl -c /opt/nad/supervisord.conf status" >&2
fi
printf '  summary: %s\n' "${summary_line:-unknown}" >&2
exit 1
