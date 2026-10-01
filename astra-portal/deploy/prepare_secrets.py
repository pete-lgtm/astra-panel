#!/usr/bin/env python3
"""Run once on the new server. Does not print or overwrite secrets."""
import getpass
import os
from pathlib import Path
import secrets


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo on the cloud server to set the authentication container's file ownership.")
    directory = Path(__file__).resolve().parent / "secrets"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    targets = [directory / "cookie-secret", directory / "oidc-client-secret"]
    if any(path.exists() for path in targets):
        raise SystemExit("Secrets already exist. Rotate them deliberately; this command will not overwrite them.")
    client_secret = getpass.getpass("OIDC client secret (hidden): ").strip()
    if not client_secret or "REPLACE" in client_secret or len(client_secret) > 8192:
        raise SystemExit("A real OIDC client secret is required.")
    os.umask(0o077)
    for path, data in zip(targets, [secrets.token_bytes(32), client_secret.encode()]):
        with path.open("xb") as file:
            file.write(data)
        os.chown(path, 65532, 65532)
        path.chmod(0o400)
    print("Authentication secrets created privately. No values were printed.")


if __name__ == "__main__":
    main()
