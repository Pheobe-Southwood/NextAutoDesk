#!/usr/bin/env bash
# Start the NextAutoDesk HTTP API (supervisord program "api").
#
# Waits for the Xvfb/XFCE desktop to be usable first (the application's health
# checks require a working window manager), then replaces this shell with the
# uvicorn process so supervisord supervises the API directly.
#
# Console logging: supervisord leaves the API's stdout/stderr attached to its own
# stdout, so `docker logs` shows it live; the application writes rotating
# structured logs to $NEXT_AUTODESK_DATA_DIR/logs/ itself.

set -euo pipefail

log() {
    printf '%s [start-api] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

: "${NEXT_AUTODESK_CONFIG:?NEXT_AUTODESK_CONFIG is not set (entrypoint should export it)}"

"/opt/nad/wait-for-desktop.sh" "${NAD_API_DESKTOP_TIMEOUT_S:-120}"

log "starting API with ${NEXT_AUTODESK_CONFIG}"
exec /opt/venv/bin/python -m nextautodesk serve --config "${NEXT_AUTODESK_CONFIG}"
