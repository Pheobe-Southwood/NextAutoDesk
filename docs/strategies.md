# Strategy layer

NextAutoDesk separates **what to do** from **how the input should feel**:

- the action layer describes the intent (`move the pointer to (x, y)`, `type this
  text`, `click left`);
- the strategy layer decides how it is performed — trajectory, easing, key
  rhythm, jitter, human-like pacing and random seeding;
- the driver layer injects X11 events immediately and contains no pacing logic.

Each of the 19 mouse/keyboard operations has an **independent** provider and
parameter entry under `strategies.operations`. You can therefore use
human-movement for typing, a pre-trained model for pointer movement, and
`direct` for a low-level state primitive — all by configuration, without
changing code.

The selection rule is deliberately conservative: **do not invent human-like
behaviour algorithms**. Defaults and optional providers wrap researched,
published implementations, and their licences are recorded in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## 1. Built-in providers

| Provider | Licence | Supported operations | Default for | Description |
| --- | --- | --- | --- | --- |
| `human_movement` | MIT | 15 of 19 (all except the 4 cross-request state primitives) | **15 operations** | [human-movement](https://pypi.org/project/human-movement/): trajectories and key rhythm derived from real input data, Fitts' law and minimum-jerk profiles. Events are generated and injected by the driver with their timestamps. |
| `human_move_mouse` | MIT | `mouse_move`, `mouse_move_relative` | optional | [HumanMoveMouse](https://github.com/TomokotoKiyoshi/HumanMoveMouse): pre-trained statistical pointer-trajectory model. |
| `windmouse` | GPL-3.0-only | `mouse_move`, `mouse_move_relative` | optional | [windmouse](https://github.com/AsfhtgkDavid/windmouse): WindMouse gravity/wind/damping algorithm. **GPL**: included in the default image but subject to its licence. |
| `clack` | MIT | `keyboard_type_text` | optional | [Clack](https://github.com/ThisWasAryan/clack) CLI: typing rhythm with configurable WPM, jitter, typos and self-correction. Vendored binary with SHA-256 verification. ASCII-only. |
| `direct` | MIT (this project) | all 19 | 4 cross-request state primitives | No trajectory/rhythm modelling: immediate driver calls with simple pacing. Used for debugging, benchmarking, explicit fallback, and cross-request state. |

Availability is exposed by `GET /api/v1/strategies`: for each operation you get
the active provider, parameters, availability, and candidate providers with
`available`/`reason`. `GET /api/v1/actions` exposes action parameter schemas.

## 2. Operation defaults

### Mouse (12 operations)

| Operation | Default provider | Notes |
| --- | --- | --- |
| `mouse_move` | `human_movement` | Absolute move |
| `mouse_move_relative` | `human_movement` | Relative move |
| `mouse_click_left` | `human_movement` | Single left click |
| `mouse_click_right` | `human_movement` | Single right click |
| `mouse_click_middle` | `human_movement` | Single middle click |
| `mouse_double_click` | `human_movement` | Double click |
| `mouse_down` | `direct` | Cross-request state primitive |
| `mouse_up` | `direct` | Cross-request state primitive |
| `mouse_hold` | `human_movement` | Hold for a duration |
| `mouse_drag` | `human_movement` | Press, move, release |
| `mouse_scroll_vertical` | `human_movement` | Vertical wheel |
| `mouse_scroll_horizontal` | `human_movement` | Horizontal wheel |

### Keyboard (7 operations)

| Operation | Default provider | Notes |
| --- | --- | --- |
| `keyboard_type_text` | `human_movement` | Character-by-character typing |
| `keyboard_key_press` | `human_movement` | Press + release |
| `keyboard_key_down` | `direct` | Cross-request state primitive |
| `keyboard_key_release` | `direct` | Cross-request state primitive |
| `keyboard_key_hold` | `human_movement` | Hold for a duration |
| `keyboard_hotkey` | `human_movement` | Simultaneous keys |
| `keyboard_key_sequence` | `human_movement` | Ordered keys with interval |

### Why four operations default to `direct`

`mouse_down`, `mouse_up`, `keyboard_key_down` and `keyboard_key_release` must
keep a button/key pressed **across HTTP requests**. The human-movement SDK closes
its `HumanInput` session at the end of each execution and releases everything it
held, so it cannot satisfy that contract. `direct` injects the down/up event
immediately and leaves the state alone:

```text
POST /mouse/down  → GET /mouse/state shows ["left"]  → POST /mouse/up
```

These operations can still be configured or overridden per request, but only
providers that support them are accepted. Forcing `human_movement` on one of
them returns `409 STRATEGY_UNAVAILABLE`; the operation remains listed with the
supported candidates in `GET /strategies`.

## 3. Configuration

Defaults live in `src/nextautodesk/config/default.yml`; deployments override them
with a YAML file (`config/nextautodesk.example.yml` is a starting point).

```yaml
strategies:
  providers:
    human_movement:
      enabled: true
      params:
        profile: standard
        engine: minimum_jerk
        seed: null
        realtime: true
        target_width: 24.0
        hold_default_ms: 120
        sequence_interval_ms: 150
    human_move_mouse:
      enabled: true
      params: {speed_factor: 1.0, num_points: 100, jitter_amplitude: 0.3, seed: null}
    windmouse:
      enabled: true
      params: {gravity: 9, wind: 3, max_step: 15, damped_distance: 12, seed: null}
    clack:
      enabled: true
      params: {wpm: 80, jitter: 0.15, error_rate: 0.0, correction_rate: 0.85,
               layout: qwerty, seed: null, no_fatigue: true}
    direct:
      enabled: true
      params: {hold_default_ms: 100, sequence_interval_ms: 120}

  operations:
    mouse_move: {provider: human_movement}
    keyboard_type_text: {provider: human_movement}
    mouse_down: {provider: direct}
    mouse_up: {provider: direct}
    keyboard_key_down: {provider: direct}
    keyboard_key_release: {provider: direct}
```

- Provider-level `params` are defaults; they are overlaid by operation-level
  `params` and then by the per-request `strategy` override (which only selects a
  provider, not arbitrary parameters).
- `params` must belong to the selected provider. Action arguments (for example
  `clicks` on `POST /mouse/click`) are request fields, not strategy parameters.
- Double click is an independent operation (`mouse_double_click`); a
  multi-click request is executed by the selected strategy.
- Edit the deployment overlay and call `POST /api/v1/config/reload` to apply
  strategy changes without restarting. Reload rebuilds provider instances and
  rejects invalid configurations with `500 RELOAD_FAILED`.
- A single request can override the provider:
  `{"x": 800, "y": 600, "strategy": "windmouse"}`. Set
  `server.allow_strategy_override: false` to forbid overrides globally.
- `seed` makes generated trajectories/keystrokes reproducible
  (human_movement, human_move_mouse, windmouse, clack). Real-time pacing still
  introduces small differences; do not expect byte-identical timing.

## 4. Parameter reference

### human_movement

| Parameter | Default | Description |
| --- | --- | --- |
| `profile` | `standard` | Input profile (human-movement built-in profiles) |
| `engine` | `minimum_jerk` | Trajectory engine |
| `seed` | `null` | Random seed (`null` = non-deterministic) |
| `realtime` | `true` | Inject events with real-time pacing; `false` runs as fast as possible |
| `stop_on_physical_input` | `false` | Abort on physical input (no physical input in the container; keep `false`) |
| `target_width` | `24.0` | Fitts' law target width in pixels |
| `hold_default_ms` | `120` | Default hold duration |
| `sequence_interval_ms` | `150` | Default interval for key sequences |

### human_move_mouse

| Parameter | Default | Description |
| --- | --- | --- |
| `model_path` | `null` | Custom trajectory model path; `null` uses the bundled model |
| `num_points` | `100` | Trajectory sample count |
| `jitter_amplitude` | `0.3` | End-of-motion jitter amplitude |
| `speed_factor` | `1.0` | Speed multiplier (>1 is faster) |
| `seed` | `null` | Random seed |

### windmouse

| Parameter | Default | Description |
| --- | --- | --- |
| `gravity` | `9` | Pull toward the target |
| `wind` | `3` | Wind perturbation |
| `max_step` | `15` | Maximum pixels per step |
| `damped_distance` | `12` | Damping distance |
| `step_duration_s` | `0.1` | Duration per injected step (seconds) |
| `seed` | `null` | Random seed |

### clack

| Parameter | Default | Description |
| --- | --- | --- |
| `binary` | `/usr/local/bin/clack` | Clack CLI path |
| `wpm` | `80` | Target typing speed (words per minute) |
| `jitter` | `0.15` | Rhythm jitter ratio |
| `error_rate` | `0.0` | Typo probability (0..1) |
| `correction_rate` | `0.85` | Probability that a typo is corrected |
| `layout` | `qwerty` | Keyboard layout (`qwerty`/`dvorak`/`colemak`/`azerty`) |
| `seed` | `null` | Random seed |
| `no_fatigue` | `true` | Disable the fatigue model (more stable for long tasks) |
| `max_pause_ms` | `5000` | Maximum thinking pause |
| `thinking_pause_prob` | `0.0` | Probability of a thinking pause |
| `code_mode` | `false` | Code-aware punctuation/indentation rhythm |
| `startup_timeout_s` / `char_timeout_s` | `5` / `10` | CLI startup / per-character timeout |

### direct

| Parameter | Default | Description |
| --- | --- | --- |
| `hold_default_ms` | `100` | Default hold duration |
| `sequence_interval_ms` | `120` | Default interval for key sequences |

## 5. Extending strategies

1. Implement `StrategyProvider` from `src/nextautodesk/strategies/base.py`:
   - class attributes `name`, `version`, `description`, `supported`
     (`frozenset[Operation]`);
   - `availability()` returns `ProviderAvailability` — report missing optional
     dependencies with `available=False` and a `reason` instead of raising;
   - `execute(request, params, ctx)` performs the operation through
     `ctx.drivers`, checks `ctx.check_cancelled()` in long loops, and returns a
     `StrategyOutcome`;
   - optionally implement `validate_params(params)` for fail-fast configuration
     validation.
2. Register the provider:
   - drop the module under `src/nextautodesk/strategies/providers/` (auto-discovery
     in a development tree), or
   - publish it from another package via a Python entry point:

     ```toml
     [project.entry-points."nextautodesk.strategies"]
     my_provider = "my_package.provider:MyProvider"
     ```

3. Reference it in `strategies.providers` and assign it to one or more
   operations in `strategies.operations`.
4. Add headless unit tests with fake drivers (no X11 required) and document the
   provider's licence and rationale. Providers are loaded at startup and on
   configuration reload; a broken entry point is logged and ignored rather than
   preventing the node from booting.

## 6. Design constraints

- **Drivers contain no pacing.** Every sleep, trajectory or jitter originates in
  a strategy; a driver must remain a thin, immediate adapter.
- **Cancellation-aware.** Long operations check `ctx.check_cancelled()` between
  events so task cancellation and timeouts take effect promptly.
- **Graceful degradation.** A missing optional dependency disables only its
  provider; the default configuration still starts. Selecting an unavailable
  provider returns `409 STRATEGY_UNAVAILABLE` with a reason.
- **No self-invented human-likeness.** New behaviour must come from a documented,
  research-backed implementation, and its licence must be added to
  `THIRD_PARTY_NOTICES.md`.
- **Secrets never leak.** Provider and operation params are redacted by
  `/config` and `/strategies` when they look secret (`token`, `password`, …).
- **Clack newline caveat.** With `error_rate > 0`, do not include `\n` in text
  typed through Clack: its self-correction stream may emit a newline and then
  backspace, but the shell executes the line immediately and Enter cannot be
  undone. Type the line, then send Enter separately. The default
  `error_rate: 0.0` does not have this problem.

## 7. Licences

- `human_movement` (human-movement) — MIT.
- `human_move_mouse` (HumanMoveMouse) — MIT, optional extra
  (`.[humanmove_mouse]`).
- `windmouse` — GPL-3.0-only, optional extra (`.[windmouse]`). The default image
  installs the optional extras. Redistributing an image with `windmouse`
  triggers GPL obligations; build with
  `--build-arg WITH_OPTIONAL_STRATEGIES=false` (or
  `./scripts/build-image.sh --no-optional-strategies`) for a
  permissively-licensed image, in which case the provider reports
  `available=false`.
- `clack` — MIT. Vendored binary under `third_party/clack/` with `LICENSE` and
  `SHA256SUMS`; upstream repository linked in the notices file.
- `direct` — MIT (part of this project).

See [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) for exact versions,
upstream URLs and attribution. The runtime image keeps `LICENSE`,
`THIRD_PARTY_NOTICES.md` and the Clack licence under `/app/licenses/`.
