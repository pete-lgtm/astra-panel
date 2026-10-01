import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import arm_coverage
import door_policy
import onboarding as ob
import protection_state
import sensor_settings
import server
from device_config import load_site, selected_sensors
from live_entry import EntryObserver
from pin_store import create_pin
from test_onboarding import Console


class DoorPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict('os.environ', {}, clear=True); environment.start(); self.addCleanup(environment.stop)
        create_pin(self.root / '.keypad-pin.json', '829316', '829316')
        self.console = Console(); self.console.generation = 1
        state = {}
        choices = {'name': 'Test', 'profileName': 'Home', 'profileId': '', 'sensorIds': ['sensor-1', 'sensor-2'], 'receivers': [self.console.users[0]['value']]}
        plan = ob.review(self.console, choices, self.root, state)
        ob.apply(self.console, {'planId': plan['planId']}, self.root, state)
        self.site = load_site(self.root / 'site.json')
        self.observer = EntryObserver()
        self.observer.ingest('disarmed', [], {})
        for sensor in self.console.sensors[:2]:
            self.observer.sensor_update(sensor)
        self.observer.verified_doors = {'Front'}
        self.observer.verified_event = True
        self.posts = []
        def api(method, path):
            if method == 'GET' and path == 'sensors':
                return copy.deepcopy(self.console.sensors)
            self.posts.append((method, path))
            return {}
        for p in (patch.object(server, 'ROOT', self.root), patch.object(server, 'PIN_FILE', self.root / '.keypad-pin.json'),
                  patch.object(server, 'SITE', self.site), patch.object(server, 'ENTRY', self.observer),
                  patch.object(server, 'ENTRY_DOORS', set(self.site['doors'])), patch.object(server, 'WEBHOOKS', self.site['webhooks']),
                  patch.object(server, 'CONFIG', {'host': self.console.host}), patch.object(server, 'LOCAL_SESSION', self.console),
                  patch.object(server, 'EXIT_UNTIL', 0), patch.object(server, 'ARM_PLANS', {}), patch.object(server, 'UNLOCKS', {}),
                  patch.object(server, 'STATUS_SENSORS', []), patch.object(server, 'request_unifi', side_effect=api)):
            p.start(); self.addCleanup(p.stop)
        self.grant()

    def grant(self):
        actor = server.account_store().authenticate('829316')
        server.UNLOCKS['test'] = {'user': actor, 'expires': time.monotonic() + 60}

    def review(self):
        self.observer.last_ok = self.observer.clock()
        return server.review_arm({'unlock': 'test'})

    def arm(self, plan, **kwargs):
        return server.webhook_command('arm', {'confirm': True, 'unlock': 'test', 'planId': plan['planId'], **kwargs})

    def test_unchecked_door_can_be_bypassed_without_losing_other_coverage(self):
        plan = self.review()
        self.assertTrue(plan['canReview']); self.assertTrue(plan['needsConfirmation'])
        self.assertEqual(plan['bypassed'][0]['code'], 'unchecked')
        self.arm(plan, confirmBypass=True)
        self.assertEqual(len(self.posts), 1)
        rule = next(r for r in self.console.rules if r['id'] == self.site['ruleIds']['doors'])
        self.assertEqual(rule['scope']['data']['scope_all_entry_sensors'], ['001122334455'])
        self.assertEqual(self.observer.active_names, {'Front'})
        self.assertEqual(self.observer.verified_doors, {'Front'})
        self.assertTrue(self.observer.enabled)
        self.assertEqual(arm_coverage.saved(self.root, self.site)['phase'], 'sent')

    def test_unreachable_and_missing_doors_do_not_destroy_fresh_profile_state(self):
        self.console.sensors = [s for s in self.console.sensors if s['id'] != 'sensor-2']
        rows = selected_sensors(self.site, self.console.sensors, allow_missing=True)
        for row in rows:
            self.observer.sensor_update(row)
        self.assertTrue(self.observer.snapshot()['fresh'])
        plan = self.review()
        self.assertEqual(plan['bypassed'][0]['code'], 'unreachable')
        self.arm(plan, confirmBypass=True)
        self.assertEqual(len(self.posts), 1)

    def test_bypass_requires_explicit_consent_and_valid_pin_review(self):
        count = len(self.console.writes)
        plan = self.review()
        with self.assertRaisesRegex(ValueError, 'Confirm the listed'):
            self.arm(plan)
        self.assertFalse(self.posts); self.assertEqual(len(self.console.writes), count)
        with self.assertRaisesRegex(ValueError, 'Keypad locked'):
            self.review()

    def test_changed_health_or_configuration_and_expired_review_do_not_send(self):
        for change in ('door', 'bypass', 'rule', 'expired', 'login'):
            with self.subTest(change=change):
                self.grant(); plan = self.review()
                if change == 'door': self.console.sensors[0]['isOpened'] = True
                if change == 'bypass': self.console.sensors[1]['state'] = 'DISCONNECTED'
                if change == 'rule': self.console.profiles[0]['activation_delay'] = 0
                if change == 'expired': server.ARM_PLANS[plan['planId']]['expires'] = 0
                if change == 'login': self.console.generation += 1
                with self.assertRaises(ValueError): self.arm(plan, confirmBypass=True)
                self.assertFalse(self.posts)
                self.console.sensors[0]['isOpened'] = False
                self.console.sensors[1]['state'] = 'CONNECTED'
                self.console.profiles[0]['activation_delay'] = 60

    def test_unknown_alarm_state_and_no_available_alarm_doors_still_block(self):
        self.observer.fail('No console state')
        coverage = door_policy.assess(self.site, self.observer.snapshot(), {})
        self.assertFalse(coverage['bypassed'], 'A signed-out console must not label every door unreachable')
        with self.assertRaises(ValueError): self.review()
        self.observer.ingest('disarmed', [], {})
        self.observer.verified_doors.clear()
        with self.assertRaises(ValueError): self.review()
        self.assertFalse(self.posts)

    def test_twenty_day_mute_is_scoped_to_issue_and_never_hides_bypass(self):
        coverage = door_policy.assess(self.site, self.observer.snapshot(), {}, now=1000)
        door_policy.mute(self.root, self.site, coverage['bypassed'], now=1000)
        prefs = door_policy.read(self.root, self.site)
        quiet = door_policy.assess(self.site, self.observer.snapshot(), prefs, now=1001)
        self.assertFalse(quiet['needsConfirmation']); self.assertEqual(len(quiet['bypassed']), 1)
        expired = door_policy.assess(self.site, self.observer.snapshot(), prefs, now=1000 + door_policy.MUTE_SECONDS)
        self.assertTrue(expired['needsConfirmation'])
        self.observer.doors['Back']['connected'] = False
        self.assertTrue(door_policy.assess(self.site, self.observer.snapshot(), prefs, now=1002)['needsConfirmation'])
        self.observer.doors['Back']['connected'] = True
        door_policy.clear_mutes(self.root, self.site)
        self.assertTrue(door_policy.assess(self.site, self.observer.snapshot(), door_policy.read(self.root, self.site))['needsConfirmation'])

    def test_mute_days_parameter_controls_expiry(self):
        coverage = door_policy.assess(self.site, self.observer.snapshot(), {}, now=1000)
        door_policy.mute(self.root, self.site, coverage['bypassed'], days=7, now=1000)
        prefs = door_policy.read(self.root, self.site)
        until = prefs['mutes'][coverage['bypassed'][0]['id']]['until']
        self.assertEqual(until, 1000 + 7 * 24 * 60 * 60)
        quiet = door_policy.assess(self.site, self.observer.snapshot(), prefs, now=1000 + 7 * 24 * 60 * 60 - 1)
        self.assertFalse(quiet['needsConfirmation'])
        back = door_policy.assess(self.site, self.observer.snapshot(), prefs, now=1000 + 7 * 24 * 60 * 60)
        self.assertTrue(back['needsConfirmation'])

    def test_thirty_day_mute_is_honored_but_longer_is_not(self):
        coverage = door_policy.assess(self.site, self.observer.snapshot(), {}, now=1000)
        door_policy.mute(self.root, self.site, coverage['bypassed'], days=30, now=1000)
        prefs = door_policy.read(self.root, self.site)
        row_id = coverage['bypassed'][0]['id']
        self.assertEqual(prefs['mutes'][row_id]['until'], 1000 + 30 * 24 * 60 * 60)
        self.assertFalse(door_policy.assess(self.site, self.observer.snapshot(), prefs, now=1000 + 29 * 24 * 60 * 60)['needsConfirmation'])
        prefs['mutes'][row_id]['until'] = 1000 + 31 * 24 * 60 * 60
        self.assertTrue(door_policy.assess(self.site, self.observer.snapshot(), prefs, now=1001)['needsConfirmation'],
                        'A forged over-long mute must not silence the reminder')

    def test_mute_bypass_reminders_endpoint(self):
        self.observer.last_ok = self.observer.clock()
        with self.assertRaisesRegex(ValueError, 'Keypad locked'):
            server.mute_bypass_reminders({'days': 7})
        with self.assertRaisesRegex(ValueError, 'Choose 1, 7, or 30 days'):
            server.mute_bypass_reminders({'unlock': 'test', 'days': 5})
        result = server.mute_bypass_reminders({'unlock': 'test', 'days': 7})
        self.assertEqual(result['mutedDays'], 7)
        self.assertEqual(len(result['muted']), 1)
        events = server.account_store().history()['events']
        self.assertTrue(any(e['action'] == 'bypass_reminders_muted' and '7 days' in e['detail'] for e in events))
        coverage = door_policy.assess(self.site, self.observer.snapshot(), door_policy.read(self.root, self.site))
        self.assertFalse(coverage['needsConfirmation'])
        self.observer.verified_doors.add('Back')
        with self.assertRaisesRegex(ValueError, 'nothing to dismiss'):
            server.mute_bypass_reminders({'unlock': 'test', 'days': 1})
        self.assertEqual(door_policy.path(self.root).stat().st_mode & 0o777, 0o600)

    def test_muted_repeat_arm_still_checks_rules_and_records_bypass(self):
        plan = self.review(); self.arm(plan, confirmBypass=True, muteBypass=True)
        self.observer.ingest('disarmed', [], {}); server.EXIT_UNTIL = 0
        arm_coverage.restore_scope(self.console, self.site, self.root)
        self.grant(); plan = self.review()
        self.assertFalse(plan['needsConfirmation']); self.assertEqual(len(plan['bypassed']), 1)
        self.arm(plan)
        self.assertEqual(len(self.posts), 2)
        events = server.account_store().history()['events']
        self.assertEqual(sum(e['action'] == 'doors_bypassed' for e in events), 2)

    def test_scope_write_failure_never_sends_arm_and_can_restore(self):
        plan = self.review()
        self.console.lie_at = len(self.console.writes) + 1
        with self.assertRaisesRegex(ValueError, 'verify the bypass'):
            self.arm(plan, confirmBypass=True)
        self.assertFalse(self.posts)
        self.assertEqual(arm_coverage.saved(self.root, self.site)['phase'], 'prepared')
        arm_coverage.restore_scope(self.console, self.site, self.root)
        self.assertIsNone(arm_coverage.saved(self.root, self.site))

    def test_scope_stays_fixed_if_bypassed_door_recovers_and_restores_after_disarm(self):
        plan = self.review(); self.arm(plan, confirmBypass=True)
        self.observer.verified_doors.add('Back')
        self.assertEqual(self.observer.active_names, {'Front'})
        server.EXIT_UNTIL = 0
        server.restore_protection_once()
        self.assertIsNone(self.observer.active_names)
        self.assertFalse(self.observer.bypassed)
        rule = next(r for r in self.console.rules if r['id'] == self.site['ruleIds']['doors'])
        self.assertEqual(set(rule['scope']['data']['scope_all_entry_sensors']), {'001122334455', '001122334466'})

    def test_restart_recovers_only_verified_active_scope(self):
        plan = self.review(); self.arm(plan, confirmBypass=True)
        replacement = EntryObserver(); replacement.ingest('armed', [], {})
        self.console.profiles[0]['state'] = 'armed'
        with patch.object(server, 'ENTRY', replacement):
            server.restore_protection_once()
            self.assertTrue(replacement.enabled)
            self.assertEqual(replacement.active_names, {'Front'})
            self.assertEqual(replacement.bypassed[0]['name'], 'Back')

    def test_status_only_and_permanent_bypass_remove_native_alarm_scope(self):
        state = {}
        plan = sensor_settings.review(self.console, {'sensorIds': ['sensor-1'], 'modes': {
            'sensor-1': 'alarm', 'sensor-2': 'status', 'sensor-bad': 'bypass'}}, self.root, state)
        self.assertEqual(plan['statusOnly'], ['Back'])
        self.assertEqual(plan['bypassed'], ['Faulty'])
        sensor_settings.apply(self.console, {'planId': plan['planId']}, self.root, state)
        rows = sensor_settings.discover(self.console, self.root)
        self.assertEqual({s['id']: s['mode'] for s in rows['sensors']}, {'sensor-1':'alarm', 'sensor-2':'status', 'sensor-bad':'bypass'})
        site = load_site(self.root / 'site.json')
        self.assertEqual(site['doors'], ['Front'])
        native = next(r for r in self.console.rules if r['id'] == site['ruleIds']['doors'])
        self.assertEqual(native['scope']['data']['scope_all_entry_sensors'], ['001122334455'])
        self.assertFalse(self.posts)

    def test_bypassed_events_cannot_start_or_reset_entry_timer(self):
        clock = [1000.0]
        observer = EntryObserver(clock=lambda: clock[0], wall=lambda: clock[0])
        observer.active_names = {'Front'}; observer.enabled = True
        observer.ingest('armed', [], {'one':'Front','two':'Back'})
        clock[0] += 1
        observer.ingest('armed', [{'id':'a','type':'sensorOpened','sensor':'two','start':clock[0]*1000}], {'one':'Front','two':'Back'})
        self.assertIsNone(observer.deadline)
        observer.sensor_update({'name':'Back','isOpened':False,'openStatusChangedAt':clock[0]*1000})
        clock[0] += 1
        observer.sensor_update({'name':'Back','isOpened':True,'openStatusChangedAt':clock[0]*1000})
        self.assertIsNone(observer.deadline)
        observer.ingest('armed', [{'id':'b','type':'sensorOpened','sensor':'one','start':clock[0]*1000}], {'one':'Front','two':'Back'})
        self.assertEqual(observer.snapshot()['remaining'], 60)
        clock[0] += 60
        output = []
        observer.dispatch(lambda: output.append('alert'))
        self.assertEqual(output, ['alert'])
        observer.ingest('disarmed', [], {})
        self.assertIsNone(observer.deadline)


if __name__ == '__main__':
    unittest.main()
