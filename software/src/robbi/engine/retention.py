from __future__ import annotations

import logging
import threading
import time

from robbi.configuration.settings import LOG_DIR, PHOTOS_DIR, RUNTIME_DIR, settings


class RetentionManager:
    def __init__(self) -> None:
        self._log = logging.getLogger(__name__)
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._targets = [PHOTOS_DIR, LOG_DIR, RUNTIME_DIR]

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="retention")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def run_once(self) -> int:
        deleted = 0
        cutoff = time.time() - (settings.cleanup_days * 86400)
        for directory in self._targets:
            if not directory.exists():
                continue
            for path in directory.rglob("*"):
                if not path.is_file():
                    continue
                if path.name.endswith(".json"):
                    continue
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    deleted += 1
        if deleted:
            self._log.info("Retention removed %s old files", deleted)
        return deleted

    def _loop(self) -> None:
        while self._running:
            try:
                self.run_once()
            except OSError as exc:
                self._log.warning('Retention could not finish: %s', exc)
            self._stop_event.wait(6 * 3600)

