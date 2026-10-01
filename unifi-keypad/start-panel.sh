#!/bin/bash
# Export UNIFI_CA_FILE or an independently verified UNIFI_CERT_SHA256 before
# connecting a console. No installation-specific trust or credentials ship here.
set -euo pipefail
cd -- "$(dirname -- "$0")"
exec python3 server.py
