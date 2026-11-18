#!/usr/bin/env bash
# End-to-end verification of a deployed NextAutoDesk instance.
#
# Two execution modes:
#   --host <ssh-target>  Run from a workstation. HTTP is done locally when the
#                        published port is reachable, otherwise through SSH;
#                        container/runtime checks always run through SSH.
#   --here               Run directly on the deployment host (no SSH at all):
#                        HTTP via 127.0.0.1, Docker locally, API key read from
#                        the deployment .env (never printed). Use this when the
#                        API is not published or only the VPS is accessible.
#
# The target host has no default: pass --host or --here explicitly (see --help).
# Exit code 0 only when every check passed.
set -euo pipefail

readonly SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10
                   -o ServerAliveInterval=15 -o ServerAliveCountMax=4)

HOST=""
HTTP_HOST=""
PORT=8765
MODE="auto"
HERE=false
KEY=""
CONTAINER=""
DEPLOY_DIR="/opt/nextautodesk"
SKIP_PERCEPTION=false

usage() {
    cat <<'EOF'
End-to-end verification of a deployed NextAutoDesk instance.

Usage:
  ./deploy/vps/verify.sh --host <vps-host> [options]
  ./deploy/vps/verify.sh --here [options]

Required (one of):
  --host TARGET       SSH target: hostname, IP or user@host (no default)
  --here              run directly on the deployment host (no SSH)

Options:
  --port PORT         published API port (default: 8765)
  --local             (--host mode) force local HTTP against http://<host>:<port>
                      instead of auto-detecting whether it is reachable
  --key KEY           API key (default: read from <deploy-dir>/.env on the host)
  --deploy-dir DIR    deployment directory with .env (default: /opt/nextautodesk)
  --container NAME    container name (default: auto-detected / nextautodesk-nextautodesk-1)
  --skip-perception   skip the /elements + /interactions checks (image built
                      before the perception/composite tiers existed)
  -h, --help          show this help

Examples:
  ./deploy/vps/verify.sh --host <vps-host>
  ./deploy/vps/verify.sh --host <user>@<vps-host> --port 8765
  ssh <vps-host> 'cd /root/nad-release/build && ./deploy/vps/verify.sh --here'

Checks: health/auth, actions & strategies, mouse/screen/windows, keyboard into a
real xfce4-terminal, task queue (success + cancellation), runtime footprint
(container healthy, no restarts, no OOM) and, unless --skip-perception is used,
the perception (/elements) and composite (/interactions) tiers: locator
availability, OCR, text-element find, template find from an OCR bbox and a real
interaction.click, plus negative checks (401 without a key, 404 ELEMENT_NOT_FOUND).
EOF
}

die() {
    printf 'verify: error: %s\n' "$*" >&2
    exit 1
}

remote() {
    # In --here mode the same command strings run locally through bash; in
    # --host mode they run on the target through SSH.
    if [[ "${HERE}" == "true" ]]; then
        bash -c "$*"
    else
        ssh "${SSH_OPTS[@]}" "${HOST}" "$@"
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)
            [[ $# -ge 2 ]] || die "--host requires a value"
            HOST="$2"
            shift 2
            ;;
        --here)
            HERE=true
            shift
            ;;
        --port)
            [[ $# -ge 2 ]] || die "--port requires a value"
            PORT="$2"
            shift 2
            ;;
        --local)
            MODE="local"
            shift
            ;;
        --key)
            [[ $# -ge 2 ]] || die "--key requires a value"
            KEY="$2"
            shift 2
            ;;
        --deploy-dir)
            [[ $# -ge 2 ]] || die "--deploy-dir requires a value"
            DEPLOY_DIR="$2"
            shift 2
            ;;
        --container)
            [[ $# -ge 2 ]] || die "--container requires a value"
            CONTAINER="$2"
            shift 2
            ;;
        --skip-perception)
            SKIP_PERCEPTION=true
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

if [[ "${HERE}" == "true" && -n "${HOST}" ]]; then
    die "use either --host <vps-host> or --here, not both"
fi
if [[ "${HERE}" != "true" && -z "${HOST}" ]]; then
    usage >&2
    printf '\n' >&2
    die "--host <vps-host> or --here is required"
fi
if [[ "${HERE}" != "true" && ! "${HOST}" =~ ^[A-Za-z0-9._@:%+-]+$ ]]; then
    die "invalid --host '${HOST}' (expected hostname, IP or user@host)"
fi
[[ "${PORT}" =~ ^[0-9]+$ ]] || die "--port must be numeric"
((PORT >= 1 && PORT <= 65535)) || die "--port must be between 1 and 65535"
[[ "${DEPLOY_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "invalid --deploy-dir '${DEPLOY_DIR}'"

if [[ "${HERE}" == "true" ]]; then
    for tool in curl python3 docker; do
        command -v "${tool}" >/dev/null 2>&1 || die "${tool} is required on this machine"
    done
    HTTP_HOST="127.0.0.1"
    MODE="local"
else
    for tool in curl python3 ssh; do
        command -v "${tool}" >/dev/null 2>&1 || die "${tool} is required on this machine"
    done
    # ssh accepts user@host; the HTTP URL only needs the host part. Bare IPv6
    # addresses are wrapped in brackets so they can be used in a URL.
    HTTP_HOST="${HOST##*@}"
    if [[ "${HTTP_HOST}" == *:* && "${HTTP_HOST}" != \[* ]]; then
        HTTP_HOST="[${HTTP_HOST}]"
    fi
    remote 'true' || die "cannot SSH to ${HOST} (BatchMode; set up the key/host alias first)"
    if [[ "${MODE}" == "auto" ]]; then
        if curl -fsS --max-time 5 "http://${HTTP_HOST}:${PORT}/healthz" >/dev/null 2>&1; then
            MODE="local"
        else
            MODE="remote"
        fi
    fi
fi

docker_cmd() {
    # Running a docker command against the deployment host, in either mode.
    remote "$*"
}

if [[ -z "${CONTAINER}" ]]; then
    CONTAINER="$(docker_cmd "docker ps --filter label=com.docker.compose.service=nextautodesk --format '{{.Names}}' 2>/dev/null | head -n1" || true)"
    [[ -n "${CONTAINER}" ]] || CONTAINER="nextautodesk-nextautodesk-1"
fi

if [[ -z "${KEY}" ]]; then
    KEY="$(docker_cmd "sed -n 's/^NEXT_AUTODESK_API_KEYS=//p' ${DEPLOY_DIR}/.env 2>/dev/null | tail -1" || true)"
fi
[[ -n "${KEY}" ]] || die "could not determine the API key (pass --key)"

# Never put the API key into a process argument list (curl -H '<key>' is
# visible in ps on this machine / on the deployment host). Write a 0600 header
# file instead and pass it to curl as `-H @file`.
AUTH_HEADER=""
REMOTE_HEADER=""
cleanup_auth_headers() {
    [[ -n "${AUTH_HEADER}" ]] && rm -f "${AUTH_HEADER}"
    if [[ -n "${REMOTE_HEADER}" ]]; then
        remote "rm -f '${REMOTE_HEADER}'" >/dev/null 2>&1 || true
    fi
}
trap cleanup_auth_headers EXIT
if [[ "${MODE}" == "local" ]]; then
    AUTH_HEADER="$(mktemp "${TMPDIR:-/tmp}/nad-verify-key.XXXXXX")"
    chmod 0600 "${AUTH_HEADER}"
    printf 'X-API-Key: %s\n' "${KEY}" >"${AUTH_HEADER}"
else
    REMOTE_HEADER="$(remote 'mktemp /tmp/nad-verify-key.XXXXXX')" \
        || die "cannot create a temporary header file on the deployment host"
    printf 'X-API-Key: %s\n' "${KEY}" | remote "umask 077; cat > '${REMOTE_HEADER}'" \
        || die "cannot write the temporary header file on the deployment host"
fi

if [[ "${HERE}" == "true" ]]; then
    echo "verify: target 127.0.0.1:${PORT} (local HTTP + local docker, container ${CONTAINER})"
else
    echo "verify: target ${HOST}:${PORT} (${MODE} HTTP via curl, container ${CONTAINER})"
fi


# ---------------------------------------------------------------------------
# HTTP helper: runs curl locally or through SSH; prints "<body>\n<status>"
# ---------------------------------------------------------------------------
http() {
    local method="$1" path="$2" body="${3:-}"
    local auth=(-H "@${AUTH_HEADER}")
    if [[ "${method}" == "GET" && -z "${body}" ]]; then
        if [[ "${MODE}" == "local" ]]; then
            curl -sS -m 30 -w '\n%{http_code}' "${auth[@]}" "http://${HTTP_HOST}:${PORT}${path}"
        else
            remote "curl -sS -m 30 -w '\\n%{http_code}' -H @${REMOTE_HEADER} 'http://127.0.0.1:${PORT}${path}'"
        fi
    elif [[ -n "${body}" ]]; then
        if [[ "${MODE}" == "local" ]]; then
            printf '%s' "${body}" | curl -sS -m 30 -w '\n%{http_code}' -X "${method}" \
                -H 'Content-Type: application/json' "${auth[@]}" --data-binary @- "http://${HTTP_HOST}:${PORT}${path}"
        else
            printf '%s' "${body}" | remote "curl -sS -m 30 -w '\\n%{http_code}' -X '${method}' -H 'Content-Type: application/json' -H @${REMOTE_HEADER} --data-binary @- 'http://127.0.0.1:${PORT}${path}'"
        fi
    else
        if [[ "${MODE}" == "local" ]]; then
            curl -sS -m 30 -w '\n%{http_code}' -X "${method}" "${auth[@]}" "http://${HTTP_HOST}:${PORT}${path}"
        else
            remote "curl -sS -m 30 -w '\\n%{http_code}' -X '${method}' -H @${REMOTE_HEADER} 'http://127.0.0.1:${PORT}${path}'"
        fi
    fi
}

PASS=0
FAIL=0
declare -a FAILURES=()
LAST_BODY=""

check() {
    local name="$1" expected="$2" response="$3"
    local status body
    status="$(printf '%s' "${response}" | tail -1)"
    body="$(printf '%s' "${response}" | sed '$d')"
    LAST_BODY="${body}"
    if [[ "${status}" == "${expected}" ]]; then
        PASS=$((PASS + 1))
        printf '  PASS  %-50s [%s] %s\n' "${name}" "${status}" "$(printf '%s' "${body}" | head -c 110 | tr '\n' ' ')"
        return 0
    fi
    FAIL=$((FAIL + 1))
    FAILURES+=("${name}: expected HTTP ${expected}, got ${status}: $(printf '%s' "${body}" | head -c 300)")
    printf '  FAIL  %-50s [%s] %s\n' "${name}" "${status}" "$(printf '%s' "${body}" | head -c 160 | tr '\n' ' ')"
    return 1
}

# Accepts any of the statuses in $2 (space separated), e.g. check_any "200 404".
check_any() {
    local name="$1" expected_csv="$2" response="$3"
    local status body expected
    status="$(printf '%s' "${response}" | tail -1)"
    body="$(printf '%s' "${response}" | sed '$d')"
    LAST_BODY="${body}"
    for expected in ${expected_csv}; do
        if [[ "${status}" == "${expected}" ]]; then
            PASS=$((PASS + 1))
            printf '  PASS  %-50s [%s] %s\n' "${name}" "${status}" "$(printf '%s' "${body}" | head -c 110 | tr '\n' ' ')"
            return 0
        fi
    done
    FAIL=$((FAIL + 1))
    FAILURES+=("${name}: expected HTTP ${expected_csv}, got ${status}: $(printf '%s' "${body}" | head -c 300)")
    printf '  FAIL  %-50s [%s] %s\n' "${name}" "${status}" "$(printf '%s' "${body}" | head -c 160 | tr '\n' ' ')"
    return 1
}

json_get() {
    printf '%s' "${LAST_BODY}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(${1})" 2>/dev/null || true
}

pass() {
    PASS=$((PASS + 1))
    echo "  PASS  $*"
}

fail() {
    FAIL=$((FAIL + 1))
    FAILURES+=("$*")
    echo "  FAIL  $*"
}

# Run a Python assertion over the last JSON body (${LAST_BODY}). Prints the
# script's stdout as detail on success and its error on failure.
python_gate() {
    local name="$1" script="$2" output rc
    output="$(printf '%s' "${LAST_BODY}" | python3 -c "${script}" 2>&1)"
    rc=$?
    if ((rc == 0)); then
        if [[ -n "${output}" ]]; then
            pass "${name}: ${output}"
        else
            pass "${name}"
        fi
    else
        fail "${name}: ${output:-python check failed}"
    fi
}

# ---------------------------------------------------------------------------
# 1. Unauthenticated probes and auth enforcement
# ---------------------------------------------------------------------------
echo "[1] health & auth"
check "GET /healthz" 200 "$(http GET /healthz)"
check "GET /readyz" 200 "$(http GET /readyz)"
unauth_resp="$(
    if [[ "${MODE}" == "local" ]]; then
        curl -sS -m 10 -w '\n%{http_code}' "http://${HTTP_HOST}:${PORT}/api/v1/info"
    else
        remote "curl -sS -m 10 -w '\\n%{http_code}' 'http://127.0.0.1:${PORT}/api/v1/info'"
    fi
)"
check "GET /api/v1/info without key -> 401" 401 "${unauth_resp}"
check "GET /api/v1/info" 200 "$(http GET /api/v1/info)"
check "GET /api/v1/actions" 200 "$(http GET /api/v1/actions)"
check "GET /api/v1/strategies" 200 "$(http GET /api/v1/strategies)"

# ---------------------------------------------------------------------------
# 2. Pointer, screen and screenshot
# ---------------------------------------------------------------------------
echo "[2] mouse / screen / windows"
check "POST /api/v1/mouse/move" 200 "$(http POST /api/v1/mouse/move '{"x":120,"y":110}')"
check "GET /api/v1/mouse/position" 200 "$(http GET /api/v1/mouse/position)"
echo "        position after move: $(json_get "d.get('x'),d.get('y')")"
check "POST /api/v1/mouse/move (direct strategy)" 200 "$(http POST /api/v1/mouse/move '{"x":200,"y":150,"strategy":"direct"}')"
check "POST /api/v1/mouse/move-relative" 200 "$(http POST /api/v1/mouse/move-relative '{"dx":10,"dy":10}')"
check "POST /api/v1/mouse/click left" 200 "$(http POST /api/v1/mouse/click '{"x":300,"y":200,"button":"left","clicks":1}')"
check "POST /api/v1/mouse/scroll" 200 "$(http POST /api/v1/mouse/scroll '{"dx":0,"dy":-1}')"
# State primitives: the default config must keep the button held between the
# mouse.down and mouse.up requests, and /mouse/state must expose it.
check "POST /api/v1/mouse/down (state primitive)" 200 "$(http POST /api/v1/mouse/down '{"button":"left"}')"
state_resp="$(http GET /api/v1/mouse/state)"
check "GET /api/v1/mouse/state" 200 "${state_resp}"
LAST_BODY="$(printf '%s' "${state_resp}" | sed '$d')"
if printf '%s' "${LAST_BODY}" | python3 -c "
import json,sys
d=json.load(sys.stdin)
raise SystemExit(0 if d.get('buttons') == ['left'] else 1)
" 2>/dev/null; then
    PASS=$((PASS + 1))
    echo "  PASS  mouse button state reflects mouse.down: ${LAST_BODY}"
else
    FAIL=$((FAIL + 1))
    FAILURES+=("mouse button state after mouse.down: ${LAST_BODY}")
    echo "  FAIL  mouse button state after mouse.down: ${LAST_BODY}"
fi
check "POST /api/v1/mouse/up (releases)" 200 "$(http POST /api/v1/mouse/up '{"button":"left"}')"
check "GET /api/v1/screen/size" 200 "$(http GET /api/v1/screen/size)"
echo "        screen size: $(json_get "d.get('width'),d.get('height')")"
SCREEN_W="$(json_get "d.get('width')" || true)"
SCREEN_H="$(json_get "d.get('height')" || true)"
[[ "${SCREEN_W}" =~ ^[0-9]+$ ]] || SCREEN_W=1920
[[ "${SCREEN_H}" =~ ^[0-9]+$ ]] || SCREEN_H=1080
check "POST /api/v1/screen/screenshot" 200 "$(http POST /api/v1/screen/screenshot '{"format":"png"}')"
check "GET /api/v1/windows" 200 "$(http GET /api/v1/windows)"
# An idle headless desktop may have no focused window at all -> 404 is valid.
check_any "GET /api/v1/windows/active (200 or 404)" "200 404" "$(http GET /api/v1/windows/active)"

# ---------------------------------------------------------------------------
# 3. Keyboard: ensure a terminal window exists, focus it, type a command, then
#    verify the file it writes from inside the container.
# ---------------------------------------------------------------------------
echo "[3] keyboard -> xfce4-terminal -> file"
terminal_id=""
find_terminal() {
    local resp id
    resp="$(http GET /api/v1/windows)"
    LAST_BODY="$(printf '%s' "${resp}" | sed '$d')"
    id="$(printf '%s' "${LAST_BODY}" | python3 -c "
import json,sys
try: data=json.load(sys.stdin)
except Exception: raise SystemExit(0)
items = data.get('items', data) if isinstance(data, dict) else data
if isinstance(items, dict): items = items.get('windows', [])
def text(w):
    return ' '.join(str(w.get(k,'') or '') for k in ('title','class_name','instance_name','wm_class'))
hits = [w for w in items if 'terminal' in text(w).lower()]
print(hits[0]['id'] if hits else '')
" 2>/dev/null || true)"
    printf '%s' "${id}"
}

# XFCE's keyboard shortcut only exists if a distro default setting is installed;
# try it first, then fall back to launching xfce4-terminal inside the container.
check "POST /api/v1/keyboard/hotkey ctrl+alt+t" 200 "$(http POST /api/v1/keyboard/hotkey '{"keys":["ctrl","alt","t"]}')"
for _ in $(seq 1 10); do
    terminal_id="$(find_terminal)"
    [[ -n "${terminal_id}" ]] && break
    sleep 0.4
done
if [[ -z "${terminal_id}" ]]; then
    echo "        hotkey did not open a terminal; launching xfce4-terminal via docker exec"
    remote "docker exec -d ${CONTAINER} xfce4-terminal --title=NAD-E2E --geometry=120x30" || true
    for _ in $(seq 1 20); do
        terminal_id="$(find_terminal)"
        [[ -n "${terminal_id}" ]] && break
        sleep 0.4
    done
fi
if [[ -n "${terminal_id}" ]]; then
    PASS=$((PASS + 1))
    echo "  PASS  terminal window found: ${terminal_id}"
    check "POST /api/v1/windows/{id}/activate" 200 "$(http POST "/api/v1/windows/${terminal_id}/activate" '{}')"
else
    FAIL=$((FAIL + 1))
    FAILURES+=("no terminal window appeared after hotkey/fallback")
    echo "  FAIL  no terminal window appeared after hotkey/fallback"
fi

check "POST /api/v1/keyboard/type" 200 "$(http POST /api/v1/keyboard/type '{"text":"echo nextautodesk-e2e > /tmp/nad-e2e.txt\n"}')"
sleep 2
typed="$(remote "docker exec ${CONTAINER} cat /tmp/nad-e2e.txt 2>/dev/null" || true)"
if [[ "${typed}" == "nextautodesk-e2e" ]]; then
    PASS=$((PASS + 1))
    echo "  PASS  keyboard typing produced /tmp/nad-e2e.txt"
else
    FAIL=$((FAIL + 1))
    FAILURES+=("keyboard typing: /tmp/nad-e2e.txt contains '${typed}'")
    echo "  FAIL  keyboard typing: /tmp/nad-e2e.txt contains '${typed}'"
fi

# ---------------------------------------------------------------------------
# 4. Task queue: submit a two-step task, poll to completion, then verify cancel
# ---------------------------------------------------------------------------
echo "[4] task queue"
submit_resp="$(http POST /api/v1/tasks '{
  "name": "verify-e2e",
  "steps": [
    {"action": "mouse.move", "params": {"x":400, "y":300}},
    {"action": "screen.screenshot", "params": {"format": "png"}}
  ]
}')"
check "POST /api/v1/tasks (202 Accepted)" 202 "${submit_resp}" || true
task_id="$(json_get "d.get('id','')")"
if [[ -n "${task_id}" ]]; then
    task_status=""
    for _ in $(seq 1 60); do
        poll_resp="$(http GET "/api/v1/tasks/${task_id}")"
        LAST_BODY="$(printf '%s' "${poll_resp}" | sed '$d')"
        task_status="$(json_get "d.get('status')")"
        [[ "${task_status}" == "succeeded" || "${task_status}" == "failed" ]] && break
        sleep 0.5
    done
    if [[ "${task_status}" == "succeeded" ]]; then
        PASS=$((PASS + 1))
        echo "  PASS  task ${task_id} finished with status=succeeded"
    else
        FAIL=$((FAIL + 1))
        FAILURES+=("task ${task_id} status=${task_status}")
        echo "  FAIL  task ${task_id} status=${task_status}"
    fi
else
    FAIL=$((FAIL + 1))
    FAILURES+=("POST /api/v1/tasks returned no task id")
    echo "  FAIL  POST /api/v1/tasks returned no task id"
fi

cancel_resp="$(http POST /api/v1/tasks '{"name":"verify-cancel","steps":[{"action":"desktop.wait","params":{"ms":5000}}]}')"
LAST_BODY="$(printf '%s' "${cancel_resp}" | sed '$d')"
cancel_id="$(json_get "d.get('id','')")"
if [[ -n "${cancel_id}" ]]; then
    sleep 0.5
    cancel_result="$(http POST "/api/v1/tasks/${cancel_id}/cancel" '{}')"
    LAST_BODY="$(printf '%s' "${cancel_result}" | sed '$d')"
    cancel_status="$(json_get "d.get('status')")"
    cancel_http="$(printf '%s' "${cancel_result}" | tail -1)"
    if [[ "${cancel_http}" == "200" && "${cancel_status}" == "cancelled" ]]; then
        PASS=$((PASS + 1))
        echo "  PASS  cancelled task ${cancel_id}"
    else
        FAIL=$((FAIL + 1))
        FAILURES+=("cancel task ${cancel_id}: http=${cancel_http} status=${cancel_status}")
        echo "  FAIL  cancel task ${cancel_id}: http=${cancel_http} status=${cancel_status}"
    fi
else
    FAIL=$((FAIL + 1))
    FAILURES+=("cancel task submit returned no task id")
    echo "  FAIL  cancel task submit returned no task id"
fi

# ---------------------------------------------------------------------------
# 5. Perception tier (/elements) and composite tier (/interactions)
#
# Exercises the public HTTP API only. The desktop side effect is one additional
# maximised terminal window showing a known OCR-friendly line; the window is
# titled NAD-OCR so it can be found through the API.
# Template search is end-to-end: OCR a line -> crop its bounding box into the
# screenshot store -> find that stored template on screen and require the match
# to be within a few pixels of the OCR bbox.
# ---------------------------------------------------------------------------
if [[ "${SKIP_PERCEPTION}" == "true" ]]; then
    echo "[5] perception / composite tier: skipped (--skip-perception)"
else
echo "[5] perception / composite tier"

# Accept both request shapes used by the perception API during development:
# {"query": {...}} (action-parameter mirror) and a flattened {...} variant. A
# 422 on the nested shape is retried with the flattened one.
element_post() {
    local path="$1" query_json="$2" extra="${3:-}"
    local resp status flat
    resp="$(http POST "${path}" "{\"query\":${query_json}${extra}}")"
    status="$(printf '%s' "${resp}" | tail -1)"
    if [[ "${status}" == "422" ]]; then
        flat="{${query_json:1:-1}${extra}}"
        resp="$(http POST "${path}" "${flat}")"
    fi
    printf '%s' "${resp}"
}

# 5.1 discovery: locator availability, tier metadata, capabilities
check "GET /api/v1/elements/locators" 200 "$(http GET /api/v1/elements/locators)"
python_gate "locators: enabled with template+text available" '
import json, sys
d = json.load(sys.stdin)
locs = {item.get("name"): item for item in d.get("locators") or []}
assert d.get("enabled") is True, "perception disabled"
missing = [name for name in ("template", "text") if name not in locs]
assert not missing, "missing locators: " + repr(missing)
unavailable = [name for name in ("template", "text") if not locs[name].get("available")]
assert not unavailable, "unavailable locators: " + repr(unavailable)
versions = ", ".join("%s=%s" % (name, locs[name].get("version") or "?") for name in sorted(locs))
print(versions)
'

check "GET /api/v1/actions (tier metadata)" 200 "$(http GET /api/v1/actions)"
python_gate "action tiers: perception+composite populated" '
import json, sys
d = json.load(sys.stdin)
actions = set(d.get("actions_by_name") or [])
if not actions:
    actions = {item.get("name") for item in d.get("actions") or [] if isinstance(item, dict)}
tiers = d.get("tiers") or {}
need_perception = {"element.find", "element.wait", "element.ocr"}
need_composite = {"interaction.click", "interaction.type"}
missing_perception = sorted(need_perception - set(tiers.get("perception") or []))
missing_composite = sorted(need_composite - set(tiers.get("composite") or []))
assert not missing_perception, "missing from tiers.perception: " + repr(missing_perception)
assert not missing_composite, "missing from tiers.composite: " + repr(missing_composite)
assert need_perception <= actions, "element actions missing from catalogue"
assert need_composite <= actions, "interaction actions missing from catalogue"
print("perception=%d composite=%d primitive=%d" % (
    len(tiers.get("perception") or []), len(tiers.get("composite") or []), len(tiers.get("primitive") or [])))
'

check "GET /api/v1/info (perception capability)" 200 "$(http GET /api/v1/info)"
python_gate "capabilities: perception block + api tiers" '
import json, sys
d = json.load(sys.stdin)
caps = d.get("capabilities") if isinstance(d.get("capabilities"), dict) else {}
perception = d.get("perception") or caps.get("perception") or {}
features = d.get("features") or caps.get("features") or {}
assert perception, "no perception capability block"
assert perception.get("enabled") is True, "perception not enabled in capabilities"
locators = perception.get("locators") or []
assert locators, "perception.locators is empty"
tiers = features.get("api_tiers") or []
for expected in ("primitive", "perception", "composite", "workflow"):
    assert expected in tiers, "features.api_tiers missing " + expected
assert features.get("screen_element_perception") is True, "features.screen_element_perception missing"
print("locators=%d api_tiers=%s" % (len(locators), ",".join(tiers)))
'

# 5.2 create an OCR-friendly window with known large text
OCR_FIXTURE="NADE2E OCR 12345"
OCR_TEXT_B64="$(printf '%s\n' "${OCR_FIXTURE}" | base64 | tr -d '\n')"
OCR_WRAPPER_B64="$(printf '#!/bin/sh\ncat /home/nad/nad-ocr.txt\nexec sleep 900\n' | base64 | tr -d '\n')"
remote "docker exec ${CONTAINER} sh -c 'echo ${OCR_TEXT_B64} | base64 -d > /home/nad/nad-ocr.txt'" \
    || echo "        warning: could not write the OCR fixture inside the container"
remote "docker exec ${CONTAINER} sh -c 'echo ${OCR_WRAPPER_B64} | base64 -d > /home/nad/nad-ocr.sh && chmod 0755 /home/nad/nad-ocr.sh'" \
    || echo "        warning: could not write the OCR helper script inside the container"
remote "docker exec -d ${CONTAINER} xfce4-terminal --title=NAD-OCR --font='DejaVu Sans Mono 22' --maximize --command=/home/nad/nad-ocr.sh" \
    || echo "        warning: could not launch the OCR terminal window"

ocr_window_id=""
for _ in $(seq 1 20); do
    ocr_windows_resp="$(http GET /api/v1/windows)"
    ocr_windows_body="$(printf '%s' "${ocr_windows_resp}" | sed '$d')"
    ocr_window_id="$(printf '%s' "${ocr_windows_body}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
items = data.get("windows") if isinstance(data, dict) else data
items = items or []
hits = [item for item in items if "NAD-OCR" in str(item.get("title") or "")]
print(hits[0].get("id", "") if hits else "")
' 2>/dev/null || true)"
    [[ -n "${ocr_window_id}" ]] && break
    sleep 0.5
done
if [[ -n "${ocr_window_id}" ]]; then
    pass "OCR window appeared (${ocr_window_id})"
    check "POST /api/v1/windows/{id}/activate (OCR window)" 200 \
        "$(http POST "/api/v1/windows/${ocr_window_id}/activate" '{}')"
else
    fail "OCR window NAD-OCR did not appear; OCR/template checks will likely fail"
fi
sleep 0.5

# 5.3 OCR the screen and pick the fixture line
ocr_resp="$(http POST /api/v1/elements/ocr '{"languages":["eng"],"psm":6,"min_confidence":40}')"
check "POST /api/v1/elements/ocr" 200 "${ocr_resp}"
OCR_PICK="$(printf '%s' "${LAST_BODY}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
lines = data.get("lines") or []
candidates = []
for line in lines:
    text = (line.get("text") or "").strip()
    box = line.get("box") or {}
    center = line.get("center") or {}
    width = int(box.get("width") or 0)
    height = int(box.get("height") or 0)
    if not text or len(text) < 6 or width < 80 or height < 10:
        continue
    candidates.append((
        text,
        int(box.get("x") or 0), int(box.get("y") or 0), width, height,
        int(center.get("x") or 0), int(center.get("y") or 0),
        float(line.get("confidence") or 0.0),
    ))
if not candidates:
    print("ERROR:no usable OCR line; text=" + repr((data.get("text") or "")[:160]))
    raise SystemExit(1)
target = next((item for item in candidates if "NADE2E" in item[0].upper()), None)
if target is None:
    target = max(candidates, key=lambda item: (item[7], item[3] * item[4]))
print("|".join(str(value) for value in target))
' 2>&1 || true)"
if [[ "${OCR_PICK}" == ERROR:* ]]; then
    fail "OCR fixture text not recognised (${OCR_PICK#ERROR:})"
    OCR_TEXT=""
    OCR_X=0; OCR_Y=0; OCR_W=0; OCR_H=0; OCR_CX=0; OCR_CY=0; OCR_CONF=0
else
    IFS='|' read -r OCR_TEXT OCR_X OCR_Y OCR_W OCR_H OCR_CX OCR_CY OCR_CONF <<<"${OCR_PICK}"
    if [[ "${OCR_TEXT^^}" == *NADE2E* ]]; then
        pass "OCR found fixture text: '${OCR_TEXT}' (confidence ${OCR_CONF}, box ${OCR_X},${OCR_Y} ${OCR_W}x${OCR_H})"
    else
        fail "OCR line does not contain the fixture token: '${OCR_TEXT}'"
    fi
fi
export SCREEN_W SCREEN_H OCR_X OCR_Y OCR_W OCR_H OCR_CX OCR_CY

if [[ -n "${OCR_TEXT}" ]]; then
    # 5.4 store the OCR line bbox as a screenshot template, then find it again
    TEMPLATE_PAD=4
    TX=$((OCR_X - TEMPLATE_PAD)); ((TX < 0)) && TX=0
    TY=$((OCR_Y - TEMPLATE_PAD)); ((TY < 0)) && TY=0
    TW=$((OCR_W + 2 * TEMPLATE_PAD)); ((TX + TW > SCREEN_W)) && TW=$((SCREEN_W - TX))
    TH=$((OCR_H + 2 * TEMPLATE_PAD)); ((TY + TH > SCREEN_H)) && TH=$((SCREEN_H - TY))
    shot_resp="$(http POST /api/v1/screen/screenshot \
        "{\"region\":{\"x\":${TX},\"y\":${TY},\"width\":${TW},\"height\":${TH}},\"format\":\"png\",\"save\":true}")"
    check "POST /api/v1/screen/screenshot (template crop ${TW}x${TH})" 200 "${shot_resp}"
    TEMPLATE_NAME="$(json_get "d.get('screenshot',{}).get('name','')")"
    TEMPLATE_INFO="$(json_get "d.get('screenshot',{}).get('width'),d.get('screenshot',{}).get('height')")"
    if [[ "${TEMPLATE_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
        pass "stored template ${TEMPLATE_NAME} (${TEMPLATE_INFO})"
        template_probe="$(remote "docker exec ${CONTAINER} /opt/venv/bin/python -c \"from PIL import Image; im=Image.open('/data/screenshots/${TEMPLATE_NAME}'); print(im.width, im.height, im.mode)\"" 2>/dev/null || true)"
        if [[ -n "${template_probe}" ]]; then
            pass "template file readable inside the container: ${template_probe}"
        else
            fail "template file /data/screenshots/${TEMPLATE_NAME} is not readable with Pillow inside the container"
        fi
    else
        fail "template screenshot was not saved (response: $(printf '%s' "${LAST_BODY}" | head -c 200))"
        TEMPLATE_NAME=""
    fi

    SEARCH_PAD=120
    SX=$((OCR_X - SEARCH_PAD)); ((SX < 0)) && SX=0
    SY=$((OCR_Y - SEARCH_PAD)); ((SY < 0)) && SY=0
    SW=$((OCR_W + 2 * SEARCH_PAD)); ((SX + SW > SCREEN_W)) && SW=$((SCREEN_W - SX))
    SH=$((OCR_H + 2 * SEARCH_PAD)); ((SY + SH > SCREEN_H)) && SH=$((SCREEN_H - SY))
    SEARCH_REGION="{\"x\":${SX},\"y\":${SY},\"width\":${SW},\"height\":${SH}}"

    if [[ -n "${TEMPLATE_NAME}" ]]; then
        template_query="{\"locator\":\"template\",\"template\":{\"screenshot\":\"${TEMPLATE_NAME}\",\"threshold\":0.6,\"grayscale\":true},\"region\":${SEARCH_REGION},\"max_results\":5}"
        find_resp="$(element_post /api/v1/elements/find "${template_query}")"
        check "POST /api/v1/elements/find (stored template)" 200 "${find_resp}"
        python_gate "template match found near the OCR bbox" '
import json, os, sys
data = json.load(sys.stdin)
matches = data.get("matches") or []
assert matches, "no template matches in " + str(data)[:200]
match = matches[0]
assert match.get("locator") == "template", "unexpected locator " + repr(match.get("locator"))
center = match.get("center") or {}
dx = abs(int(center.get("x") or 0) - int(os.environ["OCR_CX"]))
dy = abs(int(center.get("y") or 0) - int(os.environ["OCR_CY"]))
assert dx <= 30 and dy <= 30, "match too far from OCR bbox: dx=%d dy=%d center=%r ocr=(%s,%s)" % (
    dx, dy, center, os.environ["OCR_CX"], os.environ["OCR_CY"])
print("center=(%s,%s) confidence=%.3f offset=(%d,%d)" % (
    center.get("x"), center.get("y"), float(match.get("confidence") or 0.0), dx, dy))
'
    fi

    # 5.5 find the same text through the text locator
    text_query="{\"locator\":\"text\",\"text\":{\"text\":\"NADE2E\",\"match\":\"contains\",\"case_sensitive\":false,\"languages\":[\"eng\"],\"psm\":6,\"min_confidence\":30,\"granularity\":\"line\"},\"region\":${SEARCH_REGION},\"max_results\":5}"
    text_resp="$(element_post /api/v1/elements/find "${text_query}")"
    check "POST /api/v1/elements/find (text NADE2E)" 200 "${text_resp}"
    python_gate "text match found near the OCR bbox" '
import json, os, sys
data = json.load(sys.stdin)
matches = data.get("matches") or []
assert matches, "no text matches in " + str(data)[:200]
match = matches[0]
words = [str(item.get("text") or "") for item in matches]
assert any("NADE2E" in word.upper() for word in words), "matches do not contain NADE2E: " + repr(words[:5])
center = match.get("center") or {}
dx = abs(int(center.get("x") or 0) - int(os.environ["OCR_CX"]))
dy = abs(int(center.get("y") or 0) - int(os.environ["OCR_CY"]))
assert dx <= 40 and dy <= 40, "text match too far from OCR bbox: dx=%d dy=%d center=%r" % (dx, dy, center)
print("center=(%s,%s) confidence=%.3f text=%r" % (
    center.get("x"), center.get("y"), float(match.get("confidence") or 0.0), match.get("text")))
'

    # 5.6 composite tier: click the located text through /interactions/click
    click_body="{\"query\":${text_query},\"button\":\"left\",\"clicks\":1,\"wait_timeout_ms\":5000}"
    click_resp="$(http POST /api/v1/interactions/click "${click_body}")"
    check "POST /api/v1/interactions/click (text target)" 200 "${click_resp}"
    python_gate "interaction.click reused element.wait + mouse.click" '
import json, os, sys
data = json.load(sys.stdin)
assert data.get("success") is True, "success is not true: " + str(data)[:200]
output = data.get("output") or {}
target = output.get("target") or {}
position = output.get("position") or {}
assert target.get("locator") == "text", "unexpected target locator " + repr(target.get("locator"))
assert isinstance(output.get("click"), dict) and output["click"], "mouse.click output missing"
attempts = int(output.get("attempts") or 0)
assert attempts >= 1, "attempts < 1"
dx = abs(int(position.get("x") or 0) - int(os.environ["OCR_CX"]))
dy = abs(int(position.get("y") or 0) - int(os.environ["OCR_CY"]))
assert dx <= 40 and dy <= 40, "click position too far from OCR bbox: dx=%d dy=%d" % (dx, dy)
print("clicked (%s,%s) target_text=%r attempts=%d" % (
    position.get("x"), position.get("y"), target.get("text"), attempts))
'

    # 5.7 negative checks
    if [[ "${MODE}" == "local" ]]; then
        unauth_elements="$(curl -sS -m 10 -w '\n%{http_code}' \
            "http://${HTTP_HOST}:${PORT}/api/v1/elements/locators")"
    else
        unauth_elements="$(remote "curl -sS -m 10 -w '\\n%{http_code}' 'http://127.0.0.1:${PORT}/api/v1/elements/locators'")"
    fi
    check "GET /elements/locators without key -> 401" 401 "${unauth_elements}"

    if [[ "${MODE}" == "local" ]]; then
        unauth_find="$(printf '%s' '{}' | curl -sS -m 10 -w '\n%{http_code}' \
            -X POST -H 'Content-Type: application/json' --data-binary @- \
            "http://${HTTP_HOST}:${PORT}/api/v1/elements/find")"
    else
        unauth_find="$(printf '%s' '{}' | remote "curl -sS -m 10 -w '\\n%{http_code}' -X POST -H 'Content-Type: application/json' --data-binary @- 'http://127.0.0.1:${PORT}/api/v1/elements/find'")"
    fi
    check "POST /elements/find without key -> 401" 401 "${unauth_find}"

    missing_query="{\"locator\":\"text\",\"text\":{\"text\":\"ZZN0SUCHTEXT9F3A\",\"match\":\"contains\",\"languages\":[\"eng\"]},\"region\":${SEARCH_REGION}}"
    wait_resp="$(element_post /api/v1/elements/wait "${missing_query}" ',"timeout_ms":400,"poll_interval_ms":100')"
    check "POST /api/v1/elements/wait missing element -> 404" 404 "${wait_resp}"
    python_gate "wait error is ELEMENT_NOT_FOUND" '
import json, sys
data = json.load(sys.stdin)
error = data.get("error") or {}
code = error.get("code") or ""
assert code == "ELEMENT_NOT_FOUND", "unexpected error code " + repr(code)
print("code=ELEMENT_NOT_FOUND message=%r" % (error.get("message") or "")[:120])
'
fi

fi

# ---------------------------------------------------------------------------
# 6. Runtime footprint and cleanliness
# ---------------------------------------------------------------------------
echo "[6] runtime footprint"
remote "docker ps --filter name=nextautodesk --format 'container: {{.Names}} {{.Status}}'" | sed 's/^/        /' || true
remote "docker stats --no-stream --format 'cpu={{.CPUPerc}} mem={{.MemUsage}}' ${CONTAINER}" | sed 's/^/        /' || true
remote "docker exec ${CONTAINER} sh -c 'ps -eo comm | sort | uniq -c | sort -rn | head -8'" | sed 's/^/        /' || true
remote "docker exec ${CONTAINER} xdpyinfo | grep -E 'dimensions|depth of root' | head -2" | sed 's/^/        /' || true
remote "docker exec ${CONTAINER} sh -c 'command -v x11vnc >/dev/null && echo \"VNC INSTALLED\" || echo \"VNC not installed (reserved)\"'" | sed 's/^/        /' || true

# Deployment acceptance: the container must be running and healthy with no
# restarts and no OOM kill (a supervisord process that died silently would keep
# the container "up" while the API is unusable, so this is checked explicitly).
container_state="$(remote "docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}} {{.State.OOMKilled}}' ${CONTAINER}" 2>/dev/null || true)"
c_status=""
c_health=""
c_restarts=""
c_oom=""
read -r c_status c_health c_restarts c_oom <<<"${container_state}" || true
if [[ "${c_status}" == "running" && "${c_health}" == "healthy" \
    && "${c_restarts}" == "0" && "${c_oom}" == "false" ]]; then
    PASS=$((PASS + 1))
    echo "  PASS  container ${CONTAINER} running/healthy (restarts=0, oom=false)"
else
    FAIL=$((FAIL + 1))
    FAILURES+=("container ${CONTAINER}: status=${c_status:-unknown} health=${c_health:-unknown} restarts=${c_restarts:-unknown} oom=${c_oom:-unknown}")
    echo "  FAIL  container ${CONTAINER}: status=${c_status:-unknown} health=${c_health:-unknown} restarts=${c_restarts:-unknown} oom=${c_oom:-unknown}"
fi

# ---------------------------------------------------------------------------
# Fixture cleanup (perception E2E writes a temporary OCR window/helper)
# ---------------------------------------------------------------------------
# Close the window first (the wrapper ends in ``exec sleep``, so its argv no
# longer names the script), then kill any leftover helper.  The bracket in
# "[n]ad-ocr.sh" plus the separate ``rm`` exec keep ``pkill -f`` from matching
# the shell that runs it: a combined command line would contain the literal
# ``/home/nad/nad-ocr.sh`` from the ``rm`` arguments and SIGTERM itself before
# cleaning anything up.
remote "docker exec ${CONTAINER} sh -c 'wmctrl -c \"NAD-OCR\" >/dev/null 2>&1 || true'" >/dev/null 2>&1 || true
remote "docker exec ${CONTAINER} sh -c 'pkill -f \"[n]ad-ocr.sh\" >/dev/null 2>&1 || true'" >/dev/null 2>&1 || true
remote "docker exec ${CONTAINER} sh -c 'rm -f /home/nad/nad-ocr.txt /home/nad/nad-ocr.sh'" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
if ((FAIL == 0)); then
    echo "verify: ALL ${PASS} CHECKS PASSED"
    exit 0
fi
echo "verify: ${FAIL} FAILED, ${PASS} passed"
printf '  - %s\n' "${FAILURES[@]}"
exit 1
