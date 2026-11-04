"""Small atomic file writes; database transactions belong to domain stores."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: str | bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content.encode("utf-8") if isinstance(content, str) else content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, data) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")



def read_text_limited(path: Path, max_bytes: int, *, label="File") -> str:
    """Bound allocation while reading, even if a file grows after stat()."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("Read limit must be a positive integer")
    with Path(path).open("rb") as source:
        data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes // 1024} KiB")
    return data.decode("utf-8")
