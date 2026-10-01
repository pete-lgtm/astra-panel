"""Verify and restrict the managed Protect door rule for a single arm cycle.

The journal is written before the scope PATCH. An uncertain write is reconciled
by reading the rule, never by replaying an Arm webhook.
"""
import json
import time
import alarm_outputs
import onboarding as ob
from device_config import atomic_json, paths
from protection_state import fingerprint


def scope_path(root):
    return paths(root=root)[0] / '.arm-coverage.json'


def saved(root, site):
    try:
        data = json.loads(scope_path(root).read_text())
        if (data['configuration'] == fingerprint(site) and isinstance(data['activeIds'], list) and
                data['activeIds'] and set(data['activeIds']) <= {s['id'] for s in site['sensors']} and
                set(data['macs']) == {s['id'] for s in site['sensors']} and data['phase'] in ('prepared', 'sent')):
            for value in data['macs'].values():
                ob.mac({'mac': value})
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def identities(root, site, sensors):
    """Missing devices retain only their saved identity, never a healthy reading."""
    result = {}
    record = saved(root, site)
    if record:
        result.update(record['macs'])
    # The installer already saved stable IDs/MACs when it created the rule.
    try:
        journal = json.loads(ob.store_paths(root)[1].read_text())
        if journal.get('plan', {}).get('installationId') == site.get('installationId'):
            result.update({s['id']: ob.mac(s) for s in journal['plan']['sensors']})
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for sensor in sensors:
        if sensor.get('id') in {s['id'] for s in site['sensors']}:
            value = ob.mac(sensor)
            if sensor['id'] in result and result[sensor['id']] != value:
                raise ValueError('A sensor identity changed. Review the replacement in Settings.')
            result[sensor['id']] = value
    if not {s['id'] for s in site['sensors']} <= set(result):
        raise ValueError('A missing sensor has no saved identity. Bypass or replace it in Settings first.')
    return {s['id']: result[s['id']] for s in site['sensors']}


def prepare(session, site, root, active_ids, require_disarmed=True, mode='arm'):
    """Re-check every door sensor and confirm the keypad controls the current alarm.

    mode selects which arm profile is about to be armed ('arm', 'arm_stay', or
    'arm_away'); the membership check covers that profile, since each mode has
    its own Protect alarm profile.
    """
    if site.get('version') != 2 or session.host != site['consoleHost']:
        raise ValueError('Complete this console’s alarm setup before arming.')
    if not active_ids or not set(active_ids) <= {s['id'] for s in site['sensors']}:
        raise ValueError('At least one checked, connected, closed alarm door is required.')
    profile_field = {'arm': 'profileId', 'arm_stay': 'stayProfileId', 'arm_away': 'awayProfileId'}[mode]
    profile_id = site.get(profile_field) or site['profileId']
    data = ob.snapshot(session)
    if require_disarmed:
        ob.disarmed(data['profiles'])
    macs = identities(root, site, data['sensors'])
    profile_ids = ob.profile_ids_for(site)
    existing = ob.known_rules(site, data, profile_ids)
    choices = {'receivers': site['outputs'][0]['receivers']}
    full = [{**s, 'mac': macs[s['id']]} for s in site['sensors']]
    desired = ob.payloads(choices, full, profile_ids, site['webhooks'], 'Loxicom', existing, alarm_outputs.siren_output(site))
    record = saved(root, site)
    for kind in desired:
        if kind not in existing:
            raise ValueError('A saved alarm rule is missing. Review alarm setup in Settings.')
        expected = desired[kind]
        if kind == 'doors' and record:
            target = {'mode': 'include', 'data': {'scope_all_entry_sensors': sorted(macs[key] for key in record['activeIds'])}}
            if ob.equivalent(existing[kind], dict(expected, scope=target)):
                expected = dict(expected, scope=target)
        if not ob.equivalent(existing[kind], expected):
            raise ValueError('The saved alarm rules changed. Review alarm setup in Settings before arming.')
    profile = next((p for p in data['profiles'] if p.get('id') == profile_id), None)
    if not profile or profile.get('activation_delay') != 60 or not {site['ruleIds'][k] for k in ('doors', 'entry_alert')} <= set(profile.get('alarm_ids', [])):
        raise ValueError('The alarm profile no longer matches the saved one-minute delays.')
    active_macs = {macs[key] for key in active_ids}
    armed_ids = [pid for pid in profile_ids.values() if pid]
    if any(r['id'] not in set(site['ruleIds'].values()) and ob.overlaps(r, active_macs, armed_ids) for r in data['rules']):
        raise ValueError('Another rule uses these alarm doors. Review it in Protect before arming.')
    desired['doors']['scope'] = {'mode': 'include', 'data': {'scope_all_entry_sensors': sorted(active_macs)}}
    return {'baseline': ob.configuration_hash(data), 'payload': desired['doors'], 'current': existing['doors'], 'macs': macs}


def apply(session, site, root, active_ids, bypassed, plan, mode='arm'):
    record = {'configuration': fingerprint(site), 'activeIds': active_ids, 'bypassed': bypassed,
              'macs': plan['macs'], 'phase': 'prepared', 'createdAt': time.time(), 'mode': mode}
    atomic_json(scope_path(root), record)
    if not ob.equivalent(plan['current'], plan['payload']):
        ob.ensure_disarmed(session)
        ob.console(session, 'PATCH', ob.RULES + '/' + site['ruleIds']['doors'], plan['payload'])
    rules = ob.console(session, 'GET', ob.RULES)
    actual = next((r for r in rules if r.get('id') == site['ruleIds']['doors']), {})
    if not ob.equivalent(actual, plan['payload']):
        raise ValueError('Could not verify the bypass in Protect. No Arm request was sent.')
    ob.ensure_disarmed(session)
    return record


def mark_sent(root, record):
    atomic_json(scope_path(root), dict(record, phase='sent'))


def restore_scope(session, site, root):
    record = saved(root, site)
    if not record:
        return
    ids = [s['id'] for s in site['sensors']]
    plan = prepare(session, site, root, ids, mode=record.get('mode', 'arm'))
    # Keep the old journal until read-back proves that the full scope returned.
    if not ob.equivalent(plan['current'], plan['payload']):
        ob.ensure_disarmed(session)
        ob.console(session, 'PATCH', ob.RULES + '/' + site['ruleIds']['doors'], plan['payload'])
    rules = ob.console(session, 'GET', ob.RULES)
    actual = next((r for r in rules if r.get('id') == site['ruleIds']['doors']), {})
    if not ob.equivalent(actual, plan['payload']):
        raise ValueError('Could not restore door coverage after disarming. Review Sensors in Settings.')
    scope_path(root).unlink(missing_ok=True)
