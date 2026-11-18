# HTTP API reference

NextAutoDesk exposes one headless X11 desktop over HTTP. Every interaction with
the desktop — primitive input, screen perception, composed intent and queued
workflows — goes through the same authenticated API.

- Base URL: `http://<host>:8765`
- API prefix: `/api/v1`
- Interactive docs: `GET /docs` (Swagger UI), `GET /openapi.json`
- Public probes: `GET /`, `GET /healthz`, `GET /readyz`

The v0.1 surface contains **50 authenticated HTTP operations across 47 path
templates**, plus the three public probes — 50 unique paths and 53 operations in
total. The perception and interaction tiers add 6 operations to the 44
pre-existing authenticated operations. Paths and action names are stable API
identifiers; new capabilities are additive.

## 1. Authentication

The service is authenticated with static API keys. At least one key must be
configured in the deployment environment (`NEXT_AUTODESK_API_KEYS`, comma
separated) — otherwise the container refuses to start.

Two header forms are accepted:

```bash
# default header
curl -H "X-API-Key: $KEY" http://<host>:8765/api/v1/info

# Bearer token
curl -H "Authorization: Bearer $KEY" http://<host>:8765/api/v1/info
```

The header name is configurable (`auth.header`). Missing or invalid credentials
return `401` with the unified error envelope and `WWW-Authenticate: ApiKey`.
`/`, `/healthz`, `/readyz` and (by default) `/docs` + `/openapi.json` are public;
set `auth.protect_docs: true` to require a key for the documentation endpoints as
well.

## 2. Conventions

- Request and response bodies are JSON (`Content-Type: application/json`), except
  `GET /screen/screenshot`, which returns image bytes.
- Request models are strict: unknown fields are rejected with `422`.
- Every response carries `X-Request-ID`; a caller-supplied `X-Request-ID` header
  is propagated into logs and the error envelope.
- Timestamps are ISO 8601 UTC (`...Z`).
- Distances are pixels; times are milliseconds unless noted.
- Booleans, `null` and numbers follow JSON conventions; omitted optional fields
  fall back to action defaults or configuration values.

## 3. Error model

All failures use one envelope, including framework validation and routing errors:

```json
{
  "error": {
    "code": "ELEMENT_NOT_FOUND",
    "message": "element not found within 5000 ms",
    "details": {
      "query": {"locator": "text",
                "text": {"text": "Sign in", "match": "contains", "case_sensitive": false}},
      "attempts": 25,
      "timeout_ms": 5000
    },
    "request_id": "01J..."
  }
}
```

`code` is a stable identifier. Table below lists codes emitted by this version:

| HTTP | Error code | Meaning |
| --- | --- | --- |
| 400 | `BAD_REQUEST` | Framework-level malformed HTTP request |
| 400 | `INVALID_ARGUMENT` | Semantically invalid argument (bad key name, invalid window id, strategy override disabled, …) |
| 400 | `LOCATOR_NOT_SUPPORTED` | Locator exists but does not handle this query kind |
| 401 | `AUTH_MISSING` | No API key supplied |
| 401 | `AUTH_INVALID` | API key does not match any configured key |
| 403 | `FORBIDDEN` | Framework-level request is not permitted |
| 404 | `UNKNOWN_ACTION` | Unknown action name (HTTP or task step) |
| 404 | `UNKNOWN_OPERATION` | Unknown strategy operation |
| 404 | `TASK_NOT_FOUND` | Task id does not exist |
| 404 | `WINDOW_NOT_FOUND` | Window id/title selector matched nothing |
| 404 | `SCREENSHOT_NOT_FOUND` | Saved screenshot name does not exist |
| 404 | `ELEMENT_NOT_FOUND` | Wait/interaction target did not appear before the deadline |
| 404 | `NOT_FOUND` | Unknown route/resource |
| 405 | `METHOD_NOT_ALLOWED` | HTTP method not supported for this path |
| 409 | `CONFLICT` | Framework-level conflict |
| 409 | `STRATEGY_UNAVAILABLE` | Provider is disabled, missing a dependency, or does not support the operation |
| 409 | `STRATEGY_NOT_SUPPORTED` | Provider cannot execute the requested operation |
| 409 | `TASK_NOT_CANCELLABLE` | Task is already in a terminal state |
| 409 | `TASK_CANCELLED` | Cooperative cancellation interrupted the operation |
| 409 | `PERCEPTION_UNAVAILABLE` | Perception is disabled or the service is unavailable |
| 409 | `LOCATOR_UNAVAILABLE` | Locator is unknown, disabled or missing a dependency |
| 413 | `REQUEST_TOO_LARGE` | Body exceeds `server.max_request_body_bytes` (default 2 MiB) |
| 422 | `VALIDATION_ERROR` | Request body/params failed validation |
| 429 | `TOO_MANY_REQUESTS` | Framework-level rate limit triggered |
| 429 | `QUEUE_FULL` | Serial execution queue is full (fails fast while busy) |
| 500 | `CONFIG_INVALID` | Configuration is invalid |
| 500 | `RELOAD_FAILED` | Hot reload was rejected or failed |
| 500 | `STORAGE_ERROR` | Screenshot store / data directory failure |
| 500 | `INTERNAL_ERROR` | Unexpected server error |
| 502 | `ACTION_FAILED` | Action failed below the domain layer |
| 502 | `SCREENSHOT_FAILED` | Screen capture/encoding failed |
| 502 | `STRATEGY_EXECUTION_FAILED` | Provider failed while performing an operation |
| 502 | `TASK_FAILED` | A task failed |
| 502 | `WINDOW_ACTION_FAILED` | Window manager action failed |
| 502 | `PERCEPTION_FAILED` | Frame capture, OpenCV matching or Tesseract OCR failed |
| 502 | `DRIVER_ERROR` | Raw X11/PyAutoGUI driver failure below the action layer |
| 503 | `DESKTOP_UNAVAILABLE` | X11 desktop is not available |
| 503 | `DRIVER_UNAVAILABLE` | A required driver is unavailable |
| 503 | `EXECUTOR_UNAVAILABLE` | Serial executor / application context is not ready |
| 504 | `ACTION_TIMEOUT` | Single action exceeded its timeout |
| 504 | `TASK_TIMEOUT` | Task exceeded its timeout |

`GET /readyz` returns `503` with a non-error body when the node is not ready yet.

## 4. Common action options and result

Mouse and keyboard action endpoints accept two optional fields:

| Field | Type | Description |
| --- | --- | --- |
| `strategy` | `string \| null` | Provider override for this call, e.g. `human_movement`, `direct`, `windmouse`. Must be enabled and support the operation, otherwise `409 STRATEGY_UNAVAILABLE`. Can be disabled server-wide with `server.allow_strategy_override: false` (then the request is rejected with `400 INVALID_ARGUMENT`). |
| `timeout_ms` | `int (1..3600000)` | Execution timeout for this call; defaults to `tasks.step_timeout_ms`. |

Related options on other tiers:

- `/interactions/*` accept `strategy` with exactly the same validation as the
  mouse/keyboard endpoints (`400 INVALID_ARGUMENT` when
  `server.allow_strategy_override=false`, `409 STRATEGY_UNAVAILABLE` for an
  unknown/unavailable provider). It is a service option applied by the HTTP
  layer, not an action parameter. Interactions do not accept `timeout_ms`; their
  latency is bounded by `wait_timeout_ms` plus the configured action timeouts.
- `POST /elements/wait` uses `timeout_ms` as its own element-wait budget.
  `POST /elements/find` and `POST /elements/ocr` are read-only/inline calls and
  do not accept an execution timeout.
- `/desktop/wait` accepts only `ms` (0..600000); it does not accept `strategy`
  or `timeout_ms` and executes inline, bounded by `ms`. `/windows/*` accepts
  neither field and uses the configured action timeout.

Mouse, keyboard, window, desktop-wait and interaction endpoints return
`ActionResult`; `POST /screen/screenshot` and the element endpoints return their
tier-specific payloads instead:

```json
{
  "action": "mouse.move",
  "success": true,
  "started_at": "2026-11-18T12:00:00Z",
  "finished_at": "2026-11-18T12:00:00.341Z",
  "duration_ms": 341.2,
  "output": {"position": {"x": 960.0, "y": 540.0}, "requested": {"x": 960, "y": 540}},
  "strategies": [
    {"operation": "mouse_move", "provider": "human_movement",
     "provider_version": "0.1.6", "duration_ms": 338.7,
     "engine": "human-movement", "details": {"events": 54}}
  ]
}
```

`output` is action-specific (documented per endpoint); `strategies` records which
provider performed the operation, its duration and engine metadata.

## 5. Coordinate semantics

- The virtual screen origin `(0, 0)` is the top-left corner; `x` grows right and
  `y` grows down. The default screen is `1920x1080`.
- All pointer, window, screenshot region and perception coordinates are
  **absolute screen pixels**. Fractional coordinates are accepted by pointer
  actions where documented.
- A screenshot/perception `region` is `{"x", "y", "width", "height"}` in screen
  pixels. Omitting it captures the whole screen.
- Perception results are already translated: `ElementMatch.box`,
  `ElementMatch.center` and OCR boxes are absolute, even when a `region` was
  requested. See [perception.md](perception.md#4-coordinate-semantics).
- `interaction.*` uses the absolute `match.center` internally and adds the
  optional `offset_x`/`offset_y` before clicking.

## 6. API tiers and discovery

| Tier | Namespace | Description |
| --- | --- | --- |
| `primitive` | `/mouse`, `/keyboard`, `/screen`, `/windows`, `/desktop` | Device primitives; mutating calls are serialized |
| `perception` | `/elements` | Read-only screen understanding; never queued |
| `composite` | `/interactions` | Intent-level operations composed from primitives + perception |
| `workflow` | `/tasks` | Persisted multi-step tasks executed serially |

Tier discovery is machine-readable:

- `GET /api/v1/actions` includes `tiers: {"primitive": [...], "perception": [...], "composite": [...]}`
  and `count` (35 actions in this release).
- `GET /api/v1/info` includes `features` flags such as
  `"screen_element_perception": true` and `"api_tiers": ["primitive","perception","composite","workflow"]`,
  plus a `perception` block with `enabled` and locator availability.

## 7. System endpoints (public, no API key)

### `GET /`

Service banner.

```json
{
  "name": "NextAutoDesk",
  "version": "0.1.0a1",
  "api_version": "v1",
  "status": "ok",
  "api": "/api/v1",
  "docs": "/docs",
  "healthz": "/healthz",
  "readyz": "/readyz"
}
```

### `GET /healthz`

Cheap liveness probe; never touches X11.

```json
{"status": "ok", "version": "0.1.0a1", "api_version": "v1", "uptime_s": 12.4}
```

### `GET /readyz`

Readiness probe: desktop, executor and storage. Returns `200` when ready and
`503` otherwise.

```json
{"ready": true, "version": "0.1.0a1",
 "checks": {"desktop": "ok", "executor": "running", "storage": "ok"}}
```

## 8. Meta endpoints

### `GET /api/v1/info`

Capabilities of the node.

```json
{
  "name": "nextautodesk",
  "version": "0.1.0a1",
  "api_version": "v1",
  "display": ":99",
  "screen": {"width": 1920, "height": 1080},
  "desktop": {"resolution": "1920x1080x24", "window_manager": "xfwm4"},
  "actions": ["desktop.wait", "element.find", "..."],
  "providers": [{"name": "human_movement", "available": true, "...": "..."}],
  "perception": {
    "enabled": true,
    "locators": [{"name": "template", "kind": "template", "available": true,
                   "supported_queries": ["template"]}]
  },
  "features": {
    "human_like_default": true,
    "per_operation_strategy_config": true,
    "screen_element_perception": true,
    "api_tiers": ["primitive", "perception", "composite", "workflow"],
    "tasks": true,
    "persistence": true,
    "authentication": true,
    "vnc": "reserved"
  },
  "executor": {"state": "running", "pending": 0, "running": false, "completed": 0},
  "uptime_s": 12.4
}
```

`desktop.window_manager` is probed once at startup and is a string or `null`;
there is no per-request probe and no separate `desktop.vnc` key — VNC remains
`features.vnc = "reserved"`. `display` is the configured X display in `:N`
form (for example `:99`).

### `GET /api/v1/health?deep=true|false`

Aggregated health report. `deep=true` (default) probes X11, the window manager,
strategy providers and perception locator availability; `deep=false` reads only
in-process state (the perception check is reported as `unknown`/skipped). The
perception check never captures the screen: it only asks locators whether their
dependencies are installed and enabled. Its status is `degraded` when perception
is disabled by configuration, `error` when no locator is registered or none is
available, and `ok` when at least one locator is available.

Response fields: `status` (`ok` / `degraded` / `unhealthy`), `version`,
`api_version`, `timestamp`, `uptime_s`, `checks` (per-component
`{status,message,details}`) and `runtime` (memory, disk, CPU, screenshot count,
task counts).

### `GET /api/v1/actions`

Action catalogue with JSON Schema for every action.

```json
{
  "count": 35,
  "tiers": {
    "primitive": ["desktop.wait", "keyboard.press", "..."],
    "perception": ["element.find", "element.ocr", "element.wait"],
    "composite": ["interaction.click", "interaction.type"]
  },
  "actions": [
    {"name": "mouse.move", "summary": "Move the pointer to an absolute position",
     "category": "mouse", "tier": "primitive", "mutating": true,
     "params_schema": {"type": "object", "...": "..."},
     "example": {"x": 640, "y": 360}}
  ]
}
```

### `GET /api/v1/strategies`

Effective strategy configuration: one entry per operation (19 operations) with
the active provider, availability, parameters and available candidate providers,
plus a flat provider-availability list. Values that look secret are redacted.

### `GET /api/v1/config`

Effective configuration with API keys and secret-looking values masked.

### `POST /api/v1/config/reload`

Re-read the deployment configuration file and apply hot-reloadable settings:
strategy registry, perception service, task limits/timeouts/retention, logging,
auth and request limits. `desktop`, `driver`, `storage` and app-construction
settings (listening host/port, CORS, docs URLs) are rejected with
`500 RELOAD_FAILED` and a list of fields; restart the container for those.

```json
{"reloaded": true, "config": {"version": 1, "...": "..."}}
```

## 9. Primitive tier — mouse

All POST endpoints accept the common `strategy` / `timeout_ms` options and return
`ActionResult`.

| Method | Path | Summary |
| --- | --- | --- |
| GET | `/api/v1/mouse/position` | Current pointer position |
| GET | `/api/v1/mouse/state` | Position plus currently pressed buttons (read-only) |
| POST | `/api/v1/mouse/move` | Move to absolute coordinates |
| POST | `/api/v1/mouse/move-relative` | Move by an offset |
| POST | `/api/v1/mouse/click` | Click a button (optionally move first) |
| POST | `/api/v1/mouse/down` | Press and hold a button |
| POST | `/api/v1/mouse/up` | Release a button |
| POST | `/api/v1/mouse/hold` | Hold a button for a duration |
| POST | `/api/v1/mouse/drag` | Drag with a held button |
| POST | `/api/v1/mouse/scroll` | Scroll the wheel |

Request fields:

| Endpoint | Fields |
| --- | --- |
| `move` | `x`*, `y`*, `duration_ms?` (0..600000; hint for the driver; pacing normally comes from the strategy) |
| `move-relative` | `dx`*, `dy`* (not both zero), `duration_ms?` |
| `click` | `x?`, `y?`, `button` = `left`/`right`/`middle` (default `left`), `clicks` = 1..3 (default 1; 2 = double click), `interval_ms?` (0..60000) |
| `down` / `up` | `button` (default `left`), `x?`, `y?` |
| `hold` | `button`, `hold_ms?` (0..600000), `x?`, `y?` |
| `drag` | `from_x?`/`from_y?` (both or neither), `to_x`*, `to_y`*, `button`, `duration_ms?` |
| `scroll` | `dx` (−1000..1000), `dy` (−1000..1000; positive = down), at least one non-zero, `x?`, `y?` |

Outputs (inside `ActionResult.output`):

| Action | Output |
| --- | --- |
| `mouse.move` | `position`, `requested`, `duration_ms` |
| `mouse.move_relative` | `position`, `delta` |
| `mouse.click` | `position`, `button`, `clicks` |
| `mouse.down` / `mouse.up` | `position`, `button`, `pressed` |
| `mouse.hold` | `position`, `button`, `hold_ms` |
| `mouse.drag` | `position`, `to`, `button` |
| `mouse.scroll` | `position`, `dx`, `dy`, `detents` |

`GET /mouse/position` returns `{"x": 960.0, "y": 540.0}`.
`GET /mouse/state` returns `{"position": {"x": ...}, "buttons": ["left"]}`.
`mouse.down` and `mouse.up` are cross-request state primitives: the button stays
pressed between requests (default provider `direct`), and `/mouse/state` exposes
it. `human_movement` does not support these four state primitives and returns
`409 STRATEGY_UNAVAILABLE` if forced.

```bash
curl -s -X POST http://<host>:8765/api/v1/mouse/move \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"x": 960, "y": 540}'

curl -s -X POST http://<host>:8765/api/v1/mouse/click \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"x": 760, "y": 420, "clicks": 2, "interval_ms": 120}'

curl -s -X POST http://<host>:8765/api/v1/mouse/move \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"x": 500, "y": 500, "strategy": "direct"}'
```

## 10. Primitive tier — keyboard

| Method | Path | Summary |
| --- | --- | --- |
| POST | `/api/v1/keyboard/type` | Type text character by character (Unicode supported) |
| POST | `/api/v1/keyboard/press` | Press and release one key |
| POST | `/api/v1/keyboard/hotkey` | Press keys together |
| POST | `/api/v1/keyboard/sequence` | Press keys one after another |
| POST | `/api/v1/keyboard/key/down` | Press and hold one key |
| POST | `/api/v1/keyboard/key/up` | Release one key |
| POST | `/api/v1/keyboard/hold` | Hold one key for a duration |

| Endpoint | Fields |
| --- | --- |
| `type` | `text`* (1..100000 chars) |
| `press` | `key`* (name such as `enter`, `esc`, `tab`, `f1`, `ctrl`, `a`) |
| `key/down`, `key/up` | `key`* |
| `hold` | `key`*, `hold_ms?` (0..600000) |
| `hotkey` | `keys`* (1..8 names) |
| `sequence` | `keys`* (1..256 names) |

Outputs:

| Action | Output |
| --- | --- |
| `keyboard.type` | `typed`, `length` |
| `keyboard.press` | `key` (normalised) |
| `keyboard.hotkey` | `keys` |
| `keyboard.sequence` | `keys`, `count` |
| `keyboard.key_down` / `key_up` | `key`, `pressed` |
| `keyboard.hold` | `key`, `hold_ms` |

Notes:

- `keyboard.type` supports Unicode; non-ASCII characters are injected via Xlib
  keysym remapping, not the clipboard.
- `keyboard_key_down` / `keyboard_key_release` are cross-request state
  primitives (default provider `direct`). Pair them explicitly with
  `keyboard.key/up`; do not rely on cleanup at request end.
- The optional `clack` provider supports ASCII text only; non-ASCII input returns
  `400 INVALID_ARGUMENT`. The default `human_movement` provider supports Unicode.

```bash
curl -s -X POST http://<host>:8765/api/v1/keyboard/type \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"text": "echo hello > /tmp/hello.txt\n"}'

curl -s -X POST http://<host>:8765/api/v1/keyboard/hotkey \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"keys": ["ctrl", "alt", "t"]}'
```

## 11. Primitive tier — screen

| Method | Path | Response |
| --- | --- | --- |
| GET | `/api/v1/screen/size` | `{"width": 1920, "height": 1080}` |
| GET | `/api/v1/screen/screenshot` | Image bytes (`image/png` or `image/jpeg`) |
| POST | `/api/v1/screen/screenshot` | JSON metadata, optionally saved/inline |
| GET | `/api/v1/screen/screenshots?limit=&offset=` | Saved screenshot list |
| GET | `/api/v1/screen/screenshots/{name}` | Download a saved screenshot |
| DELETE | `/api/v1/screen/screenshots/{name}` | Delete a saved screenshot (`204`) |

`GET /screen/screenshot` query parameters:

| Parameter | Default | Description |
| --- | --- | --- |
| `region` | whole screen | `x,y,width,height` in screen pixels |
| `format` | `png` | `png` or `jpeg` |

The response has `Cache-Control: no-store` and an inline `Content-Disposition`.
Capture is read-only and bypasses the serial queue.

`POST /screen/screenshot` body:

| Field | Default | Description |
| --- | --- | --- |
| `region` | `null` | `Rect` in screen pixels |
| `format` | `png` | `png` or `jpeg` |
| `save` | `false` | Persist under the screenshot store |
| `name` | `null` | Optional file name when saving; safe-name grammar `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` |
| `inline` | `false` | Include base64 image bytes for **unsaved** captures |

Response for an unsaved capture (`inline=false`):
`{"format":"png","width":1920,"height":1080,"size_bytes":123456}`.
With `inline=true`, `image_base64` and `mime_type` are added.
For a saved capture, `screenshot` is a `ScreenshotInfo`
(`name`, `size_bytes`, `width`, `height`, `format`, `created_at`, `download_url`).

`GET /screen/screenshots` returns
`{"items": [ScreenshotInfo...], "total": 12, "limit": 50, "offset": 0}`.
Screenshot retention is bounded by `storage.max_screenshots` and
`storage.screenshot_retention_days`.

```bash
curl -s -H "X-API-Key: $KEY" http://<host>:8765/api/v1/screen/size
curl -s -H "X-API-Key: $KEY" -o screen.png http://<host>:8765/api/v1/screen/screenshot
curl -s -H "X-API-Key: $KEY" -o region.jpg \
  "http://<host>:8765/api/v1/screen/screenshot?region=0,0,800,600&format=jpeg"
curl -s -X POST http://<host>:8765/api/v1/screen/screenshot \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"format": "png", "save": true, "name": "login-page"}'
```

## 12. Primitive tier — windows

Windows are based on EWMH (`_NET_CLIENT_LIST` / `_NET_ACTIVE_WINDOW`); the XFCE
`xfwm4` window manager is required.

| Method | Path | Summary |
| --- | --- | --- |
| GET | `/api/v1/windows` | List top-level windows |
| GET | `/api/v1/windows/active` | Current active window (404 when none) |
| GET | `/api/v1/windows/{window_id}` | Window details |
| POST | `/api/v1/windows/{window_id}/activate` | Activate (focus) |
| POST | `/api/v1/windows/{window_id}/move` | `{"x":..,"y":..}` |
| POST | `/api/v1/windows/{window_id}/resize` | `{"width":1..16384,"height":1..16384}` |
| POST | `/api/v1/windows/{window_id}/minimize` | Minimize |
| POST | `/api/v1/windows/{window_id}/maximize` | Maximize |
| POST | `/api/v1/windows/{window_id}/restore` | Restore |
| POST | `/api/v1/windows/{window_id}/close` | Request close (WM_DELETE_WINDOW) |

`window_id` is an X11 window id in hex, optionally prefixed with `0x` (for
example `0x3a00007`). Window actions return `ActionResult` and are serialized;
a missing window returns `404 WINDOW_NOT_FOUND`.

`WindowInfo` fields: `id`, `title`, `class_name`, `instance_name`, `pid`,
`desktop`, `geometry` (`Rect`), `state` (`normal`/`minimized`/`maximized`/
`fullscreen`/`unknown`), `active`, `transient_for`.

```bash
curl -s -H "X-API-Key: $KEY" http://<host>:8765/api/v1/windows | python3 -m json.tool
curl -s -X POST http://<host>:8765/api/v1/windows/0x3a00007/move \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"x": 100, "y": 80}'
```

## 13. Primitive tier — desktop

### `POST /api/v1/desktop/wait`

Cancel-aware wait between steps. Body: `{"ms": 0..600000}` only — no
`strategy` or `timeout_ms` field. Output: `{"waited_ms": 500}`. Runs inline (not
on the serial queue) and is bounded by `ms`, but remains cancellation-aware.

## 14. Perception tier — elements (read-only)

All `/elements` calls bypass the serial queue. They never move the pointer or
change desktop state. `GET /elements/locators` reports which locators are usable
before a query is attempted.

### `GET /api/v1/elements/locators`

```json
{
  "enabled": true,
  "locators": [
    {"name": "template", "kind": "template", "available": true, "reason": null,
     "version": "...", "supported_queries": ["template"]},
    {"name": "text", "kind": "text", "available": true, "reason": null,
     "version": "...", "supported_queries": ["text"]}
  ]
}
```

A missing optional dependency or a disabled locator yields `available: false`
with a human-readable `reason`. This endpoint works even when perception is
disabled, so discovery remains possible.

### `POST /api/v1/elements/find`

Locate elements once. Body:

| Field | Type | Description |
| --- | --- | --- |
| `query` | `ElementQuery` | Required query (see below) |

`ElementQuery` fields:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `locator` | `"auto" \| "template" \| "text"` | `"auto"` | `auto` infers from the provided spec |
| `region` | `Rect \| null` | whole screen | Capture region |
| `template` | `TemplateSpec \| null` | – | Template source + tuning |
| `text` | `TextSpec \| null` | – | Text query + OCR tuning |
| `max_results` | `int \| null` | config (20) | 1..100, also capped by `perception.max_results` |
| `save_screenshot` | `bool` | `false` | Persist the captured frame and return `frame.screenshot` |

Exactly one of `template` / `text` must be provided.

`TemplateSpec` fields: `image_base64?` (raw base64 or data URL), `screenshot?`
(name in the screenshot store), `threshold?` (0..1), `grayscale?`, `scales?`
(0.1..4.0, max 8 entries). Exactly one image source is required.

`TextSpec` fields: `text`* (1..1000), `match` (`contains`/`exact`/`regex`,
default `contains`), `case_sensitive` (default `false`), `languages?`, `psm?`
(0..13), `min_confidence?` (0..100), `granularity?` (`line`/`word`).

The response is a `LocateResult`:

```json
{
  "locator": "text",
  "matches": [
    {"box": {"x": 780, "y": 410, "width": 160, "height": 32},
     "center": {"x": 860.0, "y": 426.0},
     "confidence": 0.884,
     "locator": "text",
     "text": "Sign in",
     "metadata": {"granularity": "line", "ocr_confidence": 88.4}}
  ],
  "frame": {"region": {"x": 0, "y": 0, "width": 1920, "height": 1080},
             "width": 1920, "height": 1080,
             "captured_at": "2026-11-18T12:00:00Z", "screenshot": null},
  "elapsed_ms": 112.3,
  "attempts": 1
}
```

Matches are sorted by confidence; template matches use area as the tie-breaker,
text matches prefer higher (top) positions. Boxes and centers are absolute
screen coordinates. See [perception.md](perception.md) for full tuning and
semantics.

### `POST /api/v1/elements/wait`

Poll until at least one match appears or the deadline is reached. Body:
`query` (same as find), `timeout_ms` (1..600000, default `5000`) and
`poll_interval_ms` (10..5000, default `200`). The response is a `LocateResult`
whose `attempts` is the number of locate attempts and whose `elapsed_ms` is the
wall time. Timeout returns `404 ELEMENT_NOT_FOUND` with `details.timeout_ms`,
`details.attempts` and a redacted `details.query` summary (base64 payloads
omitted, text truncated). With `save_screenshot=true`, polling runs with saving
disabled and exactly one screenshot is persisted when the element is found
(never one per poll).

### `POST /api/v1/elements/ocr`

Run OCR and return lines/words. Body:

| Field | Type | Description |
| --- | --- | --- |
| `region` | `Rect \| null` | Capture region; whole screen when omitted |
| `languages` | `string[] \| null` | Tesseract languages, e.g. `["eng", "chi_sim"]` |
| `psm` | `int \| null` | 0..13 |
| `min_confidence` | `float \| null` | 0..100 |
| `save_screenshot` | `bool` | Persist the captured frame |

Response is an `OcrResult`:

```json
{
  "lines": [
    {"text": "Sign in", "box": {"x": 780, "y": 410, "width": 160, "height": 32},
     "center": {"x": 860.0, "y": 426.0}, "confidence": 88.4,
     "words": [{"text": "Sign", "box": {"x": 780, "y": 412, "width": 68, "height": 28},
                 "center": {"x": 814.0, "y": 426.0}, "confidence": 91.2}]}
  ],
  "text": "Sign in",
  "frame": {"region": {"x": 0, "y": 0, "width": 1920, "height": 1080},
             "width": 1920, "height": 1080,
             "captured_at": "2026-11-18T12:00:01Z", "screenshot": null},
  "elapsed_ms": 268.5
}
```

All boxes are absolute screen coordinates. Tesseract confidences stay in the
`0..100` scale.

```bash
curl -s -X POST http://<host>:8765/api/v1/elements/find \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"query": {"text": {"text": "Sign in"}}}'

curl -s -X POST http://<host>:8765/api/v1/elements/wait \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"query": {"text": {"text": "Continue", "match": "exact"}},
       "timeout_ms": 15000, "poll_interval_ms": 250}'

curl -s -X POST http://<host>:8765/api/v1/elements/ocr \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"region": {"x": 0, "y": 0, "width": 960, "height": 540},
       "languages": ["eng"], "psm": 6}'
```

## 15. Composite tier — interactions

Composite operations locate a target and then reuse the primitive mouse/keyboard
actions. They are mutating and therefore go through the serial queue. They never
contain duplicated click/type logic: internally they invoke `element.wait` /
`element.find`, then `mouse.click` (and `keyboard.*` for typing), so cancellation
and results behave like the primitive actions. The composite `strategy` field is
validated and applied at the HTTP/service layer exactly like the primitive
`strategy` option; it is not an action parameter.

### `POST /api/v1/interactions/click`

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | `ElementQuery` | required | How to find the target |
| `button` | `"left" \| "right" \| "middle"` | `"left"` | Mouse button |
| `clicks` | `int` | `1` | 1..3 |
| `interval_ms` | `float \| null` | strategy default | Gap between clicks |
| `offset_x` / `offset_y` | `float` | `0` | Pixel offset added to the match center (−10000..10000) |
| `wait_timeout_ms` | `int` | `2000` | Locate budget; 0 means a single find; 0..600000 |
| `strategy` | `string \| null` | config default | Provider override for the click; validated like the primitive option (see §4) |

Output (`ActionResult.output`):

```json
{
  "target": {"box": {...}, "center": {"x": 860.0, "y": 426.0},
              "confidence": 0.88, "locator": "text", "text": "Sign in",
              "metadata": {"granularity": "line", "ocr_confidence": 88.4}},
  "position": {"x": 860.0, "y": 426.0},
  "click": {"position": {"x": 860.0, "y": 426.0}, "button": "left", "clicks": 1},
  "attempts": 1
}
```

If no target appears within `wait_timeout_ms`, the request fails with
`404 ELEMENT_NOT_FOUND` before any click is sent.

### `POST /api/v1/interactions/type`

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `query` | `ElementQuery` | required | Target element to focus |
| `text` | `string` | required | 1..100000 characters |
| `clear` | `bool` | `false` | Select-all (`ctrl+a`) before typing |
| `submit` | `bool` | `false` | Press `enter` after typing |
| `wait_timeout_ms` | `int` | `2000` | Locate budget; 0..600000 |
| `strategy` | `string \| null` | config default | Provider override for click and typing; validated like the primitive option (see §4) |

Output (`ActionResult.output`):

```json
{
  "target": {...},
  "click": {"position": {...}, "button": "left", "clicks": 1},
  "typed": {"typed": "hello@example.com", "length": 17},
  "cleared": true,
  "submitted": false,
  "attempts": 1
}
```

Order of operations: locate → click match center → optional `ctrl+a` → type text
→ optional `enter`. Failures propagate unchanged (e.g. `404 ELEMENT_NOT_FOUND`,
`409 STRATEGY_UNAVAILABLE`).

```bash
curl -s -X POST http://<host>:8765/api/v1/interactions/click \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"query": {"text": {"text": "Submit"}}, "wait_timeout_ms": 5000}'

curl -s -X POST http://<host>:8765/api/v1/interactions/type \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"query": {"text": {"text": "Email"}},
       "text": "hello@example.com", "clear": true}'
```

## 16. Workflow tier — tasks

A task is a persisted, ordered list of steps executed as one serial job. Steps
may use any action, including `element.*` and `interaction.*`.

| Method | Path | Response |
| --- | --- | --- |
| POST | `/api/v1/tasks` | `202 Accepted` + `TaskRecord` |
| GET | `/api/v1/tasks?status=&limit=&offset=` | `{"items": [...], "total": N, "limit": 50, "offset": 0}` |
| GET | `/api/v1/tasks/{task_id}` | `TaskRecord` |
| POST | `/api/v1/tasks/{task_id}/cancel` | `TaskRecord` (cooperative cancellation) |

Submit body:

```json
{
  "name": "login-flow",
  "timeout_ms": 120000,
  "metadata": {"client": "demo"},
  "client_request_id": "req-42",
  "steps": [
    {"action": "element.wait",
     "params": {"query": {"text": {"text": "Sign in"}}, "timeout_ms": 10000}},
    {"action": "interaction.click",
     "params": {"query": {"text": {"text": "Sign in"}}}},
    {"action": "interaction.type",
     "params": {"query": {"text": {"text": "Email"}},
                 "text": "hello@example.com", "clear": true}},
    {"action": "keyboard.press", "params": {"key": "enter"}, "delay_after_ms": 200},
    {"action": "element.wait",
     "params": {"query": {"text": {"text": "Welcome"}}, "timeout_ms": 10000}},
    {"action": "screen.screenshot", "params": {"format": "png", "save": true},
     "continue_on_error": true}
  ]
}
```

Task submit fields: `name?`, `steps`* (1..2000), `timeout_ms?` (100..7200000),
`metadata?`, `client_request_id?`.

Step fields: `action`*, `params?`, `name?`, `timeout_ms?` (1..3600000),
`continue_on_error` (default `false`), `delay_after_ms` (0..3600000,
cancel-aware).

`TaskRecord` fields: `id`, `name`, `status`, `steps`, `step_results`,
`created_at`, `queued_at`, `started_at`, `finished_at`, `timeout_ms`,
`cancel_requested_at`, `error`, `client_request_id`, `metadata`.

Status machine:

```text
queued ──▶ running ──▶ succeeded
                    ├─▶ failed
                    ├─▶ cancelled
                    └─▶ timed_out
queued/running ──▶ interrupted   (process restart recovery)
```

Terminal states: `succeeded`, `failed`, `cancelled`, `timed_out`, `interrupted`.
Cancelling a queued task succeeds immediately; a running task stops at the next
safe checkpoint (after the current strategy event/step boundary). Cancelling an
already-terminal task returns `409 TASK_NOT_CANCELLABLE`.

```bash
curl -s -X POST http://<host>:8765/api/v1/tasks \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d @task.json

curl -s -H "X-API-Key: $KEY" http://<host>:8765/api/v1/tasks/<task_id>
curl -s -X POST http://<host>:8765/api/v1/tasks/<task_id>/cancel \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{}'
curl -s -H "X-API-Key: $KEY" "http://<host>:8765/api/v1/tasks?status=failed&limit=20"
```

## 17. Execution behaviour

- **One writer.** Every mutating primitive, interaction and task shares a single
  FIFO worker. Concurrent requests queue; when the queue is full the API fails
  fast with `429 QUEUE_FULL`.
- **Read-only bypass.** Pointer position/state, screen size, GET screenshots,
  screenshot lists, window enumeration, `GET /elements/*` and the meta endpoints
  never wait for a long-running action.
- **Timeouts.** A single action uses `tasks.step_timeout_ms` unless `timeout_ms`
  is supplied. A task's `timeout_ms` covers the whole task. Timeouts trigger
  cooperative cancellation at the next driver/strategy safe point.
- **Reproducibility.** Providers that support `seed` produce the same
  trajectory/key sequence for the same seed and inputs (human_movement,
  human_move_mouse, windmouse, clack). Real-time pacing still causes small
  scheduling differences; output is not byte-identical.
- **VNC.** Not installed/enabled. The HTTP API is the only access path in this
  release.
- **Persistence.** Task records and saved screenshots live under `/data`
  (SQLite + files); they survive container restarts. `interrupted` tasks after a
  restart are never resumed automatically.

## 18. Python client example

```python
import time

import httpx

BASE = "http://<host>:8765"
KEY = "<api-key>"

with httpx.Client(base_url=BASE, headers={"X-API-Key": KEY}, timeout=60) as client:
    print(client.get("/healthz").json())
    print(client.get("/api/v1/screen/size").json())

    client.post("/api/v1/mouse/move", json={"x": 960, "y": 540}).raise_for_status()

    found = client.post(
        "/api/v1/elements/find",
        json={"query": {"text": {"text": "Sign in"}}},
    ).json()
    if found["matches"]:
        center = found["matches"][0]["center"]
        client.post("/api/v1/mouse/click", json=center).raise_for_status()

    task = client.post(
        "/api/v1/tasks",
        json={
            "name": "demo",
            "steps": [
                {"action": "interaction.click",
                 "params": {"query": {"text": {"text": "Sign in"}}}},
                {"action": "screen.screenshot",
                 "params": {"format": "png", "save": True}},
            ],
        },
    ).json()

    for _ in range(60):
        record = client.get(f"/api/v1/tasks/{task['id']}").json()
        if record["status"] in {"succeeded", "failed", "cancelled",
                                "timed_out", "interrupted"}:
            break
        time.sleep(0.5)
    print(record["status"], record.get("error"))
```
