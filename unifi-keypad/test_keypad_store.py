"""User permissions and attributed alarm history; all storage is temporary."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from keypad_store import KeypadStore, stamp
from pin_store import create_pin
from settings_access import SettingsAccess, SettingsLocked
from test_fixtures import legacy_site
import server


class UserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pin_file = Path(self.temp.name) / 'pin.json'
        create_pin(self.pin_file, '736291', '736291')
        self.store = KeypadStore(self.pin_file)
        self.owner = self.store.authenticate('736291')
        site = legacy_site()
        for name, value in [('SITE', site), ('WEBHOOKS', site['webhooks'])]:
            fixture = patch.object(server, name, value)
            fixture.start()
            self.addCleanup(fixture.stop)

    def add(self, name='Pat', pin='482615', role='user'):
        return self.store.save_user(self.owner, {'name': name, 'role': role, 'pin': pin, 'confirmation': pin})['user']

    def test_migrates_once_and_preserves_existing_pin(self):
        self.assertEqual(self.owner['role'], 'admin')
        self.assertEqual(self.owner['name'], 'Owner')
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        saved = self.pin_file.read_bytes()
        self.assertEqual(len(KeypadStore(self.pin_file).users()), 1)
        self.assertEqual(saved, self.pin_file.read_bytes())
        self.pin_file.unlink()
        self.assertEqual(self.store.authenticate('736291'), self.owner)
        self.assertNotIn('736291', self.store.path.read_bytes().decode(errors='replace'))
        public = json.dumps(self.store.users())
        for secret in ('salt', 'digest', '736291'):
            self.assertNotIn(secret, public)

    def test_regular_users_disarm_but_cannot_open_settings(self):
        pat = self.add()
        self.assertEqual(self.store.authenticate('482615'), pat)
        gate = SettingsAccess(self.pin_file, 18700)
        with self.assertRaisesRegex(ValueError, 'administrator'):
            gate.unlock('482615', {})
        with self.assertRaisesRegex(ValueError, 'administrator'):
            self.store.save_user(pat, {'name': 'Intruder', 'role': 'admin', 'pin': '3456', 'confirmation': '3456'})
        with patch.object(server, 'PIN_FILE', self.pin_file), patch.dict(server.UNLOCKS, {}, clear=True), patch.object(server, 'request_unifi') as send:
            server.UNLOCKS['pat'] = {'expires': server.time.monotonic()+60, 'user': pat}
            server.webhook_command('disarm', {'confirm': True, 'unlock': 'pat', 'user': self.owner})
            send.assert_called_once()
        event = self.store.history()['events'][0]
        self.assertEqual(event['user_id'], pat['id'])
        self.assertEqual(event['user_name'], 'Pat')
        self.assertEqual(event['status'], 'accepted')
        self.assertIsNone(event['confirmed_at'])

    def test_duplicate_pin_and_last_admin_are_rejected(self):
        self.add()
        for name, pin in [('Another', '482615'), ('Another', '736291')]:
            with self.assertRaisesRegex(ValueError, 'already assigned'):
                self.add(name, pin)
        with self.assertRaisesRegex(ValueError, 'administrator'):
            self.store.save_user(self.owner, {**{k:self.owner[k] for k in ('id','name','role')}, 'enabled': False})
        with self.assertRaisesRegex(ValueError, 'administrator'):
            self.store.save_user(self.owner, {'id': 'owner', 'name': 'Owner', 'role': 'user'})

    def test_update_revokes_both_kinds_of_grants_and_old_pin(self):
        pat = self.add(role='admin')
        gate = SettingsAccess(self.pin_file, 18700)
        _, cookie = gate.unlock('482615', {})
        headers = {'Cookie': cookie.split(';')[0]}
        result = self.store.save_user(self.owner, {'id': pat['id'], 'name': 'Pat', 'role': 'admin', 'pin': '928371', 'confirmation': '928371'})
        self.assertFalse(result['reauthenticate'])
        with self.assertRaises(SettingsLocked):
            gate.require(headers)
        with patch.object(server, 'PIN_FILE', self.pin_file), patch.dict(server.UNLOCKS, {}, clear=True), patch.object(server, 'request_unifi') as send:
            server.UNLOCKS['old'] = {'expires': server.time.monotonic()+60, 'user': pat}
            with self.assertRaisesRegex(ValueError, 'changed'):
                server.webhook_command('disarm', {'confirm': True, 'unlock': 'old'})
            send.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'Incorrect'):
            gate.unlock('482615', {})
        self.assertTrue(gate.unlock('928371', {})[0]['settingsUnlocked'])

    def test_disabling_user_keeps_history_and_prevents_pin_reuse(self):
        pat = self.add()
        self.store.start_command(pat, 'disarm')
        self.store.save_user(self.owner, {'id':pat['id'], 'name':'Pat', 'role':'user', 'enabled':False})
        with self.assertRaisesRegex(ValueError, 'Incorrect'):
            self.store.authenticate('482615')
        with self.assertRaisesRegex(ValueError, 'already assigned'):
            self.add('New Pat', '482615')
        self.assertTrue(any(e['user_id'] == pat['id'] and e['user_name'] == 'Pat' for e in self.store.history()['events']))

    def test_lockout_shared_across_installer_and_keypad(self):
        gate = SettingsAccess(self.pin_file, 18700)
        with patch('keypad_store.time.time', return_value=1000):
            for i in range(5):
                with self.assertRaises(ValueError):
                    (gate.verify if i%2 else self.store.authenticate)('0000')
            with self.assertRaisesRegex(ValueError, 'Wait one minute'):
                KeypadStore(self.pin_file).authenticate('736291')
        with patch('keypad_store.time.time', return_value=1061):
            self.assertEqual(self.store.authenticate('736291'), self.owner)

    def test_confirmation_requires_matching_new_protect_transition(self):
        with patch('keypad_store.time.time', return_value=1000):
            event = self.store.start_command(self.owner, 'disarm')
            self.store.command_result(event, 'accepted')
            self.store.confirm_commands('disarmed', stamp(999))
            self.store.confirm_commands('armed', stamp(1000))
            self.store.confirm_commands('disarmed', stamp(1010))
            self.assertEqual(self.store.history()['events'][0]['status'], 'accepted')
        with patch('keypad_store.time.time', return_value=1002):
            self.store.confirm_commands('disarmed', stamp(1001))
        row = self.store.history()['events'][0]
        self.assertEqual(row['status'], 'confirmed')
        self.assertEqual(row['confirmed_at'], stamp(1001))

    def test_uncertain_requests_and_restart_never_claim_success(self):
        with patch('keypad_store.time.time', return_value=1000):
            event=self.store.start_command(None,'arm');self.store.command_result(event,'accepted')
        with patch('keypad_store.time.time', return_value=1121):
            self.store.confirm_commands('unknown', None)
        self.assertEqual(self.store.history()['events'][0]['status'], 'unverified')
        self.assertEqual(self.store.history()['events'][0]['user_name'], 'Quick arm · no PIN')
        self.store.start_command(self.owner, 'disarm')
        KeypadStore(self.pin_file).recover()
        self.assertEqual(self.store.history()['events'][0]['status'], 'unverified')

    def test_failed_dispatch_is_logged_without_credentials(self):
        with patch.object(server, 'PIN_FILE', self.pin_file), patch.dict(server.UNLOCKS, {}, clear=True), patch.object(server, 'request_unifi', side_effect=ValueError('Secret details')):
            server.UNLOCKS['owner']={'expires':server.time.monotonic()+60,'user':self.owner}
            with self.assertRaises(ValueError):
                server.webhook_command('disarm',{'confirm':True,'unlock':'owner'})
        events=self.store.history()['events']
        self.assertEqual(events[0]['status'],'unverified')
        self.assertNotIn('Secret details',json.dumps(events))

    def test_history_pages_do_not_skip_events_and_preserve_names(self):
        for i in range(31):
            self.store.record(self.owner, 'user_updated', 'Update '+str(i))
        first=self.store.history();second=self.store.history(first['nextBefore'])
        self.assertEqual(len(first['events']),25);self.assertEqual(len(second['events']),6)
        self.assertEqual(len({e['id'] for e in first['events']+second['events']}),31)
        result=self.store.save_user(self.owner,{'id':'owner','name':'Installer','role':'admin'})
        self.assertTrue(result['reauthenticate'])
        self.assertEqual(self.store.history()['events'][0]['user_name'],'Owner')
        self.assertEqual(self.store.authenticate('736291')['name'],'Installer')


if __name__ == '__main__':
    unittest.main()
