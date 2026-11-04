"""Validate untrusted provider/app output before it reaches actuators."""
from __future__ import annotations

import math
import json
import re
from .errors import ValidationError


_APP_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")


def validate_app_manager(spec: object) -> dict:
    from .services.app_management_commands import OPERATIONS, VERSION_PATTERN
    if not isinstance(spec, dict) or set(spec) - {"operation", "query", "app_id", "version"}:
        raise ValidationError("app_manager accepts operation, query or app_id, and optional version")
    operation = spec.get("operation")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValidationError("Unknown app_manager operation")
    if "query" in spec and (not isinstance(spec["query"], str) or len(spec["query"]) > 120
                            or any(ord(c) < 32 for c in spec["query"])):
        raise ValidationError("app_manager query must be text up to 120 characters")
    if "app_id" in spec and (not isinstance(spec["app_id"], str)
                             or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", spec["app_id"])):
        raise ValidationError("app_manager app_id must be an exact registered identifier")
    if "query" in spec and "app_id" in spec:
        raise ValidationError("Choose one exact app_id or query")
    if operation != "search" and not (spec.get("app_id") or str(spec.get("query", "")).strip()):
        raise ValidationError("Name the exact app to manage")
    if "version" in spec and (operation not in {"install", "update", "restore", "info"}
            or not isinstance(spec["version"], str) or not re.fullmatch(VERSION_PATTERN, spec["version"])):
        raise ValidationError("app_manager version requires an exact major.minor.patch version")
    selected = dict(spec)
    if operation == "search" and "app_id" not in selected:
        selected.setdefault("query", "")
    return selected


def validate_tool_followup(spec: object) -> dict:
    if not isinstance(spec, dict) or set(spec) - {"operation", "call_id"}:
        raise ValidationError("tool_followup requires an operation and optional call_id")
    if spec.get("operation") not in {"repeat", "undo"}:
        raise ValidationError("tool_followup operation must be repeat or undo")
    if "call_id" in spec and (not isinstance(spec["call_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", spec["call_id"])):
        raise ValidationError("tool_followup call_id must identify a recent tool call")
    return dict(spec)


def validate_app_tool(spec: object) -> dict:
    """Check the small transport envelope before an app's own input schema."""
    if not isinstance(spec, dict) or set(spec) - {"app", "tool", "arguments"}:
        raise ValidationError("app_tool requires app, tool and arguments fields")
    for name in ("app", "tool"):
        value = spec.get(name)
        if not isinstance(value, str) or not _APP_NAME.fullmatch(value):
            raise ValidationError(f"app_tool {name} must be a registered identifier")
    arguments = spec.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValidationError("app_tool arguments must be an object")
    # Reuse the bounded JSON walk without an app_tool envelope. A recursive
    # app_tool in arguments is rejected; this protocol never nests invocations.
    validate_action({"arguments": arguments})
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValidationError("app_tool arguments must be valid JSON") from error
    if len(encoded) > 16384:
        raise ValidationError("app_tool arguments exceed 16 KiB")
    return {"app": spec["app"], "tool": spec["tool"], "arguments": arguments}


def validate_action(action: dict) -> dict:
    if isinstance(action,dict) and 'capability_search' in action and set(action)-{'capability_search','speak','face','social_signal'}:
        raise ValidationError('Capability discovery must stand alone')
    nodes = 0
    def walk(value, depth=0, key=""):
        nonlocal nodes
        if key == 'tool_chain' and depth == 1:
            # Runtime receipts are inert evidence, not recursively executable actions.
            # Keep their arguments (including action-shaped external text) as data.
            try:
                encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
                if not isinstance(value, dict) or len(encoded.encode('utf8')) > 40000:
                    raise ValueError('Oversized trace')
            except (ValueError, TypeError, UnicodeError, RecursionError) as error:
                raise ValidationError('Invalid tool chain receipt') from error
            return
        nodes += 1
        if nodes > 1500 or depth > 8:
            raise ValidationError("Action is too complex")
        if key == "capability_search":
            if (depth != 1 or not isinstance(value,dict) or set(value)!={"query"}
                    or not isinstance(value["query"],str) or not 1 <= len(value["query"]) <= 200):
                raise ValidationError("capability_search requires a top-level query of 1..200 characters")
        if key == "app_tool":
            if depth != 1:
                raise ValidationError("app_tool must be a single top-level action")
            validate_app_tool(value)
        if key == "tool_followup":
            if depth != 1:
                raise ValidationError("tool_followup must be a single top-level action")
            validate_tool_followup(value)
        if key == "app_manager":
            if depth != 1:
                raise ValidationError("app_manager must be a single top-level action")
            validate_app_manager(value)
        if key == "background_task":
            if depth != 1 or not isinstance(value, dict) or set(value) - {"prompt", "text"}:
                raise ValidationError("background_task must be a top-level object with optional prompt or text")
            if len(value) > 1 or any(not isinstance(item, str) or len(item) > 12000 for item in value.values()):
                raise ValidationError("background_task requires at most one text prompt up to 12000 characters")
        if isinstance(value, dict):
            if value.get('gesture') and any(value.get(k) for k in ('drive', 'face_animation', 'animation', 'video', 'filming_sequence')):
                raise ValidationError('A gesture owns its motion and presentation; put other effects in separate sequence steps')
            if value.get('face_animation') and any(value.get(k) for k in ('animation', 'video', 'filming_sequence')):
                raise ValidationError('Use one display animation per sequence step')
            if len(value) > 48:
                raise ValidationError("Action has too many fields")
            for child_key, child in value.items():
                if not isinstance(child_key, str):
                    raise ValidationError("Action keys must be text")
                walk(child, depth + 1, child_key)
        elif isinstance(value, list):
            if len(value) > (64 if key == "sequence" else 100):
                raise ValidationError("Action list exceeds limit")
            if key == "sequence" and any(not isinstance(step, dict) for step in value):
                raise ValidationError("Every sequence step must be an object")
            for child in value:
                walk(child, depth + 1)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValidationError("Action contains a non-finite number")
        elif isinstance(value, str):
            if len(value) > (12000 if key == "speak" else 20000):
                raise ValidationError(f"Action field {key} is too long")
            if key in {"face", "sound", "video", "animation", "filming_sequence", "gesture", "face_animation"} and (".." in value or "/" in value or "\\" in value):
                raise ValidationError("Assets must be selected by registered name")
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValidationError("Unsupported action value")
        if key == "wait_ms":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 120000:
                raise ValidationError("wait_ms must be between 0 and 120000")
        if key in {"speak", "face", "sound", "video", "animation", "filming_sequence", "gesture", "face_animation"} and value is not None and not isinstance(value, str):
            raise ValidationError(f"{key} must be text")
    if not isinstance(action, dict):
        raise ValidationError("Action must be an object")
    if 'decision' in action:
        if not isinstance(action['decision'], str) or action['decision'] not in {'answer', 'clarify', 'stop'}:
            raise ValidationError('decision must be answer, clarify or stop')
        if set(action) & {'app_tool', 'web_search', 'memory_search', 'capability_search'}:
            raise ValidationError('Choose a final decision or one tool call, not both')
    if "tool_followup" in action and set(action) - {"tool_followup", "speak", "face"}:
        raise ValidationError("Run a tool follow-up on its own, without other actions")
    if "app_manager" in action and set(action) - {"app_manager", "speak", "face"}:
        raise ValidationError("Run app management on its own, without other actions")
    walk(action)
    return action
