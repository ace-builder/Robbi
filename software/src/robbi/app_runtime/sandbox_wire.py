"""Bounded JSON transport; never deserialize Python objects from an app."""

import base64
import dataclasses
import json
from types import SimpleNamespace

MAX_FRAME = 18 * 1024 * 1024


def pack(value):
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if dataclasses.is_dataclass(value):
        return {"$record": pack(dataclasses.asdict(value))}
    if isinstance(value, dict):
        return {key: pack(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [pack(item) for item in value]
    return value


def unpack(value, *, records=False, depth=0):
    if depth > 16:
        raise ValueError("Sandbox message nesting exceeds limit")
    if isinstance(value, dict):
        if set(value) == {"$bytes"}:
            return base64.b64decode(value["$bytes"], validate=True)
        if records and set(value) == {"$record"}:
            return SimpleNamespace(**unpack(value["$record"], depth=depth + 1))
        return {
            key: unpack(item, records=records, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [unpack(item, records=records, depth=depth + 1) for item in value]
    return value


def encode(value):
    data = (
        json.dumps(pack(value), allow_nan=False, separators=(",", ":")).encode() + b"\n"
    )
    if len(data) > MAX_FRAME:
        raise ValueError("Sandbox message exceeds limit")
    return data
