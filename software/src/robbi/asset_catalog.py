"""Grouped authoring metadata stored in one assets/catalog.json file.

The runtime still discovers playable files from their media directories.
CatalogGroup lets authoring tools read/write one collection without replacing
the other collections. Writes are atomic and serialized across processes.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import time

from .storage import atomic_write


def catalog_path(root: Path) -> Path:
    return Path(root) / "assets" / "catalog.json"


def read_catalog(root: Path) -> dict:
    path = catalog_path(root)
    if not path.exists():
        return {"version": 1, "collections": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("collections"), dict):
        raise ValueError(f"Invalid central asset catalog: {path}")
    return payload


@contextmanager
def _write_lock(path: Path):
    # Lock a separate stable inode: the JSON itself is replaced atomically.
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        import os
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 30
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Asset catalog is being updated")
                    time.sleep(.05)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class CatalogGroup:
    """JSON text view of a named collection, not an independent file."""

    def __init__(self, root: Path, name: str):
        self.root = Path(root)
        self.name = name

    @property
    def parent(self) -> Path:
        return catalog_path(self.root).parent

    def exists(self) -> bool:
        return self.name in read_catalog(self.root)["collections"]

    def read_text(self, encoding: str = "utf-8") -> str:
        return json.dumps(read_catalog(self.root)["collections"][self.name],
                          indent=2, ensure_ascii=False) + "\n"

    def read_bytes(self) -> bytes:
        return self.read_text().encode("utf-8")

    def write_text(self, text: str, encoding: str = "utf-8") -> int:
        collection = json.loads(text)
        if not isinstance(collection, dict) or not isinstance(collection.get("clips"), list):
            raise ValueError("A catalog collection needs a clips list")
        path = catalog_path(self.root)
        with _write_lock(path):
            payload = read_catalog(self.root)
            payload["collections"][self.name] = collection
            atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False,
                                         allow_nan=False) + "\n")
        return len(text)


def catalog_group(root: Path, name: str) -> CatalogGroup:
    return CatalogGroup(root, name)
