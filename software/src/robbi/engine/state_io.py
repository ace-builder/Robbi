"""Small atomic state writes; no partial JSON if power disappears mid-write."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile


def atomic_text(path: Path, text: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
