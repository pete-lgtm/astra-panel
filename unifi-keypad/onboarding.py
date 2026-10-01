"""Saved installer workflow. No arming, webhook firing, or hardware playback here."""
import alarm_outputs
import copy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
import time
from uuid import uuid4

from pin_store import create_pin, read_pin
from keypad_store import KeypadStore
from device_config import atomic_json, load_site, paths, save_site, validate_site, identifier
from protect_setup_api import RULES, PROFILES, MANIFEST, SENSORS, entries, csrf_from_cookie

USERS = RULES + '/data/users'
KINDS = ('arm', 'disarm', 'entry_alert', 'doors', 'arm_stay', 'arm_away', 'panic')
ARM_PROFILE_KINDS = ('arm', 'arm_stay', 'arm_away')
READS = {RULES, PROFILES, MANIFEST, SENSORS, USERS, alarm_outputs.SIRENS, alarm_outputs.SOURCES}


class ReviewRequired(ValueError):
    """No provisioning write started; the installer can safely review again."""


def profile_ids_for(site):
    """Map each arm mode to its Protect alarm profile id (None when not provisioned yet)."""
    return {'arm': site.get('profileId'), 'arm_stay': site.get('stayProfileId'), 'arm_away': site.get('awayProfileId')}


def resolve_profile(profiles, saved_id, titles):
    """Reuse the saved Stay/Away profile id, adopt one with a known title, or report it missing.

    titles is tried in order: the previously saved title first, then the title
    derived from the current main profile name.
    """
    if saved_id:
        found = next((p for p in profiles if p.get('id') == saved_id), None)
        if found:
            return found['id'], {k: found.get(k) for k in ('title', 'activation_delay', 'alarm_ids')}
    for title in titles:
        found = next((p for p in profiles if p.get('title') == title), None)
        if found:
            return found['id'], {k: found.get(k) for k in ('title', 'activation_delay', 'alarm_ids')}
    return '', None


def console(session, method, path, payload=None):
    allowed = (method == 'GET' and path in READS or method == 'POST' and path in (RULES, PROFILES) or
               method == 'PATCH' and re.fullmatch('(?:' + re.escape(RULES) + '|' + re.escape(PROFILES) + r')/[0-9a-f-]{36}(?:/notifications/preferences)?', path))
    if not allowed or not session.cookie:
        raise ValueError('Sign in to UniFi to continue setup with a supported request.')
    headers = {'X-CSRF-Token': csrf_from_cookie(session.cookie)} if method != 'GET' else None
    result, cookie = session.request(session.host, method, path, payload,
                                    cookie=session.cookie, extra_headers=headers)
    if cookie:
        session.cookie = cookie
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def rule_config(rule):
    result = {k: copy.deepcopy(v) for k, v in rule.items() if k not in ('stats', 'author', 'editor', 'created_at', 'updated_at')}
    restriction = result.get('restriction')
    if isinstance(restriction, dict) and 'profile_ids' in restriction:
        restriction['profile_ids'] = ordered_ids(restriction['profile_ids'])
    return result


def ordered_ids(value):
    # Protect varies membership ordering between otherwise identical reads.
    # Keep duplicates and unexpected values so real changes still invalidate review.
    return sorted(value) if isinstance(value, list) and all(isinstance(x, str) for x in value) else value


def snapshot(session):
    data = {key: console(session, 'GET', path) for key, path in
            (('rules', RULES), ('profiles', PROFILES), ('sensors', SENSORS), ('users', USERS))}
    if any(not isinstance(value, list) or any(not isinstance(row, dict) for row in value) for value in data.values()):
        raise ValueError('The console returned an unsupported setup format.')
    manifest = console(session, 'GET', MANIFEST)
    triggers = {t.get('id') for c in manifest.get('trigger_categories', []) for t in c.get('triggers', [])}
    actions = {a.get('id') for c in manifest.get('action_categories', []) for a in c.get('actions', [])}
    if not {'protect:webhook.webhook', 'protect:sensors.openStatusChanged'} <= triggers or not {
            'protect:notify', 'protect:alarm_profile.arm', 'protect:alarm_profile.disarm'} <= actions:
        raise ValueError('This Protect version does not advertise the alarm actions required by setup.')
    data.update(alarm_outputs.inventory(session, console, manifest))
    return data


def disarmed(profiles):
    if any(p.get('state') != 'disarmed' for p in profiles):
        raise ValueError('Disarm in the keypad before saving alarm settings.')


def mac(sensor):
    value = str(sensor.get('mac', '')).replace(':', '').upper()
    if not re.fullmatch('[0-9A-F]{12}', value):
        raise ValueError('A selected sensor has no usable Protect identifier.')
    return value


def usable(sensor):
    return sensor.get('state') == 'CONNECTED' and type(sensor.get('isOpened')) is bool


def site_for(root, host):
    _, target, source = paths(root=root)
    site = load_site(source)
    if site.get('consoleHost') and site['consoleHost'] != host:
        raise ValueError('This keypad is configured for another console. Its saved setup was not replaced.')
    return target, site


def pin_path(root):
    state_dir, _, _ = paths(root=root)
    return state_dir / '.keypad-pin.json'


def pin_status(root):
    return {'pinSet': KeypadStore(pin_path(root)).has_users()}


def require_pin(root):
    if not pin_status(root)['pinSet']:
        raise ValueError('Create your keypad PIN before reviewing or saving setup.')


def store_paths(root):
    state_dir, _, _ = paths(root=root)
    return state_dir / '.installer-draft.json', state_dir / '.installer-apply.json'


def selected(body, data):
    ids = body.get('sensorIds')
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 32 or any(not isinstance(x, str) for x in ids) or
            len(set(ids)) != len(ids)):
        raise ValueError('Select at least one door, without duplicate sensors.')
    indexed = {s.get('id'): s for s in data['sensors']}
    sensors = [indexed.get(value) for value in ids]
    if any(s is None or not usable(s) for s in sensors):
        raise ValueError('Selected sensors must be connected and report an open or closed position.')
    if any(s['isOpened'] for s in sensors):
        raise ValueError('Close the selected doors before saving their setup.')
    if len({s.get('name') for s in sensors}) != len(sensors):
        raise ValueError('Give selected doors distinct names in Protect before setup.')
    for sensor in sensors:
        mac(sensor)
    return sensors


def clean_choices(body, data):
    if set(body) not in ({'name', 'sensorIds', 'profileId', 'profileName', 'receivers'},
                         {'name', 'sensorIds', 'profileId', 'profileName', 'receivers', 'sirenIds', 'sirenDuration'}):
        raise ValueError('Setup choices are incomplete. Return to alarm settings.')
    name, profile_name = body['name'], body['profileName']
    if any(not isinstance(x, str) or not 1 <= len(x.strip()) <= 80 or any(ord(c) < 32 for c in x)
           for x in (name, profile_name)):
        raise ValueError('Enter a home name and alarm profile name.')
    selected(body, data)
    profile_id = body['profileId']
    if profile_id and profile_id not in {p.get('id') for p in data['profiles']}:
        raise ValueError('The chosen alarm profile is no longer available.')
    receivers = body['receivers']
    available = {u.get('value') for u in data['users']}
    if (not isinstance(receivers, list) or not receivers or any(not isinstance(x, str) for x in receivers) or
            len(set(receivers)) != len(receivers) or not set(receivers) <= available):
        raise ValueError('Choose at least one available phone notification recipient.')
    for value in receivers:
        identifier(value)
    return {**body, 'name': name.strip(), 'profileName': profile_name.strip()}


def default_choices(site, data):
    profile = next((p for p in data['profiles'] if p.get('id') == site.get('profileId') or
                    not site.get('profileId') and p.get('title') == site.get('profile')), None)
    ids = {s['id'] for s in site.get('sensors', [])}
    sensors = [s['id'] for s in data['sensors'] if usable(s) and
               (s['id'] in ids if ids else s.get('name') in site['doors'])]
    available = {u.get('value') for u in data['users']}
    recipients = site.get('outputs', [{}])[0].get('receivers', [])
    return {'name': site['name'], 'sensorIds': sensors,
            'profileId': profile['id'] if profile else '', 'profileName': profile['title'] if profile else 'Home alarm',
            'receivers': [r for r in recipients if r in available] or sorted(available), **alarm_outputs.saved_choices(site)}


def configuration_hash(data):
    return digest({'rules': {r['id']: rule_config(r) for r in data['rules']},
                   'profiles': {p['id']: {'title': p.get('title'), 'activation_delay': p.get('activation_delay'),
                                         'alarm_ids': ordered_ids(p.get('alarm_ids'))} for p in data['profiles']}})


def known_rules(site, data, profile_ids):
    """Reuse saved IDs or legacy webhook identities, never a title-only match.

    profile_ids maps each arm mode ('arm', 'arm_stay', 'arm_away') to its
    Protect alarm profile id. Stay/Away entries may be None on installations
    saved before those modes were provisioned; their rules are then optional.
    """
    main_id = profile_ids['arm']
    result = {}
    indexed = {r['id']: r for r in data['rules']}
    for kind in KINDS:
        rule_id = site.get('ruleIds', {}).get(kind)
        if rule_id:
            if rule_id not in indexed:
                raise ValueError('A saved keypad rule is missing. Restore or review it in Protect before continuing.')
            result[kind] = indexed[rule_id]
        elif kind != 'doors' and site.get('webhooks', {}).get(kind):
            candidates = [r for r in data['rules'] if entries(r, 'trigger') == [
                {'id': 'protect:webhook.webhook', 'data': {'webhook': site['webhooks'][kind]}}]]
            if len(candidates) > 1:
                raise ValueError('Duplicate keypad webhook rules need review in Protect.')
            if candidates:
                result[kind] = candidates[0]
    if result and any(kind not in result for kind in ('arm', 'disarm', 'entry_alert')):
        raise ValueError('Only part of the existing keypad setup was found. Existing rules were preserved.')
    if result:
        for kind in ARM_PROFILE_KINDS:
            if kind not in result:
                continue
            arm = entries(result[kind], 'action')
            if (len(arm) != 1 or arm[0].get('id') != 'protect:alarm_profile.arm' or
                    (arm[0].get('data') or {}).get('profile') != profile_ids[kind]):
                raise ValueError('Keep the configured keypad profile selected when updating this installation.')
        expected = {'disarm': 'protect:alarm_profile.disarm', 'entry_alert': 'protect:notify',
                    'panic': 'protect:notify'}
        for kind, action in expected.items():
            if kind not in result:
                continue  # panic is optional on installations saved before it existed
            actual = entries(result[kind], 'action')
            sound = alarm_outputs.siren_output(site) if kind == 'entry_alert' else None
            expected_ids = [action] + ([alarm_outputs.ACTION] if sound else [])
            if (sorted(a['id'] for a in actual) != sorted(expected_ids) or
                    sound and alarm_outputs.normalized_action(next(a for a in actual if a['id'] == alarm_outputs.ACTION)) != alarm_outputs.action(sound)):
                raise ValueError('An existing keypad rule has additional or changed actions. Review it in Protect first.')
        if 'doors' not in result:
            legacy_sensors = [s for s in data['sensors'] if s.get('name') in site['doors']]
            scope = {mac(s) for s in legacy_sensors}
            candidates = [r for r in data['rules'] if
                          [t['id'] for t in entries(r, 'trigger')] == ['protect:sensors.openStatusChanged'] and
                          r.get('restriction', {}).get('profile_ids') == [main_id] and
                          r.get('scope', {}).get('mode') == 'include' and
                          set(r.get('scope', {}).get('data', {}).get('scope_all_entry_sensors', [])) == scope and
                          [a['id'] for a in entries(r, 'action')] == ['protect:notify']]
            if len(candidates) == 1:
                result['doors'] = candidates[0]
        if 'doors' in result and [a['id'] for a in entries(result['doors'], 'action')] != ['protect:notify']:
            raise ValueError('The existing door rule includes other actions. Review it in Protect first.')
    return result


def overlaps(rule, sensor_macs, profile_id):
    """Whether a foreign rule would also fire for these doors when armed.

    profile_id is the main alarm profile id, or a collection of the ids for
    every arm mode the keypad manages.
    """
    if not any(t['id'] == 'protect:sensors.openStatusChanged' for t in entries(rule, 'trigger')):
        return False
    wanted = {profile_id} if isinstance(profile_id, str) else {p for p in profile_id if p}
    restriction = rule.get('restriction') or {}
    if restriction.get('type') == 'when_armed' and not wanted & set(restriction.get('profile_ids', [])):
        return False
    scope = rule.get('scope') or {}
    values = set((scope.get('data') or {}).get('scope_all_entry_sensors', []))
    return bool(sensor_macs & values) if scope.get('mode') == 'include' and values else True


def payloads(choices, sensors, profile_ids, hooks, title_prefix, existing, siren=None):
    """Build the desired Protect rules for every arm mode the site has provisioned.

    profile_ids maps 'arm' to the main profile id and optionally 'arm_stay' /
    'arm_away' to theirs. Modes without a profile id are skipped, so installers
    saved before Stay/Away existed keep their exact current rules.
    """
    arm_kinds = tuple(kind for kind in ARM_PROFILE_KINDS if profile_ids.get(kind))
    if 'arm' not in arm_kinds:
        raise ValueError('The main alarm profile is missing.')
    armed_profiles = [profile_ids[kind] for kind in arm_kinds]
    notify = lambda channels: {'id': 'protect:notify', 'data': {
        'receivers': choices['receivers'], 'default_channels': channels, 'is_critical': True}}
    restriction = {'type': 'when_armed', 'profile_ids': armed_profiles}
    actions = {kind: {'id': 'protect:alarm_profile.arm', 'data': {'profile': profile_ids[kind]}}
               for kind in arm_kinds}
    actions.update({'disarm': {'id': 'protect:alarm_profile.disarm', 'data': {}},
                    'entry_alert': notify(['push']), 'doors': notify([]),
                    'panic': notify(['push'])})
    labels = {'arm': 'Arm', 'arm_stay': 'Arm stay', 'arm_away': 'Arm away',
              'disarm': 'Disarm', 'entry_alert': 'Entry delay expired', 'doors': 'Door events',
              'panic': 'Panic alert'}
    result = {}
    for kind in KINDS:
        if kind not in actions:
            continue
        item = {'title': existing.get(kind, {}).get('title') or title_prefix + ' \u2014 ' + labels[kind],
                'triggers_data': [[{'id': 'protect:sensors.openStatusChanged', 'data': {}}]] if kind == 'doors' else
                                 [[{'id': 'protect:webhook.webhook', 'data': {'webhook': hooks[kind]}}]],
                'actions_data': [[actions[kind]]]}
        if kind == 'entry_alert' and siren:
            item['actions_data'][0].append(alarm_outputs.action(siren))
        if kind in ('doors', 'entry_alert'):
            item['restriction'] = restriction
        if kind in ('doors', 'entry_alert', 'panic'):
            # Panic is notification-only: no profile restriction, so it
            # fires armed or disarmed, and it is never added to an alarm
            # profile's membership.
            item['notification_preferences'] = {'push': kind != 'doors', 'email': False}
        if kind == 'doors':
            item['scope'] = {'mode': 'include', 'data': {'scope_all_entry_sensors': sorted(mac(s) for s in sensors)}}
        result[kind] = item
    return result


def equivalent(rule, payload, preferences=True):
    """Check requested writable values, normalizing empty API data and set order."""
    for key, kind in (('triggers_data', 'trigger'), ('actions_data', 'action')):
        actual = entries(rule, kind)
        wanted = [item for group in payload[key] for item in group]
        norm = lambda rows: [{'id': r['id'], 'data': r.get('data') or {}} for r in rows]
        normalized_actual, normalized_wanted = norm(actual), norm(wanted)
        if kind == 'action':
            normalized_actual = sorted((alarm_outputs.normalized_action(a) for a in normalized_actual), key=lambda a: a['id'])
            normalized_wanted = sorted((alarm_outputs.normalized_action(a) for a in normalized_wanted), key=lambda a: a['id'])
        if normalized_actual != normalized_wanted:
            return False
    for key in ('title', 'restriction', 'scope', 'notification_preferences'):
        if key in payload and (preferences or key != 'notification_preferences'):
            actual, wanted = rule.get(key), payload[key]
            if key == 'notification_preferences':
                if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in wanted.items()):
                    return False
            elif key == 'scope':
                if not isinstance(actual, dict) or actual.get('mode') != wanted['mode'] or set((actual.get('data') or {}).get('scope_all_entry_sensors', [])) != set(wanted['data']['scope_all_entry_sensors']):
                    return False
            elif key == 'restriction':
                # The console may reorder profile_ids when it stores the rule;
                # the set of profiles is what matters, not their order.
                if (not isinstance(actual, dict) or actual.get('type') != wanted.get('type')
                        or set(actual.get('profile_ids') or []) != set(wanted.get('profile_ids') or [])):
                    return False
            elif actual != wanted:
                return False
    return True


def read_with_retry(session, predicate, tries=4, delay=2):
    """Return the single rule matching predicate, retrying the read briefly.

    The console can serve a stale rules list immediately after a write. This
    only re-reads; it never re-sends a create or update.
    """
    for attempt in range(tries):
        matches = [r for r in console(session, 'GET', RULES) if predicate(r)]
        if len(matches) == 1:
            return matches[0]
        if attempt < tries - 1:
            time.sleep(delay)
    return None


def public_plan(plan):
    return {'planId': plan['id'], 'name': plan['choices']['name'], 'profile': plan['profileTitle'],
            'stayProfile': plan['stayProfileTitle'], 'awayProfile': plan['awayProfileTitle'],
            'doors': [s['name'] for s in plan['sensors']], 'recipients': plan['recipientNames'],
            'statusOnly': [s['name'] for s in plan.get('doorModes', {}).values() if s['mode'] == 'status'],
            'bypassed': [s['name'] for s in plan.get('doorModes', {}).values() if s['mode'] == 'bypass'],
            'sirens': [d['name'] for d in (plan.get('sirenOutput') or {}).get('devices', [])],
            'sirenDuration': (plan.get('sirenOutput') or {}).get('durationSeconds'),
            'exitSeconds': 60, 'entrySeconds': 60, 'actions': plan['actions'],
            'message': 'Review your doors and phone alerts, then save setup.'}


def review(session, body, root, state):
    require_pin(root)
    draft_path, journal_path = store_paths(root)
    if journal_path.exists():
        previous = json.loads(journal_path.read_text())
        if previous.get('phase') != 'complete':
            raise ValueError('A saved setup is unfinished. Resume it before starting another.')
    data = snapshot(session)
    disarmed(data['profiles'])
    target, site = site_for(root, session.host)
    choices = clean_choices(body, data)
    if 'sirenIds' not in choices:
        choices.update(alarm_outputs.saved_choices(site))
    sound = alarm_outputs.selected(choices, data, site)
    sensors = selected(choices, data)
    profile_id = choices['profileId']
    profile = next((p for p in data['profiles'] if p['id'] == profile_id), None)
    installation_id = site.get('installationId') or str(uuid4())
    profile_title = profile['title'] if profile else choices['profileName'] + ' · Loxicom ' + installation_id[:8]
    if not profile and any(p.get('title') == profile_title for p in data['profiles']):
        raise ValueError('An alarm profile already has this name. Select it or choose a different name.')
    # Stay/Away arm modes get their own alarm profiles so Protect tracks each
    # mode separately. They cover the same assigned doors; interior sensors can
    # narrow Stay later without touching this flow.
    stay_id, stay_before = resolve_profile(data['profiles'], site.get('stayProfileId'),
                                           [t for t in (site.get('stayProfile'), profile_title + ' · Stay') if t])
    away_id, away_before = resolve_profile(data['profiles'], site.get('awayProfileId'),
                                           [t for t in (site.get('awayProfile'), profile_title + ' · Away') if t])
    stay_title = (stay_before or {}).get('title') or profile_title + ' · Stay'
    away_title = (away_before or {}).get('title') or profile_title + ' · Away'
    profile_ids = {'arm': profile_id, 'arm_stay': stay_id, 'arm_away': away_id}
    existing = known_rules(site, data, profile_ids)
    known_ids = {r['id'] for r in existing.values()}
    armed_ids = [pid for pid in profile_ids.values() if pid]
    conflicts = [r.get('title', 'Door rule') for r in data['rules'] if r['id'] not in known_ids and overlaps(r, {mac(s) for s in sensors}, armed_ids)]
    if conflicts:
        raise ValueError('Other rules also use these doors: ' + ', '.join(conflicts) + '. Review them in Protect before enabling delayed alerts; they were not changed.')
    # Keep previously issued webhook ids; new arm modes get fresh ones.
    hooks = {kind: str(uuid4()) for kind in KINDS if kind != 'doors'}
    if existing:
        for kind, hook in site.get('webhooks', {}).items():
            if kind in hooks:
                hooks[kind] = hook
    plan = {'id': secrets.token_urlsafe(24), 'host': session.host, 'expires': time.time() + 900,
            'baseline': configuration_hash(data), 'siteBaseline': digest(site),
            'choices': choices, 'sirenOutput': sound, 'sensors': [{k: s[k] for k in ('id', 'name', 'mac')} for s in sensors],
            'profileId': profile_id, 'profileTitle': profile_title,
            'profileBefore': {k: profile.get(k) for k in ('title', 'activation_delay', 'alarm_ids')} if profile else None,
            'installationId': installation_id, 'hooks': hooks,
            'existingIds': {kind: rule['id'] for kind, rule in existing.items()},
            'beforeHashes': {kind: digest(rule_config(rule)) for kind, rule in existing.items()},
            'foreignHash': digest({r['id']: rule_config(r) for r in data['rules'] if r['id'] not in known_ids}),
            'recipientNames': [u.get('label', 'UniFi user') for u in data['users'] if u.get('value') in choices['receivers']],
            'stayProfileId': stay_id, 'stayProfileTitle': stay_title, 'stayProfileBefore': stay_before,
            'awayProfileId': away_id, 'awayProfileTitle': away_title, 'awayProfileBefore': away_before,
            'actions': [('Reuse' if profile else 'Create') + ' alarm profile · one-minute exit delay',
                        ('Reuse' if stay_id else 'Create') + ' Stay alarm profile · one-minute exit delay',
                        ('Reuse' if away_id else 'Create') + ' Away alarm profile · one-minute exit delay'] +
                       [('Update' if kind in existing else 'Create') + ' · ' + label for kind, label in
                        (('arm', 'Arm control'), ('arm_stay', 'Arm stay control'), ('arm_away', 'Arm away control'),
                         ('disarm', 'Disarm control'), ('doors', 'Selected door events'), ('entry_alert', 'Phone alert after one minute'),
                         ('panic', 'Panic alert'))],
            'titles': {kind: rule['title'] for kind, rule in existing.items()}}
    if sound:
        plan['actions'].append('Siren after entry countdown · ' + str(sound['durationSeconds']) + ' seconds · ' + ', '.join(d['name'] for d in sound['devices']))
    elif alarm_outputs.siren_output(site):
        plan['actions'].append('Remove siren from delayed alarm')
    atomic_json(draft_path, {'host': session.host, 'choices': choices})
    state['plan'] = plan
    return public_plan(plan)


def ensure_disarmed(session):
    disarmed(console(session, 'GET', PROFILES))


def ensure_profile(session, journal, journal_path, key, plan):
    """Create or reuse the alarm profile for one arm mode, journaling the outcome.

    key is 'main', 'stay' or 'away'. The plan carries the mode's profile id
    ('' when the profile must be created), title, and the profile snapshot
    from review time (or None). Returns the profile id.
    """
    names = {'main': ('profileId', 'profileTitle', 'profileBefore'),
             'stay': ('stayProfileId', 'stayProfileTitle', 'stayProfileBefore'),
             'away': ('awayProfileId', 'awayProfileTitle', 'awayProfileBefore')}[key]
    id_key, title_key, _ = names
    sent_key = 'profile_' + key
    if id_key not in journal:
        journal[id_key] = plan.get(id_key) or ''
    profile_id = journal[id_key]
    if profile_id:
        return profile_id
    data = console(session, 'GET', PROFILES)
    if sent_key in journal['sent']:
        matches = [p for p in data if p.get('title') == plan[title_key]]
        if len(matches) != 1 or matches[0].get('activation_delay') != 60 or matches[0].get('alarm_ids'):
            raise ValueError('The new profile outcome is uncertain. Review Protect before attempting another setup.')
        profile_id = matches[0]['id']
    else:
        if any(p.get('title') == plan[title_key] for p in data):
            raise ValueError('An alarm profile with this name appeared during setup.')
        journal['sent'].append(sent_key)
        atomic_json(journal_path, journal)
        ensure_disarmed(session)
        try:
            console(session, 'POST', PROFILES, {'title': plan[title_key], 'activation_delay': 60, 'alarm_ids': []})
        except ValueError:
            pass  # The following read decides success; never retry POST.
        matches = [p for p in console(session, 'GET', PROFILES) if p.get('title') == plan[title_key]]
        if len(matches) != 1 or matches[0].get('activation_delay') != 60 or matches[0].get('state') != 'disarmed':
            raise ValueError('Profile creation could not be verified. Resume after checking the console.')
        profile_id = matches[0]['id']
    journal[id_key] = profile_id
    atomic_json(journal_path, journal)
    return profile_id


def apply(session, body, root, state):
    require_pin(root)
    if set(body) != {'planId'} or not isinstance(body['planId'], str):
        raise ValueError('Review setup before saving.')
    _, journal_path = store_paths(root)
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
    if journal and journal.get('plan', {}).get('id') == body['planId']:
        if journal['plan']['host'] != session.host:
            raise ValueError('This setup belongs to another console.')
        if journal.get('phase') == 'complete':
            return completion(journal['site'])
        plan = journal['plan']
    else:
        if journal and journal.get('phase') != 'complete':
            raise ValueError('Resume the unfinished setup first.')
        plan = state.get('plan')
        if not plan or plan['id'] != body['planId'] or plan['host'] != session.host or plan['expires'] < time.time():
            raise ReviewRequired('The setup review expired. Select Next · Review to check the current settings again.')
        data = snapshot(session)
        disarmed(data['profiles'])
        _, site = site_for(root, session.host)
        clean_choices(plan['choices'], data)
        try:
            sound = alarm_outputs.selected(plan['choices'], data, site)
        except ValueError as error:
            raise ReviewRequired(str(error)) from None
        if sound != plan.get('sirenOutput'):
            raise ReviewRequired('Siren identity changed since review. Review the sirens again.')
        if configuration_hash(data) != plan['baseline'] or digest(site) != plan['siteBaseline']:
            raise ReviewRequired('Alarm settings changed since review. Select Next · Review to check them again before saving.')
        journal = {'phase': 'applying', 'plan': copy.deepcopy(plan), 'ruleIds': {}, 'sent': [], 'profileId': plan['profileId'],
                   'mainProfileId': plan.get('profileId') or '', 'stayProfileId': plan.get('stayProfileId') or '',
                   'awayProfileId': plan.get('awayProfileId') or ''}
        atomic_json(journal_path, journal)
    # Re-read identity, health and foreign-rule fingerprints on every resume.
    data = snapshot(session)
    disarmed(data['profiles'])
    _, current_site = site_for(root, session.host)
    if alarm_outputs.selected(plan['choices'], data, current_site) != plan.get('sirenOutput'):
        raise ValueError('Siren identity or availability changed since review. Review the selected sirens again.')
    current_sensors = selected(plan['choices'], data)
    if [{k: s[k] for k in ('id', 'name', 'mac')} for s in current_sensors] != plan['sensors']:
        raise ValueError('Sensor identity changed during setup. An installer must review the unfinished setup.')
    own_ids = set(plan['existingIds'].values()) | set(journal['ruleIds'].values())
    desired_titles = {kind: plan['titles'].get(kind) or 'Loxicom ' + plan['installationId'][:8] + ' — ' + label
                      for kind, label in (('arm', 'Arm'), ('arm_stay', 'Arm stay'), ('arm_away', 'Arm away'),
                                          ('disarm', 'Disarm'), ('entry_alert', 'Entry delay expired'), ('doors', 'Door events'),
                                          ('panic', 'Panic alert'))}
    # A POST may have succeeded before its response was lost. Reconcile only the
    # exact desired object; never issue that create a second time automatically.
    for kind in KINDS:
        if kind in journal['sent'] and kind not in journal['ruleIds'] and kind not in plan['existingIds']:
            matches = [r for r in data['rules'] if r.get('title') == desired_titles[kind]]
            if len(matches) == 1:
                journal['ruleIds'][kind] = matches[0]['id']
                own_ids.add(matches[0]['id'])
    if digest({r['id']: rule_config(r) for r in data['rules'] if r['id'] not in own_ids}) != plan['foreignHash']:
        raise ValueError('Other alarm rules changed during setup. No further edits were made; installer review is required.')
    profile_ids = {}
    for mode, key in (('arm', 'main'), ('arm_stay', 'stay'), ('arm_away', 'away')):
        profile_ids[mode] = ensure_profile(session, journal, journal_path, key, plan)
    desired_kinds = tuple(kind for kind in KINDS if kind in ARM_PROFILE_KINDS and profile_ids[kind]
                          or kind not in ARM_PROFILE_KINDS)
    desired = payloads(plan['choices'], plan['sensors'], profile_ids, plan['hooks'],
                       'Loxicom ' + plan['installationId'][:8], {k: {'title': v} for k, v in plan['titles'].items()}, plan.get('sirenOutput'))
    for kind in desired_kinds:
        ensure_disarmed(session)
        rule_id = journal['ruleIds'].get(kind) or plan['existingIds'].get(kind)
        # A stale console read can hide a rule that already matches; retry the
        # read briefly before concluding the rule needs work.
        current = None
        if rule_id:
            for attempt in range(3):
                current = next((r for r in console(session, 'GET', RULES) if r['id'] == rule_id), None)
                if current is not None and equivalent(current, desired[kind]):
                    break
                if attempt < 2:
                    time.sleep(2)
        if current and equivalent(current, desired[kind]):
            journal['ruleIds'][kind] = rule_id
            atomic_json(journal_path, journal)
            continue
        shape_matches = current is not None and equivalent(current, desired[kind], preferences=False)
        if rule_id:
            if current is None or not shape_matches and digest(rule_config(current)) != plan['beforeHashes'].get(kind):
                raise ValueError('A managed rule changed during setup. Further edits stopped for review.')
        elif kind in journal['sent']:
            raise ValueError('A rule creation outcome is uncertain. It was not sent again; an installer must review it.')
        if not shape_matches:
            journal['sent'].append(kind)
            atomic_json(journal_path, journal)
            payload = {k: v for k, v in desired[kind].items() if k != 'notification_preferences'}
            try:
                console(session, 'PATCH' if rule_id else 'POST', RULES + '/' + rule_id if rule_id else RULES, payload)
            except ValueError:
                pass
            current = read_with_retry(session, lambda r: (r['id'] == rule_id if rule_id else r.get('title') == desired[kind]['title']) and equivalent(r, desired[kind], preferences=False), tries=5, delay=2)
            if current is None:
                raise ValueError('A saved rule did not verify. Setup is unfinished; use Resume setup after checking Protect.')
            rule_id = current['id']
            journal['ruleIds'][kind] = rule_id
            atomic_json(journal_path, journal)
        if not equivalent(current, desired[kind]) and 'notification_preferences' in desired[kind]:
            ensure_disarmed(session)
            try:
                # This is a separate per-user API. Rule default_channels only
                # initializes preferences for newly added recipients.
                console(session, 'PATCH', RULES + '/' + rule_id + '/notifications/preferences', desired[kind]['notification_preferences'])
            except ValueError:
                pass
        verified = read_with_retry(session, lambda r: r['id'] == rule_id and equivalent(r, desired[kind]), tries=5, delay=2)
        if verified is None:
            raise ValueError('Notification preferences did not verify. Setup remains unfinished; resume after checking Protect.')
        journal['ruleIds'][kind] = verified['id']
        atomic_json(journal_path, journal)
    ensure_disarmed(session)
    profiles = console(session, 'GET', PROFILES)
    mode_before = {'main': plan.get('profileBefore'), 'stay': plan.get('stayProfileBefore'), 'away': plan.get('awayProfileBefore')}
    mode_titles = {'main': plan['profileTitle'], 'stay': plan['stayProfileTitle'], 'away': plan['awayProfileTitle']}
    mode_ids = {'main': profile_ids['arm'], 'stay': profile_ids['arm_stay'], 'away': profile_ids['arm_away']}
    for mode in ('main', 'stay', 'away'):
        if not mode_ids[mode]:
            continue
        profile = next((p for p in profiles if p['id'] == mode_ids[mode]), None)
        expected_alarms = set((mode_before[mode] or {}).get('alarm_ids') or []) | {journal['ruleIds']['doors'], journal['ruleIds']['entry_alert']}
        if not profile or profile['title'] != mode_titles[mode] or not set(profile.get('alarm_ids') or []) <= expected_alarms:
            raise ValueError('Alarm profile membership changed during setup. Further edits stopped for review.')
        if profile.get('activation_delay') != 60 or set(profile.get('alarm_ids') or []) != expected_alarms:
            try:
                console(session, 'PATCH', PROFILES + '/' + mode_ids[mode],
                        {'title': mode_titles[mode], 'activation_delay': 60, 'alarm_ids': sorted(expected_alarms)})
            except ValueError:
                pass
        profile = next((p for p in console(session, 'GET', PROFILES) if p['id'] == mode_ids[mode]), None)
        if not profile or profile.get('state') != 'disarmed' or profile.get('activation_delay') != 60 or set(profile.get('alarm_ids') or []) != expected_alarms:
            raise ValueError('Alarm profile verification did not complete. Setup has not been marked ready.')
    after = console(session, 'GET', RULES)
    by_id = {r['id']: r for r in after}
    if any(not equivalent(by_id.get(journal['ruleIds'][kind], {}), desired[kind]) for kind in desired_kinds):
        raise ValueError('Alarm rules changed before setup could finish.')
    own_ids = set(journal['ruleIds'].values())
    if digest({r['id']: rule_config(r) for r in after if r['id'] not in own_ids}) != plan['foreignHash']:
        raise ValueError('Other alarm rules changed while saving. Setup needs review.')
    site = {'version': 2, 'installationId': plan['installationId'], 'consoleHost': session.host,
            'name': plan['choices']['name'], 'profile': plan['profileTitle'], 'profileId': profile_ids['arm'],
            'stayProfile': plan['stayProfileTitle'], 'stayProfileId': profile_ids['arm_stay'],
            'awayProfile': plan['awayProfileTitle'], 'awayProfileId': profile_ids['arm_away'],
            'sensors': [{k: s[k] for k in ('id', 'name')} for s in plan['sensors']],
            'doors': [s['name'] for s in plan['sensors']],
            'webhooks': {kind: plan['hooks'][kind] for kind in desired_kinds if kind != 'doors'},
            'ruleIds': {kind: journal['ruleIds'][kind] for kind in desired_kinds},
            'outputs': [{'type': 'push', 'receivers': plan['choices']['receivers']}] + ([plan['sirenOutput']] if plan.get('sirenOutput') else []),
            'exitDelaySeconds': 60, 'entryDelaySeconds': 60,
            'appliedAt': datetime.now(timezone.utc).isoformat()}
    target, _ = site_for(root, session.host)
    save_site(target, site)
    journal.update(phase='complete', site=site)
    atomic_json(journal_path, journal)
    state.pop('plan', None)
    return completion(site)


def completion(site):
    return {'saved': True, 'pinSet': True, 'name': site['name'], 'doors': site['doors'],
            'message': 'Your alarm setup is saved.',
            'next': 'Return to the keypad to check the connection and door test.',
            'keypadUrl': '/'}


def context(session, root, data=None):
    data = snapshot(session) if data is None else data
    _, site = site_for(root, session.host)
    choices = default_choices(site, data)
    draft_path, journal_path = store_paths(root)
    if draft_path.exists():
        draft = json.loads(draft_path.read_text())
        if draft.get('host') == session.host:
            choices = {**alarm_outputs.saved_choices(site), **draft['choices']}
    pending = None
    if journal_path.exists():
        journal = json.loads(journal_path.read_text())
        if journal.get('phase') != 'complete' and journal['plan']['host'] == session.host:
            pending = public_plan(journal['plan'])
    return {'connected': True, 'choices': choices, 'saved': site.get('version') == 2,
            'pending': pending, 'host': session.host, **pin_status(root),
            'sensors': [{**{k: s.get(k) for k in ('id', 'name', 'state', 'isOpened')}, 'available': usable(s)} for s in data['sensors']],
            'sirens': alarm_outputs.visible_rows(data, site), 'sirenDurations': data['sirenDurations'],
            'profiles': [{k: p.get(k) for k in ('id', 'title', 'state')} for p in data['profiles']],
            'users': [{'id': u['value'], 'name': u.get('label', 'UniFi user')} for u in data['users']],
            'message': 'Connected to your console.'}


def handle(path, body, session, root, state):
    if path == '/installer/login':
        host = str(ipaddress.IPv4Address(body.get('host', '')))
        if not any(ipaddress.ip_address(host) in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
            raise ValueError('Enter your console’s private IPv4 address.')
        site_for(root, host)
        result = session.login(host, body.get('username'), body.get('password'), body.get('verificationCode'))
        return result if result.get('mfaRequired') else context(session, root)
    if path in ('/installer/pin/status', '/installer/pin/create'):
        if not session.cookie:
            raise ValueError('Sign in to UniFi before setting up the keypad PIN.')
        site_for(root, session.host)
        if path.endswith('/create'):
            if pin_status(root)['pinSet']:
                raise ValueError('A keypad PIN is already saved. Setup cannot replace it.')
            if set(body) != {'pin', 'confirmation'}:
                raise ValueError('Enter and confirm your keypad PIN.')
            # Confirm this is still an authenticated installer session. The PIN
            # itself stays local and never enters a console request or journal.
            console(session, 'GET', PROFILES)
            create_pin(pin_path(root), body.get('pin'), body.get('confirmation'))
        return pin_status(root)
    if path == '/installer/context':
        return context(session, root)
    if path == '/installer/review':
        return review(session, body, root, state)
    if path == '/installer/apply':
        return apply(session, body, root, state)
    if path == '/installer/logout':
        session.clear()
        state.clear()
        return {'connected': False}
    raise ValueError('Unknown installer operation.')
