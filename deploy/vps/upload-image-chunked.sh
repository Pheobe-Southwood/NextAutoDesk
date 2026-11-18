#!/usr/bin/env bash
# Upload a locally built Docker image to a remote host over a slow or lossy SSH
# link. The default streamed transfer in deploy.sh (docker save | gzip | ssh
# docker load) keeps a single TCP connection alive for the whole image; on links
# with packet loss that connection can stall or time out. This helper makes the
# transfer restartable: the compressed image is split into small chunks, the
# chunks are uploaded in parallel with retries, the remote copy is reassembled
# and its SHA-256 is verified before docker load.
#
# Usage:
#   deploy/vps/upload-image-chunked.sh --host <ssh-target> [options]
#
# Options:
#   --host HOST            SSH target (required; must already accept BatchMode)
#   --image IMAGE          Local image reference (default nextautodesk:<version from pyproject.toml>)
#   --chunk-size BYTES     Chunk size passed to split(1) (default 2097152 = 2 MiB)
#   --jobs N               Parallel upload streams (default 16)
#   --retries N            Attempts per chunk (default 8)
#   --remote-dir DIR       Remote working directory (default /tmp/nad-image-<sha-prefix>)
#   --keep-archive         Keep the local compressed archive after success
#   -h, --help             Show this help
#
# Re-running the command after an interruption reuses the same remote directory
# (derived from the archive SHA-256), skips chunks that are already complete and
# verifies every chunk before load, so it resumes instead of starting over.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=deploy/vps/_common.sh
source "${SCRIPT_DIR}/_common.sh" 2>/dev/null || true

IMAGE="$(nad_default_image "${REPO_ROOT}")"
HOST=""
CHUNK_SIZE=2097152
JOBS=16
RETRIES=8
REMOTE_DIR=""
KEEP_ARCHIVE=false

usage() { sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "upload-image-chunked: error: $*" >&2; exit 1; }
log() { echo "upload-image-chunked: $*"; }

while (( $# > 0 )); do
    case "$1" in
        --host) HOST="${2:?--host requires a value}"; shift 2 ;;
        --image) IMAGE="${2:?--image requires a value}"; shift 2 ;;
        --chunk-size) CHUNK_SIZE="${2:?--chunk-size requires a value}"; shift 2 ;;
        --jobs) JOBS="${2:?--jobs requires a value}"; shift 2 ;;
        --retries) RETRIES="${2:?--retries requires a value}"; shift 2 ;;
        --remote-dir) REMOTE_DIR="${2:?--remote-dir requires a value}"; shift 2 ;;
        --keep-archive) KEEP_ARCHIVE=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done
[[ -n "${HOST}" ]] || die "--host is required (deploy.sh never guesses a host)"
[[ "${CHUNK_SIZE}" =~ ^[1-9][0-9]*$ ]] || die "--chunk-size must be a positive integer"
[[ "${JOBS}" =~ ^[1-9][0-9]*$ ]] || die "--jobs must be a positive integer"
[[ "${RETRIES}" =~ ^[1-9][0-9]*$ ]] || die "--retries must be a positive integer"

for tool in docker zstd split sha256sum; do
    command -v "${tool}" >/dev/null || die "${tool} is required locally"
done
docker image inspect "${IMAGE}" >/dev/null 2>&1 || die "local image ${IMAGE} not found"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=20 -o ServerAliveCountMax=6)
remote() { ssh "${SSH_OPTS[@]}" "${HOST}" "$@"; }
remote 'docker --version >/dev/null 2>&1 && command -v zstd >/dev/null && command -v sha256sum >/dev/null' \
    || die "${HOST} needs docker, zstd and sha256sum"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/nad-chunked-upload.XXXXXX")"
ARCHIVE="${WORKDIR}/image.tar.zst"
CLEAN_REMOTE=false
cleanup() {
    if [[ "${CLEAN_REMOTE}" == "true" ]]; then
        remote "rm -rf '${REMOTE_DIR}'" >/dev/null 2>&1 || true
    fi
    if [[ "${KEEP_ARCHIVE}" == "true" && -f "${ARCHIVE}" ]]; then
        echo "upload-image-chunked: kept archive at ${ARCHIVE}" >&2
    else
        rm -rf "${WORKDIR}"
    fi
}
trap cleanup EXIT

local_id="$(docker image inspect --format '{{.Id}}' "${IMAGE}")"
local_size="$(docker image inspect --format '{{.Size}}' "${IMAGE}")"
local_arch="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "${IMAGE}")"
remote_arch="$(remote "docker version --format '{{.Server.Os}}/{{.Server.Arch}}'")" \
    || die "cannot query the Docker server platform on ${HOST}"
[[ "${local_arch}" == "${remote_arch}" ]] \
    || die "image is ${local_arch}, ${HOST} runs ${remote_arch}"
log "compressing ${IMAGE} (${local_size} bytes, ${local_arch}, id ${local_id#sha256:})"
docker save "${IMAGE}" | zstd -3 -T0 -o "${ARCHIVE}" -f
archive_sha="$(sha256sum "${ARCHIVE}" | cut -d' ' -f1)"
archive_size="$(stat -c %s "${ARCHIVE}")"
[[ -n "${REMOTE_DIR}" ]] || REMOTE_DIR="/tmp/nad-image-${archive_sha:0:12}"
log "archive ${archive_size} bytes, sha256 ${archive_sha}"
log "remote working directory ${REMOTE_DIR}"

remote "mkdir -p '${REMOTE_DIR}'" || die "cannot create ${REMOTE_DIR} on ${HOST}"

# Split into deterministic chunk names and upload them in parallel. A chunk that
# is already present with the expected size is reused, so an interrupted run
# resumes cheaply.
split -b "${CHUNK_SIZE}" -d -a 4 "${ARCHIVE}" "${WORKDIR}/chunk-"
chunk_count=$(find "${WORKDIR}" -maxdepth 1 -name 'chunk-*' | wc -l)
log "uploading ${chunk_count} chunks with ${JOBS} parallel streams, ${RETRIES} attempts each"

upload_one() {
    local file="$1" name size attempt remote_size
    name="$(basename "${file}")"
    size="$(stat -c %s "${file}")"
    remote_size="$(remote "stat -c %s '${REMOTE_DIR}/${name}' 2>/dev/null" || true)"
    if [[ "${remote_size}" == "${size}" ]]; then
        return 0
    fi
    for (( attempt = 1; attempt <= RETRIES; attempt++ )); do
        if timeout 240 ssh "${SSH_OPTS[@]}" "${HOST}" "cat > '${REMOTE_DIR}/${name}'" < "${file}"; then
            remote_size="$(remote "stat -c %s '${REMOTE_DIR}/${name}' 2>/dev/null" || true)"
            if [[ "${remote_size}" == "${size}" ]]; then
                return 0
            fi
            log "chunk ${name}: size mismatch after upload"
        fi
        log "chunk ${name}: retry ${attempt}/${RETRIES}"
        sleep $(( attempt * 2 ))
    done
    return 1
}
export -f upload_one
export SSH_OPTS HOST REMOTE_DIR RETRIES
export -f remote 2>/dev/null || true
# upload_one needs the remote() helper in the subshell.
export -f remote
# shellcheck disable=SC2016
find "${WORKDIR}" -maxdepth 1 -name 'chunk-*' -print0 | sort -z \
    | xargs -0 -P "${JOBS}" -I{} bash -c '
        remote() { ssh "${SSH_OPTS[@]}" "${HOST}" "$@"; }
        log() { echo "upload-image-chunked: $*"; }
        export -f remote log
        upload_one "$1"
    ' _ {} || die "chunk upload failed; rerun the same command to resume"

remote_count="$(remote "find '${REMOTE_DIR}' -maxdepth 1 -name 'chunk-*' | wc -l")"
[[ "${remote_count}" == "${chunk_count}" ]] \
    || die "expected ${chunk_count} chunks on ${HOST}, found ${remote_count}"

log "verifying reassembled archive on ${HOST}"
remote "cd '${REMOTE_DIR}' && cat \$(find . -maxdepth 1 -name 'chunk-*' | sort) > image.tar.zst && sha256sum image.tar.zst" \
    || die "cannot reassemble the archive on ${HOST}"
remote_sha="$(remote "sha256sum '${REMOTE_DIR}/image.tar.zst' | cut -d' ' -f1")"
[[ "${remote_sha}" == "${archive_sha}" ]] \
    || die "archive sha256 mismatch (remote ${remote_sha}, local ${archive_sha})"

log "loading image on ${HOST}"
remote "zstd -d -c '${REMOTE_DIR}/image.tar.zst' | docker load" \
    || die "docker load failed on ${HOST}"
remote_id="$(remote "docker image inspect --format '{{.Id}}' '${IMAGE}'" 2>/dev/null)" \
    || die "image ${IMAGE} is missing on ${HOST} after load"
[[ "${remote_id}" == "${local_id}" ]] \
    || die "loaded image id ${remote_id} does not match local ${local_id}"
CLEAN_REMOTE=true
log "loaded ${IMAGE} on ${HOST}: ${remote_id#sha256:} (local image id matches)"
log "done; run deploy/vps/deploy.sh --host ${HOST} or 'docker compose up -d' to activate it"
