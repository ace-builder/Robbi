from __future__ import annotations

import logging
import threading
from collections import defaultdict
from pathlib import Path
from robbi.errors import BusyError

from robbi.configuration.settings import ASSETS_DIR, ANIMATIONS_DIR


class AssetRegistry:
    def __init__(self) -> None:
        self._registry: dict[str, dict[str, Path]] = defaultdict(dict)
        self._directory_state: dict[str, tuple[float, int]] = {}
        self._scan_lock = threading.Lock()

    def scan(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            raise BusyError("Asset refresh is already running")
        try:
            self._scan()
        finally:
            self._scan_lock.release()

    def _scan(self) -> None:
        # Keep the previous catalogue usable while scanning and if scanning
        # fails. Readers only ever see a fully constructed registry.
        registry = {
            "faces": self._load_dir(ASSETS_DIR / "images" / "faces"),
            "images": self._load_dir(ASSETS_DIR / "images"),
            "sounds": self._load_dir(ASSETS_DIR / "sounds"),
            "videos": self._load_dir(ASSETS_DIR / "videos"),
            "voices": self._load_dir(ASSETS_DIR / "voices"),
            "animations": self._load_dir(ANIMATIONS_DIR, suffix=".yaml"),
        }
        directory_state = self._capture_directory_state()
        self._registry = registry
        self._directory_state = directory_state
        logging.getLogger(__name__).info(
            "Assets scanned: %s",
            {name: len(values) for name, values in self._registry.items()},
        )

    def refresh(self, force: bool = False) -> bool:
        if not self._scan_lock.acquire(blocking=False):
            raise BusyError("Asset refresh is already running")
        try:
            if force or self._capture_directory_state() != self._directory_state:
                self._scan()
                return True
            return False
        finally:
            self._scan_lock.release()

    def _capture_directory_state(self) -> dict[str, tuple[float, int]]:
        directories = {
            "faces": ASSETS_DIR / "images" / "faces",
            "images": ASSETS_DIR / "images",
            "sounds": ASSETS_DIR / "sounds",
            "videos": ASSETS_DIR / "videos",
            "voices": ASSETS_DIR / "voices",
            "animations": ANIMATIONS_DIR,
        }
        return {name: self._directory_signature(path) for name, path in directories.items()}

    def _directory_signature(self, directory: Path) -> tuple[float, int]:
        if not directory.exists():
            return (0.0, 0)
        newest_mtime = directory.stat().st_mtime
        file_count = 0
        for path in directory.glob("*"):
            if not path.is_file():
                continue
            file_count += 1
            newest_mtime = max(newest_mtime, path.stat().st_mtime)
        return (newest_mtime, file_count)

    def _load_dir(self, directory: Path, suffix: str | None = None) -> dict[str, Path]:
        values = {}
        if not directory.exists():
            return values
        for path in sorted(directory.glob("*")):
            if not path.is_file():
                continue
            if self._should_ignore_asset(path):
                continue
            if suffix and path.suffix != suffix:
                continue
            values[path.name] = path
        return values

    def get(self, category: str, name: str, extensions: tuple[str, ...] = ()) -> Path | None:
        values = self._registry.get(category, {})
        for candidate_name in self._candidate_names(name):
            if candidate_name in values:
                return values[candidate_name]
        for extension in extensions:
            for candidate_name in self._candidate_names(name):
                candidate = candidate_name if candidate_name.endswith(extension) else f"{candidate_name}{extension}"
                if candidate in values:
                    return values[candidate]
        return None

    def names(self, category: str) -> list[str]:
        return sorted(self._registry.get(category, {}).keys())

    def _should_ignore_asset(self, path: Path) -> bool:
        name = path.name
        return name.startswith("._") or name == ".DS_Store"

    def _candidate_names(self, name: str) -> list[str]:
        raw = str(name or "").strip()
        if not raw:
            return []
        candidates: list[str] = [raw]
        if raw.startswith("._"):
            candidates.append(raw[2:])
        return candidates
