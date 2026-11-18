#!/usr/bin/env bash
# Deploy NextAutoDesk to a remote Ubuntu host over SSH.
#
# The image is normally built on the *local* machine (see scripts/build-image.sh)
# and streamed to the target with `docker save | gzip | ssh docker load`, so the
# server never compiles anything and never needs network access to a registry.
# Alternatively build directly on the target with deploy/vps/build-on-host.sh
# (--host mode already redeploys); this script also accepts --image to deploy an
# image reference that was built/tagged on the host itself.
#
# The image tag and OCI version are derived from pyproject.toml by default (PEP
# 440 1.2.3a1 -> docker tag 1.2.3-alpha1) and written to the deployment .env as
# NAD_IMAGE_TAG, which deploy/docker-compose.yml consumes.
#
# The target host has no default: pass --host explicitly (see --help).
# The script is idempotent: an existing .env/API key and configuration overlay
# are preserved. Re-running it transfers a new image and restarts the container.
#
# Target layout (all state lives in one directory):
#   /opt/nextautodesk/docker-compose.yml
#   /opt/nextautodesk/.env                     (chmod 600, API key)
#   /opt/nextautodesk/config/nextautodesk.yml  (read-only overlay, optional)
#   /opt/nextautodesk/data/                    (SQLite DB, screenshots, logs; uid 1000)
set -euo pipefail

readonly REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REMOTE_DIR="/opt/nextautodesk"
# shellcheck source=deploy/vps/_common.sh
source "${REPO_ROOT}/deploy/vps/_common.sh"
IMAGE="${IMAGE:-}"
readonly CONTAINER="nextautodesk-nextautodesk-1"
readonly SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10
                   -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
readonly SCP_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)

HOST=""
PUBLIC_PORT=8765
ALLOW_FIREWALL=false
ROTATE_KEY=false

usage() {
    cat <<'EOF'
Deploy NextAutoDesk to a remote host over SSH (image streamed, no registry).

Usage:
  ./deploy/vps/deploy.sh --host <vps-host> [options]

Required:
  --host TARGET         SSH target: hostname, IP or user@host (no default)

Options:
  --image REF           image reference to deploy (default: nextautodesk:<version
                        derived from pyproject.toml>, e.g. nextautodesk:1.2.3-alpha1)
  --port PORT           published API port (default: 8765)
  --allow-firewall      open PORT/tcp in ufw when ufw is active on the target
  --rotate-key          generate a new API key instead of reusing the existing one
  -h, --help            show this help

Examples:
  ./deploy/vps/deploy.sh --host <vps-host>
  ./deploy/vps/deploy.sh --host <user>@<vps-host> --allow-firewall
  ./deploy/vps/deploy.sh --host <vps-host> --port 8765 --rotate-key

The image must already be built locally (scripts/build-image.sh). The API key is
stored in /opt/nextautodesk/.env (mode 600) on the target; this script does not
print it. Run ./deploy/vps/verify.sh --host <vps-host> afterwards to run the
end-to-end acceptance checks.
EOF
}

die() {
    printf 'deploy-vps: error: %s\n' "$*" >&2
    exit 1
}

log() {
    printf 'deploy-vps: %s\n' "$*" >&2
}

remote() {
    ssh "${SSH_OPTS[@]}" "${HOST}" "$@"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)
            [[ $# -ge 2 ]] || die "--host requires a value"
            HOST="$2"
            shift 2
            ;;
        --image)
            [[ $# -ge 2 ]] || die "--image requires a value"
            IMAGE="$2"
            shift 2
            ;;
        --port)
            [[ $# -ge 2 ]] || die "--port requires a value"
            PUBLIC_PORT="$2"
            shift 2
            ;;
        --allow-firewall)
            ALLOW_FIREWALL=true
            shift
            ;;
        --rotate-key)
            ROTATE_KEY=true
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

if [[ -z "${HOST}" ]]; then
    usage >&2
    printf '\n' >&2
    die "--host <vps-host> is required"
fi
if [[ -z "${IMAGE}" ]]; then
    IMAGE="$(nad_default_image "${REPO_ROOT}")"
fi
if [[ ! "${IMAGE}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]*$ || "${IMAGE}" != *:* ]]; then
    die "invalid --image '${IMAGE}' (expected repository:tag)"
fi
IMAGE_TAG="${IMAGE##*:}"
if [[ ! "${HOST}" =~ ^[A-Za-z0-9._@:%+-]+$ ]]; then
    die "invalid --host '${HOST}' (expected hostname, IP or user@host)"
fi
[[ "${PUBLIC_PORT}" =~ ^[0-9]+$ ]] || die "--port must be numeric"
((PUBLIC_PORT >= 1 && PUBLIC_PORT <= 65535)) || die "--port must be between 1 and 65535"

# ---------------------------------------------------------------------------
# Preflight (local)
# ---------------------------------------------------------------------------
for tool in docker gzip ssh scp; do
    command -v "${tool}" >/dev/null 2>&1 || die "${tool} is required on the build host"
done
[[ -f "${REPO_ROOT}/deploy/docker-compose.yml" ]] || die "deploy/docker-compose.yml not found"
[[ -f "${REPO_ROOT}/config/nextautodesk.example.yml" ]] || die "example config not found"
docker image inspect "${IMAGE}" >/dev/null 2>&1 \
    || die "local image ${IMAGE} not found; run scripts/build-image.sh first"

local_id="$(docker image inspect --format '{{.Id}}' "${IMAGE}")"
local_arch="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "${IMAGE}")"
local_size="$(docker image inspect --format '{{.Size}}' "${IMAGE}")"
log "target ${HOST}, image ${IMAGE} (${local_size} bytes, ${local_arch}, ${local_id#sha256:}), public port ${PUBLIC_PORT}"

# ---------------------------------------------------------------------------
# Preflight (remote)
# ---------------------------------------------------------------------------
remote 'true' || die "cannot SSH to ${HOST} (BatchMode; set up the key/host alias first)"
remote 'docker --version >/dev/null 2>&1 && docker compose version >/dev/null 2>&1' \
    || die "docker + docker compose v2 are required on ${HOST} (see deploy/vps/install-docker.sh)"
remote 'command -v curl >/dev/null' || die "curl is required on ${HOST} (used for health checks)"
remote 'command -v gunzip >/dev/null' || die "gzip/gunzip is required on ${HOST} (image transfer)"

remote_arch="$(remote "docker version --format '{{.Server.Os}}/{{.Server.Arch}}'")" \
    || die "cannot query the Docker server platform on ${HOST}"
if [[ "${remote_arch}" != "${local_arch}" ]]; then
    die "image platform is ${local_arch} but ${HOST} runs ${remote_arch}; rebuild with: scripts/build-image.sh --platform ${remote_arch}"
fi

# ---------------------------------------------------------------------------
# Remote layout
# ---------------------------------------------------------------------------
log "preparing ${REMOTE_DIR} on ${HOST}"
remote "install -d -m 0755 ${REMOTE_DIR} ${REMOTE_DIR}/config \
        && install -d -m 0755 ${REMOTE_DIR}/data \
        && chown -R 1000:1000 ${REMOTE_DIR}/data"

# API key: reuse the existing one unless --rotate-key was requested. A copied
# .env.example placeholder ("replace-with-...") is treated as missing.
existing_key=""
if remote "test -f ${REMOTE_DIR}/.env"; then
    existing_key="$(remote "sed -n 's/^NEXT_AUTODESK_API_KEYS=//p' ${REMOTE_DIR}/.env | tail -1" || true)"
fi
if [[ "${ROTATE_KEY}" == "true" || -z "${existing_key}" || "${existing_key}" == replace-with-* ]]; then
    if command -v openssl >/dev/null 2>&1; then
        api_key="$(openssl rand -hex 32)"
    else
        api_key="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    fi
    [[ "${api_key}" =~ ^[0-9a-f]{64}$ ]] || die "could not generate a 32-byte hex API key"
    log "generated a new API key"
else
    api_key="${existing_key}"
    log "reusing the existing API key (use --rotate-key to regenerate)"
fi

tmp_env="$(mktemp)"
cleanup() {
    rm -f "${tmp_env}"
    remote "rm -f ${REMOTE_DIR}/.env.tmp ${REMOTE_DIR}/.api-header" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cat >"${tmp_env}" <<EOF
# NextAutoDesk deployment environment (generated by deploy/vps/deploy.sh)
NEXT_AUTODESK_API_KEYS=${api_key}
# Image tag consumed by deploy/docker-compose.yml (derived from pyproject.toml).
NAD_IMAGE_TAG=${IMAGE_TAG}
NAD_BIND_IP=0.0.0.0
NAD_PORT=${PUBLIC_PORT}
NAD_DATA_DIR=./data
NAD_CONFIG_DIR=./config
NEXT_AUTODESK_LOG_LEVEL=INFO
TZ=UTC
EOF

# Write the .env over SSH with umask 077 and atomically rename it into place:
# the file contains the API key and is never group/world-readable, not even for
# the instant between scp and chmod.
if ! remote "umask 077; cat > ${REMOTE_DIR}/.env.tmp" <"${tmp_env}"; then
    die "could not upload ${REMOTE_DIR}/.env"
fi
remote "chmod 0600 ${REMOTE_DIR}/.env.tmp && mv -f ${REMOTE_DIR}/.env.tmp ${REMOTE_DIR}/.env"

scp -q "${SCP_OPTS[@]}" "${REPO_ROOT}/deploy/docker-compose.yml" \
    "${HOST}:${REMOTE_DIR}/docker-compose.yml.tmp"
remote "chmod 0644 ${REMOTE_DIR}/docker-compose.yml.tmp && mv -f ${REMOTE_DIR}/docker-compose.yml.tmp ${REMOTE_DIR}/docker-compose.yml"

# Configuration overlay: never overwrite a deployment that already customised it.
if remote "test -f ${REMOTE_DIR}/config/nextautodesk.yml"; then
    log "keeping existing config/nextautodesk.yml"
else
    scp -q "${SCP_OPTS[@]}" "${REPO_ROOT}/config/nextautodesk.example.yml" \
        "${HOST}:${REMOTE_DIR}/config/nextautodesk.yml.tmp"
    remote "chmod 0644 ${REMOTE_DIR}/config/nextautodesk.yml.tmp \
            && mv -f ${REMOTE_DIR}/config/nextautodesk.yml.tmp ${REMOTE_DIR}/config/nextautodesk.yml"
    log "installed default config/nextautodesk.yml"
fi
# The container runs as uid 1000 and mounts the config read-only: it must be
# world-readable (checkout files may be 0600).
remote "chmod 0644 ${REMOTE_DIR}/config/nextautodesk.yml ${REMOTE_DIR}/docker-compose.yml"

# ---------------------------------------------------------------------------
# Image transfer (streamed: no registry, no double disk usage)
# ---------------------------------------------------------------------------
log "transferring image (docker save | gzip -1 | ssh 'gunzip | docker load') ..."
transfer_start=${SECONDS}
if ! docker save "${IMAGE}" | gzip -1 | remote 'gunzip | docker load'; then
    die "image transfer failed"
fi
remote_id="$(remote "docker image inspect --format '{{.Id}}' ${IMAGE}" 2>/dev/null)" \
    || die "image ${IMAGE} is not present on ${HOST} after the transfer"
if [[ "${remote_id}" != "${local_id}" ]]; then
    die "loaded image id ${remote_id} does not match the local image ${local_id}"
fi
log "loaded image OK in $((SECONDS - transfer_start))s: ${remote_id#sha256:}"

# ---------------------------------------------------------------------------
# Start / restart
# ---------------------------------------------------------------------------
log "starting the container"
remote "cd ${REMOTE_DIR} && docker compose up -d --remove-orphans --wait --wait-timeout 180" \
    || die "docker compose up failed; inspect: ssh ${HOST} 'cd ${REMOTE_DIR} && docker compose logs --tail=100'"

# ---------------------------------------------------------------------------
# Firewall (opt-in: only when the API must be reachable from the internet)
# ---------------------------------------------------------------------------
if [[ "${ALLOW_FIREWALL}" == "true" ]]; then
    if remote "command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'"; then
        remote "ufw allow ${PUBLIC_PORT}/tcp" >/dev/null
        log "opened ${PUBLIC_PORT}/tcp in ufw"
    else
        log "ufw is not active on ${HOST}; no firewall rule added"
    fi
fi

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
log "waiting for /readyz (X display + executor + storage) ..."
deadline=$((SECONDS + 180))
until remote "curl -fsS --max-time 5 http://127.0.0.1:${PUBLIC_PORT}/readyz >/dev/null 2>&1"; do
    if ((SECONDS >= deadline)); then
        remote "cd ${REMOTE_DIR} && docker compose ps && docker compose logs --tail=80" || true
        die "container did not become healthy within 180s"
    fi
    sleep 3
done

log "readyz OK"
remote "curl -fsS http://127.0.0.1:${PUBLIC_PORT}/healthz"
printf '\n'
# The API key is written to a 0600 header file over stdin and consumed with
# `curl -H @file`, so it never appears in a process argument list (ps/ssh argv).
if printf 'X-API-Key: %s\n' "${api_key}" | remote "umask 077; cat > ${REMOTE_DIR}/.api-header"; then
    remote "curl -fsS -H @${REMOTE_DIR}/.api-header http://127.0.0.1:${PUBLIC_PORT}/api/v1/info" | head -c 800
    printf '\n'
    remote "rm -f ${REMOTE_DIR}/.api-header" >/dev/null 2>&1 || true
else
    log "could not query /api/v1/info via the temporary header file (non-fatal)"
fi
remote "docker inspect --format 'container status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}' ${CONTAINER}" \
    | sed 's/^/deploy-vps: /'

host_ip="$(remote "hostname -I 2>/dev/null | awk '{print \$1}'" || true)"
log "API base URL: http://${host_ip:-<vps-host>}:${PUBLIC_PORT}/api/v1"
log "API key: stored on ${HOST} in ${REMOTE_DIR}/.env (mode 600); this script does not print it"
log "retrieve it with: ssh ${HOST} \"grep ^NEXT_AUTODESK_API_KEYS= ${REMOTE_DIR}/.env | cut -d= -f2-\""
log "verify with: ./deploy/vps/verify.sh --host ${HOST} --port ${PUBLIC_PORT}"
