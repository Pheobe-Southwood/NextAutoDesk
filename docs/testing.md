# Testing

NextAutoDesk is validated on **Linux/X11**: unit tests are headless, while the
integration and container suites run against a real Xvfb/XFCE desktop inside the
Docker image. There is no macOS/Windows validation path — the drivers and
runtime are X11-specific by design.

## 1. Test layout

| Path | Scope | Requirements |
| --- | --- | --- |
| `tests/unit/` | Configuration, models, strategy generation, registries, executor, task state machine, API (FastAPI `TestClient` with fake drivers), perception locators/service/actions | Python only; no X11 required |
| `tests/integration/` | Real X11 driver/provider behaviour, alternative strategy providers | Running X11 display (container) |
| `tests/unit/test_engine_integration.py` | The real-X11 engine suite: screen capture, pointer/keyboard injection, Unicode typing, window management | Running X11 display; skipped without `DISPLAY` |

Pytest is configured in `pyproject.toml` (`pythonpath = ["src"]`, async mode,
markers `integration` and `network`).

## 2. Local development

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev,humanmove_mouse,windmouse]"

pytest tests/unit -q          # headless unit suite
ruff check src tests          # lint (E, F, W, I, B, UP, C4, SIM, RUF)
```

OCR-dependent unit tests are skipped automatically when the Tesseract binary is
not installed; install `tesseract-ocr` + `tesseract-ocr-eng` (and
`tesseract-ocr-chi-sim` if you exercise Chinese OCR) to run them locally. The
full-image tests use Tesseract from the Docker base image.

Integration tests need an X11 display and the container tooling:

```bash
# run the real-X11 engine suite against an image (throws away the container)
./scripts/engine-e2e-test.sh --image <image-tag>
```

## 3. Container and image tests

These scripts require Docker and run on a Linux host:

| Script | What it checks |
| --- | --- |
| `scripts/base-smoke-test.sh <image>` | Desktop stack only: Xvfb/XFCE, window manager, PyAutoGUI, human-movement import, strategy providers |
| `scripts/container-smoke-test.sh --image <tag>` | Full image: supervisord programs, X11 resolution, API auth, screenshot PNG, pointer movement, keyboard input into a real terminal, task queue, log/data layout |
| `scripts/engine-e2e-test.sh --image <tag>` | Real-X11 integration pytest suite (drivers + human-movement + Unicode + windows) inside a throw-away container |
| `deploy/vps/verify.sh --here` | Post-deployment HTTP/desktop/task/perception verification on the deployment host |
| `deploy/vps/verify.sh --host <target>` | Same verification, driven from a remote build host over SSH |

Smoke-test containers are removed on success (use `--keep` to inspect a failure).
The image itself ships without pytest; the engine suite installs it into a
throw-away directory inside the container.

## 4. CI

`.github/workflows/ci.yml` has two jobs:

1. **lint-and-test** (Python 3.12, every push/PR): installs the package with dev
   extras and the Tesseract runtime, runs `ruff check src tests`, then
   `pytest tests/unit -q`.
2. **docker-build** (main pushes and manual dispatches): builds the runtime image
   with the same Dockerfile users build, inspects the image user/version, and
   runs `scripts/container-smoke-test.sh` against it.

The OCR tests therefore run for real in CI, and the container job validates that
the image starts, serves the API and can capture/type on X11.

## 5. End-to-end verification

After a deployment, `deploy/vps/verify.sh` exercises a live node: public probes,
authentication, action catalogue/strategies, pointer/screen/windows, keyboard
input into a terminal, task submit/poll/cancel, perception locators and
find/OCR, an interaction click, and container health (running, healthy, no OOM
kill). It exits non-zero if any check fails and prints a failure summary. Use
`--skip-perception` for an older image without the perception tier; see
[deployment.md](deployment.md#6-verify).

## 6. Linux-only validation

- Unit tests run headless on Linux and are the only supported local test path
  for contributors on non-X11 environments (for example a CI runner).
- The real driver, strategy and container suites require a running X11 display
  and Linux Docker; they are not expected to pass on macOS or Windows.
- The supported runtime is the provided `linux/amd64` container image. Running
  the Python package directly against a non-X11 host is unsupported.
- Tests were written and run against the Ubuntu 24.04 image pinned in
  `docker/Dockerfile`; other distributions may work but are not validated.

## 7. Known limitations

- **X11 dependency.** Anything that injects input or captures a screen needs a
  display; without `DISPLAY` those tests are skipped, not failed.
- **Timing sensitivity.** Human-like strategies run in real time. Long or
  concurrent test runs on a loaded machine can exceed timing tolerances; retry
  on an idle host and prefer the container scripts for consistent conditions.
- **OCR variability.** Tesseract output depends on fonts, language data and
  anti-aliasing. Unit tests render their own images, but a missing language pack
  or font can still skip a case. Do not assert exact OCR strings from arbitrary
  screenshots.
- **Perception needs real rendering.** Template matches depend on the exact
  display resolution/theme; tests that involve perception on a real desktop use
  coordinates/patterns produced by the same virtual screen.
- **No VNC coverage.** VNC is a reserved, disabled integration point and is not
  part of the tested surface.
- **Fake drivers are not X11.** Unit tests use fake drivers to stay fast and
  deterministic; they cannot catch X11 quirks. Use the engine/container suites
  for that.
- **Docker availability.** Container tests, E2E verification and image builds
  need a working Docker Engine/Compose v2; CI provides the former, and the
  deployment scripts require it on the target host.

## 8. Adding tests

- Put new headless tests under `tests/unit/` and keep them independent of X11,
  network access and a running Docker daemon.
- Use the existing fake drivers/store helpers (`tests/unit/test_engine_fakes.py`)
  and FastAPI `TestClient` fixtures for API tests.
- Mark real-display tests with `@pytest.mark.integration` so they can be
  deselected with `-m "not integration"`.
- Never add real keys, hostnames, IPs or personal data to fixtures; use
  placeholders such as `<host>` and generated temporary paths.
- Keep new perception/strategy tests deterministic: synthetic Pillow frames,
  fixed seeds, and skip conditions for optional dependencies.
