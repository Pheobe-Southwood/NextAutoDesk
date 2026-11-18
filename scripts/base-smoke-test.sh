#!/usr/bin/env bash
# Validate the Dockerfile "base" stage in isolation: Ubuntu 24.04 + Xvfb + XFCE +
# X11 tools + Python virtualenv (PyAutoGUI, human-movement, optional strategy
# packages). Useful while the application code is still being written, and as a
# fast regression test for the desktop stack.
#
#   ./scripts/base-smoke-test.sh [IMAGE]
#
# Default IMAGE: nextautodesk:base (build it with
#   ./scripts/build-image.sh --target base
# )

set -euo pipefail

readonly DOCKER="${DOCKER:-docker}"
IMAGE="${1:-${IMAGE:-nextautodesk:base}}"

if ! "${DOCKER}" image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "base-smoke-test: image '${IMAGE}' not found; build it with ./scripts/build-image.sh --target base" >&2
    exit 2
fi

echo "base-smoke-test: validating desktop stack in image ${IMAGE}"

set +e
"${DOCKER}" run --rm -i --name "nad-base-smoke-$$" \
    --shm-size=256m \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    "${IMAGE}" \
    bash -s <<'CONTAINER_SCRIPT'
set -uo pipefail

export DISPLAY=:99
failures=0
ok()   { printf '  [OK]   %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; failures=$((failures + 1)); }
info() { printf '  [info] %s\n' "$*"; }
section() { printf '\n== %s ==\n' "$*"; }

cleanup() {
    pkill -P $$ >/dev/null 2>&1 || true
    pkill -f 'xfce4-session|xfwm4|xfdesktop|xfce4-panel|dbus-run-session' >/dev/null 2>&1 || true
    kill $xvfb_pid >/dev/null 2>&1 || true
}
trap cleanup EXIT

section "Xvfb"
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp -dpi 96 >/tmp/xvfb.log 2>&1 &
xvfb_pid=$!
for _ in $(seq 1 100); do xdpyinfo >/dev/null 2>&1 && break; sleep 0.1; done
if xdpyinfo >/tmp/xdpyinfo.out 2>&1; then
    dims=$(awk '/dimensions:/ {print $2; exit}' /tmp/xdpyinfo.out)
    [[ "$dims" == "1920x1080" ]] && ok "Xvfb screen ${dims} pixels" || fail "unexpected screen: $dims"
    depth=$(awk '/depth of root window:/ {print $5; exit}' /tmp/xdpyinfo.out)
    [[ "$depth" == "24" ]] && ok "root window depth 24" || fail "unexpected depth: ${depth:-unknown}"
else
    fail "Xvfb did not come up"; cat /tmp/xvfb.log; exit 1
fi

section "XFCE session (dbus-run-session startxfce4)"
dbus-run-session -- startxfce4 >/tmp/xfce.log 2>&1 &
for _ in $(seq 1 300); do wmctrl -m >/dev/null 2>&1 && break; sleep 0.1; done
if wmctrl -m >/tmp/wmctrl.out 2>&1; then
    wm_name=$(awk '/Name:/ {print $2; exit}' /tmp/wmctrl.out)
    ok "window manager: ${wm_name}"
else
    fail "no EWMH window manager after 30s"; tail -30 /tmp/xfce.log
fi
for _ in $(seq 1 150); do pgrep -x xfdesktop >/dev/null 2>&1 && break; sleep 0.1; done
for _ in $(seq 1 100); do wmctrl -l >/dev/null 2>&1 && break; sleep 0.1; done
wmctrl -l >/dev/null 2>&1 && ok "wmctrl can list windows" || fail "wmctrl -l failed"
pgrep -x xfwm4 >/dev/null && ok "xfwm4 process running" || fail "xfwm4 not running"
pgrep -x xfdesktop >/dev/null && ok "xfdesktop process running" || fail "xfdesktop not running"
pgrep -x xfce4-session >/dev/null && ok "xfce4-session process running" || fail "xfce4-session not running"

section "X11 command-line tools"
xdpyinfo >/dev/null 2>&1 && ok "xdpyinfo" || fail "xdpyinfo"
xset q >/dev/null 2>&1 && ok "xset" || fail "xset"
xdotool getmouselocation >/dev/null 2>&1 && ok "xdotool getmouselocation" || fail "xdotool getmouselocation"
xdotool mousemove 400 300 && sleep 0.2
loc=$(xdotool getmouselocation)
[[ "$loc" == *"x:400"* && "$loc" == *"y:300"* ]] && ok "xdotool mousemove works ($loc)" || fail "xdotool mousemove: $loc"
wmctrl -m >/dev/null 2>&1 && ok "wmctrl" || fail "wmctrl"
scrot /tmp/base-smoke.png >/dev/null 2>&1 && [[ -s /tmp/base-smoke.png ]] \
    && ok "scrot screenshot ($(stat -c%s /tmp/base-smoke.png) bytes)" \
    || fail "scrot screenshot"

section "Supervisor"
supervisord --version >/dev/null 2>&1 && ok "supervisord $(supervisord --version)" || fail "supervisord"
command -v supervisorctl >/dev/null 2>&1 && ok "supervisorctl present" || fail "supervisorctl"

section "Locale / fonts"
[[ "$(locale charmap 2>/dev/null)" == "UTF-8" ]] && ok "locale charmap UTF-8" || fail "locale is not UTF-8"
fc-list >/tmp/fc-list.out 2>/dev/null || true
grep -qi 'dejavu' /tmp/fc-list.out && ok "DejaVu fonts installed" || fail "DejaVu fonts missing"
grep -qi 'wenquanyi' /tmp/fc-list.out && ok "CJK font (WenQuanYi Micro Hei) installed" || fail "CJK font missing"

section "Python virtualenv / PyAutoGUI / human-movement"
python_rc=0
/opt/venv/bin/python - <<'PY' || python_rc=$?
import importlib.metadata as md
import sys

failures = 0
def check(name, fn):
    global failures
    try:
        detail = fn()
        print(f"  [OK]   {name}" + (f": {detail}" if detail else ""))
    except Exception as exc:  # noqa: BLE001 - smoke test reports everything
        print(f"  [FAIL] {name}: {exc!r}")
        failures += 1

print(f"  [info] python {sys.version.split()[0]}")
check("pyautogui import", lambda: __import__("pyautogui").__version__)
check("python-xlib import", lambda: __import__("Xlib").__file__)
check("human_movement import", lambda: md.version("human-movement"))
for dist in ("HumanMoveMouse", "windmouse"):
    try:
        version = md.version(dist)
    except md.PackageNotFoundError:
        print(f"  [info] {dist} not installed (optional strategies disabled)")
    else:
        check(f"{dist} installed", lambda v=version: v)
import pyautogui
check("pyautogui screen size", lambda: tuple(pyautogui.size()))
check("pyautogui position", lambda: tuple(pyautogui.position()))
pyautogui.moveTo(640, 480, duration=0.2)
pos = pyautogui.position()
check("pyautogui moveTo", lambda: tuple(pos))
if tuple(pos) != (640, 480):
    print(f"  [FAIL] pyautogui moveTo did not land: {tuple(pos)}")
    failures += 1
from PIL import Image
with Image.open("/tmp/base-smoke.png") as image:
    print(f"  [OK]   screenshot decoded by Pillow: {image.size} {image.mode}")
    if image.size != (1920, 1080):
        print(f"  [FAIL] screenshot size {image.size} != (1920, 1080)")
        failures += 1
sys.exit(1 if failures else 0)
PY
[[ ${python_rc} -eq 0 ]] && ok "Python/PyAutoGUI/human-movement checks" || fail "Python/PyAutoGUI/human-movement checks"

section "VNC reserved (must be absent in the default image)"
if command -v x11vnc >/dev/null 2>&1; then
    fail "x11vnc is installed but VNC must be reserved/disabled by default"
else
    ok "no VNC server installed"
fi

section "Result"
if [[ ${failures} -eq 0 ]]; then
    echo "  base stage smoke test: PASS"
    exit 0
fi
echo "  base stage smoke test: FAIL (${failures} failure(s))"
exit 1
CONTAINER_SCRIPT
rc=$?
set -e

echo
if [[ ${rc} -eq 0 ]]; then
    echo "base-smoke-test: PASS (${IMAGE})"
else
    echo "base-smoke-test: FAIL (${IMAGE}, exit ${rc})" >&2
fi
exit "${rc}"
