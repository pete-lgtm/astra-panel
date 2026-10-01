#!/usr/bin/env python3
"""A separate, low-priority Linux health reporter; not an alarm controller.

Standard library only. Reads public OS measurements and the release version.
It never opens keypad state, sends LAN discovery traffic, or executes a command
from the server. Even a successful server response has no action semantics.
"""
import argparse
import json
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import protocol


def read_number(path, divisor=1):
    try:
        return round(float(Path(path).read_text().strip()) / divisor, 1)
    except (OSError, ValueError):
        return None


def collect():
    try:
        version = Path("/opt/loxicom/current/VERSION").read_text().strip()
    except OSError:
        version = "unknown"
    if not protocol.VERSION.fullmatch(version):
        version = "unknown"
    try:
        service = subprocess.run(["/usr/bin/systemctl", "is-active", "loxicom-keypad.service"],
                                 capture_output=True, text=True, timeout=3, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        service = "unknown"
    if service not in protocol.SERVICES:
        service = "unknown"
    try:
        memory = dict((line.split(":")[0], int(line.split()[1])) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        memory_pct = round(100 * (1 - memory["MemAvailable"] / memory["MemTotal"]), 1)
    except (OSError, ValueError, KeyError, ZeroDivisionError):
        memory_pct = None
    try:
        uptime = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        uptime = 0
    try:
        disk = shutil.disk_usage("/")
        disk_pct = round(100 * disk.used / disk.total, 1)
    except OSError:
        disk_pct = None
    try:
        load = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        load = None
    return protocol.validate({"schema": 1, "version": version, "uptime_s": uptime,
                              "temperature_c": read_number("/sys/class/thermal/thermal_zone0/temp", 1000),
                              "memory_used_pct": memory_pct, "disk_used_pct": disk_pct,
                              "load_1m": load, "panel_service": service})


def load_config(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("Enrollment file must be readable only by its owner.")
    if path.stat().st_size > 4096:
        raise ValueError("Enrollment file is too large.")
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or set(config) != {"url", "panel_id", "secret"}:
        raise ValueError("Invalid enrollment file.")
    url = urlsplit(config["url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.path != "/api/v1/heartbeat":
        raise ValueError("A valid HTTPS fleet endpoint is required.")
    if not protocol.DEVICE_ID.fullmatch(config["panel_id"]) or not re.fullmatch(r"[a-f0-9]{64}", config["secret"]):
        raise ValueError("Invalid device identity.")
    return config


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Fleet redirects are refused", headers, fp)


def send(config, payload, opener=None):
    body = protocol.encode(protocol.validate(payload))
    timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
    request = urllib.request.Request(config["url"], data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Astra-Panel": config["panel_id"],
        "X-Astra-Time": timestamp, "X-Astra-Nonce": nonce,
        "X-Astra-Signature": protocol.signature(config["secret"], config["panel_id"], timestamp, nonce, body)
    })
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(request, timeout=8) as response:
        if response.status != 204:
            raise ValueError("Unexpected fleet acknowledgement.")
        # Deliberately no response.read(), commands, downloads or remote shell.


def main():
    parser = argparse.ArgumentParser()
    default = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/etc/astra")) / "enrollment.json"
    parser.add_argument("--config", default=str(default))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    failures = 0
    while True:
        try:
            send(config, collect())
            failures = 0
            print("Astra health check accepted.", flush=True)
        except Exception:
            failures += 1
            # Exception text can contain request URLs or response data. Never log it.
            print("Astra health check unavailable; local panel operation is independent.", flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(min(300, 60 * 2 ** min(failures, 3)) + random.uniform(0, 10))


if __name__ == "__main__":
    raise SystemExit(main())
