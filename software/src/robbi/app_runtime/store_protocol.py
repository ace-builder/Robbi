"""Version-one store signatures. No robot, app loader or network imports."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from robbi.errors import ValidationError

MAX_SIGNED_BYTES = 2 * 1024 * 1024


def canonical(payload):
    """Exact wire canonicalization: sorted keys, compact UTF-8, finite JSON."""
    if not isinstance(payload, dict):
        raise ValidationError("Signed store payload must be an object")
    count = 0

    def inspect(value, depth=0):
        nonlocal count
        count += 1
        if depth > 20 or count > 100000:
            raise ValidationError("Signed store payload exceeds structural limits")
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValidationError("Store JSON keys must be strings")
            for child in value.values():
                inspect(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                inspect(child, depth + 1)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValidationError("Store JSON numbers must be finite")
        elif value is not None and type(value) not in {str, int, bool}:
            raise ValidationError("Store payload must contain JSON values only")

    inspect(payload)
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValidationError("Invalid signed store JSON") from None
    if len(encoded) > MAX_SIGNED_BYTES:
        raise ValidationError("Signed store payload exceeds 2 MiB")
    return encoded


def _decode(value, size, label):
    if not isinstance(value, str) or len(value) > 128:
        raise ValidationError(f"Invalid store {label}")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValidationError(f"Invalid store {label}") from None
    if len(raw) != size or base64.b64encode(raw).decode("ascii") != value:
        raise ValidationError(f"Invalid store {label}")
    return raw


def public_key_bytes(public_key):
    if isinstance(public_key, Ed25519PublicKey):
        return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if isinstance(public_key, bytes) and len(public_key) == 32:
        return public_key
    return _decode(public_key, 32, "public key")


def fingerprint(public_key):
    return hashlib.sha256(public_key_bytes(public_key)).hexdigest()


def encode_public_key(public_key):
    return base64.b64encode(public_key_bytes(public_key)).decode("ascii")


def sign_payload(payload, private_key):
    if not isinstance(private_key, Ed25519PrivateKey):
        raw = private_key if isinstance(private_key, bytes) and len(private_key) == 32 else _decode(private_key, 32, "private key")
        private_key = Ed25519PrivateKey.from_private_bytes(raw)
    encoded = canonical(payload)
    envelope = {"payload": json.loads(encoded), "signature": base64.b64encode(private_key.sign(encoded)).decode("ascii"),
                "key_id": fingerprint(private_key.public_key())}
    # Flask's compact JSON response adds one trailing newline on the wire.
    if len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) + 1 > MAX_SIGNED_BYTES:
        raise ValidationError("Signed store envelope exceeds 2 MiB")
    return envelope


def verify_envelope(envelope, public_key):
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature", "key_id"}:
        raise ValidationError("Invalid signed store envelope")
    raw_key = public_key_bytes(public_key)
    if envelope["key_id"] != fingerprint(raw_key):
        raise ValidationError("Store signature key does not match the trusted key")
    signature = _decode(envelope["signature"], 64, "signature")
    encoded = canonical(envelope["payload"])
    if len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) + 1 > MAX_SIGNED_BYTES:
        raise ValidationError("Signed store envelope exceeds 2 MiB")
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, encoded)
    except InvalidSignature:
        raise ValidationError("Store signature is invalid") from None
    return json.loads(encoded)
