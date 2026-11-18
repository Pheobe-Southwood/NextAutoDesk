#!/bin/sh
# Container health probe (used by the Docker HEALTHCHECK / compose healthcheck).
#
# Probes /readyz (X11 display + serial executor + writable data directory), not
# /healthz: /healthz is a deliberately cheap liveness probe that never touches
# the desktop, so a dead X server or a stopped executor would otherwise leave
# the container marked healthy while it cannot accept any work.
#
# The probe runs in its own process with the *container* environment; the
# effective API port is exported by docker/entrypoint.sh only for the supervised
# processes, so it also publishes the port in /tmp/nad-api-port. Fall back to the
# default port when the marker is absent (e.g. `docker run image /bin/bash`).
set -eu

port="${NEXT_AUTODESK_PORT:-}"
if [ -z "${port}" ] && [ -r /tmp/nad-api-port ]; then
    port="$(cat /tmp/nad-api-port 2>/dev/null || true)"
fi
[ -n "${port}" ] || port=8765

exec curl -fsS --max-time 4 "http://127.0.0.1:${port}/readyz" >/dev/null
