"""Provisioning tests use an in-memory console; never the household alarm."""
import base64
import copy
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import onboarding as ob
from device_config import load_site, save_site, selected_sensors
from pin_store import create_pin


def rule(payload, rule_id=None):
    result = copy.deepcopy(payload)
    result['id'] = rule_id or str(uuid4())
    for key, kind, field in (('triggers_data', 'trigger', 'triggers'), ('actions_data', 'action', 'actions')):
        result[kind + '_categories'] = [{field: items} for items in result.pop(key)]
    result['stats'] = {'executions_24h': 0}
    return result


class Console:
    host = '192.168.0.1'
    cookie = 'TOKEN=h.' + base64.urlsafe_b64encode(b'{"csrfToken":"test-only"}').decode().rstrip('=') + '.sig'

    def __init__(self):
        self.rules = []
        self.profiles = []
        self.sensors = [{'id': 'sensor-1', 'name': 'Front', 'mac': '001122334455', 'state': 'CONNECTED', 'isOpened': False},
                        {'id': 'sensor-2', 'name': 'Back', 'mac': '001122334466', 'state': 'CONNECTED', 'isOpened': False},
                        {'id': 'sensor-bad', 'name': 'Faulty', 'mac': '001122334477', 'state': 'CONNECTED', 'isOpened': None}]
        self.users = [{'value': str(uuid4()), 'label': 'Installer'}]
        self.writes = []
        self.uncertain = False
        self.fail_read_once = False
        self.fail_write_at = None
        self.lie_at = None
        self.manifest = {'trigger_categories': [{'triggers': [{'id': 'protect:webhook.webhook'}, {'id': 'protect:sensors.openStatusChanged'}]}],
                         'action_categories': [{'actions': [{'id': x} for x in ('protect:notify', 'protect:alarm_profile.arm', 'protect:alarm_profile.disarm')]}]}

    def request(self, host, method, path, payload=None, **kwargs):
        if method == 'GET':
            if self.fail_read_once:
                self.fail_read_once = False
                raise ValueError('Network interruption')
            return copy.deepcopy({ob.RULES: self.rules, ob.PROFILES: self.profiles, ob.SENSORS: self.sensors,
                                  ob.USERS: self.users, ob.MANIFEST: self.manifest}[path]), None
        self.writes.append((method, path, copy.deepcopy(payload)))
        self.assert_write_headers(kwargs)
        if len(self.writes) == self.fail_write_at:
            raise ValueError('Write rejected')
        if len(self.writes) == self.lie_at:
            return {}, None
        if path.endswith('/notifications/preferences'):
            item = next(x for x in self.rules if x['id'] == path.split('/')[-3])
            item['notification_preferences'].update(copy.deepcopy(payload))
            return copy.deepcopy(item['notification_preferences']), None
        target = self.rules if path.startswith(ob.RULES) else self.profiles
        if method == 'POST':
            item = rule(payload) if target is self.rules else {**copy.deepcopy(payload), 'id': str(uuid4()), 'state': 'disarmed'}
            if target is self.rules:
                notify = next((a for a in ob.entries(item, 'action') if a['id'] == 'protect:notify'), None)
                channels = (notify or {}).get('data', {}).get('default_channels', [])
                item['notification_preferences'] = {'push': 'push' in channels, 'email': 'email' in channels}
            target.append(item)
        else:
            item = next(x for x in target if x['id'] == path.split('/')[-1])
            # UniFi ignores notification_preferences on the rule PATCH itself.
            updated = rule(payload, item['id']) if target is self.rules else copy.deepcopy(payload)
            updated.pop('notification_preferences', None)
            item.update(updated)
        if target is self.rules:
            for profile in self.profiles:
                if profile['id'] in item.get('restriction', {}).get('profile_ids', []) and item['id'] not in profile['alarm_ids']:
                    profile['alarm_ids'].append(item['id'])
        if self.uncertain and method == 'POST':
            self.uncertain = False
            raise ValueError('Create response lost')
        return copy.deepcopy(item), None

    def assert_write_headers(self, kwargs):
        if kwargs.get('extra_headers') != {'X-CSRF-Token': 'test-only'}:
            raise AssertionError('Missing CSRF header')


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = Console()
        self.state = {}
        self.choices = {'name': 'Test Home', 'profileName': 'Home alarm', 'profileId': '',
                        'sensorIds': ['sensor-1', 'sensor-2'], 'receivers': [self.session.users[0]['value']]}
        self.env = patch.dict('os.environ', {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        create_pin(self.root / '.keypad-pin.json', '927461', '927461')

    def review(self):
        return ob.review(self.session, self.choices, self.root, self.state)

    def apply(self, plan):
        return ob.apply(self.session, {'planId': plan['planId']}, self.root, self.state)

    def saved(self):
        return load_site(self.root / 'site.json')

    def test_complete_setup_persists_ids_and_never_fires_alarm(self):
        result = self.apply(self.review())
        self.assertTrue(result['saved'])
        site = self.saved()
        self.assertEqual(site['doors'], ['Front', 'Back'])
        self.assertEqual([s['id'] for s in site['sensors']], self.choices['sensorIds'])
        self.assertEqual(len(self.session.rules), 7)
        self.assertEqual(len(self.session.profiles), 3)
        self.assertEqual({p['title'] for p in self.session.profiles},
                         {'Home alarm · Loxicom ' + site['installationId'][:8],
                          'Home alarm · Loxicom ' + site['installationId'][:8] + ' · Stay',
                          'Home alarm · Loxicom ' + site['installationId'][:8] + ' · Away'})
        for profile in self.session.profiles:
            self.assertEqual(profile['state'], 'disarmed')
            self.assertEqual(profile['activation_delay'], 60)
            self.assertEqual(len(profile['alarm_ids']), 2)
        self.assertEqual(set(site['webhooks']), {'arm', 'disarm', 'entry_alert', 'arm_stay', 'arm_away', 'panic'})
        self.assertEqual(set(site['ruleIds']), {'arm', 'disarm', 'entry_alert', 'doors', 'arm_stay', 'arm_away', 'panic'})
        self.assertEqual(len(set(site['webhooks'].values())), 6)
        self.assertEqual(len(set(site['ruleIds'].values())), 7)
        self.assertEqual(site['stayProfileId'], site['ruleIds'] and next(
            p['id'] for p in self.session.profiles if p['title'].endswith('· Stay')))
        self.assertEqual(site['awayProfileId'], next(
            p['id'] for p in self.session.profiles if p['title'].endswith('· Away')))
        self.assertTrue(all(path in (ob.PROFILES, ob.RULES) or method == 'PATCH' for method, path, _ in self.session.writes))
        self.assertEqual(stat.S_IMODE((self.root / 'site.json').stat().st_mode), 0o600)
        self.assertNotIn(self.session.cookie, (self.root / 'site.json').read_text())

    def test_stay_away_rules_target_their_own_profiles(self):
        self.apply(self.review())
        site = self.saved()
        by_id = {r['id']: r for r in self.session.rules}
        for kind, field in (('arm', 'profileId'), ('arm_stay', 'stayProfileId'), ('arm_away', 'awayProfileId')):
            rule = by_id[site['ruleIds'][kind]]
            actions = ob.entries(rule, 'action')
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]['id'], 'protect:alarm_profile.arm')
            self.assertEqual(actions[0]['data']['profile'], site[field])
            triggers = ob.entries(rule, 'trigger')
            self.assertEqual(triggers[0]['data']['webhook'], site['webhooks'][kind])
        # The door and entry-delay rules watch every armed mode.
        all_profiles = [site['profileId'], site['stayProfileId'], site['awayProfileId']]
        for kind in ('doors', 'entry_alert'):
            rule = by_id[site['ruleIds'][kind]]
            self.assertEqual(rule['restriction']['profile_ids'], all_profiles)

    def test_review_lists_stay_away_steps_before_apply(self):
        plan = self.review()
        actions = ' '.join(plan['actions'])
        self.assertIn('Stay alarm profile', actions)
        self.assertIn('Away alarm profile', actions)
        self.assertIn('Arm stay control', actions)
        self.assertIn('Arm away control', actions)

    def test_review_lists_panic_step_before_apply(self):
        plan = self.review()
        self.assertIn('Panic alert', ' '.join(plan['actions']))
        hooks = self.state['plan']['hooks']
        self.assertIn('panic', hooks)
        self.assertNotEqual(hooks['panic'], hooks['disarm'])

    def test_panic_rule_has_no_profile_restriction_and_notifies(self):
        self.apply(self.review())
        site = self.saved()
        by_id = {r['id']: r for r in self.session.rules}
        panic = by_id[site['ruleIds']['panic']]
        triggers = ob.entries(panic, 'trigger')
        self.assertEqual(triggers, [{'id': 'protect:webhook.webhook',
                                     'data': {'webhook': site['webhooks']['panic']}}])
        self.assertEqual([a['id'] for a in ob.entries(panic, 'action')], ['protect:notify'])
        # Panic must fire whether the system is armed or disarmed.
        self.assertNotIn('restriction', panic)
        self.assertNotIn('scope', panic)
        self.assertEqual(panic['notification_preferences'], {'push': True, 'email': False})
        # ... and it is never an alarm-profile member.
        for profile in self.session.profiles:
            self.assertNotIn(panic['id'], profile.get('alarm_ids', []))

    def test_pre_panic_installation_gains_panic_rule(self):
        # Installations saved after Stay/Away but before panic carry six rules.
        profile_id, stay_id, away_id = str(uuid4()), str(uuid4()), str(uuid4())
        self.session.profiles = [
            {'id': pid, 'title': title, 'state': 'disarmed', 'activation_delay': 60, 'alarm_ids': []}
            for pid, title in ((profile_id, 'Arm'), (stay_id, 'Arm \u00b7 Stay'), (away_id, 'Arm \u00b7 Away'))]
        hooks = {kind: str(uuid4()) for kind in ('arm', 'disarm', 'entry_alert', 'arm_stay', 'arm_away')}
        saved = {'version': 2, 'installationId': str(uuid4()), 'consoleHost': '192.168.0.1',
                 'name': 'Example Home', 'profile': 'Arm', 'profileId': profile_id,
                 'stayProfile': 'Arm \u00b7 Stay', 'stayProfileId': stay_id,
                 'awayProfile': 'Arm \u00b7 Away', 'awayProfileId': away_id,
                 'sensors': [{'id': 'sensor-1', 'name': 'Front'}, {'id': 'sensor-2', 'name': 'Back'}],
                 'doors': ['Front', 'Back'], 'webhooks': hooks,
                 'ruleIds': {kind: str(uuid4()) for kind in
                             ('arm', 'disarm', 'entry_alert', 'doors', 'arm_stay', 'arm_away')},
                 'outputs': [{'type': 'push', 'receivers': [self.session.users[0]['value']]}],
                 'exitDelaySeconds': 60, 'entryDelaySeconds': 60, 'appliedAt': '2026-09-28T21:03:39+00:00'}
        save_site(self.root / 'prototype-site.json', saved)
        payloads = ob.payloads({'receivers': [self.session.users[0]['value']]}, self.session.sensors[:2],
                               {'arm': profile_id, 'arm_stay': stay_id, 'arm_away': away_id},
                               {**hooks, 'panic': str(uuid4())}, 'Loxicom x', {})
        for kind, payload in payloads.items():
            if kind == 'panic':
                continue  # panic did not exist on this installation yet
            item = rule(payload)
            item['id'] = saved['ruleIds'][kind]
            self.session.rules.append(item)
            if payload.get('restriction'):
                for profile in self.session.profiles:
                    if profile['id'] in payload['restriction']['profile_ids']:
                        profile['alarm_ids'].append(item['id'])
        self.choices['profileId'] = profile_id
        self.apply(self.review())
        site = self.saved()
        # Every previously saved id and webhook is untouched; panic is added.
        for kind in ('arm', 'disarm', 'entry_alert', 'doors', 'arm_stay', 'arm_away'):
            self.assertEqual(site['ruleIds'][kind], saved['ruleIds'][kind])
        for kind in ('arm', 'disarm', 'entry_alert', 'arm_stay', 'arm_away'):
            self.assertEqual(site['webhooks'][kind], hooks[kind])
        self.assertEqual(len(self.session.rules), 7)
        by_id = {r['id']: r for r in self.session.rules}
        panic = by_id[site['ruleIds']['panic']]
        self.assertEqual(ob.entries(panic, 'trigger'),
                         [{'id': 'protect:webhook.webhook', 'data': {'webhook': site['webhooks']['panic']}}])
        self.assertNotIn('restriction', panic)
        for profile in self.session.profiles:
            self.assertNotIn(panic['id'], profile.get('alarm_ids', []))

    def test_resolve_profile_prefers_saved_id_then_known_titles(self):
        profiles = [{'id': 'p-main', 'title': 'Home alarm', 'activation_delay': 60, 'alarm_ids': []},
                    {'id': 'p-stay', 'title': 'Home alarm · Stay', 'activation_delay': 60, 'alarm_ids': []}]
        found_id, _ = ob.resolve_profile(profiles, 'p-stay', ['Other · Stay', 'Home alarm · Stay'])
        self.assertEqual(found_id, 'p-stay')
        found_id, _ = ob.resolve_profile(profiles, 'deleted-id', ['Home alarm · Stay'])
        self.assertEqual(found_id, 'p-stay')
        found_id, before = ob.resolve_profile(profiles, 'deleted-id', ['Nothing matches'])
        self.assertEqual(found_id, '')
        self.assertIsNone(before)
        # A saved id that still exists wins over a title match.
        profiles.append({'id': 'p-other', 'title': 'Other · Stay', 'activation_delay': 60, 'alarm_ids': []})
        found_id, _ = ob.resolve_profile(profiles, 'p-other', ['Home alarm · Stay'])
        self.assertEqual(found_id, 'p-other')

    def test_transitional_site_upgrades_without_touching_existing_rules(self):
        # Legacy installation shape: four managed rules, one profile, no Stay/Away yet.
        profile_id = str(uuid4())
        self.session.profiles = [{'id': profile_id, 'title': 'Arm', 'state': 'disarmed',
                                  'activation_delay': 60, 'alarm_ids': []}]
        hooks = {kind: str(uuid4()) for kind in ('arm', 'disarm', 'entry_alert')}
        legacy = {'version': 2, 'installationId': str(uuid4()), 'consoleHost': '192.168.0.1',
                  'name': 'Example Home', 'profile': 'Arm', 'profileId': profile_id,
                  'sensors': [{'id': 'sensor-1', 'name': 'Front'}, {'id': 'sensor-2', 'name': 'Back'}],
                  'doors': ['Front', 'Back'], 'webhooks': hooks,
                  'ruleIds': {kind: str(uuid4()) for kind in ('arm', 'disarm', 'entry_alert', 'doors')},
                  'outputs': [{'type': 'push', 'receivers': [self.session.users[0]['value']]}],
                  'exitDelaySeconds': 60, 'entryDelaySeconds': 60, 'appliedAt': '2026-09-28T20:06:53+00:00'}
        save_site(self.root / 'prototype-site.json', legacy)
        payloads = ob.payloads({'receivers': [self.session.users[0]['value']]}, self.session.sensors[:2],
                               {'arm': profile_id, 'arm_stay': None, 'arm_away': None},
                               {**hooks, 'panic': str(uuid4())}, 'Loxicom x', {})
        for kind, payload in payloads.items():
            if kind not in legacy['ruleIds']:
                continue  # panic did not exist on this installation yet
            item = rule(payload)
            item['id'] = legacy['ruleIds'][kind]
            self.session.rules.append(item)
            if payload.get('restriction'):
                self.session.profiles[0]['alarm_ids'].append(item['id'])
        self.choices['profileId'] = profile_id
        self.apply(self.review())
        site = self.saved()
        # Every previously saved id and webhook is untouched.
        self.assertEqual(site['installationId'], legacy['installationId'])
        for kind in ('arm', 'disarm', 'entry_alert', 'doors'):
            self.assertEqual(site['ruleIds'][kind], legacy['ruleIds'][kind])
        for kind in ('arm', 'disarm', 'entry_alert'):
            self.assertEqual(site['webhooks'][kind], hooks[kind])
        # Stay/Away profiles and rules were added, covering the same doors,
        # plus the new panic notification rule.
        self.assertEqual(len(self.session.profiles), 3)
        self.assertEqual(len(self.session.rules), 7)
        stay = next(p for p in self.session.profiles if p['id'] == site['stayProfileId'])
        self.assertEqual(stay['activation_delay'], 60)
        self.assertEqual(stay['state'], 'disarmed')
        by_id = {r['id']: r for r in self.session.rules}
        doors_rule = by_id[site['ruleIds']['doors']]
        self.assertEqual(doors_rule['restriction']['profile_ids'],
                         [profile_id, site['stayProfileId'], site['awayProfileId']])

    def test_repeated_save_and_restart_resume_do_not_duplicate(self):
        plan = self.review()
        first = self.apply(plan)
        writes = len(self.session.writes)
        self.state.clear()
        self.assertEqual(first, self.apply(plan))
        self.assertEqual(writes, len(self.session.writes))
        context = ob.context(self.session, self.root)
        self.assertTrue(context['saved'])
        self.assertEqual(context['choices']['sensorIds'], ['sensor-1', 'sensor-2'])

    def test_saved_installation_updates_without_creating_more_rules(self):
        self.apply(self.review())
        original_ids = self.saved()['ruleIds']
        self.choices['profileId'] = self.saved()['profileId']
        self.choices['sensorIds'] = ['sensor-1']
        self.apply(self.review())
        self.assertEqual(self.saved()['ruleIds'], original_ids)
        self.assertEqual(len(self.session.rules), 7)
        self.assertEqual(self.saved()['doors'], ['Front'])
        self.assertTrue((self.root / 'site.previous.json').exists())

    def test_unknown_offline_open_duplicate_or_empty_sensor_rejected(self):
        for ids in ([], ['sensor-bad'], ['absent'], ['sensor-1', 'sensor-1']):
            self.choices['sensorIds'] = ids
            with self.assertRaises(ValueError):
                self.review()
        self.choices['sensorIds'] = ['sensor-1']
        for changes in ({'state': 'DISCONNECTED'}, {'state': 'CONNECTED', 'isOpened': True}):
            self.session.sensors[0].update(changes)
            with self.assertRaises(ValueError):
                self.review()
        self.assertEqual(self.session.writes, [])

    def test_stale_plan_tampering_and_expiry_rejected(self):
        plan = self.review()
        for token in ('bad', ''):
            with self.assertRaises(ValueError):
                self.apply({'planId': token})
        self.state['plan']['expires'] = 0
        with self.assertRaises(ValueError):
            self.apply(plan)
        plan = self.review()
        self.session.rules.append({'id': str(uuid4()), 'title': 'Other rule'})
        with self.assertRaisesRegex(ValueError, 'changed since review'):
            self.apply(plan)
        self.assertEqual(self.session.writes, [])

    def test_arm_during_review_blocks_apply(self):
        profile = {'id': str(uuid4()), 'title': 'Existing', 'activation_delay': 60, 'alarm_ids': [], 'state': 'disarmed'}
        self.session.profiles.append(profile)
        self.choices['profileId'] = profile['id']
        plan = self.review()
        profile['state'] = 'armed'
        with self.assertRaisesRegex(ValueError, 'Disarm'):
            self.apply(plan)
        self.assertEqual(self.session.writes, [])

    def test_profile_membership_reordering_does_not_invalidate_review(self):
        self.apply(self.review())
        site = self.saved()
        self.choices['profileId'] = site['profileId']
        plan = self.review()
        before = copy.deepcopy(self.session.rules)
        for item in self.session.rules:
            if item.get('restriction'):
                item['restriction']['profile_ids'].reverse()
        for profile in self.session.profiles:
            profile['alarm_ids'].reverse()
        self.session.writes.clear()
        self.assertTrue(self.apply(plan)['saved'])
        self.assertEqual(self.session.writes, [], 'An order-only change needs no console write')
        self.assertEqual(self.saved()['ruleIds'], site['ruleIds'])
        self.assertNotEqual(before, self.session.rules, 'Fixture must actually reorder membership')

    def test_profile_membership_changes_still_require_a_new_review(self):
        self.apply(self.review())
        self.choices['profileId'] = self.saved()['profileId']
        for target in ('rule', 'profile'):
            with self.subTest(target=target):
                plan = self.review()
                if target == 'rule':
                    item = next(r for r in self.session.rules if r.get('restriction'))
                    membership = item['restriction']['profile_ids']
                else:
                    membership = self.session.profiles[0]['alarm_ids']
                membership.append(str(uuid4()))
                self.session.writes.clear()
                with self.assertRaises(ob.ReviewRequired):
                    self.apply(plan)
                self.assertEqual(self.session.writes, [])
                membership.pop()

    def test_rule_fingerprint_normalization_does_not_mutate_console_response(self):
        item = {'id': 'test', 'restriction': {'type': 'when_armed', 'profile_ids': ['b', 'a', 'a']}}
        original = copy.deepcopy(item)
        result = ob.rule_config(item)
        self.assertEqual(item, original)
        self.assertEqual(result['restriction']['profile_ids'], ['a', 'a', 'b'])

    def test_lost_post_response_is_reconciled_not_retried(self):
        self.session.uncertain = True
        result = self.apply(self.review())
        self.assertTrue(result['saved'])
        self.assertEqual(len(self.session.writes), 10)
        self.assertEqual(len(self.session.profiles), 3)

    def test_unverified_partial_setup_never_says_saved_or_reposts(self):
        plan = self.review()
        self.session.fail_write_at = 5
        with self.assertRaisesRegex(ValueError, 'did not verify'):
            self.apply(plan)
        self.assertFalse((self.root / 'site.json').exists())
        count = len(self.session.writes)
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            self.apply(plan)
        self.assertEqual(len(self.session.writes), count)
        self.assertIsNotNone(ob.context(self.session, self.root)['pending'])
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            self.review()

    def test_resume_after_process_loss_reconciles_exact_owned_object(self):
        plan = self.review()
        real = ob.atomic_json
        def crash(path, body):
            if body.get('ruleIds', {}).get('disarm'):
                raise RuntimeError('Power loss before journaling response')
            return real(path, body)
        with patch.object(ob, 'atomic_json', side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.apply(plan)
        self.state.clear()
        self.assertTrue(self.apply(plan)['saved'])
        self.assertEqual(len(self.session.rules), 7)

    def test_foreign_door_rule_blocks_setup_without_modification(self):
        self.session.rules.append(rule({'title': 'Existing immediate alert',
            'triggers_data': [[{'id': 'protect:sensors.openStatusChanged', 'data': {}}]],
            'actions_data': [[{'id': 'protect:notify', 'data': {}}]],
            'scope': {'mode': 'include', 'data': {'scope_all_entry_sensors': ['001122334455']}}}))
        with self.assertRaisesRegex(ValueError, 'Other rules'):
            self.review()
        self.assertEqual(self.session.writes, [])

    def test_legacy_migration_preserves_extra_profile_rules_and_hook_ids(self):
        profile_id = str(uuid4())
        self.choices['profileId'] = profile_id
        hooks = {kind: str(uuid4()) for kind in ('arm', 'disarm', 'entry_alert')}
        legacy = {'name': 'Test Home', 'profile': 'Home alarm', 'doors': ['Front', 'Back'], 'webhooks': hooks}
        save_site(self.root / 'prototype-site.json', legacy)
        foreign_id = str(uuid4())
        self.session.rules = [{'id': foreign_id, 'title': 'Keep this rule'}]
        self.session.profiles = [{'id': profile_id, 'title': 'Home alarm', 'state': 'disarmed', 'activation_delay': 60, 'alarm_ids': [foreign_id]}]
        payloads = ob.payloads(self.choices, self.session.sensors[:2],
                                 {'arm': profile_id, 'arm_stay': None, 'arm_away': None},
                                 {**hooks, 'panic': str(uuid4())}, 'Original', {})
        for kind, payload in payloads.items():
            if kind == 'panic':
                continue  # panic did not exist on this installation yet
            item = rule(payload)
            self.session.rules.append(item)
            if payload.get('restriction'):
                self.session.profiles[0]['alarm_ids'].append(item['id'])
        initial = {r['id'] for r in self.session.rules}
        self.assertEqual(len(initial), 5)  # foreign + the four legacy keypad rules
        self.apply(self.review())
        site = self.saved()
        # Only the two new arm-mode rules and the panic rule are added;
        # every legacy id is reused.
        self.assertEqual(len(self.session.rules), 8)
        self.assertEqual(initial | {site['ruleIds']['arm_stay'], site['ruleIds']['arm_away'],
                                    site['ruleIds']['panic']},
                         {r['id'] for r in self.session.rules})
        for kind in ('arm', 'disarm', 'entry_alert'):
            self.assertEqual(site['webhooks'][kind], hooks[kind])
        self.assertEqual(len(self.session.profiles), 3)
        self.assertIn(foreign_id, self.session.profiles[0]['alarm_ids'])

    def test_existing_preferences_use_the_separate_endpoint(self):
        self.apply(self.review())
        site = self.saved()
        door_rule = next(r for r in self.session.rules if r['id'] == site['ruleIds']['doors'])
        door_rule['notification_preferences']['push'] = True
        self.choices['profileId'] = site['profileId']
        self.apply(self.review())
        self.assertFalse(door_rule['notification_preferences']['push'])
        self.assertTrue(any(path.endswith('/notifications/preferences') for _, path, _ in self.session.writes))
        self.assertEqual(len(self.session.rules), 7)

    def test_same_title_does_not_claim_foreign_rule(self):
        self.session.rules.append({'id': str(uuid4()), 'title': 'Loxicom Keypad — Arm'})
        self.apply(self.review())
        self.assertEqual(len(self.session.rules), 8)
        self.assertEqual(self.session.rules[0]['title'], 'Loxicom Keypad — Arm')

    def test_duplicate_name_and_other_console_rejected(self):
        self.session.sensors[1]['name'] = 'Front'
        with self.assertRaisesRegex(ValueError, 'distinct'):
            self.review()
        self.session.sensors[1]['name'] = 'Back'
        self.apply(self.review())
        self.session.host = '192.168.0.2'
        with self.assertRaisesRegex(ValueError, 'another console'):
            ob.context(self.session, self.root)

    def test_sensor_rename_keeps_coverage(self):
        self.apply(self.review())
        self.session.sensors[0]['name'] = 'Renamed front'
        selected = selected_sensors(self.saved(), self.session.sensors)
        self.assertEqual(selected[0]['id'], 'sensor-1')
        self.assertEqual(selected[0]['name'], 'Front')

    def test_future_outputs_cannot_be_enabled_by_client(self):
        self.choices['outputs'] = [{'type': 'siren'}]
        with self.assertRaises(ValueError):
            self.review()
        self.assertEqual(self.session.writes, [])

    def test_missing_pin_blocks_review_and_apply_before_console_changes(self):
        plan = self.review()
        (self.root / '.keypad-pin.json').unlink()
        (self.root / '.keypad-data.sqlite3').unlink()
        for operation in (self.review, lambda: self.apply(plan)):
            with self.assertRaisesRegex(ValueError, 'Create your keypad PIN'):
                operation()
        self.assertEqual(self.session.writes, [])
        self.assertFalse((self.root / 'site.json').exists())

    def test_context_exposes_only_whether_pin_is_set(self):
        result = ob.context(self.session, self.root)
        self.assertTrue(result['pinSet'])
        serialized = json.dumps(result)
        self.assertNotIn('927461', serialized)
        self.assertNotIn('digest', serialized)
        self.assertNotIn('salt', serialized)

    def test_pin_setup_requires_signin_confirmation_and_cannot_replace(self):
        path = self.root / '.keypad-pin.json'
        saved = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'already saved'):
            ob.handle('/installer/pin/create', {'pin': '13579', 'confirmation': '13579'}, self.session, self.root, self.state)
        self.assertEqual(path.read_bytes(), saved)
        path.unlink()
        (self.root / '.keypad-data.sqlite3').unlink()
        self.session.cookie = ''
        with self.assertRaisesRegex(ValueError, 'Sign in'):
            ob.handle('/installer/pin/create', {'pin': '13579', 'confirmation': '13579'}, self.session, self.root, self.state)
        self.assertFalse(path.exists())
        self.session.cookie = Console.cookie
        with self.assertRaisesRegex(ValueError, 'match'):
            ob.handle('/installer/pin/create', {'pin': '13579', 'confirmation': '24680'}, self.session, self.root, self.state)
        self.assertFalse(path.exists())
        result = ob.handle('/installer/pin/create', {'pin': '13579', 'confirmation': '13579'}, self.session, self.root, self.state)
        self.assertEqual(result, {'pinSet': True})
        self.assertEqual(self.session.writes, [])
        self.assertFalse((self.root / '.installer-draft.json').exists())
        self.assertFalse((self.root / '.installer-apply.json').exists())

    def test_finished_setup_cannot_bypass_a_missing_pin(self):
        plan = self.review()
        self.apply(plan)
        writes = len(self.session.writes)
        (self.root / '.keypad-pin.json').unlink()
        (self.root / '.keypad-data.sqlite3').unlink()
        with self.assertRaisesRegex(ValueError, 'Create your keypad PIN'):
            self.apply(plan)
        self.assertEqual(len(self.session.writes), writes)

    def test_mutation_allowlist_excludes_triggers_and_hardware(self):
        for path in ('/api/auth/login', ob.RULES + '/webhook/example', ob.SENSORS, ob.PROFILES + '/arm'):
            with self.assertRaises(ValueError):
                ob.console(self.session, 'POST', path)
        self.assertEqual(self.session.writes, [])

    def test_restriction_order_does_not_matter_but_membership_does(self):
        payload = {'title': 'T', 'triggers_data': [[{'id': 'x', 'data': {}}]],
                   'actions_data': [[{'id': 'y', 'data': {}}]],
                   'restriction': {'type': 'when_armed', 'profile_ids': ['p1', 'p2']}}
        reordered = rule(payload)
        reordered['restriction']['profile_ids'] = ['p2', 'p1']
        self.assertTrue(ob.equivalent(reordered, payload))
        self.assertTrue(ob.equivalent(reordered, payload, preferences=False))
        removed = rule(payload)
        removed['restriction']['profile_ids'] = ['p1']
        self.assertFalse(ob.equivalent(removed, payload))
        swapped = rule(payload)
        swapped['restriction']['profile_ids'] = ['p1', 'p3']
        self.assertFalse(ob.equivalent(swapped, payload))
        wrong_type = rule(payload)
        wrong_type['restriction']['type'] = 'always'
        self.assertFalse(ob.equivalent(wrong_type, payload))

    def test_resume_tolerates_console_reordered_restriction(self):
        plan = self.review()
        self.apply(plan)
        site = self.saved()
        writes = len(self.session.writes)
        # The live console reorders profile_ids when it stores the rule; the
        # rule is unchanged in every way that matters.
        entry = next(r for r in self.session.rules if r['id'] == site['ruleIds']['entry_alert'])
        entry['restriction']['profile_ids'] = list(reversed(entry['restriction']['profile_ids']))
        self.state.clear()
        self.assertTrue(self.apply(plan)['saved'])
        self.assertEqual(len(self.session.writes), writes)
        self.assertEqual(self.saved()['ruleIds'], site['ruleIds'])

    def test_read_with_retry_heals_one_stale_response(self):
        payload = {'title': 'T', 'triggers_data': [[{'id': 'x', 'data': {}}]],
                   'actions_data': [[{'id': 'y', 'data': {}}]]}
        fresh = rule(payload, 'r1')
        stale = rule({**payload, 'title': 'Old'}, 'r1')
        calls = []

        class Stub:
            host = 'h'
            cookie = Console.cookie

            def request(self, host, method, path, payload=None, **kwargs):
                calls.append(method)
                return copy.deepcopy([fresh] if len(calls) > 1 else [stale]), None

        with patch('time.sleep') as asleep:
            found = ob.read_with_retry(Stub(), lambda r: ob.equivalent(r, payload), tries=3, delay=2)
        self.assertEqual(found['id'], 'r1')
        self.assertEqual(len(calls), 2)
        asleep.assert_called_once_with(2)

    def test_read_with_retry_gives_up_after_repeated_stale_responses(self):
        payload = {'title': 'T', 'triggers_data': [[{'id': 'x', 'data': {}}]],
                   'actions_data': [[{'id': 'y', 'data': {}}]]}
        stale = rule({**payload, 'title': 'Old'}, 'r1')
        calls = []

        class Stub:
            host = 'h'
            cookie = Console.cookie

            def request(self, host, method, path, payload=None, **kwargs):
                calls.append(method)
                return copy.deepcopy([stale]), None

        with patch('time.sleep') as asleep:
            self.assertIsNone(ob.read_with_retry(Stub(), lambda r: ob.equivalent(r, payload), tries=3, delay=2))
        self.assertEqual(len(calls), 3)
        self.assertEqual(asleep.call_count, 2)


if __name__ == '__main__':
    unittest.main()
