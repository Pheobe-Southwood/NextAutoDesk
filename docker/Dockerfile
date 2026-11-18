# NextAutoDesk runtime image
#
# Headless X11/XFCE GUI automation node:
#   Ubuntu 24.04 + Xvfb + XFCE 4 + PyAutoGUI + FastAPI, supervised by supervisord
#   (Xvfb, XFCE session and the HTTP API all run as the non-root user "nad").
#
# Build from the repository root (the build context must contain pyproject.toml,
# src/, config/, docker/ and third_party/):
#
#   docker build -f docker/Dockerfile -t nextautodesk:0.1.0-alpha1 .
#   docker build -f docker/Dockerfile --target base -t nextautodesk:base .
#
# scripts/build-image.sh wraps the above and forwards HTTP(S)_PROXY build args.
#
# Stages
# ------
#   base : OS desktop stack + non-root user + Python virtualenv with the pinned
#          runtime dependencies (no application code). Can be built and validated
#          on its own (scripts/base-smoke-test.sh) while the Python packages are
#          still being written.
#   app  : base + NextAutoDesk package + supervisord/entrypoint runtime scripts.
#          This is the final image (last stage is the default build target).
#
# Build args
# ----------
#   UBUNTU_TAG                (24.04)        base image tag
#   APP_VERSION               (0.1.0-alpha1) OCI label only
#   USER_UID / USER_GID       (1000 / 1000)  ids of the "nad" runtime user
#   WITH_OPTIONAL_STRATEGIES  (true)         install HumanMoveMouse + WindMouse
#   INSTALL_VNC               (false)        reserved: install x11vnc + enable its
#                                            supervisord program (never default)
#   PIP_INDEX_URL             (unset)         optional pip index (mirror) used while
#                                            installing the runtime dependencies;
#                                            useful when the default PyPI route is
#                                            slow/unreliable. Not persisted in the image.
#
# VNC is reserved for a later phase: the default image does not contain any VNC
# server and listens only on the API port.

ARG UBUNTU_TAG=24.04

# =============================================================================
# base: operating-system desktop stack, non-root user, Python virtualenv
# =============================================================================
FROM ubuntu:${UBUNTU_TAG} AS base

ARG DEBIAN_FRONTEND=noninteractive
ARG USER_UID=1000
ARG USER_GID=1000
ARG WITH_OPTIONAL_STRATEGIES=true
ARG PIP_INDEX_URL=

# Build-time network settings. BuildKit consumes these while running RUN steps
# (the build host may only reach the internet through an HTTP(S) proxy); they are
# never persisted in the resulting image.
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG http_proxy
ARG https_proxy
ARG NO_PROXY
ARG no_proxy

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV LANG=C.UTF-8 \
    LANGUAGE=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    HOME=/home/nad \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XDG_RUNTIME_DIR=/home/nad/.xdg-runtime \
    XDG_CONFIG_HOME=/home/nad/.config \
    XDG_CACHE_HOME=/home/nad/.cache \
    DISPLAY=:99

# Single layer: desktop stack + tools. --no-install-recommends keeps the XFCE
# meta-package lean (no office/browser/multimedia extras); xfce4-session brings
# xfwm4 (EWMH window manager) and xfdesktop, which wmctrl/xdotool rely on.
#
# Screen perception: OpenCV is installed from the pip wheel
# (opencv-python-headless, pulled in by pyproject.toml); Tesseract is the OS OCR
# engine with the English and Simplified-Chinese language data.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        dbus-x11 \
        fontconfig \
        fonts-dejavu-core \
        fonts-wqy-microhei \
        libgl1 \
        locales \
        procps \
        python3 \
        python3-tk \
        python3-venv \
        scrot \
        supervisor \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
        tzdata \
        wmctrl \
        x11-utils \
        x11-xserver-utils \
        xdotool \
        xfce4 \
        xfce4-terminal \
        xvfb \
 && ln -snf /usr/share/zoneinfo/UTC /etc/localtime \
 && echo "UTC" > /etc/timezone \
 && fc-cache -f >/dev/null 2>&1 \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
 && rm -rf /usr/share/doc/*/changelog* /usr/share/doc/*/NEWS* || true

# Unprivileged runtime user. The container runs as this user (USER nad below);
# /data is the single persistent volume and must be writable by this uid.
# The current ubuntu:24.04 image ships a default "ubuntu" user with uid/gid 1000;
# remove it first so "nad" can own the conventional 1000:1000 ids.
RUN set -eux; \
    if id -u ubuntu >/dev/null 2>&1; then userdel --remove ubuntu || true; fi; \
    if getent group ubuntu >/dev/null 2>&1; then groupdel ubuntu || true; fi; \
    groupadd --gid "${USER_GID}" nad; \
    useradd --uid "${USER_UID}" --gid "${USER_GID}" --create-home \
        --home-dir /home/nad --shell /bin/bash nad; \
    mkdir -p /data/logs /data/screenshots /app/config \
             /home/nad/.xdg-runtime /home/nad/.config /home/nad/.cache \
             /tmp/.X11-unix; \
    chown -R nad:nad /data /home/nad; \
    chmod 0700 /home/nad/.xdg-runtime; \
    chmod 1777 /tmp/.X11-unix

# Python virtualenv with pinned build tooling. Pinning keeps rebuilds
# deterministic; bump deliberately, not implicitly.
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/python -m pip install --no-cache-dir --upgrade \
        "pip==26.3" \
        "setuptools==84.2.1" \
        "wheel==0.48.0"

# Runtime Python dependencies are derived from pyproject.toml (single source of
# truth) instead of being duplicated here. Optional strategy extras are appended
# when WITH_OPTIONAL_STRATEGIES=true.
#
# "evdev" (a pynput dependency, itself required by HumanMoveMouse) only ships an
# sdist and compiles three small CPython extensions against the Linux input
# headers, so a throw-away C toolchain (gcc + python3-dev + linux-libc-dev) is
# installed for this step and purged again in the same layer to keep the runtime
# image lean.
COPY docker/gen-requirements.py /usr/local/bin/nad-gen-requirements.py
COPY pyproject.toml /app/pyproject.toml
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends gcc python3-dev linux-libc-dev; \
    /opt/venv/bin/python /usr/local/bin/nad-gen-requirements.py \
        /app/pyproject.toml /tmp/requirements.txt "${WITH_OPTIONAL_STRATEGIES}"; \
    pip_args=(--no-cache-dir --no-build-isolation); \
    if [[ -n "${PIP_INDEX_URL}" ]]; then \
        echo "using pip index-url: ${PIP_INDEX_URL}"; \
        pip_args+=(--index-url "${PIP_INDEX_URL}"); \
    fi; \
    /opt/venv/bin/pip install "${pip_args[@]}" -r /tmp/requirements.txt; \
    rm -f /tmp/requirements.txt; \
    apt-get purge -y --auto-remove gcc python3-dev linux-libc-dev; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/*

# PyAutoGUI 0.9.54 declares the abandoned "python3-Xlib" 0.15 fork as its Linux
# dependency, while this project pins python-xlib 0.33. Both distributions ship
# the same Xlib/ package, so keep exactly one implementation: drop python3-Xlib
# and reinstall python-xlib last, which restores all Xlib files from 0.33.
RUN set -eux; \
    /opt/venv/bin/pip uninstall -y python3-Xlib || true; \
    /opt/venv/bin/pip install --no-cache-dir --no-deps --force-reinstall \
        "python-xlib==0.33"; \
    /opt/venv/bin/python -c "import Xlib; print('python-xlib ->', Xlib.__file__)"

# =============================================================================
# app: application package, runtime scripts/configuration (final image)
# =============================================================================
FROM base AS app

ARG UBUNTU_TAG=24.04
ARG APP_VERSION=0.1.0-alpha1
ARG INSTALL_VNC=false

# The desktop is a real X11 session (Xvfb). pyscreeze (used by PyAutoGUI's
# screenshot path) only enables its scrot backend when XDG_SESSION_TYPE is
# "x11"; without it screen capture fails even though X11 + scrot are present.
ENV XDG_SESSION_TYPE=x11

LABEL org.opencontainers.image.title="NextAutoDesk" \
      org.opencontainers.image.description="Headless X11/XFCE GUI automation node: human-like mouse & keyboard policies, screen element perception (template matching + OCR), screenshots, window management, serial task queue and HTTP API." \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="docker.io/library/ubuntu:${UBUNTU_TAG}" \
      org.opencontainers.image.vendor="NextAutoDesk contributors"

WORKDIR /app

# Install the package into /opt/venv as a regular wheel (no editable install, no
# build tooling needed at runtime). Dependencies are already present in the base
# stage, hence --no-deps.
COPY pyproject.toml /app/pyproject.toml
COPY README.md /app/README.md
COPY src/ /app/src/
RUN /opt/venv/bin/pip install --no-cache-dir --no-build-isolation --no-deps /app \
 && /opt/venv/bin/python -c \
        "from importlib.metadata import version; print('nextautodesk', version('nextautodesk'))"

# Deployment configuration: the example overlay is also the built-in default so
# the image starts with sane values; API keys and most overrides come from the
# environment (see entrypoint.sh). A deployment can mount its own config dir at
# /app/config (see deploy/docker-compose.yml).
COPY config/nextautodesk.example.yml /app/config/nextautodesk.yml
COPY config/nextautodesk.example.yml /app/config/nextautodesk.example.yml
# Build-context files keep their host mode, and some checkouts have them 0600;
# the non-root runtime user must be able to read the effective configuration, so
# normalise the modes explicitly (both the default and the fallback copy).
RUN chmod 0644 /app/config/nextautodesk.example.yml /app/config/nextautodesk.yml \
 && mkdir -p /opt/nad/config \
 && install -m 0644 /app/config/nextautodesk.example.yml /opt/nad/config/nextautodesk.yml

# License material shipped with the public image. The default image bundles
# optional third-party strategy implementations (e.g. GPL-3.0 WindMouse), so the
# project LICENSE, the consolidated notices and the vendored binary license are
# kept readable at /app/licenses.
COPY LICENSE /app/licenses/LICENSE
COPY THIRD_PARTY_NOTICES.md /app/licenses/THIRD_PARTY_NOTICES.md
COPY third_party/clack/LICENSE /app/licenses/clack-LICENSE
RUN chmod 0644 /app/licenses/LICENSE /app/licenses/THIRD_PARTY_NOTICES.md \
        /app/licenses/clack-LICENSE

# Vendored Clack CLI (optional keyboard-rhythm strategy). Verified against the
# SHA-256 recorded in third_party/clack/SHA256SUMS before it is installed.
COPY third_party/clack/ /tmp/clack/
RUN set -eux; \
    cd /tmp/clack; \
    grep -E 'clack$' SHA256SUMS | sed -E 's#  .*/clack$#  clack#' > /tmp/clack.sha256; \
    sha256sum -c /tmp/clack.sha256; \
    install -m 0755 /tmp/clack/clack /usr/local/bin/clack; \
    rm -rf /tmp/clack /tmp/clack.sha256

# Process supervision + container bootstrap.
COPY docker/supervisord.conf /opt/nad/supervisord.conf
COPY docker/entrypoint.sh /opt/nad/entrypoint.sh
COPY docker/start-xfce.sh /opt/nad/start-xfce.sh
COPY docker/wait-for-desktop.sh /opt/nad/wait-for-desktop.sh
COPY docker/start-api.sh /opt/nad/start-api.sh
COPY docker/config-to-env.py /opt/nad/config-to-env.py
COPY docker/healthcheck.sh /opt/nad/healthcheck.sh
COPY docker/vnc.conf.disabled /opt/nad/conf.d/vnc.conf.disabled
RUN chmod 0755 /opt/nad/*.sh /opt/nad/config-to-env.py \
 && chmod 0644 /opt/nad/supervisord.conf /opt/nad/conf.d/vnc.conf.disabled \
 && mkdir -p /data/logs /data/screenshots \
 && chown -R nad:nad /data

# Reserved VNC support (optional, not installed by default). Building with
# --build-arg INSTALL_VNC=true installs x11vnc and enables its supervisord
# program; deploy/docker-compose.vnc.example.yml is the matching overlay.
RUN set -eux; \
    if [ "${INSTALL_VNC}" = "true" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends x11vnc; \
        apt-get clean; \
        rm -rf /var/lib/apt/lists/*; \
        cp /opt/nad/conf.d/vnc.conf.disabled /opt/nad/conf.d/vnc.conf; \
    fi

# Drop privileges: everything at runtime (supervisord and all supervised
# processes) runs as "nad".
USER nad

EXPOSE 8765
VOLUME ["/data"]

STOPSIGNAL SIGTERM

# The probe targets /readyz (X11 display + serial executor + writable data
# directory), not the cheap public /healthz liveness endpoint. The start period
# covers Xvfb + XFCE + FastAPI cold start. The probe script resolves the
# *effective* API port (the entrypoint publishes it in /tmp/nad-api-port), so a
# config that changes server.port keeps working.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
    CMD ["/opt/nad/healthcheck.sh"]

ENTRYPOINT ["/opt/nad/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-c", "/opt/nad/supervisord.conf"]
