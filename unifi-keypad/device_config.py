"""Validated, private per-installation configuration outside application releases."""
import alarm_outputs
import ipaddress
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID

ROOT = Path(__file__).resolve().parent
HOOKS = {'arm', 'disarm', 'entry_alert', 'arm_stay', 'arm_away', 'panic'}
# Installations saved before Stay/Away auto-provisioning carry only these.
PREV_HOOKS = {'arm', 'disarm', 'entry_alert'}
# Installations saved after Stay/Away but before panic auto-provisioning.
PRE_PANIC_HOOKS = {'arm', 'disarm', 'entry_alert', 'arm_stay', 'arm_away'}
STAY_AWAY_KEYS = {'stayProfile', 'stayProfileId', 'awayProfile', 'awayProfileId'}


def identifier(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('Invalid installation identifier.')
    return value


def validate_site(site):
    legacy = {'name', 'profile', 'doors', 'webhooks'}
    current = legacy | {'version', 'installationId', 'consoleHost', 'profileId', 'sensors',
                        'ruleIds', 'outputs', 'entryDelaySeconds', 'exitDelaySeconds', 'appliedAt'} | STAY_AWAY_KEYS
    # Transitional: saved before Stay/Away profiles were auto-provisioned.
    transitional = current - STAY_AWAY_KEYS
    if not isinstance(site, dict) or set(site) not in (legacy, transitional, current):
        raise ValueError('Site configuration has an unsupported format.')
    for key in ('name', 'profile'):
        if not isinstance(site[key], str) or not 1 <= len(site[key]) <= 100:
            raise ValueError('Invalid site name or alarm profile.')
    doors = site['doors']
    if (not isinstance(doors, list) or not 1 <= len(doors) <= 32 or
            any(not isinstance(x, str) or not 1 <= len(x) <= 100 for x in doors) or
            len(set(doors)) != len(doors)):
        raise ValueError('Configure unique sensor names for this site.')
    hooks = site['webhooks']
    if not isinstance(hooks, dict) or set(hooks) not in (PREV_HOOKS, PRE_PANIC_HOOKS, HOOKS):
        raise ValueError('Configure the alarm webhooks for this site.')
    for value in hooks.values():
        identifier(value)
    if set(site) in (transitional, current):
        if site['version'] != 2:
            raise ValueError('Unsupported site configuration version.')
        identifier(site['installationId'])
        identifier(site['profileId'])
        for key in ('stayProfile', 'awayProfile'):
            if key in site and (not isinstance(site[key], str) or not 1 <= len(site[key]) <= 100):
                raise ValueError('Invalid Stay/Away alarm profile.')
        for key in ('stayProfileId', 'awayProfileId'):
            if key in site:
                identifier(site[key])
        host = ipaddress.ip_address(site['consoleHost'])
        if host.version != 4 or not any(host in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
            raise ValueError('Use a private console address.')
        sensors = site['sensors']
        if (not isinstance(sensors, list) or len(sensors) != len(doors) or
                any(not isinstance(s, dict) or set(s) != {'id', 'name'} or
                    not isinstance(s['id'], str) or not 1 <= len(s['id']) <= 128 for s in sensors) or
                [s['name'] for s in sensors] != doors or len({s['id'] for s in sensors}) != len(sensors)):
            raise ValueError('Configure unique sensor identifiers and matching names.')
        if not isinstance(site['ruleIds'], dict) or set(site['ruleIds']) != set(hooks) | {'doors'}:
            raise ValueError('All managed alarm rules must be recorded.')
        for value in site['ruleIds'].values():
            identifier(value)
        if len(set(site['ruleIds'].values())) != len(site['ruleIds']):
            raise ValueError('Managed rule identifiers must be unique.')
        if site['entryDelaySeconds'] != 60 or site['exitDelaySeconds'] != 60:
            raise ValueError('This release supports one-minute entry and exit delays.')
        outputs = site['outputs']
        if (not isinstance(outputs, list) or len(outputs) not in (1, 2) or not isinstance(outputs[0], dict) or
                set(outputs[0]) != {'type', 'receivers'} or outputs[0]['type'] != 'push' or
                not isinstance(outputs[0]['receivers'], list) or not outputs[0]['receivers'] or
                len(set(outputs[0]['receivers'])) != len(outputs[0]['receivers'])):
            raise ValueError('Choose phone notification recipients and optional sirens.')
        if len(outputs) == 2:
            alarm_outputs.validate_output(outputs[1])
        for value in outputs[0]['receivers']:
            identifier(value)
        if not isinstance(site['appliedAt'], str) or len(site['appliedAt']) > 50:
            raise ValueError('Invalid saved setup timestamp.')
    return site


def load_site(path):
    path = Path(path)
    if not path.exists():
        return {'name': 'Home security', 'profile': '', 'doors': [], 'webhooks': {}}
    return validate_site(json.loads(path.read_text()))


def atomic_json(path, data):
    """Replace complete files atomically; secrets/config never become world-readable."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_site(path, site):
    validate_site(site)
    path = Path(path)
    if path.exists():
        atomic_json(path.with_suffix('.previous.json'), load_site(path))
    atomic_json(path, site)


def paths(environ=None, root=ROOT):
    env = os.environ if environ is None else environ
    root = Path(root)
    appliance = env.get('KEYPAD_APPLIANCE') == '1'
    state = Path(env.get('KEYPAD_STATE_DIR', str(root)))
    if appliance and (not state.is_absolute() or state.resolve() == root.resolve() or root.resolve() in state.resolve().parents):
        raise ValueError('Appliance state must be stored outside the application release.')
    saved = Path(env['KEYPAD_SITE_FILE']) if env.get('KEYPAD_SITE_FILE') else state / 'site.json'
    legacy = root / 'prototype-site.json'
    source = saved if saved.exists() or appliance or env.get('KEYPAD_SITE_FILE') else legacy
    return state, saved, source


def settings(environ=None):
    state, _, source = paths(environ)
    return state, load_site(source)


def selected_sensors(site, sensors, allow_missing=False):
    """Match permanent installations by ID; retain names for older prototypes."""
    if site.get('sensors'):
        indexed = {s.get('id'): s for s in sensors}
        selected = []
        for saved in site['sensors']:
            if saved['id'] not in indexed:
                if allow_missing:
                    selected.append({**saved, 'state': 'MISSING', 'isOpened': None})
                    continue
                raise ValueError('A configured door is missing; coverage is unverified.')
            # Stable display keys keep a rename from leaving stale observer rows.
            selected.append({**indexed[saved['id']], 'name': saved['name']})
        return selected
    selected = [s for s in sensors if s.get('name') in site['doors']]
    if len(selected) != len(site['doors']) or {s.get('name') for s in selected} != set(site['doors']):
        raise ValueError('Expected all configured entry sensors; coverage is unverified.')
    return selected
