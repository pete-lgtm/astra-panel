"""Persist sensor checks separately from authorization to resume protection.

Never save an armed state, pending timer, session cookie, or credential here.
"""
import hashlib
import json
from device_config import atomic_json


def fingerprint(site):
    # Re-saving identical setup is harmless; changing a sensor, console, profile,
    # rule, delay or recipient requires verification again.
    config = {key: value for key, value in site.items() if key != 'appliedAt'}
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def checked(site, observation):
    names = set(site.get('doors', []))
    return bool(site.get('version') == 2 and names and observation.get('fresh') and
                observation.get('state') == 'disarmed' and
                names <= set(observation.get('verifiedDoors', [])) and
                set(observation.get('doors', {})) == names and
                all(door.get('connected') and door.get('position') == 'closed'
                    for door in observation['doors'].values()))


def save(path, site, observation):
    if not checked(site, observation):
        raise ValueError('While disarmed, open and close each selected door before saving protection.')
    atomic_json(path, {'version': 2, 'siteFingerprint': fingerprint(site),
                       'verifiedDoors': sorted(site['doors']), 'enabled': True})


def read(path, site):
    try:
        if path.stat().st_size > 8192:
            return None
        data = json.loads(path.read_text())
        if (set(data) == {'version', 'siteFingerprint', 'verifiedDoors', 'enabled'} and data['version'] == 2 and
                site.get('version') == 2 and data['siteFingerprint'] == fingerprint(site) and
                isinstance(data['verifiedDoors'], list) and
                all(isinstance(name, str) for name in data['verifiedDoors']) and
                data['verifiedDoors'] == sorted(set(data['verifiedDoors'])) and
                set(data['verifiedDoors']) <= set(site['doors']) and type(data['enabled']) is bool and
                (not data['enabled'] or data['verifiedDoors'] == sorted(site['doors']))):
            return data
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def save_progress(path, site, observation):
    if site.get('version') != 2 or not observation.get('fresh') or observation.get('state') != 'disarmed':
        return
    old = read(path, site)
    verified = set(old['verifiedDoors']) if old else set()
    for name in set(observation.get('verifiedDoors', [])) & set(site['doors']):
        door = observation.get('doors', {}).get(name, {})
        if door.get('connected') is True and door.get('position') == 'closed':
            verified.add(name)
    if not verified:
        return
    data = {'version': 2, 'siteFingerprint': fingerprint(site), 'verifiedDoors': sorted(verified),
            'enabled': old['enabled'] if old else False}
    if data != old:
        atomic_json(path, data)


def restore_progress(path, site, observer):
    data = read(path, site)
    if data:
        observer.verified_doors.update(data['verifiedDoors'])
        observer.verified_event = bool(observer.verified_doors)


def carry_progress(path, before, after, observation):
    """Keep real checks for unchanged device IDs when another door is replaced."""
    if before.get('version') != 2 or after.get('version') != 2 or before['consoleHost'] != after['consoleHost']:
        return
    old = read(path, before)
    verified = set(observation.get('verifiedDoors', [])) | set(old['verifiedDoors'] if old else [])
    ids = {s['id'] for s in before['sensors'] if s['name'] in verified}
    names = sorted(s['name'] for s in after['sensors'] if s['id'] in ids)
    if names:
        atomic_json(path, {'version': 2, 'siteFingerprint': fingerprint(after), 'verifiedDoors': names, 'enabled': False})


def matches(path, site):
    data = read(path, site)
    return bool(data and data['enabled'])


def restore(observer, site):
    """Caller must revalidate the saved fingerprint AND actual Protect rules first."""
    snapshot = observer.snapshot()
    names = set(site['doors'])
    if (not snapshot['fresh'] or snapshot['state'] != 'disarmed' or
            set(snapshot['doors']) != names or
            any(not door['connected'] or door['position'] != 'closed' for door in snapshot['doors'].values())):
        raise ValueError('Protection will resume when Protect confirms disarmed and all selected doors are connected and closed.')
    observer.verified_event = True
    observer.verified_doors.update(names)
    observer.enable()
