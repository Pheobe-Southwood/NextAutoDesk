# Screen perception

The perception layer answers one question: **where is the UI element on screen?**
It turns a screenshot into typed results (`ElementMatch`, `OcrLine`, …) that can
be used directly by automation logic or by the composite interaction actions.

Perception is read-only. It never moves the pointer, types, or changes desktop
state, and it always bypasses the serial input queue.

## 1. Model

```text
ElementQuery ──▶ PerceptionService ──▶ Locator ──▶ ElementMatch[]
                        │
                        ├─ captures one frame through ScreenDriver
                        ├─ merges config defaults with query overrides
                        └─ maps frame-relative boxes to absolute screen coordinates
```

### Query

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `locator` | `"auto" \| "template" \| "text"` | `"auto"` | `auto` chooses `template` when a template spec is present, otherwise `text` |
| `region` | `Rect \| null` | `null` (whole screen) | Capture region in screen pixels (`x`, `y`, `width`, `height`) |
| `template` | `TemplateSpec \| null` | `null` | Template source and tuning |
| `text` | `TextSpec \| null` | `null` | Text query and OCR tuning |
| `max_results` | `int \| null` | config (`20`) | 1..100; results are capped to the configured maximum as well |
| `save_screenshot` | `bool` | `false` | Persist the captured frame and return its metadata in `frame.screenshot` |

Exactly one of `template` / `text` must be set. `locator` must be consistent with
the provided spec (`template` requires `template`, `text` requires `text`).

### Result

```json
{
  "locator": "template",
  "matches": [
    {
      "box": {"x": 1180, "y": 642, "width": 124, "height": 38},
      "center": {"x": 1242.0, "y": 661.0},
      "confidence": 0.93,
      "locator": "template",
      "text": null,
      "metadata": {"scale": 1.0}
    }
  ],
  "frame": {
    "region": {"x": 0, "y": 0, "width": 1920, "height": 1080},
    "width": 1920,
    "height": 1080,
    "captured_at": "2026-11-18T12:00:00Z",
    "screenshot": null
  },
  "elapsed_ms": 41.7,
  "attempts": 1
}
```

- `matches` is sorted by confidence (descending); template hits use area as the
  tie-breaker, text hits use top-to-bottom order.
- `confidence` is normalised to `0..1` in `ElementMatch`. OCR confidences in
  `OcrWord`/`OcrLine` stay in Tesseract's `0..100` scale; the text locator also
  exposes the raw OCR confidence in `metadata.ocr_confidence`.
- `attempts` is 1 for a single find; `element.wait` reports how many locate
  attempts it performed.
- `frame.region` describes what was captured. `matches[*].box`, `.center` and all
  OCR boxes are already **absolute screen coordinates** (see §4).

### OCR result

```json
{
  "lines": [
    {
      "text": "Sign in",
      "box": {"x": 780, "y": 410, "width": 160, "height": 32},
      "center": {"x": 860.0, "y": 426.0},
      "confidence": 88.4,
      "words": [
        {
          "text": "Sign",
          "box": {"x": 780, "y": 412, "width": 68, "height": 28},
          "center": {"x": 814.0, "y": 426.0},
          "confidence": 91.2
        }
      ]
    }
  ],
  "text": "Sign in",
  "frame": {"region": {"x": 0, "y": 0, "width": 1920, "height": 1080},
             "width": 1920, "height": 1080,
             "captured_at": "2026-11-18T12:00:01Z", "screenshot": null},
  "elapsed_ms": 268.5
}
```

`text` is the line texts joined with `\n`. Lines and words carry their own boxes,
centers and Tesseract confidence.

## 2. Locators

Locator availability is reported by `GET /api/v1/elements/locators`:

```json
{
  "enabled": true,
  "locators": [
    {"name": "template", "kind": "template", "available": true,
     "reason": null, "version": "...", "supported_queries": ["template"]},
    {"name": "text", "kind": "text", "available": true,
     "reason": null, "version": "...", "supported_queries": ["text"]}
  ]
}
```

A disabled locator (or one whose optional dependency is missing) is still listed
with `available: false` and a `reason`, so callers can discover capabilities
without trial and error. Requesting it returns `409 LOCATOR_UNAVAILABLE`; a
locator that exists but does not support the query kind returns
`400 LOCATOR_NOT_SUPPORTED`.

### 2.1 Template locator (`template`, OpenCV)

Find one or more occurrences of a template image in the captured frame. The
implementation is `OpenCVTemplateLocator`, backed by `cv2.matchTemplate` with
`TM_CCOEFF_NORMED`.

`TemplateSpec`:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `image_base64` | `string \| null` | `null` | PNG/JPEG bytes, raw base64 or a `data:image/...;base64,` URL |
| `screenshot` | `string \| null` | `null` | Name of a screenshot already saved in the screenshot store |
| `threshold` | `float \| null` | config (`0.8`) | Match threshold, 0..1 |
| `grayscale` | `bool \| null` | config (`true`) | Convert both images to grayscale before matching |
| `scales` | `float[] \| null` | config (`[1.0]`) | 0.1..4.0, at most 8 entries |

Exactly one of `image_base64` / `screenshot` is required. `screenshot` is
resolved by the perception service against the managed screenshot store — a
locator never accepts a host path. Stored templates are resolved and validated
before the screen is captured, so invalid input never pays for a screenshot.

Matching behaviour:

- every scale is resized independently and matched; hits from all scales are
  combined and deduplicated with a deterministic greedy NMS (IoU > 0.3 suppresses
  the lower-scoring box);
- `metadata.scale` records the scale that produced the hit;
- constant templates (zero variance) are handled without crashing — non-finite
  correlation values are discarded;
- a template larger than the captured frame yields no matches (scales that do
  not fit are skipped);
- results are sorted by confidence, then area, and capped by `max_results`.

### 2.2 Text locator (`text`, Tesseract OCR)

Find text lines or words using the Tesseract engine through `pytesseract`.

`TextSpec`:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `text` | `string` | required | 1..1000 characters |
| `match` | `"contains" \| "exact" \| "regex"` | `"contains"` | Comparison mode |
| `case_sensitive` | `bool` | `false` | Case-sensitive comparison |
| `languages` | `string[] \| null` | config (`["eng", "chi_sim"]`) | Tesseract languages; must be installed in the image |
| `psm` | `int \| null` | config (`6`) | Tesseract page segmentation mode, 0..13 |
| `min_confidence` | `float \| null` | config (`40`) | Discard words below this Tesseract confidence (0..100) |
| `granularity` | `"line" \| "word"` | config (`line`) | Return line boxes or individual word boxes |

Behaviour:

- OCR runs once per query; words are grouped into lines by Tesseract's
  `(block_num, par_num, line_num)`;
- line text is words joined by single spaces; line confidence is the mean word
  confidence;
- `contains` and `exact` compare case-normalised text unless
  `case_sensitive=true`; `regex` uses `re.search` and an invalid pattern returns
  `422 VALIDATION_ERROR` with a clear message;
- matches are sorted by confidence descending, then top-to-bottom, and capped by
  `max_results`.

Tesseract language data is installed in the Docker image (`eng` and `chi_sim` by
default). If the binary or the requested language data is missing, the locator is
reported unavailable / the request fails with a descriptive error rather than
silently returning no matches.

## 3. Configuration

The perception block is hot-reloadable:

```yaml
perception:
  enabled: true
  max_results: 20
  locators:
    template:
      enabled: true
      params:
        threshold: 0.8
        grayscale: true
        scales: [1.0]
    text:
      enabled: true
      params:
        languages: [eng, chi_sim]
        psm: 6
        min_confidence: 40
        granularity: line
```

- `perception.enabled: false` makes every perception endpoint return
  `409 PERCEPTION_UNAVAILABLE`; the locator list is still served so discovery
  keeps working.
- `perception.max_results` limits result sets (1..100).
- `perception.locators.<name>.enabled: false` disables one locator; it remains
  listed with `available: false` and reason `"disabled in configuration"`.
- `params` are locator defaults; non-null values in a query override them for
  that request only.
- Locator names must match `^[a-z0-9_-]+$`. Unknown names and invalid locator
  parameters fail startup and `nextautodesk config validate`; the same
  validation runs on `POST /config/reload` before the new configuration is
  applied.

After editing the deployment overlay, call
`POST /api/v1/config/reload` to rebuild the perception service without restarting
the container.

## 4. Coordinate semantics

- A query `region` is an **absolute** rectangle on the virtual screen:
  `{"x": 100, "y": 80, "width": 640, "height": 480}`.
- Omitting `region` captures the full screen
  (`{0, 0, screen_width, screen_height}`).
- Locators compute boxes relative to the captured frame.
- `PerceptionService` adds the region origin exactly once, so:
  **all returned boxes and centers are absolute screen coordinates** and can be
  passed directly to `POST /api/v1/mouse/click` or used as
  `interaction.*` target coordinates.
- `frame.region` and `frame.width`/`height` let a client reconstruct the cropped
  area for debugging.
- `metadata` is locator-specific (for example the matching scale); treat it as
  informational and not byte-stable across versions.

The composite actions rely on this invariant: `interaction.click` clicks
`match.center + offset`, without any region correction.

## 5. Examples

Base URL and key are omitted for brevity; every call needs `X-API-Key`.

### Find an element by text

```bash
curl -s -X POST http://<host>:8765/api/v1/elements/find \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"query": {"text": {"text": "Sign in", "match": "contains"}}}'
```

### Wait for a button inside a window region

```bash
curl -s -X POST http://<host>:8765/api/v1/elements/wait \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{
    "query": {
      "locator": "text",
      "region": {"x": 640, "y": 360, "width": 640, "height": 360},
      "text": {"text": "Continue", "match": "exact"}
    },
    "timeout_ms": 15000,
    "poll_interval_ms": 250
  }'
```

A timeout returns `404 ELEMENT_NOT_FOUND` with `details.timeout_ms`,
`details.attempts` and a redacted query summary (base64 omitted, text
truncated) in `details`.

### Find a template supplied as base64

```bash
# image_base64 accepts raw base64 or a data: URL; this example reads a local PNG.
B64=$(base64 -w0 button.png)
curl -s -X POST http://<host>:8765/api/v1/elements/find \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d "{\"query\": {\"locator\": \"template\",
        \"template\": {\"image_base64\": \"data:image/png;base64,${B64}\",
                       \"threshold\": 0.85, \"scales\": [0.9, 1.0, 1.1]}}}"
```

### Find a template from a previously saved screenshot

Save the reference first, then refer to it by name — no host path is accepted:

```bash
curl -s -X POST http://<host>:8765/api/v1/screen/screenshot \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"format": "png", "save": true, "name": "login-button.png"}'

curl -s -X POST http://<host>:8765/api/v1/elements/find \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"query": {"locator": "template",
        "template": {"screenshot": "login-button.png"}}}'
```

### OCR a region

```bash
curl -s -X POST http://<host>:8765/api/v1/elements/ocr \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"region": {"x": 0, "y": 0, "width": 960, "height": 540},
       "languages": ["eng"], "psm": 6, "min_confidence": 50}'
```

### Click a found element

```bash
curl -s -X POST http://<host>:8765/api/v1/interactions/click \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"query": {"text": {"text": "Submit"}}, "wait_timeout_ms": 5000}'
```

### Type into a found field

```bash
curl -s -X POST http://<host>:8765/api/v1/interactions/type \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"query": {"locator": "template", "template": {"screenshot": "name-field.png"}},
       "text": "hello@example.com", "clear": true, "submit": false}'
```

### Save the captured frame for debugging

Add `"save_screenshot": true` to `query` (find/wait) or to the OCR request. For
`element.wait`, polling disables saving and exactly one screenshot is persisted
when the element is found. The response then includes `frame.screenshot` with the
stored name, size, dimensions and download URL. Screenshots are subject to the
configured retention policy.

## 6. Limitations

- **Visible pixels only.** Perception sees the current Xvfb framebuffer: no
  accessibility tree, DOM, widget types or hidden/occluded content. If another
  window covers the element, matching will fail or match the wrong thing.
- **Same rendering required.** Templates must come from the same display
  resolution, theme, font stack and scale as the target. DPI/font changes,
  anti-aliasing differences and animations reduce match confidence.
- **No rotation or perspective invariance.** The template locator supports
  configurable scaling only; rotated or strongly perspective-distorted templates
  are not supported.
- **OCR is approximate.** Accuracy depends on font size, contrast, language data
  and background. Use `min_confidence`, a tight `region` and `psm` tuned to the
  layout. OCR is CPU-bound on large regions.
- **Language data must exist in the image.** Passing a language that is not
  installed fails with an availability/error response; extend the image if you
  need additional languages.
- **Fixed virtual screen.** Coordinates are relative to the configured Xvfb
  screen (default 1920x1080); the device does not follow host display changes.
- **No auto-scaling for high-DPI UIs beyond configured scales.** Enumerate the
  scales you expect and keep `max_results` small for performance.
- **Read-only by design.** Perception cannot scroll, hover, focus a window or
  wait for animations; compose it with primitive or workflow actions.

## 7. Extending perception

A custom locator implements the `Locator` contract in
`nextautodesk.perception.base` and is discovered through the
`nextautodesk.locators` Python entry point group:

1. Implement `name`, `kind`, `supported_queries`, `availability()`,
   `validate_params(params)` and `execute(query, frame, params)`.
2. Return `ElementMatch` objects with **frame-relative** boxes; the service adds
   the capture-region origin.
3. Register in the consuming project's packaging metadata:

   ```toml
   [project.entry-points."nextautodesk.locators"]
   dom = "my_package.locator:DomLocator"
   ```

4. Enable and tune it in configuration under `perception.locators.<name>`.

Locators must stay pure with respect to transport and storage: do not capture
screenshots, encode images, read files, or access HTTP/storage directly. Anything
that needs a saved template or an output file belongs in `PerceptionService`.

A custom locator can also implement the `TextExtractor` protocol
(`extract_text(frame, params, ...)`) to back `element.ocr`.
