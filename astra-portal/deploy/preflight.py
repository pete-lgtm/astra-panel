#!/usr/bin/env python3
"""Offline configuration checks. Does not provision a server or obtain a certificate."""
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def check(directory):
    errors = []
    env = {}
    path = directory / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    for key in ("ASTRA_DOMAIN", "ACME_EMAIL", "ASTRA_OIDC_ISSUER", "ASTRA_OIDC_CLIENT_ID"):
        if not env.get(key) or "REPLACE" in env[key]:
            errors.append(key + " is not configured")
    domain = env.get("ASTRA_DOMAIN", "")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", domain):
        errors.append("ASTRA_DOMAIN must be a hostname")
    if urlsplit(env.get("ASTRA_OIDC_ISSUER", "")).scheme != "https":
        errors.append("OIDC issuer must use HTTPS")
    operators_path = directory / "operators.txt"
    operators = []
    if operators_path.is_file():
        operators = [line.strip() for line in operators_path.read_text().splitlines() if line.strip() and not line.startswith("#")]
    if not operators or any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", v) or "*" in v for v in operators):
        errors.append("operators.txt needs explicit operator email addresses")
    for filename in ("cookie-secret", "oidc-client-secret"):
        path = directory / "secrets" / filename
        if not path.is_file():
            errors.append(filename + " is missing")
            continue
        stat = path.stat()
        if stat.st_mode & 0o077 or stat.st_uid != 65532:
            errors.append(filename + " needs owner 65532 and mode 0400")
        if filename == "cookie-secret" and stat.st_size != 32:
            errors.append("cookie-secret must be exactly 32 raw bytes")
        if filename == "oidc-client-secret" and stat.st_size == 0:
            errors.append("OIDC client secret is empty")
    return errors


if __name__ == "__main__":
    problems = check(Path(__file__).resolve().parent)
    if problems:
        print("Server preparation is incomplete:")
        for problem in problems:
            print(" - " + problem)
        sys.exit(1)
    print("Local configuration passed. Verify MFA, DNS, container images and HTTPS sign-in before enrolling a live panel.")
