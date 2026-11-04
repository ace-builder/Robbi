"""Single parser for private environment files; no secret values in diagnostics."""

import os
from pathlib import Path

from robbi.storage import read_text_limited


def read_env(path):
    path = Path(path)
    if not path.exists():
        return {}
    values = {}
    for raw in read_text_limited(path, 65536, label="Secret file").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def load_into_environment(path):
    for key, value in read_env(path).items():
        os.environ.setdefault(key, value)
