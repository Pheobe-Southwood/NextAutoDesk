# Architecture

NextAutoDesk runs one **headless Linux desktop** and exposes it as a single HTTP
automation node. The process model is deliberately boring: one container hosts
Xvfb, an XFCE session and the FastAPI service, supervised by `supervisord`, and a
single serial executor owns every input event.

This document describes the layers, their contracts, and the design decisions
behind them. For the HTTP surface see [api.md](api.md); for screen perception see
[perception.md](perception.md); for the strategy layer see [strategies.md](strategies.md).

## 1. Layers

```
                  ┌────────────────────────────────────────────────────┐
   HTTP clients   │  API layer (FastAPI)                               │
   / task steps   │  auth · unified errors · OpenAPI · tier namespaces │
                  └───────────────────────┬────────────────────────────┘
                                          │ execute_action / submit_task
                  ┌───────────────────────▼────────────────────────────┐
                  │  Runtime layer                                     │
                  │  DesktopService facade · SerialExecutor (1 worker) │
                  │  TaskManager + SQLite · health · structured logs   │
                  └───────────────────────┬────────────────────────────┘
                                          │ ActionContext
                  ┌───────────────────────▼────────────────────────────┐
                  │  Action layer (actions/)                           │
                  │  primitive · perception · composite actions        │
                  │  strict Pydantic params → orchestration            │
                  └───────┬──────────────────────────────┬─────────────┘
                          │ perception queries           │ input operations
          ┌───────────────▼──────────────┐   ┌───────────▼─────────────┐
          │ Perception layer             │   │ Strategy layer          │
          │ LocatorRegistry              │   │ human_movement (default)│
          │ template (OpenCV)            │   │ human_move_mouse        │
          │ text (Tesseract)             │   │ windmouse · clack       │
          │ PerceptionService            │   │ direct · entry points   │
          └───────────────┬──────────────┘   └───────────┬─────────────┘
                          │ screenshot bytes / PIL frame   │ driver primitives
          ┌───────────────▼────────────────────────────────▼─────────────┐
          │  Driver layer (drivers/)                                     │
          │  MouseDriver · KeyboardDriver · ScreenDriver · WindowDriver  │
          │  PyAutoGUI / Xlib / xdotool / wmctrl                         │
          └───────────────────────────────┬──────────────────────────────┘
                                          │ X11
          ┌───────────────────────────────▼──────────────────────────────┐
          │  Desktop runtime: Docker + Xvfb :99 + XFCE 4 (xfwm4)         │
          └──────────────────────────────────────────────────────────────┘
```

Key decoupling points:

- **Drivers are dumb.** They inject an event or read state immediately and never
  contain pacing, trajectories or OCR/matching algorithms.
- **Strategies own behaviour.** A provider decides *how* an input is performed
  (trajectory, key rhythm, jitter, seeding) and only uses the driver primitives
  exposed by `DriverBundle`.
- **Perception owns interpretation.** Locators turn a captured frame into
  `ElementMatch` objects. They never capture screenshots, encode images or talk to
  HTTP, storage or drivers.
- **Actions own intent.** An action validates request parameters and composes
  lower layers; composite actions call other actions through
  `ActionContext.invoke` instead of duplicating input code.
- **The runtime owns order.** Every mutating operation goes through one FIFO
  worker, so mouse/keyboard events from concurrent clients cannot interleave.

## 2. API tiers

The API is classified into four tiers. The tier is exposed as machine-readable
metadata (`GET /api/v1/actions` → `tiers`) and as separate URL namespaces.

| Tier | Meaning | Namespace | Queue behaviour |
| --- | --- | --- | --- |
| `primitive` | Stateless device primitives (pointer, keys, screen, windows, waits) | `/mouse`, `/keyboard`, `/screen`, `/windows`, `/desktop` | Mutating actions use the serial queue; read-only queries bypass it |
| `perception` | Read-only understanding of what is on screen | `/elements` | Always bypasses the queue |
| `composite` | Intent-level operations built from primitives + perception | `/interactions` | Serial queue (mutating) |
| `workflow` | Queued, persisted multi-step tasks | `/tasks` | Serial queue (one job per task) |

Read-only calls must never wait behind a long gesture, which is why the
perception tier and read-only primitives follow a bypass path. The serial queue
is the only writer to the X11 input state.

## 3. Core contracts

### 3.1 Actions and contexts

```python
class ActionHandler(ABC, Generic[ParamsT]):
    name: ClassVar[str]
    category: ClassVar[str]
    tier: ClassVar[Literal["primitive", "perception", "composite"]]
    mutating: ClassVar[bool]
    params_model: ClassVar[type[BaseModel]]
    def info(self) -> ActionInfo: ...
    def execute(self, params: ParamsT, ctx: ActionContext) -> dict[str, Any]: ...
```

`ActionContext` is the execution-scoped toolbox handed to every action: config,
drivers, strategy registry, data store, logger, cancellation event, request/task
correlation ids, and the perception service. It also provides:

- `invoke(action, params)` — parse params with the target action's model, execute
  it and return its output dict. Composite actions use this to delegate to
  `element.wait`, `mouse.click`, `keyboard.type`, etc.
- `check_cancelled()` and `sleep(seconds)` — cooperative cancellation and
  cancel-aware waits.
- `run_strategy(...)` — resolve the configured/overridden provider and execute
  one operation.

Action parameters are strict Pydantic v2 models (`extra="forbid"`; unknown fields
are rejected). Action names are stable API identifiers: adding an action never
renames or changes an existing one.

### 3.2 Perception contract

```python
class Locator(ABC):
    name: ClassVar[str]
    kind: ClassVar[Literal["template", "text"]]
    supported_queries: ClassVar[frozenset[str]]
    def availability(self) -> LocatorAvailability: ...
    def validate_params(self, params: dict[str, Any]) -> None: ...
    def execute(self, query: ElementQuery, frame: Frame,
                params: dict[str, Any]) -> list[ElementMatch]: ...
```

`PerceptionService` is the only component that captures and encodes frames. It:

1. captures the requested region through `ScreenDriver` and decodes it to a PIL
   image once,
2. merges locator defaults from configuration with per-request overrides,
3. resolves saved screenshot templates through the data store (locators never
   receive host paths),
4. calls the selected locator,
5. maps frame-relative results to **absolute screen coordinates** by adding the
   capture region origin,
6. caps results, optionally persists the frame, and returns `LocateResult`.

`ElementMatch.box` / `ElementMatch.center` and all OCR boxes are absolute screen
coordinates. `FrameInfo.region` records which part of the screen was captured.
See [perception.md](perception.md) for the complete model and semantics.

### 3.3 Strategy contract

```python
class StrategyProvider(ABC):
    name: ClassVar[str]
    supported: ClassVar[frozenset[Operation]]
    def availability(self) -> ProviderAvailability: ...
    def validate_params(self, params: dict[str, Any]) -> None: ...
    def execute(request: OperationRequest, params: dict[str, Any],
                ctx: StrategyContext) -> StrategyOutcome: ...
```

Every mouse/keyboard operation (19 in total: 12 mouse + 7 keyboard) has an
independent strategy entry under `strategies.operations`. A request may override
the provider for one call (`"strategy": "direct"`) when
`server.allow_strategy_override` is enabled.

Four cross-request state primitives (`mouse_down`, `mouse_up`,
`keyboard_key_down`, `keyboard_key_release`) default to `direct`: the
human-movement engine releases the inputs it holds when a request finishes, so it
cannot keep a button/key held across HTTP requests. The other 15 operations
default to `human_movement`.

### 3.4 Execution and tasks

- `SerialExecutor` runs one worker thread with a bounded FIFO queue. A
  synchronous action request is one job; a task is one job that executes several
  steps serially.
- Queue routing is derived from `mutating`: mutating actions run on the serial
  executor, while read-only actions (`mutating=False`) execute inline and never
  enter the execution queue.
- Timeouts use cooperative cancellation: the executor sets the context's cancel
  event, and actions/providers check it at safe points. A job that exceeds its
  deadline is reported as `ACTION_TIMEOUT`/`TASK_TIMEOUT`.
- Task state (`queued`, `running`, `succeeded`, `failed`, `cancelled`,
  `timed_out`, `interrupted`) is persisted in SQLite. On process restart,
  unfinished tasks are marked `interrupted`; completed history is retained
  according to `tasks.retention_days` / `tasks.max_records`.
- Composite interactions reuse the same action layer and therefore the same
  queue. `interaction.click` and `interaction.type` locate the target with
  `element.wait`/`element.find`, then invoke the primitive mouse/keyboard
  actions; they contain no duplicated input logic.

### 3.5 Errors and correlation

Failures use a stable error code and a single JSON envelope:

```json
{"error": {"code": "ELEMENT_NOT_FOUND", "message": "...", "details": {}, "request_id": "..."}}
```

Perception adds: `PERCEPTION_UNAVAILABLE`, `LOCATOR_UNAVAILABLE`,
`LOCATOR_NOT_SUPPORTED`, `ELEMENT_NOT_FOUND`, `PERCEPTION_FAILED`. Existing codes
and HTTP mappings remain unchanged. Every request gets an `X-Request-ID`
(generated or propagated from the header) that is reused in access and action
logs. See [api.md](api.md#3-error-model) for the complete table.

### 3.6 Configuration

Configuration precedence, lowest to highest:

1. package default — `src/nextautodesk/config/default.yml`;
2. deployment overlay — `NEXT_AUTODESK_CONFIG` or `--config`, deep-merged;
3. environment overrides — API keys, bind host/port, data dir, display,
   resolution, log level.

The perception block is hot-reloadable, like strategies:
`POST /api/v1/config/reload` rebuilds the strategy registry and perception
locators. Changes to `desktop`, `driver` or `storage`, and app-construction
settings such as the listening host/port and CORS, require a container restart.

## 4. Repository layout

```
src/nextautodesk/
  operations.py          # Operation enum (stable strategy/config identifiers)
  config.py              # Pydantic settings, YAML deep merge, env overrides
  errors.py              # stable error codes and domain exceptions
  models.py              # cross-layer domain models
  storage.py             # /data layout, screenshot store, retention
  execution.py           # SerialExecutor: one worker, FIFO, timeout/cancel
  service.py             # DesktopService facade
  bootstrap.py           # application context construction
  health.py              # deep health report
  logging_setup.py       # structured logging
  drivers/               # PyAutoGUI / Xlib / xdotool / wmctrl adapters
  strategies/            # provider contracts, registry, built-in providers
  perception/
    models.py            # transport-agnostic perception models
    base.py              # Frame, Locator ABC, TextExtractor protocol
    registry.py          # LocatorRegistry (+ entry points)
    service.py           # PerceptionService (capture/orchestration)
    locators/            # template.py (OpenCV), text.py (Tesseract)
  actions/               # action handlers: primitive, element.*, interaction.*
  tasks/                 # SQLite task store and manager
  api/                   # FastAPI app, routes, schemas, auth, error handlers
docker/                  # Dockerfile, supervisord, entrypoint, start scripts
config/                  # deployment overlay example
deploy/                  # compose, deployment/verification scripts
scripts/                 # image build and container test helpers
tests/unit/              # headless unit tests
tests/integration/       # real-X11 tests (skippable without a desktop)
docs/                    # this documentation
```

## 5. Extension points

| Extension | Mechanism |
| --- | --- |
| Behaviour provider | Subclass `StrategyProvider`; register via `nextautodesk.strategies` entry point or drop into `strategies/providers/` |
| Perception locator | Subclass `Locator`; register via `nextautodesk.locators` entry point |
| Action | Subclass `ActionHandler` with an `ACTIONS` list in a module under `actions/`; list the module in `actions/registry.py` (or pass `module_names` to a custom registry) |
| Driver backend | Implement the driver protocols and extend `drivers/factory.py` |
| Deployment config | Overlay YAML (deep merge) and environment variables |

Locators and providers are instantiated at startup and on `config/reload`; an
entry point that fails to import is logged and ignored, so a broken optional
plugin cannot prevent the node from starting. A configured provider that is
unavailable returns `409 STRATEGY_UNAVAILABLE`; a known locator whose dependency
is missing is listed as `available=false` and returns `409 LOCATOR_UNAVAILABLE`,
while an unknown locator name or invalid locator configuration is rejected as a
configuration error when the service loads or reloads.

## 6. Design decisions

### Reuse mature projects instead of re-implementing algorithms

- **Human-like input** defaults to [human-movement](https://pypi.org/project/human-movement/)
  (MIT), which derives timing/trajectories from measured human input, Fitts' law
  and minimum-jerk profiles. Optional providers use
  [HumanMoveMouse](https://github.com/TomokotoKiyoshi/HumanMoveMouse) (MIT,
  pre-trained trajectories) and
  [WindMouse](https://github.com/AsfhtgkDavid/windmouse) (GPL-3.0-only; included
  in the default image, excluded with `--no-optional-strategies`).
- **Template matching** uses OpenCV (`cv2.matchTemplate` with normalized
  correlation and non-maximum suppression) rather than hand-rolled pixel
  comparison.
- **Text recognition** uses the Tesseract OCR engine through `pytesseract`;
  layout grouping and matching sit on top of Tesseract's word/line output.
- Text typing rhythm can optionally use the Clack CLI; the default remains
  human-movement.

This keeps behaviour auditable, avoids inventing pseudo-science for
human-likeness, and makes licence obligations explicit. The runtime image bundles
these third-party components and ships the project `LICENSE` plus
`THIRD_PARTY_NOTICES.md` under `/app/licenses/`
(see [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md)).

### One serial writer for input

X11 input is global state. Two clients moving the same pointer concurrently would
produce non-deterministic clicks. A single worker thread plus FIFO admission
makes every mutating request and task step ordered; read-only queries bypass the
queue to keep monitoring responsive. The queue is bounded and fails fast with
`429 QUEUE_FULL` rather than growing without limit.

### Absolute coordinates at the API boundary

The API exposes absolute screen pixels for element matches and OCR results.
Frame-relative coordinates stay internal to locators. This avoids a common
integration mistake: a match computed inside a crop region must not be clicked
without adding the region origin. `PerceptionService` performs that translation
once, and `interaction.*` consumes the absolute center directly.

### Read-only perception is never queued

Perception captures a frame and runs matching/OCR without changing desktop state.
Making it always bypass the queue lets an orchestrator poll for a window or
button while a long typing task is running. Mutating follow-ups (clicks, typing)
still go through the single writer.

### Linux/X11 only, by construction

The runtime depends on Xvfb/X11, XFCE/xfwm4, Xlib/PyAutoGUI and X11 CLI tools.
The supported deployment is the Docker image on an amd64 Linux host; the Python
package targets Linux and is not validated on macOS or Windows (see
[testing.md](testing.md)).

### VNC is a reserved integration point

The default image contains no VNC server. A build argument and a compose overlay
exist for later use; the HTTP API is the only enabled access path. This keeps the
default attack surface to one authenticated port.
