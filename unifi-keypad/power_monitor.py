"""Mains/battery power monitoring for the keypad panel.

Disabled unless KEYPAD_POWER_MONITOR=1. A provider reports whether the panel
is on battery power; transitions are delivered to a callback so the panel can
log them and raise an alert. Detection itself needs no network access.

Providers:
  FilePowerProvider  JSON file like {"on_battery": true, "percent": 82}.
                     For Pi UPS HATs that expose state through a file, and tests.
  NutPowerProvider   NUT (upsc) output for a USB UPS watched by nut-server.
"""
import json
import subprocess
import time
from pathlib import Path


class FilePowerProvider:
    """Read {"on_battery": bool, "percent": 0-100} from a JSON file.

    A missing or unreadable file returns None (unknown) instead of guessing:
    an unknown reading must never be reported as an outage.
    """

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        try:
            raw = json.loads(self.path.read_text())
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        on_battery = raw.get('on_battery')
        if type(on_battery) is not bool:
            return None
        percent = raw.get('percent')
        if percent is not None:
            try:
                percent = float(percent)
            except (TypeError, ValueError):
                return None
            if not 0 <= percent <= 100:
                return None
        return {'on_battery': on_battery, 'percent': percent, 'source': 'file:%s' % self.path}


class NutPowerProvider:
    """Read UPS state through NUT's upsc.

    ups.status flags: OB = on battery, LB = low battery, OL = on line power.
    """

    def __init__(self, ups='ups@localhost', command='upsc', timeout=5):
        self.ups = ups
        self.command = command
        self.timeout = timeout

    def read(self):
        try:
            completed = subprocess.run([self.command, self.ups], capture_output=True,
                                       text=True, timeout=self.timeout)
        except Exception:
            return None
        if completed.returncode != 0:
            return None
        values = {}
        for line in completed.stdout.splitlines():
            if ':' in line:
                key, _, val = line.partition(':')
                values[key.strip()] = val.strip()
        flags = set(values.get('ups.status', '').split())
        percent = None
        if 'battery.charge' in values:
            try:
                percent = float(values['battery.charge'])
            except ValueError:
                percent = None
        return {'on_battery': 'OB' in flags, 'percent': percent,
                'low_battery': 'LB' in flags, 'source': 'nut:%s' % self.ups}


class PowerMonitor:
    """Poll a provider and report transitions through on_event(event, status).

    Events: 'outage' (mains lost), 'restored' (mains back), 'low_battery'.
    The first known reading only sets the baseline; unknown readings are
    ignored. 'low_battery' fires once per outage, from the UPS low-battery
    flag or when the reported percent drops to low_battery_percent.
    """

    def __init__(self, provider, on_event=None, poll_seconds=15, low_battery_percent=25):
        self.provider = provider
        self.on_event = on_event or (lambda event, status: None)
        self.poll_seconds = poll_seconds
        self.low_battery_percent = low_battery_percent
        self._last_on_battery = None
        self._low_signalled = False

    def poll_once(self):
        status = self.provider.read()
        if status is None:
            return None
        on_battery = bool(status['on_battery'])
        if self._last_on_battery is None:
            self._last_on_battery = on_battery
            return status
        if on_battery != self._last_on_battery:
            self._last_on_battery = on_battery
            self._low_signalled = False
            self.on_event('outage' if on_battery else 'restored', status)
        percent = status.get('percent')
        if on_battery and not self._low_signalled and (
                status.get('low_battery') or
                (percent is not None and percent <= self.low_battery_percent)):
            self._low_signalled = True
            self.on_event('low_battery', status)
        return status

    def run_forever(self, stop=None):
        while stop is None or not stop.is_set():
            try:
                self.poll_once()
            except Exception:
                pass
            if stop is None:
                time.sleep(self.poll_seconds)
            elif stop.wait(self.poll_seconds):
                break
