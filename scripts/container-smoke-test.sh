#!/usr/bin/env bash
# End-to-end container smoke test for the final NextAutoDesk image.
#
# Starts the image with a throw-away container, waits for the API to become
# healthy, then verifies the whole stack from the outside *and* inside:
#   - supervisord runs Xvfb, the XFCE session (xfwm4) and the API; all RUNNING
#   - /healthz is public; /api/v1/info requires the API key
#   - Xvfb resolution/depth, wmctrl, xdotool, scrot
#   - PyAutoGUI injection (screen size, pointer position) and human-movement
#   - API screenshot endpoint returns a real 1920x1080 PNG
#   - mouse move action changes the real X11 pointer position
#   - no VNC server is installed
#   - idle memory / image size / cold-start time are reported
#
# The container is removed on success (or kept with --keep / on failure for
# debugging). Nothing outside the container and a temporary directory is touched.
#
# Usage:
#   ./scripts/container-smoke-test.sh [--image TAG] [--port PORT] [--keep]
#                                     [--skip-action-tests] [--timeout SECONDS]
#   (default image: nextautodesk:<version from pyproject.toml>)

set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly DOCKER="${DOCKER:-docker}"

# shellcheck source=deploy/vps/_common.sh
source "${REPO_ROOT}/deploy/vps/_common.sh"

IMAGE="${IMAGE:-$(nad_default_image "${REPO_ROOT}")}"
NAME="${NAME:-nad-smoke-$$}"
HOST_PORT="${HOST_PORT:-18765}"
STARTUP_TIMEOUT="${STARTUP_TIMEOUT:-180}"
IDLE_WAIT_S="${IDLE_WAIT_S:-10}"
KEEP=false
SKIP_ACTION_TESTS="${SKIP_ACTION_TESTS:-0}"
API_KEY="${NEXT_AUTODESK_API_KEYS:-}"

usage() {
    sed -n '2,29p' "${BASH_SOURCE[0]}" | sed -e 's/^# \{0,1\}//'
}

die() {
    printf 'container-smoke-test: error: %s\n' "$*" >&2
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
        --skip-action-tests)
            SKIP_ACTION_TESTS=1
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

if ! "${DOCKER}" image inspect "${IMAGE}" >/dev/null 2>&1; then
    die "image '${IMAGE}' not found; build it with ./scripts/build-image.sh"
fi

if [[ -z "${API_KEY}" ]]; then
    if command -v openssl >/dev/null 2>&1; then
        API_KEY="$(openssl rand -hex 16)"
    else
        API_KEY="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
fi

BASE_URL="http://127.0.0.1:${HOST_PORT}"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/nad-smoke.XXXXXX")"
DATA_DIR="${WORK_DIR}/data"
LOG_FILE="${WORK_DIR}/container.log"
mkdir -p "${DATA_DIR}" "${WORK_DIR}/artifacts"
# The container runs as uid/gid 1000 ("nad"); the bind-mounted data dir must be
# writable by that uid.
if [[ "$(id -u)" -eq 0 ]]; then
    chown -R 1000:1000 "${DATA_DIR}"
else
    # The container runs as uid 1000 ("nad"); allow it to write a host directory
    # owned by a different uid (throw-away directory only).
    chmod 0777 "${DATA_DIR}"
fi

FAILED=0
STARTED_AT_MS=0
STARTUP_S="n/a"
IMAGE_SIZE="$("${DOCKER}" image ls --format '{{.Size}}' "${IMAGE}")"
IDLE_MIB="n/a"
SCREENSHOT_INFO="n/a"

section() { printf '\n== %s ==\n' "$*"; }
ok() { printf '  [OK]   %s\n' "$*"; }
info() { printf '  [info] %s\n' "$*"; }
fail() {
    FAILED=$((FAILED + 1))
    printf '  [FAIL] %s\n' "$*"
}

remove_work_dir() {
    # The bind-mounted /data tree is written by uid 1000 inside the container.
    # When this script runs as a non-root host user (CI runners, developer
    # machines), the host cannot unlink those files directly; remove them from
    # a throw-away root container first, then delete the (host-owned) directory.
    rm -rf "${WORK_DIR}" 2>/dev/null && return
    "${DOCKER}" run --rm --user 0:0 \
        -v "${WORK_DIR}:/cleanup" --entrypoint /bin/sh "${IMAGE}" \
        -c 'rm -rf /cleanup/* /cleanup/.[!.]* /cleanup/..?* 2>/dev/null || true' \
        >/dev/null 2>&1 || true
    rm -rf "${WORK_DIR}" 2>/dev/null || true
}

on_exit() {
    local status=$?
    if [[ "${FAILED}" -ne 0 || "${status}" -ne 0 || "${KEEP}" == "true" ]]; then
        printf '\ncontainer-smoke-test: container left for debugging: %s\n' "${NAME}" >&2
        printf '  docker logs %s\n  docker exec -it %s bash\n' "${NAME}" "${NAME}" >&2
        printf '  data dir: %s\n' "${DATA_DIR}" >&2
        return
    fi
    "${DOCKER}" rm -f "${NAME}" >/dev/null 2>&1 || true
    remove_work_dir
}
trap on_exit EXIT

echo "container-smoke-test: image=${IMAGE} container=${NAME} port=${HOST_PORT}"

if "${DOCKER}" inspect "${NAME}" >/dev/null 2>&1; then
    die "a container named '${NAME}' already exists"
fi

section "Container start"
STARTED_AT_MS="$(date +%s%3N)"
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
    "${IMAGE}" >/dev/null
ok "container ${NAME} started"

section "Wait for /healthz (timeout ${STARTUP_TIMEOUT}s)"
while true; do
    if ! "${DOCKER}" inspect -f '{{.State.Running}}' "${NAME}" 2>/dev/null | grep -qx 'true'; then
        fail "container exited before becoming healthy"
        "${DOCKER}" logs --tail 120 "${NAME}" 2>&1 || true
        exit 1
    fi
    http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "${BASE_URL}/healthz" 2>/dev/null || true)"
    if [[ "${http_code}" == "200" ]]; then
        break
    fi
    elapsed_ms=$(( $(date +%s%3N) - STARTED_AT_MS ))
    if (( elapsed_ms > STARTUP_TIMEOUT * 1000 )); then
        fail "API not healthy after ${STARTUP_TIMEOUT}s (last /healthz HTTP ${http_code:-none})"
        "${DOCKER}" logs --tail 120 "${NAME}" 2>&1 || true
        "${DOCKER}" exec "${NAME}" supervisorctl -c /opt/nad/supervisord.conf status 2>&1 || true
        exit 1
    fi
    sleep 0.5
done
STARTUP_S="$(awk -v ms=$(( $(date +%s%3N) - STARTED_AT_MS )) 'BEGIN { printf "%.1f", ms / 1000 }')"
ok "API healthy after ${STARTUP_S}s"

"${DOCKER}" logs "${NAME}" >"${LOG_FILE}" 2>&1 || true
info "container log captured to ${LOG_FILE}"

section "Supervisord / processes"
# /healthz can answer while supervisord is still counting the xfce/api startsecs,
# so wait for every program to reach RUNNING before asserting.
status_out=""
for _ in $(seq 1 120); do
    status_out="$("${DOCKER}" exec "${NAME}" supervisorctl -c /opt/nad/supervisord.conf status 2>&1 || true)"
    if grep -qE '^xvfb[[:space:]]+RUNNING' <<<"${status_out}" \
        && grep -qE '^xfce[[:space:]]+RUNNING' <<<"${status_out}" \
        && grep -qE '^api[[:space:]]+RUNNING' <<<"${status_out}"; then
        break
    fi
    sleep 0.5
done
printf '%s\n' "${status_out}" | sed 's/^/  | /'
for program in xvfb xfce api; do
    if grep -qE "^${program}[[:space:]]+RUNNING" <<<"${status_out}"; then
        ok "supervisord program '${program}' RUNNING"
    else
        fail "supervisord program '${program}' is not RUNNING"
    fi
done
"${DOCKER}" exec "${NAME}" pgrep -x Xvfb >/dev/null && ok "Xvfb process" || fail "Xvfb process missing"
"${DOCKER}" exec "${NAME}" pgrep -x xfwm4 >/dev/null && ok "xfwm4 process" || fail "xfwm4 process missing"
"${DOCKER}" exec "${NAME}" pgrep -x xfce4-session >/dev/null && ok "xfce4-session process" || fail "xfce4-session process missing"
"${DOCKER}" exec "${NAME}" pgrep -f 'nextautodesk serve' >/dev/null && ok "API process" || fail "API process missing"
"${DOCKER}" exec "${NAME}" test -x /usr/local/bin/clack && ok "Clack strategy binary installed" || info "Clack binary not installed"

section "X server / XFCE desktop"
"${DOCKER}" exec "${NAME}" xdpyinfo >"${WORK_DIR}/xdpyinfo.out" 2>/dev/null || true
dims="$(awk '/dimensions:/ {print $2; exit}' "${WORK_DIR}/xdpyinfo.out" 2>/dev/null || true)"
if [[ "${dims}" == "1920x1080" ]]; then
    ok "Xvfb screen ${dims} pixels"
else
    fail "unexpected Xvfb screen: ${dims:-unknown}"
fi
"${DOCKER}" exec "${NAME}" wmctrl -m >"${WORK_DIR}/wmctrl.out" 2>/dev/null || true
wm_name="$(awk '/Name:/ {print $2; exit}' "${WORK_DIR}/wmctrl.out" 2>/dev/null || true)"
[[ -n "${wm_name}" ]] && ok "EWMH window manager: ${wm_name}" || fail "wmctrl cannot query the window manager"
"${DOCKER}" exec "${NAME}" xdotool getmouselocation >"${WORK_DIR}/mouseloc.out" 2>/dev/null || true
loc="$(head -n1 "${WORK_DIR}/mouseloc.out" 2>/dev/null || true)"
[[ "${loc}" == x:* ]] && ok "xdotool: ${loc}" || fail "xdotool getmouselocation failed"
if "${DOCKER}" exec "${NAME}" scrot /tmp/smoke-test.png >/dev/null 2>&1 \
    && "${DOCKER}" exec "${NAME}" test -s /tmp/smoke-test.png; then
    ok "scrot screenshot inside container ($("${DOCKER}" exec "${NAME}" stat -c%s /tmp/smoke-test.png) bytes)"
else
    fail "scrot screenshot failed"
fi

section "Python runtime (PyAutoGUI / human-movement / optional strategies)"
if py_out="$("${DOCKER}" exec -i "${NAME}" /opt/venv/bin/python - <<'PY' 2>&1
import importlib.metadata as md
import sys

print(f"nextautodesk {md.version('nextautodesk')} / python {sys.version.split()[0]}")
import pyautogui

print(f"pyautogui {pyautogui.__version__} screen={tuple(pyautogui.size())} position={tuple(pyautogui.position())}")
print(f"human-movement {md.version('human-movement')}")
for dist in ("HumanMoveMouse", "windmouse"):
    try:
        print(f"{dist} {md.version(dist)}")
    except md.PackageNotFoundError:
        print(f"{dist} not installed")
PY
)"; then
    printf '%s\n' "${py_out}" | sed 's/^/  | /'
    if grep -q 'screen=(1920, 1080)' <<<"${py_out}"; then
        ok "PyAutoGUI reports the 1920x1080 Xvfb screen"
    else
        fail "PyAutoGUI screen size mismatch"
    fi
    grep -q '^human-movement ' <<<"${py_out}" && ok "human-movement strategy engine importable" || fail "human-movement import failed"
else
    fail "in-container Python checks failed"
    printf '%s\n' "${py_out}" | sed 's/^/  | /'
fi

section "HTTP API"
health_body="$(curl -fsS --max-time 5 "${BASE_URL}/healthz" 2>&1 || true)"
[[ -n "${health_body}" ]] && ok "GET /healthz -> ${health_body:0:300}" || fail "GET /healthz returned no body"

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${BASE_URL}/api/v1/info" 2>/dev/null || true)"
if [[ "${code}" == "401" || "${code}" == "403" ]]; then
    ok "unauthenticated GET /api/v1/info rejected (HTTP ${code})"
else
    fail "unauthenticated GET /api/v1/info should be 401/403, got ${code}"
fi

if curl -fsS --max-time 10 -H "X-API-Key: ${API_KEY}" "${BASE_URL}/api/v1/info" -o "${WORK_DIR}/info.json" 2>/dev/null; then
    if python3 - "${WORK_DIR}/info.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert isinstance(data, dict) and data, f"unexpected /info payload: {data!r}"
print(json.dumps(data, ensure_ascii=False)[:400])
PY
    then
        ok "authenticated GET /api/v1/info"
    else
        fail "GET /api/v1/info payload is not a JSON object"
    fi
else
    fail "authenticated GET /api/v1/info failed"
fi

if curl -fsS --max-time 20 -H "X-API-Key: ${API_KEY}" "${BASE_URL}/api/v1/screen/screenshot" \
    -o "${WORK_DIR}/api-screenshot.png" 2>/dev/null; then
    if SCREENSHOT_INFO="$(python3 - "${WORK_DIR}/api-screenshot.png" <<'PY'
import struct, sys

data = open(sys.argv[1], "rb").read()
if data[:8] != b"\x89PNG\r\n\x1a\n":
    raise SystemExit("not a PNG file")
width, height = struct.unpack(">II", data[16:24])
print(f"{width}x{height}, {len(data)} bytes")
assert (width, height) == (1920, 1080), f"unexpected screenshot size {width}x{height}"
PY
    )"; then
        ok "GET /api/v1/screen/screenshot -> PNG ${SCREENSHOT_INFO}"
    else
        fail "screenshot returned by the API is not a 1920x1080 PNG"
    fi
else
    fail "GET /api/v1/screen/screenshot failed"
fi

if [[ "${SKIP_ACTION_TESTS}" != "1" ]]; then
    section "Desktop action through the API (mouse move)"
    move_code="$(curl -s -o "${WORK_DIR}/move.json" -w '%{http_code}' --max-time 30 \
        -X POST -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' \
        -d '{"x": 320, "y": 240}' "${BASE_URL}/api/v1/mouse/move" 2>/dev/null || true)"
    if [[ "${move_code}" == "2"* ]]; then
        ok "POST /api/v1/mouse/move -> HTTP ${move_code}"
        if curl -fsS --max-time 10 -H "X-API-Key: ${API_KEY}" "${BASE_URL}/api/v1/mouse/position" \
            -o "${WORK_DIR}/position.json" 2>/dev/null; then
            if python3 - "${WORK_DIR}/position.json" <<'PY'
import json, sys

data = json.load(open(sys.argv[1]))

def find_point(value):
    if isinstance(value, dict):
        if "x" in value and "y" in value:
            return value["x"], value["y"]
        for nested in value.values():
            point = find_point(nested)
            if point is not None:
                return point
    elif isinstance(value, list):
        for nested in value:
            point = find_point(nested)
            if point is not None:
                return point
    return None

point = find_point(data)
if point is None:
    raise SystemExit(f"no x/y in {data!r}")
x, y = point
print(f"pointer at ({x}, {y})")
assert abs(float(x) - 320) <= 2 and abs(float(y) - 240) <= 2, f"pointer did not move: {point}"
PY
            then
                ok "GET /api/v1/mouse/position confirms the X11 pointer moved"
            else
                fail "pointer position does not match the requested move"
            fi
        else
            fail "GET /api/v1/mouse/position failed"
        fi
    else
        fail "POST /api/v1/mouse/move failed (HTTP ${move_code})"
    fi
else
    info "desktop action tests skipped (--skip-action-tests)"
fi

section "Meta catalogues (/actions, /strategies)"
if curl -fsS --max-time 15 -H "X-API-Key: ${API_KEY}" "${BASE_URL}/api/v1/actions" \
    -o "${WORK_DIR}/actions.json" 2>/dev/null; then
    if python3 - "${WORK_DIR}/actions.json" <<'PY'
import json, sys

data = json.load(open(sys.argv[1]))
actions = data.get("actions", [])
names = [action.get("name") for action in actions if isinstance(action, dict)]
if len(actions) < 20 or not all(names):
    raise SystemExit(f"unexpected action catalogue: {data!r}")
print(f"{len(actions)} actions: {', '.join(sorted(names)[:8])} ...")
PY
    then
        ok "GET /api/v1/actions"
    else
        fail "GET /api/v1/actions payload unexpected"
    fi
else
    fail "GET /api/v1/actions failed"
fi

if curl -fsS --max-time 15 -H "X-API-Key: ${API_KEY}" "${BASE_URL}/api/v1/strategies" \
    -o "${WORK_DIR}/strategies.json" 2>/dev/null; then
    if python3 - "${WORK_DIR}/strategies.json" <<'PY'
import json, sys

data = json.load(open(sys.argv[1]))
operations = data.get("operations") or []
providers = data.get("providers") or []
if len(operations) < 18 or not providers:
    raise SystemExit(f"unexpected strategy catalogue: {data!r}")
names = []
for provider in providers:
    if isinstance(provider, dict):
        names.append(f"{provider.get('name', '?')}({provider.get('available', '?')})")
print(f"{len(operations)} operations, providers: {', '.join(names)}")
PY
    then
        ok "GET /api/v1/strategies"
    else
        fail "GET /api/v1/strategies payload unexpected"
    fi
else
    fail "GET /api/v1/strategies failed"
fi

if [[ "${SKIP_ACTION_TESTS}" != "1" ]]; then
    section "Keyboard E2E through the API (xfce4-terminal -> file)"
    "${DOCKER}" exec "${NAME}" bash -c \
        'nohup xfce4-terminal --title=NAD-E2E --geometry=100x30 --command="bash -c \"cat > /tmp/nad-e2e.txt\"" >/tmp/nad-terminal.log 2>&1 &' \
        >/dev/null 2>&1 || true

    window_id=""
    for _ in $(seq 1 100); do
        if curl -fsS --max-time 5 -H "X-API-Key: ${API_KEY}" "${BASE_URL}/api/v1/windows" \
            -o "${WORK_DIR}/windows.json" 2>/dev/null; then
            window_id="$(python3 - "${WORK_DIR}/windows.json" <<'PY'
import json, sys

data = json.load(open(sys.argv[1]))
for window in data.get("windows", []):
    if "NAD-E2E" in (window.get("title") or ""):
        print(window.get("id", ""))
        break
PY
)"
            [[ -n "${window_id}" ]] && break
        fi
        sleep 0.2
    done

    if [[ -z "${window_id}" ]]; then
        fail "xfce4-terminal window 'NAD-E2E' not found via GET /api/v1/windows"
        "${DOCKER}" exec "${NAME}" cat /tmp/nad-terminal.log 2>&1 | tail -20 | sed 's/^/  | /' || true
    else
        ok "xfce4-terminal window found via the API (id=${window_id})"
        activate_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
            -X POST -H "X-API-Key: ${API_KEY}" "${BASE_URL}/api/v1/windows/${window_id}/activate" 2>/dev/null || true)"
        [[ "${activate_code}" == "2"* ]] \
            && ok "POST /api/v1/windows/{id}/activate -> HTTP ${activate_code}" \
            || fail "window activate failed (HTTP ${activate_code})"

        type_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 \
            -X POST -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' \
            -d '{"text": "NAD_E2E_OK_123"}' "${BASE_URL}/api/v1/keyboard/type" 2>/dev/null || true)"
        [[ "${type_code}" == "2"* ]] \
            && ok "POST /api/v1/keyboard/type -> HTTP ${type_code}" \
            || fail "keyboard type failed (HTTP ${type_code})"
        enter_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
            -X POST -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' \
            -d '{"key": "enter"}' "${BASE_URL}/api/v1/keyboard/press" 2>/dev/null || true)"
        [[ "${enter_code}" == "2"* ]] \
            && ok "POST /api/v1/keyboard/press (enter) -> HTTP ${enter_code}" \
            || fail "keyboard press failed (HTTP ${enter_code})"

        typed_content=""
        for _ in $(seq 1 100); do
            typed_content="$("${DOCKER}" exec "${NAME}" cat /tmp/nad-e2e.txt 2>/dev/null | tr -d '\r' || true)"
            [[ -n "${typed_content}" ]] && break
            sleep 0.2
        done
        if [[ "${typed_content}" == *"NAD_E2E_OK_123"* ]]; then
            ok "typed text was delivered through X11 into /tmp/nad-e2e.txt"
        else
            fail "typed text not found in /tmp/nad-e2e.txt (content: ${typed_content:-<empty>})"
        fi

        # Unicode injection (Chinese) - exercises the Xlib keysym remapping path.
        curl -s -o /dev/null --max-time 60 \
            -X POST -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' \
            -d '{"text": "中文OK"}' "${BASE_URL}/api/v1/keyboard/type" >/dev/null 2>&1 || true
        curl -s -o /dev/null --max-time 30 \
            -X POST -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' \
            -d '{"key": "enter"}' "${BASE_URL}/api/v1/keyboard/press" >/dev/null 2>&1 || true
        unicode_content=""
        for _ in $(seq 1 100); do
            unicode_content="$("${DOCKER}" exec "${NAME}" cat /tmp/nad-e2e.txt 2>/dev/null | tr -d '\r' || true)"
            [[ "${unicode_content}" == *"中文OK"* ]] && break
            sleep 0.2
        done
        if [[ "${unicode_content}" == *"中文OK"* ]]; then
            ok "Unicode text (中文OK) delivered through X11"
        else
            info "WARNING: Unicode E2E not verified (content: ${unicode_content:-<empty>})"
        fi
    fi

    section "Task queue E2E (submit -> poll -> succeeded)"
    submit_code="$(curl -s -o "${WORK_DIR}/task1.json" -w '%{http_code}' --max-time 30 \
        -X POST -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' \
        -d '{"name": "smoke-serial", "steps": [{"action": "mouse.move", "params": {"x": 160, "y": 120}}, {"action": "desktop.wait", "params": {"ms": 300}}, {"action": "screen.screenshot", "params": {"save": true, "format": "png"}}]}' \
        "${BASE_URL}/api/v1/tasks" 2>/dev/null || true)"
    if [[ "${submit_code}" == "202" ]]; then
        task_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' "${WORK_DIR}/task1.json")"
        ok "POST /api/v1/tasks -> HTTP 202 (task ${task_id})"
        task_status=""
        task_started="$(date +%s)"
        for _ in $(seq 1 300); do
            curl -fsS --max-time 10 -H "X-API-Key: ${API_KEY}" "${BASE_URL}/api/v1/tasks/${task_id}" \
                -o "${WORK_DIR}/task1-poll.json" 2>/dev/null || true
            task_status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "${WORK_DIR}/task1-poll.json" 2>/dev/null || true)"
            case "${task_status}" in
                succeeded | failed | cancelled | timed_out | interrupted) break ;;
            esac
            sleep 0.2
        done
        task_duration=$(( $(date +%s) - task_started ))
        if [[ "${task_status}" == "succeeded" ]]; then
            step_count="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("step_results", [])))' "${WORK_DIR}/task1-poll.json")"
            ok "task ${task_id} succeeded (${step_count} steps, ${task_duration}s)"
        else
            fail "task ${task_id} ended with status '${task_status}'"
            python3 -m json.tool "${WORK_DIR}/task1-poll.json" 2>/dev/null | head -40 | sed 's/^/  | /' || true
        fi
        screenshot_count="$("${DOCKER}" exec "${NAME}" sh -c 'ls /data/screenshots 2>/dev/null | wc -l' | tr -d ' ')"
        [[ "${screenshot_count:-0}" -ge 1 ]] \
            && ok "task screenshot persisted in /data/screenshots (${screenshot_count} file(s))" \
            || fail "task screenshot was not persisted in /data/screenshots"
    else
        fail "POST /api/v1/tasks failed (HTTP ${submit_code})"
    fi

    section "Task cancellation E2E (submit long task -> cancel)"
    cancel_submit="$(curl -s -o "${WORK_DIR}/task2.json" -w '%{http_code}' --max-time 30 \
        -X POST -H "X-API-Key: ${API_KEY}" -H 'Content-Type: application/json' \
        -d '{"name": "smoke-cancel", "steps": [{"action": "desktop.wait", "params": {"ms": 20000}}]}' \
        "${BASE_URL}/api/v1/tasks" 2>/dev/null || true)"
    if [[ "${cancel_submit}" == "202" ]]; then
        cancel_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' "${WORK_DIR}/task2.json")"
        sleep 0.5
        cancel_code="$(curl -s -o "${WORK_DIR}/task2-cancel.json" -w '%{http_code}' --max-time 15 \
            -X POST -H "X-API-Key: ${API_KEY}" "${BASE_URL}/api/v1/tasks/${cancel_id}/cancel" 2>/dev/null || true)"
        [[ "${cancel_code}" == "2"* ]] \
            && ok "POST /api/v1/tasks/{id}/cancel -> HTTP ${cancel_code}" \
            || fail "task cancel request failed (HTTP ${cancel_code})"
        final_status=""
        for _ in $(seq 1 150); do
            curl -fsS --max-time 10 -H "X-API-Key: ${API_KEY}" "${BASE_URL}/api/v1/tasks/${cancel_id}" \
                -o "${WORK_DIR}/task2-poll.json" 2>/dev/null || true
            final_status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "${WORK_DIR}/task2-poll.json" 2>/dev/null || true)"
            case "${final_status}" in
                succeeded | failed | cancelled | timed_out | interrupted) break ;;
            esac
            sleep 0.2
        done
        if [[ "${final_status}" == "cancelled" ]]; then
            ok "task ${cancel_id} cancelled (status=cancelled)"
        else
            fail "cancelled task ended with status '${final_status}' (expected cancelled)"
        fi
    else
        fail "POST /api/v1/tasks (cancel case) failed (HTTP ${cancel_submit})"
    fi
else
    info "keyboard/task E2E tests skipped (--skip-action-tests)"
fi

section "VNC reserved (must not be installed by default)"
if "${DOCKER}" exec "${NAME}" sh -c 'command -v x11vnc' >/dev/null 2>&1; then
    fail "x11vnc is installed but VNC must stay reserved/disabled by default"
else
    ok "no VNC server installed in the image"
fi

section "Persistent data / logs"
"${DOCKER}" exec "${NAME}" sh -c 'ls -la /data /data/logs' 2>&1 | sed 's/^/  | /' || true
for file in supervisord.log xvfb.log xfce.log api.log nextautodesk.log; do
    if "${DOCKER}" exec "${NAME}" test -s "/data/logs/${file}"; then
        ok "log /data/logs/${file}"
    else
        fail "log /data/logs/${file} missing or empty"
    fi
done
if "${DOCKER}" exec "${NAME}" grep -qiE 'FATAL|Traceback' /data/logs/supervisord.log 2>/dev/null; then
    fail "supervisord log contains FATAL/Traceback"
else
    ok "no FATAL/Traceback in supervisord log"
fi

section "Resource usage"
sleep "${IDLE_WAIT_S}" # let the desktop settle before sampling idle memory
mem_usage="$("${DOCKER}" stats --no-stream --format '{{.MemUsage}}' "${NAME}" 2>/dev/null | awk -F'/' '{print $1}' | tr -d ' ' || true)"
if [[ -n "${mem_usage}" ]]; then
    IDLE_MIB="$(python3 - "${mem_usage}" <<'PY'
import sys

raw = sys.argv[1]
units = {"B": 1 / 1048576, "KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "TiB": 1048576.0}
number = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
unit = "".join(ch for ch in raw if ch.isalpha()) or "B"
try:
    value = float(number) * units[unit]
except (KeyError, ValueError):
    print("n/a")
else:
    print(f"{value:.1f}")
PY
)"
    if [[ "${IDLE_MIB}" != "n/a" ]]; then
        ok "idle container memory: ${IDLE_MIB} MiB"
        if python3 -c "import sys; sys.exit(0 if float('${IDLE_MIB}') < 1024 else 1)"; then
            :
        else
            fail "idle memory ${IDLE_MIB} MiB exceeds the 1 GiB safety bound"
        fi
        if python3 -c "import sys; sys.exit(0 if float('${IDLE_MIB}') < 600 else 1)"; then
            ok "idle memory is below the 600 MiB target"
        else
            info "WARNING: idle memory ${IDLE_MIB} MiB is above the 600 MiB target (still below the 1 GiB bound)"
        fi
    fi
else
    fail "could not sample container memory with docker stats"
fi

section "Summary"
printf '  image:           %s (%s)\n' "${IMAGE}" "${IMAGE_SIZE:-unknown}"
printf '  cold start:      %ss to healthy\n' "${STARTUP_S}"
printf '  idle memory:     %s MiB\n' "${IDLE_MIB}"
printf '  screenshot:      %s\n' "${SCREENSHOT_INFO}"
printf '  data dir:        %s\n' "${DATA_DIR}"
printf '  container log:   %s\n' "${LOG_FILE}"

echo
if [[ "${FAILED}" -ne 0 ]]; then
    echo "container-smoke-test: FAIL (${FAILED} failure(s))"
    exit 1
fi
echo "container-smoke-test: PASS (${IMAGE})"
