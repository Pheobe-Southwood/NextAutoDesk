"""NextAutoDesk — headless X11/XFCE GUI automation node.

Layers (bottom-up):

1. ``drivers``    — thin execution engines (PyAutoGUI / X11 tools). Strategy-agnostic.
2. ``strategies`` — behaviour policies (human-movement, HumanMoveMouse, WindMouse,
   Clack, direct). Produce and pace input events through a driver backend.
3. ``actions``    — semantic desktop operations exposed to callers (mouse, keyboard,
   screen, window, wait).
4. ``api``        — FastAPI HTTP surface with auth, unified errors and OpenAPI.
5. Runtime        — serial execution queue, task persistence, logging, health.
"""

from __future__ import annotations

__version__ = "0.1.0a1"
__version_info__ = (0, 1, 0, "alpha1")
API_VERSION = "v1"

__all__ = ["API_VERSION", "__version__", "__version_info__"]
