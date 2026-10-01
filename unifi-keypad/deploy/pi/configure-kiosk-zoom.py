#!/usr/bin/env python3
"""Set page zoom before launching the dedicated kiosk browser."""
import json
import math
import os
from pathlib import Path
import tempfile


def configure(profile, scale):
    if not math.isfinite(scale) or not 0.5 <= scale <= 3:
        raise ValueError('KEYPAD_DISPLAY_SCALE must be between 0.5 and 3.')
    profile.mkdir(parents=True, exist_ok=True)
    target = profile / 'Preferences'
    data = json.loads(target.read_text()) if target.exists() else {}
    partition = data.setdefault('partition', {})
    # Chromium uses "x" for the default storage partition. Zoom steps are 1.2x.
    partition.setdefault('default_zoom_level', {})['x'] = math.log(scale, 1.2)
    # An old per-site zoom must not override the panel's current display size.
    partition.get('per_host_zoom_levels', {}).get('x', {}).pop('127.0.0.1', None)
    descriptor, temporary = tempfile.mkstemp(prefix='.zoom-', dir=profile)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(data, stream, separators=(',', ':'))
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == '__main__':
    configure(Path.home() / '.config/loxicom-browser/Default',
              float(os.environ.get('KEYPAD_DISPLAY_SCALE', '1.75')))
