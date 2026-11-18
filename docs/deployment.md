# Deployment

NextAutoDesk ships as a **single Docker container** that contains a headless X11
desktop (Xvfb + XFCE) and the HTTP automation API. One container is one desktop
node; scale horizontally by running one container per node.

- Supported target: Linux `amd64` host with Docker Engine and Compose v2
- Recommended host size: 2 vCPU, 4 GB RAM, ≥10 GB free disk for the image and data
- Persistent data: SQLite task database, saved screenshots, logs (docker volume
  mounted at `/data`)
- Exposure: the API port (default `8765/tcp`) only; VNC is not installed in the
  default image

All commands below use placeholders: replace `<deploy-host>` with the SSH target
of the Docker host and `<api-key>` with the key you generate. Never commit real
hostnames, keys or personal data to the repository.

## 1. Deployment options

Two workflows are supported. Choose the one that matches your infrastructure:

| Workflow | When to use | Entry point |
| --- | --- | --- |
| **Build elsewhere, transfer image** | You have a build machine (laptop/CI) with Docker; the deployment host only runs containers | `scripts/build-image.sh` → `deploy/vps/deploy.sh` |
| **Build on the deployment host** | The source is available on the host (e.g. via rsync/git) and you want no image transfer | `deploy/vps/build-on-host.sh` |

The image tag is derived from the package version in `pyproject.toml` (for
example, `0.1.0a1` becomes `nextautodesk:0.1.0-alpha1`). The release label for
this version is **v0.1.0-alpha1**. Deployment scripts accept an explicit override
when you need a different tag.

## 2. Deploy directory

```text
deploy/
  docker-compose.yml               # production compose file
  .env.example                     # copy to .env and set the API key
  docker-compose.vnc.example.yml   # reserved VNC overlay (disabled)
  README.md                        # deployment quick reference
  vps/
    install-docker.sh              # one-time Docker install on Ubuntu
    deploy.sh                      # build host → remote deploy over SSH
    build-on-host.sh               # memory-guarded build on the deployment host
    host-test-env.sh               # on-host Python/Tesseract test environment
    upload-image-chunked.sh        # resumable image transfer for lossy SSH links
    verify.sh                      # end-to-end verification (--host or --here)
    _common.sh                     # shared version/tag derivation helpers
```

Related files in the repository:

```text
docker/Dockerfile                  # multi-stage image (base + app)
docker/supervisord.conf            # Xvfb + XFCE + API process supervision
docker/entrypoint.sh               # config validation, /data + XDG setup
docker/start-xfce.sh              # waits for X, starts XFCE in a D-Bus session
docker/wait-for-desktop.sh         # waits for X + EWMH window manager
docker/start-api.sh                # waits for the desktop then starts uvicorn
docker/healthcheck.sh              # container health probe
scripts/build-image.sh             # build wrapper (proxy-aware, tag/version args)
scripts/container-smoke-test.sh    # image smoke test
scripts/base-smoke-test.sh         # desktop-stack-only smoke test
scripts/engine-e2e-test.sh         # real-X11 engine suite against the image
config/nextautodesk.example.yml    # deployment configuration overlay example
```

Runtime layout on the host:

```text
/opt/nextautodesk/          (default; configurable in .env/script flags)
├── docker-compose.yml
├── .env                    # mode 600; contains NEXT_AUTODESK_API_KEYS
├── config/
│   └── nextautodesk.yml    # optional overlay, mounted read-only into /app/config
└── data/                   # SQLite, screenshots, logs; writable by uid/gid 1000
```

Inside the image, the project `LICENSE` and `THIRD_PARTY_NOTICES.md` are bundled
under `/app/licenses/`; the image also contains the third-party runtime and system
components described in that notice.

## 3. Build the image

### 3.1 Build on a separate build host (recommended)

```bash
# from the repository root
./scripts/build-image.sh
# equivalent:
docker build -f docker/Dockerfile -t nextautodesk:<image-tag> .
```

`scripts/build-image.sh` options (all optional):

| Option | Description |
| --- | --- |
| `-t, --tag TAG` | Image tag; defaults to `nextautodesk:<version-from-pyproject>` |
| `--target base\|app` | Build just the desktop stack, or the final app image (default) |
| `--no-optional-strategies` | Skip the optional HumanMoveMouse/WindMouse extras |
| `--with-vnc` | Reserved: install x11vnc (never enabled by default) |
| `--platform PLATFORM` | Target platform, e.g. `linux/amd64` (default: native) |
| `--no-cache`, `--pull` | Rebuild all layers / refresh the base image |
| `--progress MODE` | BuildKit progress output |

Build args: `UBUNTU_TAG` (default `24.04`), `USER_UID`/`USER_GID` (default
`1000`, must match the owner of the host `data/` directory),
`WITH_OPTIONAL_STRATEGIES` (default `true`) and `INSTALL_VNC` (default `false`).
If the build needs an HTTP(S) proxy, export `HTTP_PROXY`/`HTTPS_PROXY` before
calling the wrapper; it forwards the proxy build args and uses the host network
namespace for the build.

Licensing: the default image bundles the optional GPL-3.0 `windmouse` strategy
(next to MIT HumanMoveMouse). Build with `--no-optional-strategies` (or
`WITH_OPTIONAL_STRATEGIES=false`) to keep the image free of the optional extras.
The image always ships `/app/licenses/LICENSE`,
`/app/licenses/THIRD_PARTY_NOTICES.md` and `/app/licenses/clack-LICENSE` for
redistribution compliance; see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

Verify the image locally before shipping:

```bash
./scripts/base-smoke-test.sh <image-tag>       # desktop stack only
./scripts/container-smoke-test.sh --image <image-tag>
```

### 3.2 Build on the deployment host

`deploy/vps/build-on-host.sh` copies/uses the source on the host and runs a
memory-guarded Docker build there. It is intended for hosts where transferring a
large image is impractical.

Typical flow:

```bash
# from a machine that can reach the deployment host
rsync -az --exclude .git --exclude data ./ <user>@<deploy-host>:~/nextautodesk-src/

ssh <user>@<deploy-host> 'cd ~/nextautodesk-src && \
    sudo ./deploy/vps/build-on-host.sh --here'
```

Or drive both steps from the workstation with the script's `--host` mode:

```bash
./deploy/vps/build-on-host.sh --host <user>@<deploy-host> --deploy
```

The script stops any existing container before building, checks free memory /
swap to avoid an OOM kill during the build, builds with the legacy builder and
explicit `--memory` limits, records build evidence, and cleans up intermediate
objects. Useful flags: `--deploy` (back up and redeploy `/opt/nextautodesk`,
with automatic rollback on failure), `--pip-index-url <mirror>` (recommended
where direct PyPI is slow), `--dry-run` (preflight only), `--tag` and
`--skip-smoke-test`. Run `./deploy/vps/build-on-host.sh --help` for the complete
list of your checkout.

The build never pulls from Docker Hub. In `--host` mode the script streams the
`ubuntu:24.04` base image when it is missing; in `--here` mode load it first:

```bash
docker save ubuntu:24.04 | gzip -1 | ssh <user>@<deploy-host> 'gunzip | docker load'
```

> Building is the memory peak of the whole lifecycle. On a host with less than
> ~4 GB RAM, prefer the transfer workflow in §3.1 unless the script's memory
> guard reports enough headroom.

## 4. Configure

Copy the environment template and set an API key:

```bash
cp deploy/.env.example .env
chmod 600 .env
# generate a key and write it to .env
openssl rand -hex 32
```

Key variables in `.env`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXT_AUTODESK_API_KEYS` | — (required) | Comma-separated API keys; the container refuses to start without one |
| `NAD_BIND_IP` | `0.0.0.0` | Host interface the API port is published on (use `127.0.0.1` behind an SSH tunnel/reverse proxy) |
| `NAD_PORT` | `8765` | Published API port |
| `NAD_DATA_DIR` | `./data` | Host directory mounted at `/data` |
| `NAD_CONFIG_DIR` | `./config` | Host directory mounted at `/app/config` |
| `NAD_IMAGE_TAG` | `0.1.0-alpha1` (shipped in `.env.example`; required) | Docker image tag compose runs (PEP 440 `0.1.0a1` → `0.1.0-alpha1`); deployment scripts rewrite it from `pyproject.toml`, edit it to pin/roll back |
| `NEXT_AUTODESK_LOG_LEVEL` | `INFO` | API log level |
| `TZ` | `UTC` | Container timezone |

The compose file requires `NAD_IMAGE_TAG`; `deploy/.env.example` already ships the
matching release tag, and deployment scripts rewrite it from `pyproject.toml`.
Edit it to pin a different build or roll back.

The optional configuration overlay is a YAML file deep-merged over the package
defaults. Start from `config/nextautodesk.example.yml`. Common overrides: API
port, desktop resolution, storage retention, logging, strategy selection and the
perception block.

```bash
mkdir -p config data
cp config/nextautodesk.example.yml config/nextautodesk.yml
```

The container runs as uid/gid `1000` (`nad`); the host data directory must be
writable by that user:

```bash
sudo chown -R 1000:1000 data
```

The config overlay must be readable by uid 1000 (secrets belong in `.env`, not in
the YAML):

```bash
chmod 644 config/nextautodesk.yml
```

## 5. Run

### 5.1 Compose (manual)

```bash
# from the directory containing docker-compose.yml and .env
sudo docker compose up -d
sudo docker compose ps
curl -fsS http://127.0.0.1:8765/healthz
```

The compose file sets `restart: unless-stopped`, a health check, log rotation,
`mem_limit: 2g` + `memswap_limit: 2g`, `pids_limit`, `shm_size` and a small
`/tmp` tmpfs. The container `HEALTHCHECK` probes `/readyz` (desktop + executor +
storage), while `/healthz` remains the cheap liveness probe. Adjust `mem_limit`
in a local override if your host is smaller — see §8.

### 5.2 Automated deploy over SSH (image already built)

```bash
./deploy/vps/deploy.sh --host <user>@<deploy-host>
./deploy/vps/deploy.sh --host <user>@<deploy-host> --allow-firewall  # open the API port in ufw
./deploy/vps/deploy.sh --host <user>@<deploy-host> --rotate-key      # regenerate the API key
./deploy/vps/deploy.sh --host <user>@<deploy-host> --image nextautodesk:<image-tag>
```

The script checks the remote Docker platform, streams the local image
(`docker save | gzip | ssh 'gunzip | docker load'`), writes `.env` atomically
with mode 600 (preserving an existing key unless `--rotate-key`), preserves an
existing config overlay, starts the compose project and waits for the readiness
probe. It is idempotent: re-running it upgrades the image and restarts the
container. The API key is never printed; health checks receive it through a
mode-600 header file. `--image` deploys an image other than the derived default
(for example one built on the host); the tag is written to `NAD_IMAGE_TAG` in
the deployment `.env` for compose.

For a slow or lossy link where the single stream stalls, use the resumable
chunked transfer instead:

```bash
./deploy/vps/upload-image-chunked.sh --host <user>@<deploy-host> \
    --image nextautodesk:<image-tag> --chunk-size 2097152 --jobs 16 --retries 8

ssh <user>@<deploy-host> 'cd /opt/nextautodesk && docker compose up -d --force-recreate'
```

Prerequisites for the chunked helper: `docker`, `zstd`, `split`, `sha256sum`
locally and remotely, and non-interactive SSH (`BatchMode`). It resumes
interrupted uploads and verifies the reassembled image by SHA-256 / image id.

### 5.3 Direct host build

When using `build-on-host.sh`, the container is started from the same compose
project after the build. With `--deploy` the script backs up and installs the
compose file, writes `NAD_IMAGE_TAG`/`.env`, starts the stack and waits for the
readiness probe (rolling back to the previous container on failure). Without
`--deploy`, only the image is built (the previous container is restored); point
`NAD_IMAGE_TAG` at the new tag first, then recreate:

```bash
ssh <user>@<deploy-host> 'cd /opt/nextautodesk && \
  sed -i "s/^NAD_IMAGE_TAG=.*/NAD_IMAGE_TAG=<new-tag>/" .env 2>/dev/null || \
  echo "NAD_IMAGE_TAG=<new-tag>" >> .env'
ssh <user>@<deploy-host> 'cd /opt/nextautodesk && sudo docker compose up -d --force-recreate'
```

## 6. Verify

`deploy/vps/verify.sh` runs an end-to-end check from the machine that can reach
the deployment:

```bash
./deploy/vps/verify.sh --host <user>@<deploy-host>
./deploy/vps/verify.sh --host <user>@<deploy-host> --local   # force local HTTP
./deploy/vps/verify.sh --here                                # run on the deployment host
```

`--here` is for builds/deployments performed on the host itself (no SSH). Other
flags: `--port`, `--key`, `--container`, `--deploy-dir` (default
`/opt/nextautodesk`) and `--skip-perception` (for images built before the
perception tier existed). The script checks the public probes, authentication,
actions/strategies, the primitive endpoints, keyboard input into a real terminal,
the task queue, the perception/interaction endpoints, and container health. It
exits non-zero unless all checks pass. It reads the API key from the deployment
`.env` over SSH by default; pass `--key` if you keep the key elsewhere.

Minimal manual check:

```bash
curl -fsS http://<deploy-host>:8765/healthz
curl -fsS -H "X-API-Key: <api-key>" http://<deploy-host>:8765/api/v1/info
curl -fsS -H "X-API-Key: <api-key>" \
  -X POST http://<deploy-host>:8765/api/v1/mouse/move \
  -H 'Content-Type: application/json' -d '{"x": 960, "y": 540}'
curl -fsS -H "X-API-Key: <api-key>" -o /tmp/screen.png \
  http://<deploy-host>:8765/api/v1/screen/screenshot
file /tmp/screen.png
```

## 7. Operations

```bash
cd /opt/nextautodesk

sudo docker compose logs -f --tail=100        # supervisord + API console
sudo docker compose restart                   # restart the stack
sudo docker compose down                      # stop and remove the container (data kept)
sudo docker compose up -d                     # start again

sudo docker compose exec nextautodesk supervisorctl -c /opt/nad/supervisord.conf status
sudo docker compose exec nextautodesk ps -eo user,pid,args
sudo docker stats --no-stream nextautodesk-nextautodesk-1
```

Logs:

| Path | Contents |
| --- | --- |
| `/data/logs/supervisord.log` | supervisor start/stop/restart events |
| `/data/logs/xvfb.log` | virtual X server output |
| `/data/logs/xfce.log` | XFCE session output |
| `/data/logs/api.log` | rotating structured API logs |
| `docker logs` | API stdout/stderr plus supervisord console (compose log rotation) |

### Upgrade

```bash
# build host
git pull
./scripts/build-image.sh
./deploy/vps/deploy.sh --host <user>@<deploy-host>
```

The deploy script transfers the new image, recreates the container and preserves
`/data` and the config overlay. With the on-host workflow, re-sync the source and
re-run `build-on-host.sh`, then `docker compose up -d --force-recreate`.

### Rollback

Keep the previous image tag (or image id) on the host. To roll back, point
`NAD_IMAGE_TAG` at the old tag and recreate the container:

```bash
ssh <user>@<deploy-host> 'cd /opt/nextautodesk && \
  sed -i "s/^NAD_IMAGE_TAG=.*/NAD_IMAGE_TAG=<old-tag>/" .env && \
  docker tag <previous-image-id> nextautodesk:<old-tag> 2>/dev/null || true && \
  docker compose up -d --force-recreate'
```

`deploy.sh`/`build-on-host.sh` also keep the previous image id, and a failed
`--deploy` rolls back automatically. Database files are forward compatible
within a release line; restoring a backup is the safer rollback if a migration
is involved.

### Backup

```bash
ssh <user>@<deploy-host> 'tar czf /tmp/nextautodesk-data-$(date +%Y%m%d).tar.gz \
  -C /opt/nextautodesk data'
```

Back up `.env` and `config/nextautodesk.yml` separately, or keep them outside the
container as described in §4.

### Configuration changes

- Behavioural settings (strategies, perception, task limits, logging, auth and
  request limits) can be applied with
  `POST /api/v1/config/reload`.
- `desktop`, `driver`, `storage` and app-construction settings (bind host/port,
  CORS, docs URLs) require `docker compose restart`.

## 8. Resource limits and OOM guidance

The compose file caps the container so a runaway action cannot take down the
host:

| Setting | Default | Why |
| --- | --- | --- |
| `mem_limit` / `memswap_limit` | `2g` / `2g` | Headroom for screenshots and long gestures on a 4 GB host |
| `pids_limit` | `512` | Prevents process leaks from the XFCE session |
| `shm_size` | `256m` | X11 shared memory |
| `/tmp` tmpfs | `256m` | Keeps temporary image data off the disk image |
| health check | every 30 s | Detects a stuck desktop/API |
| log rotation | 10 MB × 3 | Bounds json-file growth |

Guidance:

- On a host with ~2 GB RAM total, lower `mem_limit` (e.g. `1.5g`) and avoid
  concurrent heavy jobs. The desktop stack idles below 1 GB; peak usage comes
  from screenshot encoding and long human-like gestures.
- Do not build the image on a host that is already tight on memory; use the
  transfer workflow or `build-on-host.sh`'s memory guard.
- Reduce `storage.max_screenshots` and `storage.screenshot_retention_days` if
  disk usage grows.
- Use one container per desktop node; do not run multiple nodes in one container.
- Monitor with `docker stats`, `GET /api/v1/health` (runtime memory/disk/CPU) and
  the health check status.

## 9. VNC (reserved, disabled)

The default image does **not** install or start any VNC server. The reserved
integration point is:

1. build with `--with-vnc` / `INSTALL_VNC=true` (installs `x11vnc` and enables
   its supervisord program);
2. start with the overlay:

   ```bash
   docker compose -f deploy/docker-compose.yml \
                  -f deploy/docker-compose.vnc.example.yml up -d
   ```

The overlay publishes `127.0.0.1:5900` only. Access it through an SSH tunnel
(`ssh -L 5900:127.0.0.1:5900 <user>@<deploy-host>`) and never expose port 5900
publicly: x11vnc has no TLS. The HTTP API remains the supported automation
interface.

## 10. Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| Container exits with a data-directory error | Host `data/` not owned by `1000:1000` → `sudo chown -R 1000:1000 data` |
| `configuration validation failed` | Invalid YAML, or auth enabled without `NEXT_AUTODESK_API_KEYS`; check `.env` and `config/nextautodesk.yml` |
| `/readyz` returns 503 for a while | XFCE/API still starting; check `docker compose logs` and `supervisorctl status` |
| API returns 401 on every call | Missing/wrong `X-API-Key`; keys come from `.env` |
| `PERCEPTION_UNAVAILABLE` / `LOCATOR_UNAVAILABLE` | Perception disabled in config, or OpenCV/Tesseract not available in the image; check `GET /elements/locators` and the container logs |
| `ELEMENT_NOT_FOUND` for a visible element | Tighten the region, adjust `threshold`/scales or OCR language/`psm`; capture a debug screenshot and inspect it; the element may be occluded |
| High memory / OOM kill | `docker stats`; lower `mem_limit`/retention, avoid concurrent screenshots on a small host; check `OOMKilled` in `docker inspect` |
| Image transfer stalls | Stable link: rerun `deploy.sh` (idempotent). Lossy link: use `upload-image-chunked.sh`, then recreate the container |
| Build fails reaching apt/PyPI | Export `HTTP_PROXY`/`HTTPS_PROXY` and use `scripts/build-image.sh` (proxy-aware), or use `build-on-host.sh` on a host with direct network access |
| Port already in use | Change `NAD_PORT` and restart; open the new port in the firewall if needed |
