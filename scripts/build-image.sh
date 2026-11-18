#!/usr/bin/env bash
# Build the NextAutoDesk runtime image.
#
#   ./scripts/build-image.sh [options]
#
# Options:
#   -t, --tag TAG                image tag (default: nextautodesk:<version from
#                                pyproject.toml>, e.g. nextautodesk:1.2.3-alpha1)
#       --target STAGE           build stage: app (default) or base
#       --no-optional-strategies build without HumanMoveMouse/WindMouse extras
#       --with-vnc               reserved: install x11vnc (never enabled by default)
#       --no-cache               rebuild all layers
#       --pull                   always pull the ubuntu base image
#       --progress MODE          buildkit progress output (auto, plain, tty)
#       --platform PLATFORM      target platform, e.g. linux/amd64 (default: native)
#       --with-attestation       keep BuildKit provenance/SBOM attestations
#                                (default: disabled for a deterministic single-platform
#                                image whose docker save/load id is stable)
#   -h, --help                   show this help
#
# Proxy: when HTTP_PROXY/HTTPS_PROXY (or lowercase variants) are set in the
# environment they are forwarded to the build as build args *and* the build uses
# the host network namespace, so apt/pip inside the build container can reach a
# proxy that only listens on the host loopback (e.g. 127.0.0.1:7890).
#
# Examples:
#   ./scripts/build-image.sh
#   ./scripts/build-image.sh --target base -t nextautodesk:base
#   ./scripts/build-image.sh --with-vnc -t nextautodesk:<version>-vnc
#   ./scripts/build-image.sh --platform linux/amd64     # cross-build for a VPS

set -euo pipefail

readonly REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly DOCKER="${DOCKER:-docker}"

# The version/tag are derived from pyproject.toml (single source of truth);
# IMAGE=... and APP_VERSION=... environment variables still override them.
# shellcheck source=deploy/vps/_common.sh
source "${REPO_ROOT}/deploy/vps/_common.sh"

IMAGE="${IMAGE:-$(nad_default_image "${REPO_ROOT}")}"
TARGET="app"
APP_VERSION="${APP_VERSION:-$(nad_default_tag "${REPO_ROOT}")}"
UBUNTU_TAG="${UBUNTU_TAG:-24.04}"
WITH_OPTIONAL_STRATEGIES="${WITH_OPTIONAL_STRATEGIES:-true}"
INSTALL_VNC="${INSTALL_VNC:-false}"
NO_CACHE=false
PULL=false
PROGRESS="auto"
PLATFORM="${PLATFORM:-}"
WITH_ATTESTATION="${WITH_ATTESTATION:-false}"

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed -e 's/^# \{0,1\}//'
}

die() {
    printf 'build-image: error: %s\n' "$*" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -t | --tag)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            IMAGE="$2"
            shift 2
            ;;
        --target)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            TARGET="$2"
            shift 2
            ;;
        --no-optional-strategies)
            WITH_OPTIONAL_STRATEGIES=false
            shift
            ;;
        --with-vnc)
            INSTALL_VNC=true
            shift
            ;;
        --no-cache)
            NO_CACHE=true
            shift
            ;;
        --pull)
            PULL=true
            shift
            ;;
        --progress)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            PROGRESS="$2"
            shift 2
            ;;
        --platform)
            [[ $# -ge 2 ]] || die "$1 requires a value"
            PLATFORM="$2"
            shift 2
            ;;
        --with-attestation)
            WITH_ATTESTATION=true
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

case "${TARGET}" in
    base | app) ;;
    *) die "--target must be 'base' or 'app', got '${TARGET}'" ;;
esac

[[ -f "${REPO_ROOT}/pyproject.toml" ]] || die "pyproject.toml not found in ${REPO_ROOT}"
[[ -f "${REPO_ROOT}/docker/Dockerfile" ]] || die "docker/Dockerfile not found in ${REPO_ROOT}"

build_args=(
    --build-arg "UBUNTU_TAG=${UBUNTU_TAG}"
    --build-arg "APP_VERSION=${APP_VERSION}"
    --build-arg "WITH_OPTIONAL_STRATEGIES=${WITH_OPTIONAL_STRATEGIES}"
    --build-arg "INSTALL_VNC=${INSTALL_VNC}"
    --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL:-}"
)

network_args=()
proxy_args=()
http_proxy_value="${HTTP_PROXY:-${http_proxy:-}}"
https_proxy_value="${HTTPS_PROXY:-${https_proxy:-}}"
no_proxy_value="${NO_PROXY:-${no_proxy:-localhost,127.0.0.1,::1}}"
if [[ -n "${http_proxy_value}" || -n "${https_proxy_value}" ]]; then
    # --network host: the proxy commonly listens on the host loopback only.
    network_args=(--network host)
    proxy_args=(
        --build-arg "HTTP_PROXY=${http_proxy_value}"
        --build-arg "HTTPS_PROXY=${https_proxy_value}"
        --build-arg "http_proxy=${http_proxy_value}"
        --build-arg "https_proxy=${https_proxy_value}"
        --build-arg "NO_PROXY=${no_proxy_value}"
        --build-arg "no_proxy=${no_proxy_value}"
    )
    echo "build-image: forwarding proxy ${https_proxy_value:-${http_proxy_value}} to the build (--network host)"
fi

cmd=(
    "${DOCKER}" build
    --file "${REPO_ROOT}/docker/Dockerfile"
    --tag "${IMAGE}"
    --target "${TARGET}"
    --progress "${PROGRESS}"
    "${network_args[@]}"
    "${proxy_args[@]}"
    "${build_args[@]}"
)
if [[ -n "${PLATFORM}" ]]; then
    cmd+=(--platform "${PLATFORM}")
fi
# BuildKit otherwise attaches provenance/SBOM attestations, turning the image tag
# into a multi-platform manifest list. `docker save` then contains an OCI index
# and `docker load` on the target may produce a different image id, which breaks
# the deploy-time id verification and adds transfer bytes. Keep the default image
# a plain single-platform image; opt back in with --with-attestation.
if [[ "${WITH_ATTESTATION}" != "true" ]] && "${DOCKER}" buildx version >/dev/null 2>&1; then
    cmd+=(--provenance=false --sbom=false)
fi
if [[ "${NO_CACHE}" == "true" ]]; then
    cmd+=(--no-cache)
fi
if [[ "${PULL}" == "true" ]]; then
    cmd+=(--pull)
fi
cmd+=("${REPO_ROOT}")

if [[ -n "${PIP_INDEX_URL:-}" ]]; then
    echo "build-image: using pip index-url ${PIP_INDEX_URL} for runtime dependencies (mirror)"
fi
echo "build-image: target=${TARGET} image=${IMAGE} optional_strategies=${WITH_OPTIONAL_STRATEGIES} install_vnc=${INSTALL_VNC} platform=${PLATFORM:-native} attestation=${WITH_ATTESTATION}"
echo "+ ${cmd[*]}"
"${cmd[@]}"

image_id="$("${DOCKER}" image inspect --format '{{.Id}}' "${IMAGE}" 2>/dev/null || true)"
size_bytes="$("${DOCKER}" image inspect --format '{{.Size}}' "${IMAGE}" 2>/dev/null || true)"
size_mib="$(awk -v b="${size_bytes:-0}" 'BEGIN {printf "%.1f", b / 1048576}')"
echo
echo "build-image: built ${IMAGE} (target ${TARGET})"
echo "build-image: image id ${image_id#sha256:} size ${size_bytes:-unknown} bytes (${size_mib} MiB)"
echo "build-image: note: 'docker image ls' may show a larger content-store size; use .Size above"
if [[ "${TARGET}" == "base" ]]; then
    echo "build-image: validate it with: ./scripts/base-smoke-test.sh ${IMAGE}"
else
    echo "build-image: validate it with: ./scripts/container-smoke-test.sh --image ${IMAGE}"
fi
