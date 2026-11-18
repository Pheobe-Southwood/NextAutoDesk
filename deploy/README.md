# Deployment (Docker)

This directory contains everything needed to **build, transfer, run and verify**
a NextAutoDesk node as a single Docker container: one headless XFCE desktop plus
one HTTP API.

For the full production guide (configuration, operations, resource planning,
rollback) see [docs/deployment.md](../docs/deployment.md). This file is the
directory-level quick reference.

## Layout

```text
deploy/
  docker-compose.yml               # production compose file
  .env.example                     # copy to .env and set the API key
  docker-compose.vnc.example.yml   # reserved VNC overlay (not enabled by default)
  README.md                        # this file
  vps/
    install-docker.sh              # one-time Docker install on Ubuntu
    deploy.sh                      # build host → remote host over SSH (image streamed)
    build-on-host.sh               # memory-guarded build on the deployment host
    host-test-env.sh               # Python env + Tesseract for on-host test runs
    upload-image-chunked.sh        # resumable image transfer for slow/lossy links
    verify.sh                      # end-to-end verification (--host or --here)
    _common.sh                     # shared helpers (version/tag derivation)
```

Related files:

```text
docker/Dockerfile                  # multi-stage image (base + app)
docker/supervisord.conf            # Xvfb + XFCE + API supervision
docker/entrypoint.sh               # config validation, /data + XDG setup
docker/healthcheck.sh              # container health probe
scripts/build-image.sh             # build wrapper (proxy-aware)
scripts/base-smoke-test.sh         # desktop-stack-only smoke test
scripts/container-smoke-test.sh    # full-image smoke test
scripts/engine-e2e-test.sh         # real-X11 engine suite against the image
config/nextautodesk.example.yml    # configuration overlay example
```

## Runtime layout

| Component | Details |
| --- | --- |
| Base image | `ubuntu:24.04`, pinned; non-root runtime user `nad` (uid/gid 1000) |
| Display | Xvfb `:99`, 1920x1080x24, DPI 96, no TCP listener |
| Desktop | XFCE 4 session, xfwm4 (EWMH window manager) |
| API | FastAPI/uvicorn on `0.0.0.0:8765`, prefix `/api/v1`, probes `/healthz`, `/readyz`; container `HEALTHCHECK` uses `/readyz` |
| Supervisor | `supervisord` manages `xvfb` → `xfce` → `api`, auto-restart |
| Data volume | host dir mounted at `/data`: SQLite, screenshots, rotating logs |
| Config | `/app/config` read-only mount; package defaults are used when empty |
| Licenses | `/app/licenses/`: project `LICENSE`, `THIRD_PARTY_NOTICES.md` and `clack-LICENSE` bundled in the image |
| Env | API keys and overrides from `.env` (`NEXT_AUTODESK_*`) |
| VNC | not installed/not enabled; reserved via build arg + compose overlay |

The image tag is derived from `project.version` in `pyproject.toml`
(`0.1.0a1` → `nextautodesk:0.1.0-alpha1`). Compose requires `NAD_IMAGE_TAG`;
`deploy/.env.example` ships the matching value and the scripts rewrite it from
`pyproject.toml`. Override it to pin or roll back.

## Option A — build elsewhere, transfer the image

Use this when a build machine/CI has Docker and the deployment host only needs to
run the image.

### 1. Build

```bash
# from the repository root
./scripts/build-image.sh
# equivalent:
docker build -f docker/Dockerfile -t nextautodesk:<tag> .
```

Optional variants:

```bash
./scripts/build-image.sh --target base -t nextautodesk:base   # desktop stack only
./scripts/build-image.sh --no-optional-strategies            # no HumanMoveMouse/WindMouse
./scripts/build-image.sh --with-vnc -t nextautodesk:vnc      # reserved VNC variant
```

The default image includes the optional GPL-3.0 `windmouse` strategy; use
`--no-optional-strategies` to exclude the optional extras. `/app/licenses/`
always contains the project `LICENSE`, `THIRD_PARTY_NOTICES.md` and the Clack
licence for redistribution compliance.

Proxy: export `HTTP_PROXY`/`HTTPS_PROXY` and use the wrapper; it forwards proxy
build args and builds with the host network namespace.

### 2. Check the image locally

```bash
./scripts/base-smoke-test.sh nextautodesk:<tag>
./scripts/container-smoke-test.sh --image nextautodesk:<tag>
./scripts/engine-e2e-test.sh --image nextautodesk:<tag>   # optional real-X11 suite
```

The smoke-test containers are removed on success (`--keep` keeps a failure for
debugging). They never leave a permanently running service.

### 3. Deploy over SSH

```bash
./deploy/vps/deploy.sh --host <user>@<deploy-host>
./deploy/vps/deploy.sh --host <user>@<deploy-host> --allow-firewall
./deploy/vps/deploy.sh --host <user>@<deploy-host> --rotate-key
./deploy/vps/deploy.sh --host <user>@<deploy-host> --image nextautodesk:<tag>
```

`deploy.sh` preflights the remote Docker platform, streams the image
(`docker save | gzip | ssh 'gunzip | docker load'`), writes `.env` atomically
with mode 600, keeps an existing config overlay, starts the compose project and
waits for the readiness probe. It is idempotent; re-running upgrades the image
and restarts the container. `--image` selects an image other than the tag derived
from `pyproject.toml` (for example one built on the host); the tag is written to
`NAD_IMAGE_TAG` in the deployment `.env` for compose. The API key is never printed;
health checks read it from a mode-600 header file.

If the SSH link is slow/lossy, use the resumable chunked upload instead:

```bash
./deploy/vps/upload-image-chunked.sh --host <user>@<deploy-host> \
    --image nextautodesk:<tag> --chunk-size 2097152 --jobs 16 --retries 8

ssh <user>@<deploy-host> 'cd /opt/nextautodesk && docker compose up -d --force-recreate'
```

Prerequisites: `docker`, `zstd`, `split`, `sha256sum` on both sides and
non-interactive SSH. Interrupted uploads resume; the reassembled image is
verified by SHA-256 and image id.

## Option B — build on the deployment host

Use this when the source is on the host and image transfer is impractical.
`build-on-host.sh` runs a memory-guarded Docker build there and can redeploy:

```bash
# from a development machine: rsync the tree, then build/deploy remotely
./deploy/vps/build-on-host.sh --host <user>@<deploy-host> --deploy

# or run directly on the host from a synced checkout
ssh <user>@<deploy-host> 'cd <checkout> && ./deploy/vps/build-on-host.sh --here --deploy'
```

The build never pulls from Docker Hub. `--host` mode streams the
`ubuntu:24.04` base image when it is missing; for `--here`, load it first:

```bash
docker save ubuntu:24.04 | gzip -1 | ssh <user>@<deploy-host> 'gunzip | docker load'
```

Useful flags: `--deploy`, `--pip-index-url <mirror>`, `--dry-run`, `--tag`,
`--memory`, `--skip-smoke-test`, `--no-transfer-base`. Run `--help` for the full
operator list.

What it does: preflight (Docker, legacy builder with `--memory`, disk, base
image, running deployment), preserve the running container's `/home/nad` state,
stop the container to free RAM, add temporary swap when needed, sample memory and
watch for `docker events` OOM records during the build, build with
`--memory`/`--memory-swap`, verify image id/size/version and `OOMKilled=false`,
optionally run the container smoke test, and (with `--deploy`) back up the
compose file/`.env`, install the new compose file, set `NAD_IMAGE_TAG`, start the
stack and wait for the readiness probe, with automatic rollback on failure.
Temporary swap/source/cache are cleaned after the run.

## Manual first deployment

```bash
sudo mkdir -p /opt/nextautodesk/{config,data}
sudo cp deploy/docker-compose.yml deploy/.env.example /opt/nextautodesk/
sudo cp config/nextautodesk.example.yml /opt/nextautodesk/config/nextautodesk.yml

cd /opt/nextautodesk
sudo cp .env.example .env
sudo sed -i "s/replace-with-openssl-rand-hex-32/$(openssl rand -hex 32)/" .env
sudo chmod 600 .env

# the container runs as uid/gid 1000 ("nad")
sudo chown -R 1000:1000 data
sudo chmod 644 config/nextautodesk.yml

sudo docker compose up -d
sudo docker compose ps
curl -fsS http://127.0.0.1:8765/healthz
```

Defaults: API on `0.0.0.0:8765`, data in `./data`, config overlay in `./config`.
`.env.example` ships `NAD_IMAGE_TAG=0.1.0-alpha1`; compose fails fast if the tag
is missing. `deploy.sh`/`build-on-host.sh` rewrite it from `pyproject.toml`.

If Docker is not installed:
`./deploy/vps/install-docker.sh` (on the host) or
`curl -fsSL https://get.docker.com | sudo sh` (includes the Compose plugin).

## Verify

```bash
./deploy/vps/verify.sh --host <user>@<deploy-host>
./deploy/vps/verify.sh --host <user>@<deploy-host> --local
./deploy/vps/verify.sh --here       # on the deployment host, no SSH
```

The script reads the API key from `<deploy-dir>/.env` (`/opt/nextautodesk` by
default) over SSH by default (`--key` overrides), exercises health/auth,
actions/strategies, primitive endpoints, keyboard input into a real terminal, the
task queue, perception/interaction endpoints and container health. It exits
non-zero with a failure summary if any check fails. Useful flags:
`--deploy-dir DIR`, `--container NAME`, `--port PORT` and
`--skip-perception` (for an image built before the perception tier existed).
`--here` is the no-SSH mode for on-host builds.

Manual spot checks:

```bash
KEY=$(grep ^NEXT_AUTODESK_API_KEYS .env | cut -d= -f2)
BASE=http://127.0.0.1:8765
curl -fsS $BASE/healthz
curl -fsS -H "X-API-Key: $KEY" $BASE/api/v1/info
curl -fsS -H "X-API-Key: $KEY" -X POST $BASE/api/v1/mouse/move \
  -H 'Content-Type: application/json' -d '{"x": 960, "y": 540}'
curl -fsS -H "X-API-Key: $KEY" -o /tmp/screen.png $BASE/api/v1/screen/screenshot
file /tmp/screen.png
```

## Operations

```bash
cd /opt/nextautodesk
docker compose logs -f --tail=100          # supervisord + API console
docker compose restart                     # restart the stack
docker compose down                        # remove container, keep data
docker compose up -d                       # start
docker compose exec nextautodesk supervisorctl -c /opt/nad/supervisord.conf status
docker compose exec nextautodesk ps -eo user,pid,args
docker stats --no-stream nextautodesk-nextautodesk-1
```

Logs live in `data/logs/` (`supervisord.log`, `xvfb.log`, `xfce.log`,
`api.log`); `docker logs` shows API stdout/stderr and the supervisor console.

### Upgrade

```bash
# build host: rebuild and redeploy
./scripts/build-image.sh
./deploy/vps/deploy.sh --host <user>@<deploy-host>
```

On-host workflow: re-sync source, re-run `build-on-host.sh --here --deploy`.

### Rollback

The previous deployed image id is kept by `build-on-host.sh` for automatic
rollback on a failed `--deploy`. For a manual rollback, point `NAD_IMAGE_TAG` at
the old tag and recreate the container:

```bash
ssh <user>@<deploy-host> 'cd /opt/nextautodesk && \
  sed -i "s/^NAD_IMAGE_TAG=.*/NAD_IMAGE_TAG=<previous-tag>/" .env && \
  docker tag <previous-image-id> nextautodesk:<previous-tag> 2>/dev/null || true'
ssh <user>@<deploy-host> 'cd /opt/nextautodesk && docker compose up -d --force-recreate'
```

### Backup

```bash
ssh <user>@<deploy-host> 'tar czf /tmp/nextautodesk-data-$(date +%Y%m%d).tar.gz \
  -C /opt/nextautodesk data'
```

Also back up `.env` and the config overlay (or keep them outside the host).

## Memory / OOM notes

- Compose sets `mem_limit: 2g`, `memswap_limit: 2g`, `pids_limit: 512`,
  `shm_size: 256m` and a 256 MB `/tmp` tmpfs. Idle usage is well below the limit;
  peaks come from screenshot encoding and long gestures.
- The build is the memory peak. `build-on-host.sh` refuses to start below a
  configurable free-memory threshold and builds with explicit memory limits; do
  not lower those limits without checking `docker events` for OOM.
- On smaller hosts, lower `mem_limit` (for example `1.5g`), reduce screenshot
  retention and avoid concurrent heavy actions. One container per desktop node;
  do not run multiple nodes in one container.
- Monitor with `docker stats` and `GET /api/v1/health` (runtime memory/disk/CPU).

## VNC (reserved)

The default image contains no VNC server. The reserved integration point:

1. build with `--with-vnc` / `--build-arg INSTALL_VNC=true`;
2. start with
   `docker compose -f docker-compose.yml -f docker-compose.vnc.example.yml up -d`.

The overlay publishes `127.0.0.1:5900` only. Use an SSH tunnel; x11vnc has no TLS
and must never be exposed publicly.

## On-host test environment

`host-test-env.sh` prepares a Python virtualenv and the OS packages needed to
validate the checkout on the deployment host (Tesseract + English language data,
DejaVu fonts, dev tools). It does not install the project itself: pytest uses
`pythonpath = ["src"]`. It is optional and intended for maintainers who validate
a release on the same host that runs it.

```bash
sudo ./deploy/vps/host-test-env.sh --root /root/nad-test --repo /path/to/checkout \
    --pip-index-url <pypi-mirror>     # optional, faster on hosts far from PyPI
/root/nad-test/venv/bin/ruff check src tests
/root/nad-test/venv/bin/pytest tests/unit -q
```

`--check` validates the environment without installing anything; `--skip-apt`
assumes the OS packages are already present.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Container exits with a data-directory error | Host `data/` not owned by `1000:1000` → `sudo chown -R 1000:1000 data` |
| `configuration validation failed` | Invalid YAML or missing API key in `.env` |
| `/readyz` returns 503 during startup | XFCE/API still starting; wait and check `docker compose logs` / `supervisorctl status` |
| API 401 on every call | Wrong/missing `X-API-Key`; restart after editing `.env` |
| `ELEMENT_NOT_FOUND` | Tune perception (`threshold`, scales, OCR language/psm), use a tighter region and a debug screenshot; the element may be occluded |
| `OOMKilled=true` | Lower load/host memory pressure, reduce screenshot retention, use the guarded on-host build |
| Image transfer stalls | Rerun `deploy.sh` (idempotent) or switch to `upload-image-chunked.sh` |
| Build fails reaching apt/PyPI | Configure `HTTP_PROXY`/`HTTPS_PROXY` for `scripts/build-image.sh`, or build on a host with network access |
| Port in use | Change `NAD_PORT`, open it in the firewall, recreate the container |
