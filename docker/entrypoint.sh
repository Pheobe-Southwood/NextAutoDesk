#!/usr/bin/env bash
# NextAutoDesk container entrypoint.
#
# 1. Resolves the deployment configuration and validates it (fail fast, before
#    any process starts).
# 2. Derives the display/screen/data environment from the *effective*
#    configuration, so Xvfb, XFCE and the API always agree.
# 3. Prepares the persistent /data layout and the XDG runtime directory.
# 4. Executes the container command (supervisord by default).
#
# It intentionally does not touch the X socket: Xvfb is itself a supervised
# process started after this entrypoint, and the XFCE/API wrappers
# (start-xfce.sh, start-api.sh via wait-for-desktop.sh) wait for it. Stale socket
# and lock files are removed here.
#
# Runs as the non-root "nad" user; see docker/Dockerfile.

set -euo pipefail

readonly APP_HOME="${APP_HOME:-/app}"
readonly RUNTIME_HOME="${NAD_RUNTIME_HOME:-/opt/nad}"
readonly VENV_PYTHON="${NAD_VENV_PYTHON:-/opt/venv/bin/python}"

log() {
    printf '%s [entrypoint] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

die() {
    log "ERROR: $*"
    exit 78 # EX_CONFIG
}

# ---------------------------------------------------------------------------
# Full stack mode? (default CMD is supervisord; any other command - e.g. a
# debugging shell - only gets the basic environment, not config validation)
# ---------------------------------------------------------------------------
is_stack=false
case "${1:-}" in
    supervisord | /usr/bin/supervisord) is_stack=true ;;
esac

# ---------------------------------------------------------------------------
# 1. Resolve + validate the configuration (stack mode only)
# ---------------------------------------------------------------------------
if [[ "${is_stack}" == "true" ]]; then
    if [[ -n "${NEXT_AUTODESK_CONFIG:-}" ]]; then
        config_file="${NEXT_AUTODESK_CONFIG}"
    elif [[ -f "${APP_HOME}/config/nextautodesk.yml" ]]; then
        config_file="${APP_HOME}/config/nextautodesk.yml"
    else
        # Fallback for deployments that mount an empty directory over /app/config.
        config_file="${RUNTIME_HOME}/config/nextautodesk.yml"
    fi
    [[ -f "${config_file}" ]] || die "configuration file not found: ${config_file} (set NEXT_AUTODESK_CONFIG)"
    [[ -r "${config_file}" ]] || die "configuration file is not readable by uid $(id -u): ${config_file}"
    export NEXT_AUTODESK_CONFIG="${config_file}"

    if ! config_json="$("${VENV_PYTHON}" -m nextautodesk config validate \
            --config "${config_file}" --json)"; then
        die "configuration validation failed for ${config_file}; see the error above"
    fi

    # Export the effective display/screen/storage/server values so Xvfb, the
    # XFCE session and the API process share one consistent environment.
    eval "$(printf '%s' "${config_json}" | "${VENV_PYTHON}" "${RUNTIME_HOME}/config-to-env.py")"

    DISPLAY="${NEXT_AUTODESK_DISPLAY:-${DISPLAY:-:99}}"
    export DISPLAY
    case "${DISPLAY}" in
        :[0-9]*) ;;
        *) die "desktop.display must look like ':99', got '${DISPLAY}'" ;;
    esac
    log "config OK: ${config_file}"
    log "desktop:   display=${DISPLAY} screen=${NAD_SCREEN_SIZE} dpi=${NAD_DPI}"
    log "api:       ${NEXT_AUTODESK_HOST}:${NEXT_AUTODESK_PORT}"
    log "data:      ${NEXT_AUTODESK_DATA_DIR}"

    # Docker HEALTHCHECK/compose probes run in their own process with the
    # *container* environment, not with the exports made here; publish the
    # effective API port for docker/healthcheck.sh.
    if ! printf '%s\n' "${NEXT_AUTODESK_PORT}" >/tmp/nad-api-port 2>/dev/null; then
        log "WARN: cannot write /tmp/nad-api-port; health probe will use the default port"
    fi

    # Remove stale X lock/socket files left behind by an unclean container stop.
    display_number="${DISPLAY#:}"
    display_number="${display_number%%.*}"
    export NAD_DISPLAY_NUMBER="${display_number}"
    rm -f "/tmp/.X${display_number}-lock" "/tmp/.X11-unix/X${display_number}" 2>/dev/null || true

    data_dir="${NEXT_AUTODESK_DATA_DIR}"
else
    DISPLAY="${NEXT_AUTODESK_DISPLAY:-${DISPLAY:-:99}}"
    export DISPLAY
    data_dir="${NEXT_AUTODESK_DATA_DIR:-/data}"
fi

# ---------------------------------------------------------------------------
# 2. Persistent data layout (bind-mounted volume; must be writable by this uid)
# ---------------------------------------------------------------------------
export NEXT_AUTODESK_DATA_DIR="${data_dir}"
if ! mkdir -p "${data_dir}/logs" "${data_dir}/screenshots"; then
    die "cannot create data directories under ${data_dir} (uid=$(id -u) gid=$(id -g)); fix the host directory ownership, e.g. 'chown -R $(id -u):$(id -g) <host-data-dir>'"
fi
if [[ ! -w "${data_dir}" ]]; then
    die "data directory ${data_dir} is not writable by uid=$(id -u); fix the host directory ownership, e.g. 'chown -R $(id -u):$(id -g) <host-data-dir>'"
fi

# ---------------------------------------------------------------------------
# 3. XDG runtime/config/cache directories inside the user's home
# ---------------------------------------------------------------------------
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-${HOME}/.xdg-runtime}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-${HOME}/.cache}"
mkdir -p "${XDG_RUNTIME_DIR}" "${XDG_CONFIG_HOME}" "${XDG_CACHE_HOME}"
chmod 0700 "${XDG_RUNTIME_DIR}"

# ---------------------------------------------------------------------------
# 4. Hand over to the container command (supervisord by default)
# ---------------------------------------------------------------------------
log "exec: $*"
exec "$@"
