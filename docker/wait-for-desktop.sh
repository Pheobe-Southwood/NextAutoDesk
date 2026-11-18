#!/usr/bin/env bash
# Wait until the graphical desktop is actually usable:
#   1. the X server answers on $DISPLAY,
#   2. an EWMH-compliant window manager (xfwm4) answers wmctrl.
#
# Used by start-api.sh so the HTTP API does not come up before the desktop - the
# application's health checks require a working window manager.
#
# Usage: wait-for-desktop.sh [timeout-seconds]

set -euo pipefail

log() {
    printf '%s [wait-for-desktop] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

: "${DISPLAY:?DISPLAY is not set}"
timeout_s="${1:-${NAD_DESKTOP_TIMEOUT_S:-120}}"
deadline=$((SECONDS + timeout_s))

# ---------------------------------------------------------------------------
# 1. X server
# ---------------------------------------------------------------------------
until xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
        log "X server ${DISPLAY} not reachable after ${timeout_s}s"
        exit 1
    fi
    sleep 0.2
done

# ---------------------------------------------------------------------------
# 2. Window manager (EWMH)
# ---------------------------------------------------------------------------
until wmctrl -m >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
        log "no EWMH window manager on ${DISPLAY} after ${timeout_s}s"
        exit 1
    fi
    sleep 0.2
done

log "desktop ready: $(wmctrl -m | awk '/Name:/ {print $2; exit}') on ${DISPLAY}"
