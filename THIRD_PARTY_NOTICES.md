# Third-party notices

NextAutoDesk is MIT licensed (see `LICENSE`). It integrates the following
third-party projects; their licences and attribution requirements apply to the
bundled/invoked components. Version pins are defined in `pyproject.toml`; the
Docker image also installs OS packages listed below.

## Licenses in the image

The runtime image bundles these components and ships this notice plus the project
`LICENSE` at `/app/licenses/`. The OCI image licence label carries the project's
SPDX identifier `MIT` only; the bundled components and their licences are listed
below.

| File | Content |
| --- | --- |
| `/app/licenses/LICENSE` | NextAutoDesk's MIT licence |
| `/app/licenses/THIRD_PARTY_NOTICES.md` | This notice |
| `/app/licenses/clack-LICENSE` | MIT licence of the vendored Clack binary |

The default image includes the optional GPL-3.0 `windmouse` strategy with
`WITH_OPTIONAL_STRATEGIES=true`. Build with `WITH_OPTIONAL_STRATEGIES=false`
(or `./scripts/build-image.sh --no-optional-strategies`) to exclude the optional
strategy extras from the image; the providers then report `available=false` at
runtime.

## Runtime dependencies (installed from PyPI)

| Package | Version | License | Use | Upstream |
| --- | --- | --- | --- | --- |
| human-movement | 0.1.6 | MIT | Default human-like mouse & keyboard strategy engine | https://pypi.org/project/human-movement/ |
| HumanMoveMouse | 1.2.0 | MIT | Optional pre-trained mouse trajectory strategy | https://github.com/TomokotoKiyoshi/HumanMoveMouse |
| windmouse | 1.0.2 | GPL-3.0-only | Optional WindMouse mouse trajectory strategy | https://github.com/AsfhtgkDavid/windmouse |
| PyAutoGUI | 0.9.54 | BSD-3-Clause | Low-level X11 mouse/keyboard execution | https://pypi.org/project/PyAutoGUI/ |
| FastAPI | 0.163.2 | MIT | HTTP API framework | https://github.com/fastapi/fastapi |
| Uvicorn | 0.54.0 | BSD-3-Clause | ASGI server | https://github.com/encode/uvicorn |
| Pydantic / pydantic-settings | 2.14.3 / 2.16.3 | MIT | Configuration & schema validation | https://github.com/pydantic/pydantic |
| Pillow | 13.0.0 | MIT-CMU | Screenshot encoding, frame decoding | https://github.com/python-pillow/Pillow |
| python-xlib | 0.33 | LGPL-2.1-or-later | X11 protocol access | https://github.com/python-xlib/python-xlib |
| psutil | 7.2.2 | BSD-3-Clause | Process/system metrics | https://github.com/giampaolo/psutil |
| PyYAML | 6.0.3 | MIT | Configuration parsing | https://github.com/yaml/pyyaml |
| OpenCV (opencv-python-headless) | 5.1.0.95 | Apache-2.0 | Template matching (`cv2.matchTemplate`); headless wheel, no GUI libraries | https://opencv.org/ |
| pytesseract | 0.3.13 | Apache-2.0 | Python wrapper for the Tesseract OCR engine | https://github.com/madmaze/pytesseract |
| Tesseract OCR engine | OS package | Apache-2.0 | Text recognition engine (`tesseract-ocr`) | https://github.com/tesseract-ocr/tesseract |
| Tesseract language data (`eng`, `chi_sim`) | OS package | Apache-2.0 | OCR language models (`tesseract-ocr-eng`, `tesseract-ocr-chi-sim`) | https://github.com/tesseract-ocr/tessdata |

## Transitive Python dependencies

The default image also installs transitive dependencies of the pinned packages
and optional strategy extras. The legally material ones are listed here; all
other transitive dependencies ship under their own PyPI license metadata.

| Package | Installed via | License | Use |
| --- | --- | --- | --- |
| pynput | HumanMoveMouse / input automation chain | LGPL-3.0 | Input listener abstractions |
| evdev | pynput (Linux backend) | GPL-2.0 | Linux input event device bindings; built in a throw-away build stage |
| numpy | OpenCV, windmouse, HumanMoveMouse | BSD-3-Clause | Array/trajectory maths |
| scipy | HumanMoveMouse | BSD-3-Clause | Statistical trajectory model calculations |
| scikit-learn | HumanMoveMouse | BSD-3-Clause | Pre-trained trajectory model |
| pandas | HumanMoveMouse | BSD-3-Clause | Data handling for the trajectory model |

The exact transitive versions are resolved by pip at image-build time from the
pinned direct dependencies above. Packaged distributions retain their own licence
files/metadata in the image's site-packages where upstream provides them.

## System packages (Ubuntu 24.04 base image)

Xvfb, XFCE 4, xdotool, wmctrl, scrot, dbus, supervisord, Tesseract OCR and its
language data, fonts and their transitive dependencies are distributed under
their respective open-source licenses (mostly GPL/LGPL/BSD/MIT/Apache-2.0). The
Docker image retains `/usr/share/doc` package metadata where practical.

Tesseract is installed as the OCR engine (`tesseract-ocr`); OpenCV is installed
from the `opencv-python-headless` wheel and therefore does not pull in GUI
libraries. The image neither bundles nor requires a separate OCR service.

## Vendored binary

| Component | Version | License | Notes |
| --- | --- | --- | --- |
| Clack CLI (`third_party/clack/`) | 1.0.0 | MIT | Optional text-typing rhythm strategy. Upstream: https://github.com/ThisWasAryan/clack, release asset `clack-1.0.0-x86_64-linux-gnu.tar.gz` (SHA-256 recorded next to the binary). |

### GPL note (windmouse, evdev)

`windmouse` is **GPL-3.0-only** (per its PyPI metadata). The default image
includes it because the `WITH_OPTIONAL_STRATEGIES=true` build argument installs
the `windmouse` extra (alongside MIT-licensed HumanMoveMouse). `evdev`, a
transitive dependency of pynput on Linux, is GPL-2.0. If you redistribute a
NextAutoDesk image, the GPL obligations of these components apply to that
distribution. Build with `--build-arg WITH_OPTIONAL_STRATEGIES=false` (or
`./scripts/build-image.sh --no-optional-strategies`) for an image without the
optional strategy extras; the providers then report `available=false` at
runtime. `evdev` is only installed when the optional strategy extras are
enabled, because it is pulled in through HumanMoveMouse → pynput.

### Apache-2.0 note (OpenCV, pytesseract, Tesseract)

OpenCV, pytesseract and Tesseract are Apache-2.0 licensed. If you redistribute
the NextAutoDesk image or a derivative, retain the applicable license texts and
notices for these components. `opencv-python-headless` bundles OpenCV builds
under Apache-2.0; pytesseract is a Python wrapper distributed under Apache-2.0;
the Tesseract engine and its `eng`/`chi_sim` language data are Apache-2.0.
