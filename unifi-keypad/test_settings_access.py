import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pin_store import create_pin
from settings_access import SettingsAccess, SettingsLocked, protected_route
import server
import setup_lab


class SettingsAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'pin.json'
        create_pin(self.path, '752946', '752946')
        self.gate = SettingsAccess(self.path, 12345)

    def unlock(self):
        result, cookie = self.gate.unlock('752946', {})
        return result, {'Cookie': cookie.split(';')[0]}

    def test_unverified_and_forged_sessions_are_rejected(self):
        for headers in ({}, {'Cookie': 'keypad_settings_12345=forged'}):
            with self.assertRaises(SettingsLocked):
                self.gate.require(headers)
        self.assertFalse(self.gate.status({})['settingsUnlocked'])

    def test_unlock_expires_and_does_not_extend_on_read(self):
        with patch('settings_access.time.monotonic', return_value=100):
            result, headers = self.unlock()
        self.assertEqual(result['expiresIn'], 300)
        with patch('settings_access.time.monotonic', return_value=399):
            self.gate.require(headers)
        with patch('settings_access.time.monotonic', return_value=400):
            with self.assertRaises(SettingsLocked):
                self.gate.require(headers)

    def test_explicit_lock_revokes_cookie_and_restart_invalidates_it(self):
        result, headers = self.unlock()
        self.assertNotIn('752946', json.dumps(result))
        self.gate.require(headers)
        with self.assertRaises(SettingsLocked):
            SettingsAccess(self.path, 12345).require(headers)
        self.assertIn('Max-Age=0', self.gate.lock(headers))
        with self.assertRaises(SettingsLocked):
            self.gate.require(headers)

    def test_wrong_pins_lock_out_even_a_correct_pin_temporarily(self):
        with patch('keypad_store.time.time', return_value=100):
            for _ in range(5):
                with self.assertRaisesRegex(ValueError, 'Incorrect PIN'):
                    self.gate.unlock('0000', {})
            with self.assertRaisesRegex(ValueError, 'Wait one minute'):
                self.unlock()
        with patch('keypad_store.time.time', return_value=161):
            self.unlock()

    def test_initial_setup_is_allowed_but_corrupt_pin_fails_closed(self):
        self.path.unlink()
        self.gate.require({})
        self.path.write_text('{broken')
        with self.assertRaises(ValueError):
            self.gate.require({})


class SettingsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'pin.json'
        create_pin(self.path, '752946', '752946')

    def start(self, module):
        httpd = module.ThreadingHTTPServer(('127.0.0.1', 0), module.Handler)
        port = httpd.server_address[1]
        gate = SettingsAccess(self.path, port)
        for name, value in [('PORT', port), ('ORIGIN', 'http://127.0.0.1:%s' % port),
                            ('TOKEN', 'test-csrf'), ('SETTINGS_ACCESS', gate)]:
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return port, gate

    def post(self, port, path, body=None, cookie=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        headers = {'Origin': 'http://127.0.0.1:%s' % port, 'X-Keypad-Token': 'test-csrf',
                   'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie.split(';')[0]
        connection.request('POST', path, json.dumps(body or {}), headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read()), response.getheader('Set-Cookie')
        connection.close()
        return result

    def check_closed_routes(self, module):
        port, gate = self.start(module)
        routes = ['/installer/context', '/installer/login', '/installer/review', '/installer/apply',
                  '/installer/pin/create', '/local/login', '/local/logout', '/local/status',
                  '/connect', '/disconnect', '/entry/enable', '/diagnostics', '/pin/setup', '/arm', '/disarm',
                  '/settings/users', '/settings/users/save', '/settings/activity', '/settings/sensors',
                  '/settings/sensors/review', '/settings/sensors/apply', '/settings/sensors/enable', '/settings/summary', '/settings/panel/save']
        if module is server:
            routes += ['/browser-setup/start', '/browser-setup/status', '/browser-setup/cancel']
        if module is setup_lab:
            routes += ['/login', '/inspect', '/result', '/probe/run', '/probe/cleanup', '/logout']
        for route in routes:
            self.assertTrue(protected_route(route))
            status, data, _ = self.post(port, route)
            self.assertEqual(status, 403, route)
            self.assertTrue(data['settingsLocked'], route)
        status, data, _ = self.post(port, '/settings/unlock', {'pin': '0000'})
        self.assertEqual(status, 400)
        self.assertEqual(data['error'], 'Incorrect PIN.')
        status, data, cookie = self.post(port, '/settings/unlock', {'pin': '752946'})
        self.assertEqual(status, 200)
        self.assertTrue(data['settingsUnlocked'])
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertTrue(self.post(port, '/settings/status', cookie=cookie)[1]['settingsUnlocked'])
        gate.sessions.clear()
        self.assertEqual(self.post(port, '/installer/context', cookie=cookie)[0], 403)

    def test_main_settings_and_installer_routes_are_enforced(self):
        with patch.object(server, 'request_unifi') as unifi:
            self.check_closed_routes(server)
            unifi.assert_not_called()

    def test_standalone_installer_and_legacy_routes_are_enforced(self):
        self.check_closed_routes(setup_lab)

    def test_valid_settings_access_allows_config_but_not_disarm(self):
        port, _ = self.start(server)
        _, _, cookie = self.post(port, '/settings/unlock', {'pin': '752946'})
        with patch.object(server.LOCAL_SESSION, 'profiles', return_value={'localConnected': True}) as profiles:
            self.assertEqual(self.post(port, '/local/status', cookie=cookie)[0], 200)
            profiles.assert_called_once()
        with patch.object(server, 'request_unifi') as send:
            code, _, _ = self.post(port, '/webhook/disarm', {'confirm': True}, cookie)
            self.assertEqual(code, 400)
            send.assert_not_called()
        self.post(port, '/settings/lock', cookie=cookie)
        self.assertEqual(self.post(port, '/local/status', cookie=cookie)[0], 403)

    def test_alarm_observation_stays_available_when_settings_are_locked(self):
        port, _ = self.start(server)
        with patch.object(server.ENTRY, 'snapshot', return_value={'state': 'disarmed'}):
            self.assertEqual(self.post(port, '/entry/status')[1]['state'], 'disarmed')

    def test_panel_timeout_is_saved_and_hub_interaction_renews_only_live_grants(self):
        from keypad_store import KeypadStore
        port, _ = self.start(server)
        with patch.object(server, 'PIN_FILE', self.path), patch.object(server, 'UNLOCKS', {}):
            _, default, _ = self.post(port, '/unlock', {'pin': '752946'})
            self.assertEqual(default['expiresIn'], 60)
            _, _, cookie = self.post(port, '/settings/unlock', {'pin': '752946'})
            status, saved, _ = self.post(port, '/settings/panel/save', {'autoLockSeconds': 180}, cookie)
            self.assertEqual((status, saved), (200, {'autoLockSeconds': 180}))
            self.assertEqual(KeypadStore(self.path).panel_settings()['autoLockSeconds'], 180,
                             'A new store instance must reload the persisted preference')
            for value in (None, True, 0, 30, 61, 600, '180'):
                self.assertEqual(self.post(port, '/settings/panel/save', {'autoLockSeconds': value}, cookie)[0], 400)
            with patch.object(server.time, 'monotonic', return_value=1000):
                _, unlocked, _ = self.post(port, '/unlock', {'pin': '752946'})
            grant = unlocked['unlock']
            self.assertEqual(unlocked['expiresIn'], 180)
            with patch.object(server.time, 'monotonic', return_value=1100):
                server.unlocked_user(grant)
                self.assertEqual(server.UNLOCKS[grant]['expires'], 1180, 'Background reads must not extend access')
                status, refreshed, _ = self.post(port, '/hub/activity', {'unlock': grant})
                self.assertEqual((status, refreshed['expiresIn']), (200, 180))
                self.assertEqual(server.UNLOCKS[grant]['expires'], 1280)
            with patch.object(server.time, 'monotonic', return_value=1280):
                self.assertEqual(self.post(port, '/hub/activity', {'unlock': grant})[0], 400,
                                 'An expired grant must require the PIN, not revive itself')
            self.assertEqual(self.post(port, '/hub/activity', {'unlock': 'forged'})[0], 400)
            _, unlocked, _ = self.post(port, '/unlock', {'pin': '752946'})
            self.post(port, '/lock', {'unlock': unlocked['unlock']})
            self.assertEqual(self.post(port, '/hub/activity', {'unlock': unlocked['unlock']})[0], 400)

    def test_regular_user_cannot_change_panel_timeout_and_pin_change_revokes_renewal(self):
        from keypad_store import KeypadStore
        port, _ = self.start(server)
        with patch.object(server, 'PIN_FILE', self.path), patch.object(server, 'UNLOCKS', {}):
            store = KeypadStore(self.path)
            owner = store.authenticate('752946')
            person = store.save_user(owner, {'name': 'Resident', 'role': 'user', 'pin': '593826', 'confirmation': '593826'})['user']
            with self.assertRaisesRegex(ValueError, 'administrator'):
                store.save_panel_settings(person, {'autoLockSeconds': 120})
            _, unlocked, _ = self.post(port, '/unlock', {'pin': '593826'})
            store.save_user(owner, {'id': person['id'], 'name': 'Resident', 'role': 'user', 'enabled': False})
            self.assertEqual(self.post(port, '/hub/activity', {'unlock': unlocked['unlock']})[0], 400)

    def test_user_management_requires_admin_and_returns_no_pin_verifier(self):
        port, _ = self.start(server)
        with patch.object(server, 'PIN_FILE', self.path):
            _, _, cookie = self.post(port, '/settings/unlock', {'pin': '752946'})
            code, user, _ = self.post(port, '/settings/users/save', {'name': 'Resident', 'role': 'user', 'pin': '593826', 'confirmation': '593826'}, cookie)
            self.assertEqual(code, 200)
            code, listing, _ = self.post(port, '/settings/users', cookie=cookie)
            self.assertEqual(code, 200)
            self.assertEqual(len(listing['users']), 2)
            for field in ('salt', 'digest', '593826'):
                self.assertNotIn(field, json.dumps(listing))
            code, _, _ = self.post(port, '/settings/unlock', {'pin': '593826'})
            self.assertEqual(code, 400)
            code, _, _ = self.post(port, '/unlock', {'pin': '593826'})
            self.assertEqual(code, 200)
            server.UNLOCKS.clear()

    def test_activation_requires_every_selected_door_checked(self):
        from live_entry import EntryObserver
        observer = EntryObserver()
        observer.ingest('disarmed', [], {})
        observer.verified_event = True
        observer.verified_doors = {'Front'}
        observer.doors = {name: {'connected': True, 'position': 'closed'} for name in ('Front', 'Back')}
        port, _ = self.start(server)
        _, _, cookie = self.post(port, '/settings/unlock', {'pin': '752946'})
        site = {'version': 2, 'doors': ['Front', 'Back']}
        with patch.object(server, 'PIN_FILE', self.path), patch.object(server, 'ENTRY', observer), patch.object(server, 'ENTRY_DOORS', {'Front', 'Back'}), patch.object(server, 'SITE', site), patch.object(server, 'apply_saved_site'), patch.object(server.sensor_settings, 'verify_for_activation', return_value=site), patch.object(server, 'request_unifi') as commands:
            code, _, _ = self.post(port, '/settings/sensors/enable', cookie=cookie)
            self.assertEqual(code, 400)
            self.assertFalse(observer.enabled)
            observer.verified_doors.add('Back')
            code, result, _ = self.post(port, '/settings/sensors/enable', cookie=cookie)
            self.assertEqual(code, 200)
            self.assertTrue(result['liveDelayEnabled'])
            commands.assert_not_called()

    def test_new_pin_during_first_setup_opens_only_the_creating_session(self):
        self.path.unlink()
        port, gate = self.start(server)
        def create(path, body, session, root, state):
            create_pin(self.path, body['pin'], body['confirmation'])
            return {'pinSet': True}
        with patch.object(server.onboarding, 'handle', side_effect=create), patch.object(server, 'PIN_FILE', self.path), patch.object(server, 'PIN', {}):
            code, data, cookie = self.post(port, '/installer/pin/create', {'pin': '752946', 'confirmation': '752946'})
        self.assertEqual(code, 200)
        self.assertTrue(data['settingsUnlocked'])
        gate.require({'Cookie': cookie.split(';')[0]})
        with self.assertRaises(SettingsLocked):
            gate.require({})


if __name__ == '__main__':
    unittest.main()
