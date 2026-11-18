"""Storage helpers: data directories and the screenshot store.

The data layout is intentionally simple so it can be inspected from the host::

    <data_dir>/
      nextautodesk.db          # SQLite (task history, runtime metadata)
      screenshots/             # captured screenshots
      logs/                    # rotating application logs
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .config import AppConfig
from .errors import ScreenshotNotFound, StorageError
from .models import ScreenshotFormat, ScreenshotInfo

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FORMAT_SUFFIX = {"png": ".png", "jpeg": ".jpg"}
_FORMAT_SUFFIXES = {"png": {".png"}, "jpeg": {".jpg", ".jpeg"}}


class DataStore:
    """Filesystem layout + screenshot persistence."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.root = config.storage.data_path
        self.screenshots_dir = config.storage.screenshots_path
        self.logs_dir = config.storage.logs_path
        self.database_path = config.storage.database_path
        self.screenshots = ScreenshotStore(self.screenshots_dir, config)

    def ensure_directories(self) -> None:
        for path in (self.root, self.screenshots_dir, self.logs_dir, self.database_path.parent):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StorageError(f"cannot create data directory {path}: {exc}") from exc

    def writable(self) -> bool:
        try:
            self.ensure_directories()
            probe = self.root / f".write-probe-{uuid.uuid4().hex}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except Exception:
            return False

    def disk_usage(self) -> dict[str, int]:
        import shutil

        usage = shutil.disk_usage(str(self.root if self.root.exists() else Path("/")))
        return {"total": usage.total, "used": usage.used, "free": usage.free}


class ScreenshotStore:
    """CRUD for saved screenshots with simple retention enforcement."""

    def __init__(self, directory: Path, config: AppConfig) -> None:
        self.directory = directory
        self.config = config
        self._lock = threading.Lock()

    # -- helpers -----------------------------------------------------------
    def _ensure(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"cannot create screenshots directory {self.directory}: {exc}") from exc

    @staticmethod
    def _safe_name(name: str) -> str:
        candidate = name.strip()
        if not _NAME_RE.match(candidate) or ".." in candidate:
            raise StorageError(
                f"invalid screenshot name {name!r}",
                details={"pattern": _NAME_RE.pattern},
            )
        return candidate

    def path_for(self, name: str) -> Path:
        return self.directory / self._safe_name(name)

    def _generate_name(self, fmt: ScreenshotFormat, requested: str | None) -> str:
        if requested:
            name = self._safe_name(requested)
            suffix = Path(name).suffix.lower()
            if not suffix:
                return name + _FORMAT_SUFFIX[fmt]
            if suffix not in _FORMAT_SUFFIXES[fmt]:
                allowed_exts = sorted(_FORMAT_SUFFIXES[fmt])
                raise StorageError(
                    f"screenshot name {name!r} has extension {suffix!r}, which does not "
                    f"match format {fmt!r} (allowed: {', '.join(allowed_exts)})",
                    details={"name": name, "format": fmt, "allowed": allowed_exts},
                )
            return name
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
        return f"screen_{stamp}_{uuid.uuid4().hex[:8]}{_FORMAT_SUFFIX[fmt]}"

    # -- write / read ------------------------------------------------------
    def save(
        self,
        data: bytes,
        *,
        fmt: ScreenshotFormat = "png",
        name: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> ScreenshotInfo:
        self._ensure()
        final_name = self._generate_name(fmt, name)
        target = self.directory / final_name
        tmp = target.with_suffix(target.suffix + ".tmp")
        with self._lock:
            try:
                tmp.write_bytes(data)
                os.replace(tmp, target)
            except OSError as exc:
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
                raise StorageError(f"cannot save screenshot {final_name}: {exc}") from exc
            # Run retention under the same lock: a concurrent save must never be
            # deleted while its .tmp file is being written or renamed. The file
            # just written is explicitly protected: with a small max_screenshots
            # value and equal mtimes, sorting alone could otherwise pick it for
            # deletion and turn this successful save into a StorageError.
            self._enforce_retention(keep=target)
        return self._info(target, fmt=fmt, width=width, height=height)

    def _info(
        self,
        path: Path,
        *,
        fmt: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> ScreenshotInfo:
        try:
            stat = path.stat()
        except OSError as exc:
            raise StorageError(f"cannot stat screenshot {path.name}: {exc}") from exc
        suffix = path.suffix.lower()
        detected = fmt or ("png" if suffix == ".png" else "jpeg" if suffix in {".jpg", ".jpeg"} else suffix.lstrip("."))
        return ScreenshotInfo(
            name=path.name,
            size_bytes=stat.st_size,
            width=width,
            height=height,
            format=detected,
            created_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            download_url=f"/api/v1/screen/screenshots/{path.name}",
        )

    def count(self) -> int:
        """Number of stored screenshots (excluding in-flight ``.tmp`` files)."""

        self._ensure()
        try:
            return sum(
                1
                for path in self.directory.iterdir()
                if path.is_file() and not path.name.endswith(".tmp")
            )
        except OSError as exc:  # pragma: no cover - defensive
            raise StorageError(f"cannot count screenshots in {self.directory}: {exc}") from exc

    def list(self, *, limit: int = 200, offset: int = 0) -> list[ScreenshotInfo]:
        self._ensure()
        entries: list[ScreenshotInfo] = []
        for path in self.directory.iterdir():
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            try:
                entries.append(self._info(path))
            except StorageError:
                continue
        entries.sort(key=lambda item: item.created_at, reverse=True)
        return entries[offset : offset + limit]

    def get(self, name: str) -> tuple[Path, ScreenshotInfo]:
        path = self.path_for(name)
        if not path.is_file():
            raise ScreenshotNotFound(
                f"screenshot {name!r} not found",
                details={"name": name},
            )
        return path, self._info(path)

    def delete(self, name: str) -> None:
        path = self.path_for(name)
        if not path.is_file():
            raise ScreenshotNotFound(f"screenshot {name!r} not found", details={"name": name})
        try:
            path.unlink()
        except OSError as exc:
            raise StorageError(f"cannot delete screenshot {name!r}: {exc}") from exc

    # -- retention ---------------------------------------------------------
    def _enforce_retention(self, *, keep: Path | None = None) -> None:
        """Drop expired screenshots and enforce ``storage.max_screenshots``.

        ``keep`` is the file that was just written by the current save. It is
        never a deletion candidate: the store guarantees that a successful
        ``save`` leaves its file on disk, independently of mtime resolution.
        """

        max_files = self.config.storage.max_screenshots
        retention_days = self.config.storage.screenshot_retention_days
        now = time.time()

        def _candidates() -> list[Path]:
            # Never touch in-flight ``.tmp`` files: they may belong to a save on
            # another thread and deleting them turns into a spurious StorageError.
            return [
                path
                for path in self.directory.iterdir()
                if path.is_file() and not path.name.endswith(".tmp") and path != keep
            ]

        def _created_ns(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:  # pragma: no cover - raced with another deletion
                return 0

        for path in _candidates():
            if retention_days > 0 and now - path.stat().st_mtime > retention_days * 86400:
                with contextlib.suppress(OSError):
                    path.unlink()
        files = _candidates()
        # The protected file counts towards the limit even though it is never a
        # deletion candidate, so other files may only fill the remaining slots.
        limit = max_files - (1 if keep is not None else 0)
        if len(files) > limit:
            # Deterministic ordering: oldest mtime first, file name as tie-break
            # (coarse filesystem timestamps otherwise make pruning random).
            files.sort(key=lambda path: (_created_ns(path), path.name))
            for path in files[: len(files) - limit]:
                with contextlib.suppress(OSError):
                    path.unlink()


__all__ = ["DataStore", "ScreenshotStore"]
