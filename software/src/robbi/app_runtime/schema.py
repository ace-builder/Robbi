"""Small explicit JSON-schema subset shared by app manifests and invocations."""
from __future__ import annotations

import copy
import json
import math
import re

from robbi.errors import ValidationError

IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
TYPES = {"object", "array", "string", "number", "integer", "boolean"}
SCHEMA_KEYS = {"type", "title", "description", "properties", "required", "additionalProperties",
               "items", "enum", "default", "minimum", "maximum", "minLength", "maxLength",
               "minItems", "maxItems", "format", "x-order", "x-advanced"}


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValidationError("IDs must start with a lowercase letter and use letters, digits, _ or - (64 characters maximum)")
    return value


def bounded_json(value, limit=65536):
    count, string_size = 0, 0
    def inspect(item, depth=0):
        nonlocal count, string_size
        count += 1
        if depth > 12 or count > 4000:
            raise ValidationError("App data is too deeply nested or complex")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValidationError("App JSON keys must be text")
                inspect(key, depth + 1)
                inspect(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                inspect(child, depth + 1)
        elif isinstance(item, str):
            string_size += len(item)
            if string_size > limit:
                raise ValidationError(f"App data exceeds {limit} bytes")
    inspect(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValidationError("App data must be finite JSON values") from None
    if len(encoded.encode("utf-8")) > limit:
        raise ValidationError(f"App data exceeds {limit} bytes")
    return json.loads(encoded)


def check_schema(schema, depth=0):
    if depth > 5 or not isinstance(schema, dict) or schema.get("type") not in TYPES:
        raise ValidationError("App schema needs a supported type and at most five nested levels")
    if set(schema) - SCHEMA_KEYS:
        raise ValidationError(f"Unsupported schema fields: {', '.join(sorted(set(schema) - SCHEMA_KEYS))}")
    if "x-order" in schema and (type(schema["x-order"]) is not int or not 0 <= schema["x-order"] <= 1000):
        raise ValidationError("Schema x-order must be an integer from 0 to 1000")
    if "x-advanced" in schema and not isinstance(schema["x-advanced"], bool):
        raise ValidationError("Schema x-advanced must be true or false")
    for key in ("title", "description", "format"):
        if key in schema and (not isinstance(schema[key], str) or len(schema[key]) > 2000):
            raise ValidationError(f"Schema {key} must be bounded text")
    for key in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], (int, float)) or not math.isfinite(schema[key])):
            raise ValidationError(f"Invalid schema {key}")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not 1 <= len(schema["enum"]) <= 64):
        raise ValidationError("Schema enum must contain 1 to 64 values")
    if schema["type"] == "object":
        props = schema.get("properties", {})
        if not isinstance(props, dict) or len(props) > 40:
            raise ValidationError("Object schema has too many fields")
        for name, child in props.items():
            identifier(name)
            check_schema(child, depth + 1)
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(key, str) or key not in props for key in required):
            raise ValidationError("Required schema fields must be declared properties")
        if schema.get("additionalProperties", False) is not False:
            raise ValidationError("App schemas must reject undeclared fields")
    if schema["type"] == "array":
        check_schema(schema.get("items"), depth + 1)
    if "default" in schema:
        validate_value(schema, schema["default"], apply_defaults=False)
    return schema


def validate_value(schema, value, path="input", *, apply_defaults=True, depth=0):
    if depth > 6:
        raise ValidationError(f"{path}: nesting exceeds limit")
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            raise ValidationError(f"{path} must be an object")
        props = schema.get("properties", {})
        if set(value) - set(props):
            raise ValidationError(f"{path}: unknown fields {', '.join(sorted(set(value) - set(props)))}")
        output = {}
        for name, child in props.items():
            if name in value:
                output[name] = validate_value(child, value[name], f"{path}.{name}", apply_defaults=apply_defaults, depth=depth + 1)
            elif apply_defaults and "default" in child:
                output[name] = copy.deepcopy(child["default"])
            elif name in schema.get("required", []):
                raise ValidationError(f"{path}.{name} is required")
        return output
    if kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(value) <= min(128, schema.get("maxItems", 128)):
            raise ValidationError(f"{path} must be a bounded list")
        return [validate_value(schema["items"], item, f"{path}[{index}]", apply_defaults=apply_defaults, depth=depth + 1) for index, item in enumerate(value)]
    if kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= min(12000, schema.get("maxLength", 12000)):
            raise ValidationError(f"{path} must be text within its length limits")
        if schema.get("format") == "env" and value and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", value):
            raise ValidationError(f"{path} must name an environment variable, not contain its secret value")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ValidationError(f"{path} must be true or false")
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (kind == "integer" and not isinstance(value, int)):
            raise ValidationError(f"{path} must be a finite {kind}")
        if not schema.get("minimum", -1e15) <= value <= schema.get("maximum", 1e15):
            raise ValidationError(f"{path} is outside its allowed range")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path} must be one of the declared choices")
    return value


def patch_config(schema, current, changes):
    """Merge declared fields recursively; null removes an override/default."""
    if not isinstance(changes, dict):
        raise ValidationError("App configuration must be an object")
    props = schema.get("properties", {})
    if set(changes) - set(props):
        raise ValidationError("App configuration contains undeclared fields")
    result = dict(current) if isinstance(current, dict) else {}
    for name, value in changes.items():
        if value is None:
            result.pop(name, None)
        elif props[name]["type"] == "object" and isinstance(value, dict):
            result[name] = patch_config(props[name], result.get(name, {}), value)
        else:
            result[name] = value
    return result
