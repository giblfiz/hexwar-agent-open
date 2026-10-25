"""Local HXO signing prototype. No wallet keys and no network access.

Wire encoding HXO-JSON-1: ASCII strings/keys, lists, objects, booleans, null,
integers within +/- (2**53-1). Python JSON compact separators, sorted keys,
ensure_ascii=True. This restricted encoding is specified here, not claimed to
be a complete RFC 8785 implementation. Floating point is not permitted.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DOMAIN = b"HEXWAR-AGENT-OPEN/v0\x00"


def canonical(value):
    def check(v):
        if v is None or type(v) is bool:
            return
        if type(v) is int and abs(v) <= 2**53 - 1:
            return
        if type(v) is str and v.isascii():
            return
        if type(v) is list:
            for item in v:
                check(item)
            return
        if type(v) is dict:
            for key, item in v.items():
                if type(key) is not str or not key.isascii():
                    raise ValueError("object keys must be ASCII strings")
                check(item)
            return
        raise ValueError("value outside HXO-JSON-1 encoding")
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def parse(text):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise ValueError("duplicate JSON key")
            d[k] = v
        return d
    value = json.loads(text, object_pairs_hook=pairs)
    canonical(value)
    return value


def public_hex(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def generate_key(path):
    """Create a tournament identity key, never overwriting an existing file."""
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    fd = os.open(Path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(raw)
    return public_hex(key)


def load_key(path):
    return Ed25519PrivateKey.from_private_bytes(Path(path).read_bytes())


def sign(payload, key):
    if type(payload) is not dict:
        raise ValueError("signed payload must be an object")
    return {"public_key": public_hex(key), "payload": payload,
            "signature": key.sign(DOMAIN + canonical(payload)).hex()}


def verify(envelope, expected_key=None):
    if type(envelope) is not dict or set(envelope) != {"public_key", "payload", "signature"}:
        raise ValueError("invalid signed envelope")
    canonical(envelope)
    pub, sig = envelope["public_key"], envelope["signature"]
    if type(pub) is not str or len(pub) != 64 or pub != pub.lower():
        raise ValueError("invalid public key")
    if type(sig) is not str or len(sig) != 128 or sig != sig.lower():
        raise ValueError("invalid signature encoding")
    if expected_key is not None and pub != expected_key:
        raise ValueError("unexpected signer")
    if type(envelope['payload']) is not dict:
        raise ValueError("signed payload must be an object")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(
            bytes.fromhex(sig), DOMAIN + canonical(envelope["payload"]))
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("signature verification failed") from exc
    return envelope["payload"]
