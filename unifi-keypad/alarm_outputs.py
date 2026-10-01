"""Discover adopted Protect sirens and configure the delayed alarm output.

Only the console's advertised siren action is supported. Discovery and setup
never play a sound. New devices require explicit selection.
"""
import copy
import re

ACTION = 'protect:sound.siren'
SIRENS = '/proxy/protect/api/sirens'
SOURCES = '/proxy/protect/api/automationManager/external/data/actions/sirens'
DURATIONS = (5, 10, 20, 30)


def siren_output(site):
    return next((o for o in site.get('outputs', []) if o.get('type') == 'siren'), None)


def validate_output(output):
    if (not isinstance(output, dict) or set(output) != {'type', 'devices', 'durationSeconds'} or
            output['type'] != 'siren' or type(output['durationSeconds']) is not int or
            output['durationSeconds'] not in DURATIONS):
        raise ValueError('Choose a supported siren duration.')
    devices = output['devices']
    if (not isinstance(devices, list) or not 1 <= len(devices) <= 32 or
            any(not isinstance(d, dict) or set(d) != {'id', 'name', 'mac'} or
                not isinstance(d['id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', d['id']) or
                not isinstance(d['name'], str) or not 1 <= len(d['name']) <= 100 or
                not isinstance(d['mac'], str) or not re.fullmatch('[0-9A-F]{12}', d['mac']) for d in devices) or
            len({d['id'] for d in devices}) != len(devices) or len({d['mac'] for d in devices}) != len(devices)):
        raise ValueError('Choose unique adopted sirens with valid device identities.')
    return output


def saved_choices(site):
    output = siren_output(site)
    return {'sirenIds': [d['id'] for d in output['devices']] if output else [],
            'sirenDuration': output['durationSeconds'] if output else 30}


def inventory(session, request, manifest):
    actions = [a for c in manifest.get('action_categories', []) for a in c.get('actions', []) if a.get('id') == ACTION]
    if not actions:
        return {'sirens': [], 'sirenDurations': []}
    schema = actions[0].get('options_schema', {})
    props = schema.get('properties', {})
    duration = props.get('duration', {})
    if (len(actions) != 1 or props.get('sources', {}).get('ui:dataurl') != SOURCES or
            duration.get('type') != 'string' or set(schema.get('required', [])) != {'sources', 'duration'}):
        raise ValueError('This Protect siren action needs a compatibility update. No alarm settings changed.')
    durations = [n for n in DURATIONS if str(n) in {x.get('value') for x in duration.get('ui:data', [])}]
    devices, sources = request(session, 'GET', SIRENS), request(session, 'GET', SOURCES)
    if not durations or any(not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows) for rows in (devices, sources)):
        raise ValueError('Protect returned an unsupported siren list.')
    rows = []
    for device in devices:
        if device.get('modelKey') != 'siren' or device.get('isAdopted') is False:
            continue
        row = {k: device.get(k) for k in ('id', 'name', 'state', 'type')}
        row['name'] = row['name'] or 'Siren'
        row['mac'] = str(device.get('mac', '')).replace(':', '').upper()
        try:
            validate_output({'type': 'siren', 'devices': [{k: row[k] for k in ('id', 'name', 'mac')}], 'durationSeconds': 30})
        except ValueError:
            continue
        matched = [s for s in sources if s.get('value') == row['mac'] and (s.get('metadata') or {}).get('id') == row['id']]
        row['available'] = len(matched) == 1 and row['state'] == 'CONNECTED'
        rows.append(row)
    if len({d['id'] for d in rows}) != len(rows) or len({d['mac'] for d in rows}) != len(rows):
        raise ValueError('Protect returned duplicate siren identities.')
    return {'sirens': rows, 'sirenDurations': durations}


def selected(choices, data, site):
    ids, duration = choices.get('sirenIds', []), choices.get('sirenDuration', 30)
    if (not isinstance(ids, list) or len(ids) > 32 or any(not isinstance(x, str) for x in ids) or
            len(set(ids)) != len(ids) or type(duration) is not int or duration not in DURATIONS):
        raise ValueError('Choose sirens and a supported duration.')
    if not ids:
        return None
    if duration not in data.get('sirenDurations', []):
        raise ValueError('Protect no longer advertises the selected siren duration.')
    saved = {d['id']: d for d in (siren_output(site) or {}).get('devices', [])}
    known = {d['id']: d for d in data.get('sirens', [])}
    devices = []
    for identifier in ids:
        row = known.get(identifier)
        previous = saved.get(identifier)
        if row is None:
            row = previous
        if row is None or not row.get('available') and previous is None:
            raise ValueError('New sirens must be adopted, connected, and available in Protect Alarm Manager.')
        if previous and row['mac'] != previous['mac']:
            raise ValueError('A saved siren identity changed. Remove it and review its replacement.')
        devices.append({k: row[k] for k in ('id', 'name', 'mac')})
    return validate_output({'type': 'siren', 'devices': sorted(devices, key=lambda d: d['id']), 'durationSeconds': duration})


def action(output):
    validate_output(output)
    return {'id': ACTION, 'data': {'sources': sorted(d['mac'] for d in output['devices']),
                                  'duration': str(output['durationSeconds'])}}


def visible_rows(data, site):
    rows = list(data['sirens'])
    known = {r['id'] for r in rows}
    return rows + [{**d, 'state': 'MISSING', 'available': False} for d in (siren_output(site) or {}).get('devices', []) if d['id'] not in known]


def normalized_action(value):
    value = copy.deepcopy(value)
    if value.get('id') == ACTION:
        sources = (value.get('data') or {}).get('sources')
        if isinstance(sources, list) and all(isinstance(x, str) for x in sources):
            value['data']['sources'] = sorted(sources)
    return value


def discover(session, root):
    import onboarding as ob
    site = ob.load_site(ob.paths(root=root)[2])
    saved = saved_choices(site)
    if not session.cookie:
        return {'connected': False, **saved, 'sirens': [], 'sirenDurations': [],
                'message': 'Reconnect UniFi to find sirens.'}
    data = ob.snapshot(session)
    ob.site_for(root, session.host)
    rows = visible_rows(data, site)
    return {'connected': True, **saved, 'sirens': rows, 'sirenDurations': data['sirenDurations'],
            'pending': ob.context(session, root, data).get('pending'),
            'message': 'Sounds after the one-minute entry countdown. Adopt new sirens in Protect, then scan here.' if data['sirenDurations'] else 'This Protect version does not offer a compatible siren action.'}


def review(session, body, root, state):
    import onboarding as ob
    if set(body) != {'sirenIds', 'sirenDuration'}:
        raise ValueError('Choose the sirens and duration before reviewing.')
    _, site = ob.site_for(root, session.host)
    if site.get('version') != 2:
        raise ValueError('Complete alarm setup before adding sirens.')
    choices = {'name': site['name'], 'profileId': site['profileId'], 'profileName': site['profile'],
               'sensorIds': [s['id'] for s in site['sensors']], 'receivers': site['outputs'][0]['receivers'], **body}
    return ob.review(session, choices, root, state)
