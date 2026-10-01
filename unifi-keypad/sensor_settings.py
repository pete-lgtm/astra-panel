"""Update adopted sensors using the verified installer review/save workflow."""
import alarm_outputs
import onboarding as ob
import json
import door_policy
from protection_state import fingerprint
from device_config import load_site, paths


def saved_site(root):
    return load_site(paths(root=root)[2])


def discover(session, root):
    site = saved_site(root)
    # Finish the local preference save if a restart happened after Protect and
    # site.json were committed. The alarm exclusions were already saved there.
    try:
        journal = json.loads(ob.store_paths(root)[1].read_text())
        if (journal.get('phase') == 'complete' and 'doorModes' in journal.get('plan', {}) and
                fingerprint(journal['site']) == fingerprint(site)):
            door_policy.save_modes(root, site, journal['plan']['doorModes'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    saved = site.get('sensors', [])
    modes = door_policy.read(root, site)['modes']
    if not session.cookie:
        return {'connected': False, 'selectedIds': [s['id'] for s in saved],
                'sensors': [{**s, 'state': 'UNVERIFIED', 'available': False, 'isOpened': None} for s in saved],
                'message': 'Reconnect UniFi to find new sensors and check their status.'}
    data = ob.snapshot(session)
    ob.site_for(root, session.host)
    known = {s['id'] for s in data['sensors']}
    rows = [{**{k: s.get(k) for k in ('id', 'name', 'state', 'isOpened')}, 'available': ob.usable(s)} for s in data['sensors']]
    rows += [{**s, 'state': 'MISSING', 'available': False, 'isOpened': None} for s in saved if s['id'] not in known]
    pending = ob.context(session, root).get('pending')
    rows += [{**s, 'id': key, 'state': 'MISSING', 'available': False, 'isOpened': None} for key, s in modes.items()
             if key not in {r['id'] for r in rows}]
    for row in rows:
        row['mode'] = 'alarm' if row['id'] in {s['id'] for s in saved} else modes.get(row['id'], {}).get('mode', 'status')
    return {'connected': True, 'selectedIds': [s['id'] for s in saved], 'sensors': rows, 'pending': pending,
            'message': 'Alarm doors start the entry countdown. Status-only doors show open/closed without an alarm. Bypassed doors are excluded until you change them back.'}


def review(session, body, root, state):
    if set(body) not in ({'sensorIds'}, {'sensorIds', 'modes'}):
        raise ValueError('Choose the sensors to protect.')
    site = saved_site(root)
    if site.get('version') != 2:
        raise ValueError('Complete alarm setup before managing sensors.')
    data = ob.snapshot(session)
    modes = None
    if 'modes' in body:
        known = {s['id']: s for s in data['sensors']}
        known.update({s['id']: s for s in site.get('sensors', []) if s['id'] not in known})
        known.update({key: value for key, value in door_policy.read(root, site)['modes'].items() if key not in known})
        values = body['modes']
        if (not isinstance(values, dict) or not set(values) <= set(known) or
                any(value not in door_policy.MODES for value in values.values()) or
                {key for key, value in values.items() if value == 'alarm'} != set(body['sensorIds'])):
            raise ValueError('Choose Alarm, Status only, or Bypassed for each sensor.')
        modes = {key: {'name': known[key]['name'], 'mode': value} for key, value in values.items() if value != 'alarm'}
    profile = next((p for p in data['profiles'] if p.get('id') == site['profileId']), None)
    if profile is None:
        raise ValueError('The saved alarm profile is missing. Review alarm setup first.')
    choices = {'name': site['name'], 'profileId': site['profileId'], 'profileName': profile['title'],
               'sensorIds': body['sensorIds'], 'receivers': site['outputs'][0]['receivers']}
    plan = ob.review(session, choices, root, state)
    if modes is not None:
        state['plan']['doorModes'] = modes
        plan['statusOnly'] = [v['name'] for v in modes.values() if v['mode'] == 'status']
        plan['bypassed'] = [v['name'] for v in modes.values() if v['mode'] == 'bypass']
    old = {s['id']: s['name'] for s in site['sensors']}
    new = {s['id']: s['name'] for s in ob.selected(choices, data)}
    return {**plan, 'added': [name for key, name in new.items() if key not in old],
            'removed': [name for key, name in old.items() if key not in new]}


def apply(session, body, root, state):
    result = ob.apply(session, body, root, state)
    journal = json.loads(ob.store_paths(root)[1].read_text())
    if journal['plan']['id'] == body['planId'] and 'doorModes' in journal['plan']:
        door_policy.save_modes(root, saved_site(root), journal['plan']['doorModes'])
    return result


def verify_for_activation(session, root):
    site = saved_site(root)
    if site.get('version') != 2:
        raise ValueError('Complete alarm setup before enabling protection.')
    data = ob.snapshot(session)
    ob.disarmed(data['profiles'])
    ob.site_for(root, session.host)
    choices = {'name': site['name'], 'profileId': site['profileId'], 'profileName': site['profile'],
               'sensorIds': [s['id'] for s in site['sensors']], 'receivers': site['outputs'][0]['receivers']}
    sensors = ob.selected(choices, data)
    profile_ids = ob.profile_ids_for(site)
    existing = ob.known_rules(site, data, profile_ids)
    desired = ob.payloads(choices, sensors, profile_ids, site['webhooks'], 'Loxicom', existing, alarm_outputs.siren_output(site))
    if any(kind not in existing or not ob.equivalent(existing[kind], desired[kind]) for kind in desired):
        raise ValueError('The saved alarm rules need review before protection can be enabled.')
    profile = next((p for p in data['profiles'] if p.get('id') == site['profileId']), None)
    if not profile or profile.get('activation_delay') != 60 or not {site['ruleIds'][kind] for kind in ('doors', 'entry_alert')} <= set(profile.get('alarm_ids', [])):
        raise ValueError('The alarm profile no longer matches the saved setup.')
    own = set(site['ruleIds'].values())
    armed_ids = [pid for pid in profile_ids.values() if pid]
    if any(r['id'] not in own and ob.overlaps(r, {ob.mac(s) for s in sensors}, armed_ids) for r in data['rules']):
        raise ValueError('Another rule also uses these sensors. Review it in Protect before enabling the delay.')
    return site
