# Contributing

Thanks for considering a contribution to NextAutoDesk. This project is a
Python 3.12 automation node for headless Linux/X11 desktops; changes usually
touch the HTTP API, the action/strategy layers, the drivers, or the container
runtime.

## Development setup

```bash
git clone <repository-url>
cd NextAutoDesk

python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev,humanmove_mouse,windmouse]"
```

Optional system dependency for OCR development: install the Tesseract engine and
language data (`tesseract-ocr`, `tesseract-ocr-eng`, optionally
`tesseract-ocr-chi-sim`). Without it, OCR unit tests are skipped rather than
failed.

## Tests

```bash
pytest tests/unit -q                      # headless unit suite (required for PRs)
pytest -m "not integration" -q            # everything that does not need X11
ruff check src tests                      # lint
```

Integration and container tests need a running X11 desktop or Docker:

```bash
./scripts/engine-e2e-test.sh --image <image-tag>
./scripts/container-smoke-test.sh --image <image-tag>
```

Do not run Docker builds on a memory-constrained host; use a build machine or the
deployment host's guarded build script (see [docs/deployment.md](docs/deployment.md)).
The CI workflow runs `ruff`, the unit suite (with Tesseract installed) and a
container smoke test for the runtime image.

Guidelines for tests:

- Unit tests must not require X11, network access or a running Docker daemon.
- Use fake drivers/stores (see `tests/unit/test_engine_fakes.py`) and fixed seeds
  for deterministic behaviour.
- Mark real-display tests with `@pytest.mark.integration` so they can be
  deselected.
- OCR-dependent tests should skip when the Tesseract binary/language data is
  absent, but must pass in the container image.
- Do not assert brittle counts of external resources, wall-clock timings or
  system-dependent OCR text.

## Code style

- Python 3.12, typed signatures; start modules with
  `from __future__ import annotations`.
- Pydantic v2 models: `ConfigDict(extra="forbid")` for request/config payloads.
- Keep modules small and focused; prefer composition over large classes.
- Public action names, operation names and error codes are stable API
  contracts — additive changes only.
- English for code, docstrings, comments, log messages, API messages and tests.
  Chinese belongs only in `README.zh-CN.md`.
- No hand-rolled CV or human-movement algorithms: use the established libraries
  (OpenCV, Tesseract, human-movement, …) and document licences.
- Lint configuration: `ruff` with `E,F,W,I,B,UP,C4,SIM,RUF`, line length 100
  (`pyproject.toml`). Keep `ruff check src tests` clean.
- Do not add new third-party dependencies without prior discussion; runtime pins
  live in `pyproject.toml` and must be reflected in `THIRD_PARTY_NOTICES.md`.

## Pull requests

- Keep changes focused; separate refactors from behaviour changes.
- Add or update tests for every behaviour change, and update the docs in `docs/`
  when an API/endpoint, action, configuration block or deployment step changes.
- Preserve backward compatibility of existing endpoints, action names and error
  codes unless the change is explicitly discussed as breaking (document it in the
  PR description).
- Never commit secrets, API keys, real hostnames/IPs, personal data, private
  screenshots or product-specific references. Use placeholders such as
  `<host>` in examples.
- Do not commit build artifacts, images, databases, logs or local virtualenvs
  (`.gitignore` covers the usual cases).
- Describe the change, the test commands you ran and any known limitations in the
  PR description. If CI cannot cover a platform-specific change, say so
  explicitly.
- Runtime/container changes should be validated against the Docker image, not
  only through unit tests.

## Documentation

Public documentation lives in `docs/` and is written in English; `README.zh-CN.md`
mirrors the English `README.md`. Keep documents user-facing: no internal
acceptance reports or verification logs, no personal infrastructure details, and
no claims about performance or compatibility that have not been validated for the
current release.

## Reporting issues

Include the version (`GET /api/v1/info`), the deployment environment (Linux
distribution, Docker version), the failing request/response with secrets
redacted, and relevant container logs (`docker compose logs`, `/data/logs/`).
