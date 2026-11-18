#!/usr/bin/env bash
# Build the NextAutoDesk runtime image *on the deployment host itself*, with a
# hard memory cap, then optionally redeploy the /opt/nextautodesk compose stack.
#
# Why: the reference deployment host can reach the Ubuntu archive and PyPI but
# NOT Docker Hub, and has only 2 vCPU / ~3.9 GB RAM. Building on the host avoids
# a slow image transfer, but the build must be memory-guarded and it needs the
# ubuntu:24.04 base image to be present locally (it cannot be pulled).
#
# Two modes
#   ./deploy/vps/build-on-host.sh --host <ssh-target> [options]
#       Run from a development machine. Rsyncs the working tree to
#       <remote-dir> on the target, streams the base image if it is missing
#       there, then runs this same script on the target with --here. The remote
#       source copy is removed afterwards unless --keep-source was given.
#
#   ./deploy/vps/build-on-host.sh --here [options]
#       Run directly on the build/deployment host. Builds from this checkout;
#       nothing is deleted outside the evidence/working directories.
#
# What --here does, in order:
#   1. preflight: Docker + legacy builder with --memory support, free disk,
#      ubuntu:<tag> base image present (no Docker Hub), running deployment;
#   2. preserve the running container's /home/nad state outside the repo
#      (docker cp) when possible;
#   3. stop the running deployment container to free RAM (it is restarted on
#      failure, or replaced on success with --deploy);
#   4. ensure swap if the host has none/too little, re-check free memory;
#   5. sample free -m every 2 s and docker OOM events for the whole build;
#   6. DOCKER_BUILDKIT=0 docker build --memory/--memory-swap ... ;
#   7. verify image id/size/version label and that no container was OOM-killed;
#   8. optionally run scripts/container-smoke-test.sh against the new image;
#   9. with --deploy: back up /opt/nextautodesk/docker-compose.yml + .env,
#      install the new compose file, set NAD_IMAGE_TAG, `docker compose up -d
#      --wait`, wait for /healthz and verify OOMKilled=false (rollback on
#      failure);
#  10. clean build cache (dangling intermediate images; the previous deployed
#      image id is kept for rollback) and write an evidence summary.
#
# The API key in /opt/nextautodesk/.env is never read into this script's output
# or printed. Failures restart the previous container unless --keep-stopped.
# --dry-run performs the preflight only (no stop, no build) and prints the plan.
#
# Options (defaults in brackets):
#   --host HOST            orchestrate from a workstation over SSH
#   --here                 run on the build/deployment host itself
#   --tag TAG              image tag [<version derived from pyproject.toml>]
#   --image NAME           image repository [nextautodesk]
#   --version VERSION      OCI version label [same as --tag]
#   --ubuntu-tag TAG       local base image tag [24.04]
#   --memory LIMIT         build container memory hard cap [2500m]
#   --memory-swap LIMIT    build container memory+swap cap [3500m]
#   --min-free-mb N        abort below N MB available RAM [600]
#   --min-disk-mb N        abort below N MB free disk on / [3000]
#   --smoke-port PORT      host port for the smoke-test container [18765]
#   --pip-index-url URL    pip index mirror used inside the image build []
#                          (e.g. https://pypi.tuna.tsinghua.edu.cn/simple when the
#                          host's direct PyPI access is slow)
#   --deploy               redeploy /opt/nextautodesk with the new image
#   --skip-smoke-test      do not run scripts/container-smoke-test.sh
#   --dry-run              preflight only (no container stop, no build)
#   --keep-source          keep the synced source copy after --host success
#   --keep-cache           keep dangling build-cache images after success
#   --keep-stopped         on failure, do not restart the previous container
#   --keep-swap            keep a temporary swap file created for the build
#   --no-preserve-home     skip the docker cp /home/nad snapshot
#   --no-transfer-base     do not stream the base image from this machine
#   --remote-dir DIR       rsync target in --host mode [/root/nad-release/build]
#   --evidence-dir DIR     evidence output on the host [/root/nad-evidence]
#   --preserve-dir DIR     /home/nad snapshot location [/root/nad-preserve]
#   --deploy-dir DIR       compose deployment directory [/opt/nextautodesk]
#
# Examples
#   ./deploy/vps/build-on-host.sh --host <vps-host>
#   ./deploy/vps/build-on-host.sh --host <vps-host> --deploy
#   ./deploy/vps/build-on-host.sh --host <vps-host> --skip-smoke-test --keep-source
#   ssh <vps-host> 'cd /root/nad-release/build && \
#       ./deploy/vps/build-on-host.sh --here --deploy'
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=deploy/vps/_common.sh
source "${SCRIPT_DIR}/_common.sh"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
MODE=""
HOST=""
REMOTE_DIR="/root/nad-release/build"
EVIDENCE_DIR="/root/nad-evidence"
PRESERVE_DIR="/root/nad-preserve"
DEPLOY_DIR="/opt/nextautodesk"
IMAGE="nextautodesk"
TAG=""
APP_VERSION=""
UBUNTU_TAG="24.04"
MEMORY="2500m"
MEMORY_SWAP="3500m"
MIN_FREE_MB=600
MIN_DISK_MB=3000
SMOKE_PORT=18765
SWAP_MB=2048
SWAP_FILE="/swap-nad-build.img"
WITH_OPTIONAL_STRATEGIES=true
DEPLOY=false
SKIP_SMOKE=false
KEEP_SOURCE=false
KEEP_CACHE=false
KEEP_STOPPED=false
KEEP_SWAP=false
PRESERVE_HOME=true
TRANSFER_BASE=true
DRY_RUN=false
PIP_INDEX_URL="${PIP_INDEX_URL:-}"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15
          -o ServerAliveInterval=20 -o ServerAliveCountMax=6)

# Runtime state (initialised for the EXIT trap).
SUMMARY=""
EVID=""
SAMPLER_PID=""
EVENTS_PID=""
OLD_CONTAINER=""
OLD_WAS_RUNNING=false
OLD_STOPPED=false
OLD_IMAGE_ID=""
PREVIOUS_IMAGE_ID=""
NEW_IMAGE_ID=""
NEW_IMAGE_SIZE=""
ROLLBACK_TAG=""
OOM_EVENTS=0
DEPLOYED=false
SUCCESS=false
SWAP_CREATED=false
BUILD_STARTED_AT=""
BUILD_SECONDS="0"
BUILD_RC="0"
SMOKE_RESULT="skipped"
DEPLOY_RESULT="disabled"
FREE_BEFORE=""
FREE_AFTER=""
PEAK_USED_MB=""
PEAK_MIN_AVAILABLE_MB=""
PEAK_SWAP_USED_MB=""

usage() {
    awk 'NR > 1 && $0 == "set -euo pipefail" {exit} NR > 1 {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]}"
}

die() {
    printf 'build-on-host: error: %s\n' "$*" >&2
    exit 1
}

log() {
    printf 'build-on-host: %s\n' "$*"
}

warn() {
    printf 'build-on-host: warning: %s\n' "$*" >&2
}

note() {
    if [[ -n "${SUMMARY}" ]]; then
        printf '%s\n' "$*" | tee -a "${SUMMARY}"
    else
        printf '%s\n' "$*"
    fi
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)
            [[ $# -ge 2 ]] || die "--host requires a value"
            MODE="host"
            HOST="$2"
            shift 2
            ;;
        --here)
            MODE="here"
            shift
            ;;
        --remote-dir)
            [[ $# -ge 2 ]] || die "--remote-dir requires a value"
            REMOTE_DIR="$2"
            shift 2
            ;;
        --evidence-dir)
            [[ $# -ge 2 ]] || die "--evidence-dir requires a value"
            EVIDENCE_DIR="$2"
            shift 2
            ;;
        --preserve-dir)
            [[ $# -ge 2 ]] || die "--preserve-dir requires a value"
            PRESERVE_DIR="$2"
            shift 2
            ;;
        --deploy-dir)
            [[ $# -ge 2 ]] || die "--deploy-dir requires a value"
            DEPLOY_DIR="$2"
            shift 2
            ;;
        --image)
            [[ $# -ge 2 ]] || die "--image requires a value"
            IMAGE="$2"
            shift 2
            ;;
        --tag)
            [[ $# -ge 2 ]] || die "--tag requires a value"
            TAG="$2"
            shift 2
            ;;
        --version)
            [[ $# -ge 2 ]] || die "--version requires a value"
            APP_VERSION="$2"
            shift 2
            ;;
        --ubuntu-tag)
            [[ $# -ge 2 ]] || die "--ubuntu-tag requires a value"
            UBUNTU_TAG="$2"
            shift 2
            ;;
        --memory)
            [[ $# -ge 2 ]] || die "--memory requires a value"
            MEMORY="$2"
            shift 2
            ;;
        --memory-swap)
            [[ $# -ge 2 ]] || die "--memory-swap requires a value"
            MEMORY_SWAP="$2"
            shift 2
            ;;
        --min-free-mb)
            [[ $# -ge 2 ]] || die "--min-free-mb requires a value"
            MIN_FREE_MB="$2"
            shift 2
            ;;
        --min-disk-mb)
            [[ $# -ge 2 ]] || die "--min-disk-mb requires a value"
            MIN_DISK_MB="$2"
            shift 2
            ;;
        --smoke-port)
            [[ $# -ge 2 ]] || die "--smoke-port requires a value"
            SMOKE_PORT="$2"
            shift 2
            ;;
        --swap-mb)
            [[ $# -ge 2 ]] || die "--swap-mb requires a value"
            SWAP_MB="$2"
            shift 2
            ;;
        --swap-file)
            [[ $# -ge 2 ]] || die "--swap-file requires a value"
            SWAP_FILE="$2"
            shift 2
            ;;
        --pip-index-url)
            [[ $# -ge 2 ]] || die "--pip-index-url requires a value"
            PIP_INDEX_URL="$2"
            shift 2
            ;;
        --no-optional-strategies)
            WITH_OPTIONAL_STRATEGIES=false
            shift
            ;;
        --deploy)
            DEPLOY=true
            shift
            ;;
        --skip-smoke-test)
            SKIP_SMOKE=true
            shift
            ;;
        --keep-source)
            KEEP_SOURCE=true
            shift
            ;;
        --keep-cache)
            KEEP_CACHE=true
            shift
            ;;
        --keep-stopped)
            KEEP_STOPPED=true
            shift
            ;;
        --keep-swap)
            KEEP_SWAP=true
            shift
            ;;
        --no-preserve-home)
            PRESERVE_HOME=false
            shift
            ;;
        --no-transfer-base)
            TRANSFER_BASE=false
            shift
            ;;
        --dry-run)
            DRY_RUN=true
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

[[ -n "${MODE}" ]] || die "one of --host <ssh-target> or --here is required (try --help)"
[[ -f "${REPO_ROOT}/pyproject.toml" ]] || die "pyproject.toml not found in ${REPO_ROOT}"
[[ -f "${REPO_ROOT}/docker/Dockerfile" ]] || die "docker/Dockerfile not found in ${REPO_ROOT}"

# Version/tag come from pyproject.toml unless explicitly overridden.
[[ -n "${TAG}" ]] || TAG="$(nad_default_tag "${REPO_ROOT}")"
[[ -n "${APP_VERSION}" ]] || APP_VERSION="${TAG}"

validate_value() {
    local name="$1" value="$2" pattern="$3"
    [[ "${value}" =~ ${pattern} ]] || die "invalid ${name} '${value}' (pattern ${pattern})"
}

validate_value "tag" "${TAG}" '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'
validate_value "image repository" "${IMAGE}" '^[A-Za-z0-9][A-Za-z0-9._:/-]*$'
validate_value "version" "${APP_VERSION}" '^[A-Za-z0-9_.:+-]{1,128}$'
validate_value "ubuntu tag" "${UBUNTU_TAG}" '^[A-Za-z0-9_.:+-]{1,64}$'
validate_value "memory" "${MEMORY}" '^[0-9]+[bkmg]?$'
validate_value "memory swap" "${MEMORY_SWAP}" '^[0-9]+[bkmg]?$'
validate_value "min-free-mb" "${MIN_FREE_MB}" '^[0-9]+$'
validate_value "min-disk-mb" "${MIN_DISK_MB}" '^[0-9]+$'
validate_value "smoke-port" "${SMOKE_PORT}" '^[0-9]+$'
validate_value "swap-mb" "${SWAP_MB}" '^[0-9]+$'
validate_value "deploy dir" "${DEPLOY_DIR}" '^/[A-Za-z0-9._/-]+$'
validate_value "remote dir" "${REMOTE_DIR}" '^/root/[A-Za-z0-9._/-]+$'
validate_value "evidence dir" "${EVIDENCE_DIR}" '^/[A-Za-z0-9._/-]+$'
validate_value "preserve dir" "${PRESERVE_DIR}" '^/[A-Za-z0-9._/-]+$'
validate_value "swap file" "${SWAP_FILE}" '^/[A-Za-z0-9._/-]+$'
if [[ -n "${PIP_INDEX_URL}" && "${PIP_INDEX_URL}" == *[[:space:]]* ]]; then
    die "invalid --pip-index-url '${PIP_INDEX_URL}' (must not contain whitespace)"
fi
if [[ "${REMOTE_DIR}" == *"/../"* || "${REMOTE_DIR}" == *".."* \
    || "${EVIDENCE_DIR}" == *".."* || "${PRESERVE_DIR}" == *".."* ]]; then
    die "paths must not contain '..'"
fi
if [[ "${HOST}" =~ ^([A-Za-z0-9._-]+@)?([A-Za-z0-9._-]+)$ ]]; then
    :
elif [[ -n "${HOST}" ]]; then
    die "invalid --host '${HOST}' (expected hostname, IP or user@host)"
fi
((SMOKE_PORT >= 1 && SMOKE_PORT <= 65535)) || die "--smoke-port must be between 1 and 65535"

# ===========================================================================
# Host-side helpers (used by --here and by the remote half of --host)
# ===========================================================================
free_summary() {
    free -m | awk '
        /^Mem:/ {printf "mem total=%sMB used=%sMB free=%sMB available=%sMB", $2, $3, $4, $7}
        /^Swap:/ {printf " swap total=%sMB used=%sMB free=%sMB", $2, $3, $4}
    '
}

available_mb() {
    awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo
}

disk_available_mb() {
    df -Pm / | awk 'NR==2 {print $4}'
}

create_swap_if_needed() {
    local avail swap_total
    avail="$(available_mb)"
    swap_total="$(awk '/^SwapTotal:/ {print int($2 / 1024)}' /proc/meminfo)"
    if ((avail >= MIN_FREE_MB && swap_total >= 512)); then
        note "swap-guard: no action needed (available=${avail}MB, swap total=${swap_total}MB)"
        return 0
    fi
    if ((SWAP_MB <= 0)); then
        note "swap-guard: skipped (--swap-mb 0)"
        return 0
    fi
    note "swap-guard: available=${avail}MB swap=${swap_total}MB; creating ${SWAP_MB}MB swap at ${SWAP_FILE}"
    if [[ -f "${SWAP_FILE}" ]]; then
        swapoff "${SWAP_FILE}" 2>/dev/null || true
        rm -f "${SWAP_FILE}"
    fi
    if command -v fallocate >/dev/null 2>&1; then
        fallocate -l "${SWAP_MB}M" "${SWAP_FILE}" 2>/dev/null \
            || dd if=/dev/zero of="${SWAP_FILE}" bs=1M count="${SWAP_MB}" status=none
    else
        dd if=/dev/zero of="${SWAP_FILE}" bs=1M count="${SWAP_MB}" status=none
    fi
    chmod 0600 "${SWAP_FILE}"
    mkswap "${SWAP_FILE}" >/dev/null
    swapon "${SWAP_FILE}"
    SWAP_CREATED=true
    note "swap-guard: swap active, $(free_summary)"
}

remove_swap() {
    if [[ "${SWAP_CREATED}" == "true" && "${KEEP_SWAP}" != "true" ]]; then
        swapoff "${SWAP_FILE}" 2>/dev/null || true
        rm -f "${SWAP_FILE}"
        note "swap-guard: removed temporary swap file ${SWAP_FILE}"
        SWAP_CREATED=false
    fi
}

start_samplers() {
    (
        while :; do
            free -m | awk -v ts="$(date +%s)" '
                /^Mem:/ {used=$3; avail=$7}
                /^Swap:/ {swap=$3}
                END {printf "%s %s %s %s\n", ts, used, avail, swap}
            '
            sleep 2
        done
    ) > "${EVID}/mem-samples.log" 2>&1 &
    SAMPLER_PID=$!

    # Any OOM kill of a container during the build/smoke window is recorded
    # here; docker events is also the only way to see short-lived build
    # containers after they have exited.
    docker events --filter type=container --filter event=oom \
        --format '{{.Time}} {{.Actor.Attributes.name}} {{.Action}}' \
        > "${EVID}/oom-events.log" 2>&1 &
    EVENTS_PID=$!
    note "samplers: memory every 2s + docker OOM events -> ${EVID}"
}

stop_samplers() {
    local pid
    for pid in "${SAMPLER_PID}" "${EVENTS_PID}"; do
        [[ -n "${pid}" ]] || continue
        kill "${pid}" 2>/dev/null || true
        wait "${pid}" 2>/dev/null || true
    done
    SAMPLER_PID=""
    EVENTS_PID=""
}

summarise_peaks() {
    [[ -s "${EVID}/mem-samples.log" ]] || return 0
    read -r PEAK_USED_MB PEAK_MIN_AVAILABLE_MB PEAK_SWAP_USED_MB < <(
        awk '
            BEGIN {max_used = 0; min_avail = 1e9; max_swap = 0}
            {if ($2 > max_used) max_used = $2
             if ($3 < min_avail) min_avail = $3
             if ($4 > max_swap) max_swap = $4}
            END {printf "%d %d %d\n", max_used, min_avail, max_swap}
        ' "${EVID}/mem-samples.log"
    )
    note "memory-peak: max used=${PEAK_USED_MB}MB, min available=${PEAK_MIN_AVAILABLE_MB}MB, max swap used=${PEAK_SWAP_USED_MB}MB"
}

detect_existing_container() {
    OLD_CONTAINER="$(docker ps -q \
        --filter label=com.docker.compose.service=nextautodesk 2>/dev/null | head -n1 || true)"
    if [[ -z "${OLD_CONTAINER}" ]]; then
        OLD_CONTAINER="$(docker ps -aq --filter name=nextautodesk-nextautodesk-1 2>/dev/null \
            | head -n1 || true)"
    fi
    if [[ -n "${OLD_CONTAINER}" ]]; then
        OLD_WAS_RUNNING="$(docker inspect --format '{{.State.Running}}' "${OLD_CONTAINER}" 2>/dev/null || true)"
        OLD_IMAGE_ID="$(docker inspect --format '{{.Image}}' "${OLD_CONTAINER}" 2>/dev/null || true)"
        OLD_CONTAINER="$(docker inspect --format '{{.Name}}' "${OLD_CONTAINER}" 2>/dev/null | sed 's#^/##' || true)"
        [[ "${OLD_WAS_RUNNING}" == "true" ]] || OLD_WAS_RUNNING=false
    else
        OLD_WAS_RUNNING=false
    fi
}

preserve_home_state() {
    [[ "${PRESERVE_HOME}" == "true" && -n "${OLD_CONTAINER}" ]] || return 0
    local target="${PRESERVE_DIR}/home-nad"
    if [[ -d "${target}" ]]; then
        note "preserve: keeping existing /home/nad snapshot at ${target}"
        return 0
    fi
    mkdir -p "${PRESERVE_DIR}"
    if docker cp "${OLD_CONTAINER}:/home/nad" "${target}" >/dev/null 2>&1; then
        note "preserve: copied container /home/nad -> ${target} ($(du -sh "${target}" | cut -f1))"
    else
        rm -rf "${target}"
        note "preserve: ${OLD_CONTAINER} has no /home/nad directory to preserve"
    fi
}

stop_existing_container() {
    if [[ "${OLD_WAS_RUNNING}" != "true" ]]; then
        note "deployment: no running nextautodesk container found; nothing to stop"
        return 0
    fi
    note "deployment: stopping ${OLD_CONTAINER} to free RAM (previous image id ${OLD_IMAGE_ID#sha256:})"
    docker stop --time 45 "${OLD_CONTAINER}" >/dev/null
    OLD_STOPPED=true
    sleep 2
    note "deployment: stopped; $(free_summary)"
}

restart_previous_container() {
    if [[ "${OLD_STOPPED}" != "true" ]]; then
        return 0
    fi
    if [[ "${KEEP_STOPPED}" == "true" ]]; then
        warn "leaving the previous container stopped (--keep-stopped); restart with: docker start ${OLD_CONTAINER}"
        return 0
    fi
    log "restoring the previous deployment container ${OLD_CONTAINER}"
    if docker start "${OLD_CONTAINER}" >/dev/null 2>&1; then
        note "deployment: previous container ${OLD_CONTAINER} restarted (old image)"
        OLD_STOPPED=false
    else
        warn "could not restart ${OLD_CONTAINER}; inspect: docker logs ${OLD_CONTAINER}"
    fi
}

build_image() {
    local -a args=(
        --build-arg "UBUNTU_TAG=${UBUNTU_TAG}"
        --build-arg "APP_VERSION=${APP_VERSION}"
        --build-arg "WITH_OPTIONAL_STRATEGIES=${WITH_OPTIONAL_STRATEGIES}"
        --build-arg "INSTALL_VNC=false"
        --build-arg "USER_UID=1000"
        --build-arg "USER_GID=1000"
        --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}"
    )
    note "build: DOCKER_BUILDKIT=0 docker build --memory=${MEMORY} --memory-swap=${MEMORY_SWAP} -t ${IMAGE}:${TAG} ."
    note "build: APP_VERSION=${APP_VERSION} UBUNTU_TAG=${UBUNTU_TAG} optional-strategies=${WITH_OPTIONAL_STRATEGIES}"
    BUILD_STARTED_AT="$(date +%s)"
    set +e
    DOCKER_BUILDKIT=0 docker build \
        --memory "${MEMORY}" \
        --memory-swap "${MEMORY_SWAP}" \
        --pull=false \
        -f docker/Dockerfile \
        --target app \
        "${args[@]}" \
        -t "${IMAGE}:${TAG}" \
        . 2>&1 | tee -a "${EVID}/build.log"
    BUILD_RC=${PIPESTATUS[0]}
    set -e
    BUILD_SECONDS=$(( $(date +%s) - BUILD_STARTED_AT ))
    if ((BUILD_RC != 0)); then
        note "build: FAILED after ${BUILD_SECONDS}s (exit ${BUILD_RC}); see ${EVID}/build.log"
        return 1
    fi
    note "build: finished in ${BUILD_SECONDS}s"
}

verify_built_image() {
    local id size version_label
    if ! docker image inspect "${IMAGE}:${TAG}" >/dev/null 2>&1; then
        note "verify: image ${IMAGE}:${TAG} is missing after a successful build"
        return 1
    fi
    id="$(docker image inspect --format '{{.Id}}' "${IMAGE}:${TAG}")"
    size="$(docker image inspect --format '{{.Size}}' "${IMAGE}:${TAG}")"
    version_label="$(docker image inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "${IMAGE}:${TAG}")"
    NEW_IMAGE_ID="${id}"
    NEW_IMAGE_SIZE="${size}"
    note "image: ${IMAGE}:${TAG} id=${id#sha256:} size=${size} bytes ($(awk -v b="${size}" 'BEGIN {printf "%.1f", b / 1048576}') MiB) version-label=${version_label}"
    if [[ "${version_label}" != "${APP_VERSION}" ]]; then
        warn "image version label '${version_label}' != APP_VERSION '${APP_VERSION}'"
    fi
    local oom_count=0
    [[ -s "${EVID}/oom-events.log" ]] && oom_count="$(grep -c . "${EVID}/oom-events.log" || true)"
    if ((oom_count > 0)); then
        OOM_EVENTS="${oom_count}"
        note "verify: docker reported ${oom_count} OOM event(s) during the build:"
        sed 's/^/  /' "${EVID}/oom-events.log" | tee -a "${SUMMARY}"
        return 1
    fi
    OOM_EVENTS=0
    note "verify: no container OOM events during the build (OOMKilled=false equivalent)"
    return 0
}

run_smoke_test() {
    if [[ "${SKIP_SMOKE}" == "true" ]]; then
        SMOKE_RESULT="skipped (--skip-smoke-test)"
        note "smoke: skipped (--skip-smoke-test)"
        return 0
    fi
    note "smoke: scripts/container-smoke-test.sh --image ${IMAGE}:${TAG} --port ${SMOKE_PORT}"
    set +e
    bash "${REPO_ROOT}/scripts/container-smoke-test.sh" \
        --image "${IMAGE}:${TAG}" --port "${SMOKE_PORT}" --timeout 240 \
        2>&1 | tee -a "${EVID}/smoke.log"
    local rc=${PIPESTATUS[0]}
    set -e
    if ((rc != 0)); then
        SMOKE_RESULT="failed (exit ${rc})"
        note "smoke: FAILED (exit ${rc}); see ${EVID}/smoke.log"
        return 1
    fi
    SMOKE_RESULT="passed"
    note "smoke: passed (log ${EVID}/smoke.log)"
}

upsert_env_tag() {
    # Replace/append NAD_IMAGE_TAG in an existing .env without touching any other
    # line (the API key must be preserved verbatim and never printed).
    local env_file="$1" tag="$2"
    python3 - "${env_file}" "${tag}" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
tag = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()
out: list[str] = []
seen = False
for line in lines:
    if line.startswith("NAD_IMAGE_TAG="):
        if not seen:
            out.append(f"NAD_IMAGE_TAG={tag}")
            seen = True
        continue
    out.append(line)
if not seen:
    out.append(f"NAD_IMAGE_TAG={tag}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

rollback_deployment() {
    # Restore the previous deployment. The previous *image id* is used because
    # the version tag may be identical (e.g. a rebuild of the same version), so
    # rolling back by tag name would silently run the new image again.
    local dir="${DEPLOY_DIR}" env_file="${DEPLOY_DIR}/.env" compose="${DEPLOY_DIR}/docker-compose.yml"
    local backup="$1" rollback_tag="" rc=0
    note "deploy: rolling back to the previous container image"
    cp -a "${backup}/.env" "${env_file}"
    if [[ -n "${OLD_IMAGE_ID}" ]]; then
        rollback_tag="np-rollback-$(date +%s)"
        if docker tag "${OLD_IMAGE_ID}" "nextautodesk:${rollback_tag}" >/dev/null 2>&1; then
            # Keep the version-parameterised compose file and point it at a
            # temporary tag that references the previous image id directly.
            install -m 0644 "${REPO_ROOT}/deploy/docker-compose.yml" "${compose}.tmp.$$"
            mv -f "${compose}.tmp.$$" "${compose}"
            upsert_env_tag "${env_file}" "${rollback_tag}"
            note "deploy: rollback tag nextautodesk:${rollback_tag} -> ${OLD_IMAGE_ID#sha256:}"
        else
            rollback_tag=""
        fi
    fi
    if [[ -z "${rollback_tag}" ]]; then
        if [[ -f "${backup}/docker-compose.yml" ]]; then
            cp -a "${backup}/docker-compose.yml" "${compose}"
        fi
    fi
    set +e
    (cd "${dir}" && docker compose up -d --remove-orphans --force-recreate --wait --wait-timeout 180) \
        2>&1 | tee -a "${EVID}/deploy-rollback.log"
    rc=${PIPESTATUS[0]}
    set -e
    if ((rc == 0)); then
        OLD_STOPPED=false
        note "deploy: rollback OK; the previous deployment is running again"
    else
        warn "deploy: rollback FAILED (exit ${rc}); manual recovery needed: docker logs in ${dir}"
    fi
}

deploy_new_image() {
    local dir="${DEPLOY_DIR}" env_file="${DEPLOY_DIR}/.env" compose="${DEPLOY_DIR}/docker-compose.yml"
    local backup port container state rc
    [[ -d "${dir}" ]] || {
        note "deploy: ${dir} does not exist; expected an existing compose deployment"
        return 1
    }
    [[ -f "${env_file}" ]] || {
        note "deploy: ${env_file} missing; refusing to create a deployment without an API key"
        return 1
    }
    if ! grep -q '^NEXT_AUTODESK_API_KEYS=..*' "${env_file}"; then
        note "deploy: ${env_file} has no NEXT_AUTODESK_API_KEYS value; refusing to clobber it"
        return 1
    fi
    backup="${dir}/backup-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "${backup}"
    chmod 0700 "${backup}"
    cp -a "${env_file}" "${backup}/.env"
    if [[ -f "${compose}" ]]; then
        cp -a "${compose}" "${backup}/docker-compose.yml"
    fi
    note "deploy: backed up compose + .env to ${backup} (the API key is not logged)"

    install -m 0644 "${REPO_ROOT}/deploy/docker-compose.yml" "${compose}.tmp.$$"
    mv -f "${compose}.tmp.$$" "${compose}"
    upsert_env_tag "${env_file}" "${TAG}"
    if ! (cd "${dir}" && docker compose config --quiet); then
        note "deploy: docker compose config rejected the new compose file; restoring the previous files"
        rollback_deployment "${backup}"
        DEPLOY_RESULT="failed (invalid compose file)"
        return 1
    fi

    note "deploy: docker compose up -d --force-recreate (image ${IMAGE}:${TAG}) in ${dir}"
    set +e
    (cd "${dir}" && docker compose up -d --remove-orphans --force-recreate \
        --wait --wait-timeout 240) 2>&1 | tee -a "${EVID}/deploy.log"
    rc=${PIPESTATUS[0]}
    set -e
    if ((rc != 0)); then
        note "deploy: compose up FAILED (exit ${rc})"
        rollback_deployment "${backup}"
        DEPLOY_RESULT="failed (compose up; rolled back)"
        return 1
    fi

    port="$(sed -n 's/^NAD_PORT=//p' "${env_file}" 2>/dev/null | tail -1)"
    port="${port:-8765}"
    note "deploy: waiting for http://127.0.0.1:${port}/readyz (X display + executor + storage)"
    local ok=false
    for _ in $(seq 1 90); do
        if curl -fsS --max-time 5 "http://127.0.0.1:${port}/readyz" >/dev/null 2>&1; then
            ok=true
            break
        fi
        sleep 2
    done
    if [[ "${ok}" != "true" ]]; then
        note "deploy: /readyz did not become reachable within 180s"
        (cd "${dir}" && docker compose logs --tail=80) 2>&1 | tee -a "${EVID}/deploy.log" || true
        rollback_deployment "${backup}"
        DEPLOY_RESULT="failed (unhealthy; rolled back)"
        return 1
    fi

    container="$(cd "${dir}" && docker compose ps -q 2>/dev/null | head -n1)"
    if [[ -n "${container}" ]]; then
        container="$(docker inspect --format '{{.Name}}' "${container}" | sed 's#^/##')"
    fi
    state="$(docker inspect --format \
        '{{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}} oom={{.State.OOMKilled}}' \
        "${container}" 2>/dev/null || true)"
    if [[ "${state}" == running* && "${state}" == *"health=healthy"* \
        && "${state}" == *"restarts=0"* && "${state}" == *"oom=false"* ]]; then
        # The version tag now points at the new image, so the previous image id
        # would become dangling. Keep it under an explicit rollback tag: the
        # cleanup step below preserves tagged images and it can be rolled back to
        # with a single ``docker tag``/compose change.
        if [[ -n "${OLD_IMAGE_ID}" ]]; then
            ROLLBACK_TAG="np-rollback-$(date +%s)"
            if docker tag "${OLD_IMAGE_ID}" "nextautodesk:${ROLLBACK_TAG}" >/dev/null 2>&1; then
                note "deploy: previous image tagged nextautodesk:${ROLLBACK_TAG} (${OLD_IMAGE_ID#sha256:}) for rollback"
            else
                ROLLBACK_TAG=""
                warn "deploy: could not tag the previous image for rollback"
            fi
        fi
        if grep -q . "${EVID}/oom-events.log" 2>/dev/null; then
            note "deploy: WARNING docker OOM events were recorded during the build/run window"
        fi
        docker stats --no-stream --format '  deploy-stats: mem={{.MemUsage}} cpu={{.CPUPerc}}' \
            "${container}" 2>/dev/null | tee -a "${SUMMARY}" || true
        OLD_STOPPED=false
        DEPLOYED=true
        DEPLOY_RESULT="passed (${container}: ${state})"
        note "deploy: OK ${container:-container} ${state}"
        note "deploy: /readyz on 127.0.0.1:${port} OK (API key unchanged)"
        return 0
    fi
    note "deploy: container not healthy after compose up: ${state:-unknown}"
    rollback_deployment "${backup}"
    DEPLOY_RESULT="failed (container state ${state:-unknown}; rolled back)"
    return 1
}

cleanup_build_cache() {
    [[ "${KEEP_CACHE}" == "true" ]] && {
        note "cleanup: keeping build cache (--keep-cache)"
        return 0
    }
    local id old_id new_id removed=0
    old_id="${OLD_IMAGE_ID#sha256:}"
    new_id="${NEW_IMAGE_ID#sha256:}"
    # ``--no-trunc`` + prefix-normalisation: ``docker images -q`` prints short
    # ids while ``docker inspect`` prints ``sha256:<full>``; comparing those
    # verbatim would remove the previous deployment image this cleanup is
    # supposed to preserve.
    for id in $(docker images -q --no-trunc --filter dangling=true 2>/dev/null); do
        id="${id#sha256:}"
        if [[ -n "${old_id}" && "${id}" == "${old_id}" ]]; then
            continue
        fi
        if [[ -n "${new_id}" && "${id}" == "${new_id}" ]]; then
            continue
        fi
        if docker rmi "${id}" >/dev/null 2>&1; then
            removed=$((removed + 1))
        fi
    done
    note "cleanup: removed ${removed} dangling build-cache image(s); previous deployment image kept for rollback${old_id:+ (${old_id})}${ROLLBACK_TAG:+ as nextautodesk:${ROLLBACK_TAG}}"
}

write_summary() {
    [[ -n "${SUMMARY}" ]] || return 0
    {
        echo
        echo "== build evidence =="
        echo "status=$( [[ "${SUCCESS}" == "true" ]] && echo success || echo failed )"
        echo "host=$(hostname)"
        echo "tag=${TAG}"
        echo "app_version=${APP_VERSION}"
        echo "image=${IMAGE}:${TAG}"
        echo "image_id=${NEW_IMAGE_ID#sha256:}"
        echo "image_size_bytes=${NEW_IMAGE_SIZE}"
        echo "image_size_mib=$(awk -v b="${NEW_IMAGE_SIZE:-0}" 'BEGIN {printf "%.1f", b / 1048576}')"
        echo "build_seconds=${BUILD_SECONDS}"
        echo "build_exit=${BUILD_RC}"
        echo "memory_limit=${MEMORY}"
        echo "memory_swap_limit=${MEMORY_SWAP}"
        echo "memory_used_peak_mb=${PEAK_USED_MB}"
        echo "memory_available_min_mb=${PEAK_MIN_AVAILABLE_MB}"
        echo "swap_used_peak_mb=${PEAK_SWAP_USED_MB}"
        echo "oom_events=${OOM_EVENTS}"
        echo "free_before=${FREE_BEFORE}"
        echo "free_after=${FREE_AFTER}"
        echo "smoke=${SMOKE_RESULT}"
        echo "deploy=${DEPLOY_RESULT}"
        echo "previous_image_id=${OLD_IMAGE_ID#sha256:}"
        echo "rollback_image_tag=${ROLLBACK_TAG:+nextautodesk:}${ROLLBACK_TAG}"
        echo "evidence_dir=${EVID}"
    } | tee -a "${SUMMARY}"
}

on_exit() {
    local rc=$?
    if [[ "${MODE}" == "here" ]]; then
        stop_samplers
        summarise_peaks 2>/dev/null || true
        FREE_AFTER="$(free_summary 2>/dev/null || true)"
        if [[ "${SUCCESS}" != "true" ]]; then
            # Build/smoke/deploy failed: put the previous container back.
            restart_previous_container
        fi
        remove_swap
        write_summary 2>/dev/null || true
        log "evidence: ${EVID} (summary.txt)"
    fi
    exit "${rc}"
}

# ===========================================================================
# --host orchestration (development machine -> deployment host)
# ===========================================================================
quote_args() {
    local quoted="" arg
    for arg in "$@"; do
        quoted+=" $(printf '%q' "${arg}")"
    done
    printf '%s' "${quoted}"
}

run_orchestrator() {
    local tool base_id local_base_id base_arch host_arch remote_rc remote_args
    for tool in ssh rsync docker gzip python3; do
        command -v "${tool}" >/dev/null 2>&1 || die "${tool} is required on this machine"
    done
    ssh "${SSH_OPTS[@]}" "${HOST}" 'true' \
        || die "cannot SSH to ${HOST} (BatchMode; set up the key/host alias first)"
    ssh "${SSH_OPTS[@]}" "${HOST}" \
        'command -v docker >/dev/null && command -v rsync >/dev/null' \
        || die "${HOST} needs docker and rsync"

    if [[ "${TRANSFER_BASE}" == "true" ]]; then
        base_id="$(ssh "${SSH_OPTS[@]}" "${HOST}" \
            "docker image inspect --format '{{.Id}}' ubuntu:${UBUNTU_TAG} 2>/dev/null" || true)"
        if [[ -z "${base_id}" ]]; then
            log "base image ubuntu:${UBUNTU_TAG} is missing on ${HOST}; streaming it from this machine"
            docker image inspect "ubuntu:${UBUNTU_TAG}" >/dev/null 2>&1 \
                || die "local ubuntu:${UBUNTU_TAG} image not found; load it first (docker pull is not used in this workflow)"
            local_base_id="$(docker image inspect --format '{{.Id}}' "ubuntu:${UBUNTU_TAG}")"
            base_arch="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "ubuntu:${UBUNTU_TAG}")"
            host_arch="$(ssh "${SSH_OPTS[@]}" "${HOST}" \
                "docker version --format '{{.Server.Os}}/{{.Server.Arch}}'")"
            [[ "${base_arch}" == "${host_arch}" ]] \
                || die "base image is ${base_arch}, ${HOST} runs ${host_arch}"
            docker save "ubuntu:${UBUNTU_TAG}" | gzip -1 \
                | ssh "${SSH_OPTS[@]}" "${HOST}" 'gunzip | docker load'
            base_id="$(ssh "${SSH_OPTS[@]}" "${HOST}" \
                "docker image inspect --format '{{.Id}}' ubuntu:${UBUNTU_TAG}")"
            [[ "${base_id}" == "${local_base_id}" ]] \
                || die "loaded base image id ${base_id} != local ${local_base_id}"
            log "base image ubuntu:${UBUNTU_TAG} loaded on ${HOST} (${base_id#sha256:})"
        else
            log "base image ubuntu:${UBUNTU_TAG} already present on ${HOST} (${base_id#sha256:})"
        fi
    fi

    ssh "${SSH_OPTS[@]}" "${HOST}" "mkdir -p '${REMOTE_DIR}'" \
        || die "cannot create ${REMOTE_DIR} on ${HOST}"
    log "syncing ${REPO_ROOT} -> ${HOST}:${REMOTE_DIR}"
    rsync -a --delete \
        --exclude '.git/' --exclude 'data/' --exclude '.venv/' \
        --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
        --exclude '.mypy_cache/' --exclude '*.pyc' --exclude 'node_modules/' \
        --exclude 'dist/' --exclude 'build/' --exclude '*.egg-info/' \
        "${REPO_ROOT}/" "${HOST}:${REMOTE_DIR}/" \
        || die "rsync to ${HOST}:${REMOTE_DIR} failed"

    remote_args=(
        --here
        --image "${IMAGE}"
        --tag "${TAG}"
        --version "${APP_VERSION}"
        --ubuntu-tag "${UBUNTU_TAG}"
        --memory "${MEMORY}"
        --memory-swap "${MEMORY_SWAP}"
        --min-free-mb "${MIN_FREE_MB}"
        --min-disk-mb "${MIN_DISK_MB}"
        --smoke-port "${SMOKE_PORT}"
        --swap-mb "${SWAP_MB}"
        --swap-file "${SWAP_FILE}"
        --deploy-dir "${DEPLOY_DIR}"
        --evidence-dir "${EVIDENCE_DIR}"
        --preserve-dir "${PRESERVE_DIR}"
    )
    if [[ -n "${PIP_INDEX_URL}" ]]; then remote_args+=(--pip-index-url "${PIP_INDEX_URL}"); fi
    if [[ "${WITH_OPTIONAL_STRATEGIES}" != "true" ]]; then remote_args+=(--no-optional-strategies); fi
    if [[ "${DEPLOY}" == "true" ]]; then remote_args+=(--deploy); fi
    if [[ "${SKIP_SMOKE}" == "true" ]]; then remote_args+=(--skip-smoke-test); fi
    if [[ "${KEEP_CACHE}" == "true" ]]; then remote_args+=(--keep-cache); fi
    if [[ "${KEEP_STOPPED}" == "true" ]]; then remote_args+=(--keep-stopped); fi
    if [[ "${KEEP_SWAP}" == "true" ]]; then remote_args+=(--keep-swap); fi
    if [[ "${DRY_RUN}" == "true" ]]; then remote_args+=(--dry-run); fi
    if [[ "${PRESERVE_HOME}" != "true" ]]; then remote_args+=(--no-preserve-home); fi

    log "running build on ${HOST}: ${REMOTE_DIR}/deploy/vps/build-on-host.sh$(quote_args "${remote_args[@]}")"
    set +e
    ssh "${SSH_OPTS[@]}" "${HOST}" \
        "cd '${REMOTE_DIR}' && bash deploy/vps/build-on-host.sh$(quote_args "${remote_args[@]}")"
    remote_rc=$?
    set -e

    log "remote evidence summary:"
    ssh "${SSH_OPTS[@]}" "${HOST}" \
        "cat '${EVIDENCE_DIR}/latest/summary.txt' 2>/dev/null" | sed 's/^/  /' || true

    if ((remote_rc == 0)) && [[ "${KEEP_SOURCE}" != "true" ]]; then
        log "cleaning remote source copy ${REMOTE_DIR}"
        ssh "${SSH_OPTS[@]}" "${HOST}" "rm -rf '${REMOTE_DIR}'" || warn "could not remove ${REMOTE_DIR}"
    elif ((remote_rc != 0)); then
        warn "build failed; keeping ${REMOTE_DIR} on ${HOST} for retry/debugging (remove with: ssh ${HOST} 'rm -rf ${REMOTE_DIR}')"
    fi
    if ((remote_rc == 0)); then
        log "build succeeded: ${IMAGE}:${TAG} on ${HOST} (evidence: ${EVIDENCE_DIR}/latest/)"
    else
        log "build failed on ${HOST} (exit ${remote_rc})"
    fi
    exit "${remote_rc}"
}

# ===========================================================================
# --here execution (on the build/deployment host)
# ===========================================================================
run_here() {
    local host_arch base_id avail disk
    command -v docker >/dev/null 2>&1 || die "docker is required on the build host"
    command -v curl >/dev/null 2>&1 || die "curl is required on the build host"
    docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon on this host"
    if ! DOCKER_BUILDKIT=0 docker build --help 2>&1 | grep -q -- '--memory'; then
        die "the legacy builder (DOCKER_BUILDKIT=0 docker build) with --memory support is required; buildx is not available"
    fi

    mkdir -p "${EVIDENCE_DIR}"
    EVID="${EVIDENCE_DIR}/build-${TAG}-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "${EVID}"
    ln -sfn "${EVID}" "${EVIDENCE_DIR}/latest"
    SUMMARY="${EVID}/summary.txt"
    : > "${SUMMARY}"
    trap on_exit EXIT

    note "start: $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(hostname) ($(nproc) vCPU, $(free -m | awk '/^Mem:/{print $2}') MB RAM)"
    note "host: $(free_summary); disk avail=$(disk_available_mb)MB"

    disk="$(disk_available_mb)"
    ((disk >= MIN_DISK_MB)) \
        || die "only ${disk}MB free disk on / (need >= ${MIN_DISK_MB}MB for the build)"
    avail="$(available_mb)"
    FREE_BEFORE="$(free_summary)"

    base_id="$(docker image inspect --format '{{.Id}}' "ubuntu:${UBUNTU_TAG}" 2>/dev/null || true)"
    [[ -n "${base_id}" ]] \
        || die "base image ubuntu:${UBUNTU_TAG} is not present locally and Docker Hub is unreachable; transfer it: docker save ubuntu:${UBUNTU_TAG} | gzip -1 | ssh <host> 'gunzip | docker load'"
    note "base-image: ubuntu:${UBUNTU_TAG} id=${base_id#sha256:}"

    if docker image inspect "${IMAGE}:${TAG}" >/dev/null 2>&1; then
        PREVIOUS_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${IMAGE}:${TAG}" 2>/dev/null || true)"
        note "previous-tagged-image: ${IMAGE}:${TAG} id=${PREVIOUS_IMAGE_ID#sha256:}"
    fi

    detect_existing_container
    if [[ -n "${OLD_CONTAINER}" ]]; then
        note "deployment: found container ${OLD_CONTAINER} (running=${OLD_WAS_RUNNING}, image=${OLD_IMAGE_ID#sha256:})"
    else
        note "deployment: no existing nextautodesk container found"
    fi

    FREE_BEFORE="$(free_summary)"
    note "free-before: ${FREE_BEFORE}"
    if [[ "${DRY_RUN}" == "true" ]]; then
        note "dry-run: would preserve ${OLD_CONTAINER:-no container} /home/nad (if any), stop it to free RAM,"
        note "dry-run: then DOCKER_BUILDKIT=0 docker build --memory=${MEMORY} --memory-swap=${MEMORY_SWAP} -t ${IMAGE}:${TAG} ."
        note "dry-run: available=$(available_mb)MB disk=$(disk_available_mb)MB base=ubuntu:${UBUNTU_TAG}"
        note "dry-run: deploy=${DEPLOY} smoke-test=$([[ "${SKIP_SMOKE}" == "true" ]] && echo skipped || echo enabled)"
        SUCCESS=true
        return 0
    fi
    preserve_home_state

    stop_existing_container
    create_swap_if_needed

    avail="$(available_mb)"
    ((avail >= MIN_FREE_MB)) \
        || die "only ${avail}MB RAM available after stopping the deployment (need >= ${MIN_FREE_MB}MB); increase swap with --swap-mb"
    note "free-before-build: $(free_summary); available=${avail}MB"

    start_samplers

    build_image || die "image build failed; previous deployment state will be restored"
    verify_built_image || die "built image verification failed"
    run_smoke_test || die "container smoke test failed"

    if [[ "${DEPLOY}" == "true" ]]; then
        deploy_new_image || die "redeploying ${IMAGE}:${TAG} failed"
        note "deploy: deployment now runs ${IMAGE}:${TAG}; previous image ${OLD_IMAGE_ID#sha256:} kept for rollback"
    else
        DEPLOY_RESULT="disabled (build only)"
        note "deploy: --deploy not given; the image is built and tagged but the deployment was not switched"
        restart_previous_container
    fi

    SUCCESS=true
    stop_samplers
    summarise_peaks
    FREE_AFTER="$(free_summary)"
    note "free-after: ${FREE_AFTER}"
    cleanup_build_cache
    note "done: ${IMAGE}:${TAG} id=${NEW_IMAGE_ID#sha256:}"
}

if [[ "${MODE}" == "host" ]]; then
    run_orchestrator
else
    run_here
fi
