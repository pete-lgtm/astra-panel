#!/bin/sh
set -eu
# A single-application compositor; no desktop, panel, or file manager is started.
# Chromium's sandbox stays enabled. Never run this as root.
# The 5-inch panel is used at 1280x720 in landscape with larger touch targets.
# Page zoom keeps the Wayland output at its native scale and touch coordinates.
# It applies to setup and recovery; desktop web browsers are unaffected.
/usr/local/lib/loxicom/configure-kiosk-display.sh || \
  echo 'Could not rotate the display; retaining its current orientation.' >&2
while :; do
  # Start on a real local page, rather than Chromium's connection-error screen.
  python3 - <<'PY'
import time
import urllib.request
while True:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8764/', timeout=2) as response:
            if response.status == 200:
                break
    except OSError:
        pass
    time.sleep(1)
PY
  python3 /usr/local/lib/loxicom/configure-kiosk-zoom.py || \
    echo 'Could not update display size; using the saved browser preferences.' >&2
  chromium --ozone-platform=wayland --kiosk --no-first-run --noerrdialogs \
    --disable-session-crashed-bubble --password-store=basic \
    --user-data-dir=/home/loxicom-kiosk/.config/loxicom-browser \
    http://127.0.0.1:8764/ || true
  sleep 2
done
