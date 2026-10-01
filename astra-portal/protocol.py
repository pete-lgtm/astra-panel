"""Astra health protocol. No device commands or alarm control in this protocol."""
import hashlib
import hmac
import json
import math
import re

DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
NONCE = re.compile(r"^[a-f0-9]{32}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,39}$")
FIELDS = {"schema", "version", "uptime_s", "temperature_c", "memory_used_pct",
          "disk_used_pct", "load_1m", "panel_service"}
SERVICES = {"active", "inactive", "failed", "unknown"}
MAX_BODY = 4096


def encode(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def signature(secret, panel_id, timestamp, nonce, body):
    envelope = b"astra-health-v1\n" + panel_id.encode() + b"\n" + str(timestamp).encode() + b"\n" + nonce.encode() + b"\n" + body
    return hmac.new(bytes.fromhex(secret), envelope, hashlib.sha256).hexdigest()


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError("Only the documented health fields are accepted.")
    if type(payload["schema"]) is not int or payload["schema"] != 1:
        raise ValueError("Unsupported health schema.")
    if not isinstance(payload["version"], str) or not VERSION.fullmatch(payload["version"]):
        raise ValueError("Invalid version.")
    if payload["panel_service"] not in SERVICES:
        raise ValueError("Invalid service status.")
    bounds = {"uptime_s": (0, 10**10), "temperature_c": (-40, 150),
              "memory_used_pct": (0, 100), "disk_used_pct": (0, 100), "load_1m": (0, 1000)}
    for key, (low, high) in bounds.items():
        value = payload[key]
        if value is None and key != "uptime_s":
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Invalid health measurement.")
    return payload
