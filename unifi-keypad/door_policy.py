"""Per-door arming decisions and reminders. A reminder mute never mutes alarms."""
import json
import time
from device_config import atomic_json, paths
from protection_state import fingerprint

MUTE_SECONDS = 20 * 24 * 60 * 60
MAX_MUTE_SECONDS = 30 * 24 * 60 * 60
MUTE_DAY_CHOICES = (1, 7, 30)
MODES = {'alarm', 'status', 'bypass'}


def path(root):
    return paths(root=root)[0] / '.door-preferences.json'


def read(root, site):
    try:
        data = json.loads(path(root).read_text())
        if data.get('installationId') == site.get('installationId') and isinstance(data.get('modes'), dict) and isinstance(data.get('mutes'), dict):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {'installationId': site.get('installationId'), 'modes': {}, 'mutes': {}}


def save_modes(root, site, modes):
    data = read(root, site)
    if data['modes'] == modes:
        return
    data['modes'] = modes
    # Consent belongs to the exact alarm configuration, never a replacement.
    data['mutes'] = {}
    atomic_json(path(root), data)


def assess(site, observation, preferences, now=None):
    now = time.time() if now is None else now
    if not observation.get('fresh'):
        # A lost console session is not evidence that individual doors failed.
        return {'active': [], 'bypassed': [], 'canReview': False, 'needsConfirmation': False}
    active, bypassed = [], []
    for sensor in site.get('sensors', []):
        door = observation.get('doors', {}).get(sensor['name'], {})
        if not door.get('connected'):
            code, reason = 'unreachable', 'Unreachable'
        elif door.get('position') not in ('open', 'closed'):
            code, reason = 'unknown', 'Position unknown'
        elif door['position'] == 'open':
            code, reason = 'open', 'Door is open'
        elif sensor['name'] not in observation.get('verifiedDoors', []):
            code, reason = 'unchecked', 'Sensor check not completed'
        else:
            active.append(dict(sensor))
            continue
        mute = preferences.get('mutes', {}).get(sensor['id'], {})
        until = mute.get('until', 0)
        muted = (mute.get('reason') == code and mute.get('configuration') == fingerprint(site) and
                 type(until) in (int, float) and now < until <= now + MAX_MUTE_SECONDS)
        bypassed.append({**sensor, 'reason': reason, 'code': code, 'mutedUntil': until if muted else None})
    ready = bool(site.get('version') == 2 and observation.get('fresh') and observation.get('state') == 'disarmed' and active)
    return {'active': active, 'bypassed': bypassed, 'canReview': ready,
            'needsConfirmation': any(not row['mutedUntil'] for row in bypassed)}


def signature(coverage):
    # Names, health changes and replacement IDs invalidate a pending review.
    return {'active': coverage['active'], 'bypassed': [{k: row[k] for k in ('id', 'name', 'code')} for row in coverage['bypassed']]}


def mute(root, site, bypassed, days=20, now=None):
    """Snooze the reminder banner for the given bypass rows. A mute never
    mutes alarms: the banner returns early if the issue changes (a new code)
    or the alarm configuration changes, and always when the mute expires."""
    now = time.time() if now is None else now
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 20
    seconds = max(1, min(days, 30)) * 24 * 60 * 60
    data = read(root, site)
    for row in bypassed:
        data['mutes'][row['id']] = {'reason': row['code'], 'until': now + seconds, 'configuration': fingerprint(site)}
    atomic_json(path(root), data)


def clear_mutes(root, site):
    data = read(root, site)
    data['mutes'] = {}
    atomic_json(path(root), data)
