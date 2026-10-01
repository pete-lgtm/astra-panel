#!/bin/sh
set -eu
# Rotate the Wayland output before Chromium starts. The matching 90-degree touch
# calibration is in 95-loxicom-touchscreen.rules; update both to change rotation.
# Native scale stays at 1; readable text is handled by Chromium page zoom.
output=${KEYPAD_DISPLAY_OUTPUT:-DSI-1}
transform=${KEYPAD_DISPLAY_TRANSFORM:-90}
case "$transform" in
  normal|90|180|270) ;;
  *) echo 'Invalid keypad display transform.' >&2; exit 1 ;;
esac
wlr-randr --output "$output" --transform "$transform" --scale 1
