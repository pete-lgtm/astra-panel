"""Isolated installer compatibility experiment; never manages the live keypad.

The Global Alarm Manager API is unofficial and version-sensitive. Keep this
experiment separate from startup and production provisioning until verified.
"""
import base64
import copy
import json
import os
import re
from pathlib import Path
from uuid import uuid4, UUID
from local_auth import LocalSession

from protect_setup_api import RULES, MANIFEST, PROFILES, SENSORS, csrf_from_cookie, entries


class RuleSetupSession(LocalSession):
    def read(self, path):
        if path not in (RULES, MANIFEST, PROFILES, SENSORS):
            raise ValueError('Unsupported installer read.')
        if not self.cookie:
            raise ValueError('Sign in to the setup test first.')
        result, cookie = self.request(self.host, 'GET', path, cookie=self.cookie)
        if cookie:
            self.cookie = cookie
        return result

    def write(self, method, path, body=None):
        if method == 'POST':
            valid = path == RULES
        else:
            valid = method in ('PATCH', 'DELETE') and re.fullmatch(re.escape(RULES) + r'/[0-9a-f-]{36}', path)
        if not valid or not self.cookie:
            raise ValueError('Unsupported installer request.')
        result, cookie = self.request(self.host, method, path, body, cookie=self.cookie,
                                      extra_headers={'X-CSRF-Token': csrf_from_cookie(self.cookie)})
        if cookie:
            self.cookie = cookie
        return result


def parameter_shape(value):
    """Describe rule parameters without returning existing URLs or secrets."""
    if isinstance(value, dict):
        return {k: parameter_shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [parameter_shape(value[0])] if value else []
    return type(value).__name__


def rule_summary(rule):
    return {
        'id': rule.get('id'), 'title': rule.get('title'),
        'enabled': rule.get('enabled'),
        'fields': list(rule),
        'scopeShape': parameter_shape(rule.get('scope')),
        'triggers': [{'id': trigger.get('id'), 'title': trigger.get('title'), 'parameters': parameter_shape(trigger.get('data'))}
                     for category in rule.get('trigger_categories', []) for trigger in category.get('triggers', [])],
        'actions': [{'id': action.get('id'), 'title': action.get('title'), 'parameters': parameter_shape(action.get('data'))}
                    for category in rule.get('action_categories', []) for action in category.get('actions', [])],
    }


def inspect_console(session):
    rules, profiles, sensors = session.read(RULES), session.read(PROFILES), session.read(SENSORS)
    if not all(isinstance(x, list) for x in (rules, profiles, sensors)):
        raise ValueError('This console returned an unrecognized setup format. No rules were changed.')
    manifest = session.read(MANIFEST)
    return {
        'connected': True,
        'profiles': [{k: p.get(k) for k in ('id', 'title', 'state')} for p in profiles],
        'sensors': [{k: s.get(k) for k in ('id', 'name', 'state', 'isOpened')} for s in sensors],
        'rules': [rule_summary(r) for r in rules],
        'manifest': manifest,
        'message': 'Connected. Sensor and rule discovery completed; no settings have changed.',
    }


def configuration(rules):
    """Ignore changing execution counters; compare all saved rule configuration."""
    ignored = {'stats', 'author', 'editor', 'created_at', 'updated_at'}
    return {r['id']: {k: v for k, v in r.items() if k not in ignored} for r in rules}


def write_journal(path, data):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def probe_payload(title, trigger):
    # This rule only responds to its random, never-called webhook. Empty
    # channels prevent notifications; it has no arm/disarm/hardware action.
    return {'title': title,
            'triggers_data': [[{'id': 'protect:webhook.webhook', 'data': {'webhook': trigger}}]],
            'actions_data': [[{'id': 'protect:notify', 'data': {
                'default_channels': [], 'receivers': ['ALL_ITEMS'], 'is_critical': False}}]]}


def owned_probe(rule, journal):
    return (rule.get('id') not in journal['beforeIds'] and
            rule.get('title') in (journal['title'], journal['title'] + ' · verified') and
            entries(rule, 'trigger') == [{'id': 'protect:webhook.webhook', 'data': {'webhook': journal['trigger']}}] and
            entries(rule, 'action') == [{'id': 'protect:notify', 'data': {
                'default_channels': [], 'receivers': ['ALL_ITEMS'], 'is_critical': False}}])


def cleanup_probe(session, journal, path):
    """Only delete this run's exact, inert object after reading it back."""
    if journal.get('host') != session.host:
        raise ValueError('This temporary rule belongs to a different console.')
    rules = session.read(RULES)
    if not isinstance(rules, list):
        raise ValueError('Could not verify temporary rule cleanup.')
    candidates = [r for r in rules if owned_probe(r, journal)]
    for rule in candidates:
        rule_id = rule.get('id', '')
        if str(UUID(rule_id)) != rule_id:
            raise ValueError('Temporary rule identifier was not recognized.')
        session.write('DELETE', RULES + '/' + rule_id)
    remaining = session.read(RULES)
    if not isinstance(remaining, list):
        raise ValueError('Could not confirm temporary rule removal.')
    if any(r.get('id') == journal.get('ruleId') or r.get('title') in (journal['title'], journal['title'] + ' · verified') or
           any(t.get('data') == {'webhook': journal['trigger']} for t in entries(r, 'trigger'))
           for r in remaining):
        raise ValueError('Temporary rule remains or was modified. Manual review is required; no other rule was changed.')
    journal['cleaned'] = True
    write_journal(path, journal)
    return remaining


def run_creation_probe(session, journal_path):
    """Create, read, rename, read, remove. Never call any alarm trigger."""
    path = Path(journal_path)
    if path.exists():
        previous = json.loads(path.read_text())
        if not previous.get('cleaned'):
            raise ValueError('A previous test needs cleanup before another can run.')
    before = session.read(RULES)
    profiles = session.read(PROFILES)
    manifest = session.read(MANIFEST)
    if not isinstance(before, list) or not isinstance(profiles, list) or not profiles:
        raise ValueError('Existing rules or alarm state could not be verified.')
    if any(p.get('state') != 'disarmed' for p in profiles):
        raise ValueError('Disarm in your working keypad before running this installer test.')
    trigger_ids = {t.get('id') for c in manifest.get('trigger_categories', []) for t in c.get('triggers', [])}
    action_ids = {a.get('id') for c in manifest.get('action_categories', []) for a in c.get('actions', [])}
    if 'protect:webhook.webhook' not in trigger_ids or 'protect:notify' not in action_ids:
        raise ValueError('This console does not advertise the expected test rule types.')
    # Fail before any write if this session lacks a supported CSRF token.
    csrf_from_cookie(session.cookie)
    marker = str(uuid4())
    title = 'Loxicom setup test ' + marker
    journal = {'host': session.host, 'title': title, 'trigger': str(uuid4()),
               'beforeIds': [r['id'] for r in before], 'phase': 'prepared', 'cleaned': False}
    baseline = configuration(before)
    write_journal(path, journal)
    payload = probe_payload(title, journal['trigger'])
    checks = []
    failure = None
    try:
        journal['phase'] = 'create_sent'
        write_journal(path, journal)
        # No automatic POST retries: an uncertain response may have created it.
        session.write('POST', RULES, payload)
        readback = session.read(RULES)
        matches = [r for r in readback if owned_probe(r, journal)]
        if len(matches) != 1:
            raise ValueError('Created rule did not read back exactly as requested.')
        created = matches[0]
        journal['ruleId'] = created['id']
        journal['phase'] = 'created_verified'
        write_journal(path, journal)
        if created.get('stats', {}).get('executions_24h', 0) != 0:
            raise ValueError('Unexpected test-rule activity; cleanup is required.')
        checks.append('Created and read back an untriggered webhook rule with notification channels off.')
        updated = copy.deepcopy(payload)
        updated['title'] = title + ' · verified'
        session.write('PATCH', RULES + '/' + created['id'], updated)
        matches = [r for r in session.read(RULES) if r.get('id') == created['id']]
        if len(matches) != 1 or matches[0].get('title') != updated['title'] or not owned_probe(matches[0], journal):
            raise ValueError('Temporary rule update could not be verified.')
        checks.append('Updated the temporary rule and verified the change.')
    except ValueError as error:
        failure = str(error)
    finally:
        try:
            after = cleanup_probe(session, journal, path)
            checks.append('Removed the temporary rule and confirmed it is gone.')
        except (ValueError, TypeError, KeyError) as error:
            journal['phase'] = 'cleanup_required'
            journal['error'] = str(error)
            write_journal(path, journal)
            raise ValueError('Temporary-rule cleanup needs review. ' + str(error)) from None
    unchanged = configuration(after) == baseline
    checks.append('Existing alarm rules are unchanged.' if unchanged else 'Existing rules changed during this test; review before proceeding.')
    states_after = session.read(PROFILES)
    states_equal = ({p['id']: p.get('state') for p in states_after} == {p['id']: p.get('state') for p in profiles})
    journal.update(phase='passed' if not failure and unchanged and states_equal else 'incomplete',
                   checks=checks, existingRulesUnchanged=unchanged, alarmStateUnchanged=states_equal,
                   error=failure)
    write_journal(path, journal)
    return {'passed': journal['phase'] == 'passed', 'checks': checks,
            'existingRulesUnchanged': unchanged, 'alarmStateUnchanged': states_equal,
            'temporaryRuleRemoved': True, 'error': failure,
            'message': 'Automatic rule creation, update, and cleanup verified.' if journal['phase'] == 'passed'
                       else 'The test did not pass every check. Review the details before continuing.'}
