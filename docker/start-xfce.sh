#!/usr/bin/env bash
# Start the XFCE desktop session inside the container.
#
# Supervised by supervisord (program "xfce"). Waits for the Xvfb display started
# by supervisord's higher-priority "xvfb" program, disables screen blanking/DPMS
# so screenshots never go black, and runs a clean XFCE session under a private
# D-Bus session bus (dbus-launch would leak; dbus-run-session exits with XFCE).

set -euo pipefail

log() {
    printf '%s [start-xfce] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

: "${DISPLAY:?DISPLAY is not set}"
timeout_s="${NAD_XFCE_X_TIMEOUT_S:-60}"

# ---------------------------------------------------------------------------
# Wait for the X server (Xvfb is started by supervisord just before us).
# ---------------------------------------------------------------------------
deadline=$((SECONDS + timeout_s))
until xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
        log "X server ${DISPLAY} not reachable after ${timeout_s}s"
        exit 1
    fi
    sleep 0.2
done
log "X server ${DISPLAY} is ready: $(xdpyinfo -display "${DISPLAY}" | awk '/dimensions:/ {print $2; exit}')"

# ---------------------------------------------------------------------------
# Headless display hygiene: no blanking, no DPMS power saving, no beeps.
# ---------------------------------------------------------------------------
xset -display "${DISPLAY}" s off 2>/dev/null || true
xset -display "${DISPLAY}" -dpms 2>/dev/null || true
xset -display "${DISPLAY}" -b 2>/dev/null || true

# Software rendering only: the container has no GPU/DRI device.
export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}"

# A headless node always starts from a pristine desktop: drop any saved-session
# cache left behind by an unclean stop so xfce4-session cannot try to restore
# windows that no longer exist.
rm -rf "${HOME}/.cache/sessions" 2>/dev/null || true

log "starting XFCE session (dbus-run-session startxfce4) on ${DISPLAY}"
exec dbus-run-session -- startxfce4
