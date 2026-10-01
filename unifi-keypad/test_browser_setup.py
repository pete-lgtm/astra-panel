"""Desktop setup uses a synthetic console and temporary files, never a real alarm."""
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import browser_setup
import credential_store
from device_config import load_site
from pin_store import create_pin
from settings_access import SettingsAccess
import server
from test_onboarding import Console


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.access = Mock(sessions={})
        self.pair = browser_setup.BrowserSetup(server.Handler, lambda: self.access, lambda: {},
                                               origin='https://setup.example.test:8767', clock=lambda: self.now)
        for method in ('available', 'start_listener', 'schedule_expiry'):
            item = patch.object(self.pair, method, return_value=True)
            item.start(); self.addCleanup(item.stop)
        self.addCleanup(self.pair.end)

    def test_code_and_qr_are_one_use_and_not_returned_in_status(self):
        result = self.pair.begin()
        self.assertEqual(result['url'], 'https://setup.example.test:8767')
        self.assertNotIn(self.pair.current['token'], json.dumps(self.pair.status()))
        self.assertNotIn(result['code'].replace(' ', ''), json.dumps(self.pair.status()))
        session = self.pair.claim({'code': result['code']})
        headers = {'Cookie': self.pair.cookie_name + '=' + session}
        self.assertTrue(self.pair.authorized(headers))
        with self.assertRaises(ValueError): self.pair.claim({'code': result['code']})
        self.assertFalse(self.pair.authorized({'Cookie': self.pair.cookie_name + '=forged'}))
        self.pair.end()
        self.assertFalse(self.pair.authorized(headers))

    def test_short_code_guessing_is_limited_and_pairing_expires(self):
        self.pair.begin()
        for _ in range(8):
            with self.assertRaises(ValueError): self.pair.claim({'code': 'incorrect'})
        self.assertEqual(self.pair.status()['state'], 'locked')
        code = self.pair.begin()['code']; self.now += 600
        with self.assertRaises(ValueError): self.pair.claim({'code': code})
        self.assertEqual(self.pair.status()['state'], 'expired')

    def test_new_code_revokes_old_session_and_expiry_cannot_be_extended(self):
        self.pair.begin(); old_token = self.pair.current['token']
        first = self.pair.claim({'token': old_token})
        self.now += 1799
        self.assertTrue(self.pair.authorized({'Cookie': self.pair.cookie_name + '=' + first}))
        self.now += 1
        self.assertFalse(self.pair.authorized({'Cookie': self.pair.cookie_name + '=' + first}))
        new_code = self.pair.begin()['code']
        with self.assertRaises(ValueError): self.pair.claim({'token': old_token})
        second = self.pair.claim({'code': new_code})
        self.assertNotEqual(first, second)
        self.pair.begin()
        self.assertFalse(self.pair.authorized({'Cookie': self.pair.cookie_name + '=' + second}))

    def test_unconfigured_and_public_listeners_are_unavailable(self):
        self.assertFalse(browser_setup.BrowserSetup(server.Handler, lambda: self.access, lambda: {}).available())
        for bind in ('0.0.0.0', '8.8.8.8', '::'):
            pair = browser_setup.BrowserSetup(server.Handler, lambda: self.access, lambda: {},
                                              'https://setup.example.test', bind, __file__, __file__)
            self.assertFalse(pair.available())


class BrowserHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.cert, cls.key = Path(cls.keys.name) / 'cert.pem', Path(cls.keys.name) / 'key.pem'
        config = Path(cls.keys.name) / 'openssl.conf'
        config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\nbasicConstraints=critical,CA:TRUE\n')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-config', str(config), '-keyout', str(cls.key), '-out', str(cls.cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.pin = self.root / '.keypad-pin.json'
        self.console = Console(); self.console.login = Mock(return_value={})
        self.gate = SettingsAccess(self.pin, 'local')
        values = {'ROOT': self.root, 'STATE_DIR': self.root, 'PIN_FILE': self.pin,
                  'CREDENTIAL_FILE': self.root / '.api-connection.json', 'PIN': {}, 'CONFIG': {},
                  'INSTALLER_STATE': {}, 'LOCAL_SESSION': self.console, 'SETTINGS_ACCESS': self.gate,
                  'SITE': {'name': 'Test home', 'doors': [], 'profile': '', 'webhooks': {}},
                  'read_status': Mock(return_value={'connected': True, 'state': 'Disarmed'}),
                  'apply_saved_site': Mock(side_effect=self.load_saved_site),
                  'coverage_status': Mock(return_value={'state': 'disarmed', 'fresh': True, 'doors': {}, 'verifiedDoors': []})}
        env = patch.dict('os.environ', {}, clear=True); env.start(); self.addCleanup(env.stop)
        for key, value in values.items():
            item = patch.object(server, key, value); item.start(); self.addCleanup(item.stop)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        self.origin = f'https://localhost:{self.port}'
        self.pair = browser_setup.BrowserSetup(server.Handler,
            lambda: SettingsAccess(self.pin, 'browser', lifetime=1800, secure=True),
            server.setup_session_info, self.origin, '127.0.0.1', str(self.cert), str(self.key))
        self.cookies = {}; self.csrf = ''
        self.addCleanup(self.close_pair)
        self.start = self.pair.begin()

    def close_pair(self):
        self.pair.end()
        if self.pair.closing: self.pair.closing.join(timeout=3)

    def load_saved_site(self):
        server.SITE = load_site(self.root / 'site.json')

    def request(self, path, body=None, headers=None, cookies=True, method=None):
        connection = http.client.HTTPSConnection('localhost', self.port,
            context=ssl.create_default_context(cafile=str(self.cert)), timeout=5)
        request_headers = {'Origin': self.origin, 'Content-Type': 'application/json', 'X-Keypad-Token': self.csrf}
        if cookies: request_headers['Cookie'] = '; '.join(key + '=' + value for key, value in self.cookies.items())
        request_headers.update(headers or {})
        connection.request(method or ('POST' if body is not None else 'GET'), path,
                           json.dumps(body) if body is not None else None, request_headers)
        response = connection.getresponse(); raw = response.read(); response_headers = response.getheaders()
        for key, value in response_headers:
            if key.lower() == 'set-cookie':
                jar = SimpleCookie(); jar.load(value)
                self.cookies.update({name: item.value for name, item in jar.items()})
        result = json.loads(raw) if response.getheader('Content-Type') == 'application/json' else raw
        status = response.status; connection.close()
        return status, result, response_headers

    def claim(self):
        code, result, headers = self.request('/claim', {'code': self.start['code']})
        self.assertEqual(code, 200)
        self.assertTrue(result['paired'])
        self.assertIn('Secure; HttpOnly; SameSite=Strict', str(headers))
        code, data, _ = self.request('/session'); self.assertEqual(code, 200)
        self.csrf = data['token']
        self.assertNotEqual(self.csrf, server.TOKEN)
        return data

    def test_https_pairing_origin_pin_csrf_and_route_boundaries(self):
        create_pin(self.pin, '927461', '927461')
        code, page, _ = self.request('/'); self.assertEqual(code, 200); self.assertIn(b'pair-code', page)
        for path in ('/session', '/setup', '/setup.js', '/site.json', '/.api-connection.json'):
            self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.request('/claim', {'code': self.start['code']}, {'Origin': 'https://wrong.test'})[0], 403)
        self.assertEqual(self.request('/claim', {'code': self.start['code']}, {'Host': 'wrong.test'})[0], 403)
        self.assertTrue(self.claim()['pinSet'])
        for path in ('/installer/context', '/entry/status', '/connect', '/status'):
            status, result, _ = self.request(path, {})
            self.assertEqual(status, 403, path); self.assertTrue(result['settingsLocked'])
        status, _, cookies = self.request('/settings/unlock', {'pin': '927461'})
        self.assertEqual(status, 200); self.assertIn('__Host-keypad_settings_browser', str(cookies))
        self.assertEqual(self.request('/installer/context', {})[0], 200)
        self.assertEqual(self.request('/installer/context', {}, {'X-Keypad-Token': server.TOKEN})[0], 401)
        self.assertEqual(self.request('/installer/context', {}, {'Origin': 'https://wrong.test'})[0], 403)
        for path in ('/arm/review', '/webhook/arm', '/webhook/disarm', '/hub/wifi/reveal', '/hub/network/apply',
                     '/settings/users', '/disconnect', '/browser-setup/start', '/phone-pair/start'):
            self.assertEqual(self.request(path, {})[0], 403, path)
        for path in ('/site.json', '/app.js', '/.unifi-session.json'):
            self.assertEqual(self.request(path)[0], 404)
        # Locking the touchscreen does not revoke this computer's PIN grant.
        self.gate.lock({})
        self.assertTrue(self.request('/settings/status', {})[1]['settingsUnlocked'])
        self.request('/settings/lock', {})
        self.assertEqual(self.request('/installer/context', {})[0], 403)
        self.assertFalse(self.console.writes)

    def test_complete_first_time_setup_persists_shared_state_and_finishes(self):
        self.assertFalse(self.claim()['apiConfigured'])
        code, body, _ = self.request('/connect', {'host': self.console.host, 'key': 'test-only-api-key'})
        self.assertEqual(code, 200, body)
        self.assertEqual(credential_store.load(server.CREDENTIAL_FILE)['key'], 'test-only-api-key')
        code, context, _ = self.request('/installer/login', {'host': self.console.host, 'username': 'test', 'password': 'test-only'})
        self.assertEqual(code, 200, context); self.assertEqual(len(context['sensors']), 3)
        code, result, _ = self.request('/installer/pin/create', {'pin': '927461', 'confirmation': '927461'})
        self.assertEqual(code, 200, result); self.assertTrue(result['settingsUnlocked'])
        self.assertFalse(self.gate.status({})['settingsUnlocked'])
        choices = {'name': 'Browser test home', 'profileName': 'Home alarm', 'profileId': '',
                   'sensorIds': ['sensor-1', 'sensor-2'], 'receivers': [self.console.users[0]['value']]}
        code, review, _ = self.request('/installer/review', choices); self.assertEqual(code, 200, review)
        server.INSTALLER_STATE['plan']['expires'] = 0
        code, stale, _ = self.request('/installer/apply', {'planId': review['planId']})
        self.assertEqual(code, 400); self.assertTrue(stale['reviewRequired'])
        self.assertFalse(self.console.writes, 'Expired review must not create or change rules')
        code, review, _ = self.request('/installer/review', choices); self.assertEqual(code, 200, review)
        code, saved, _ = self.request('/installer/apply', {'planId': review['planId']})
        self.assertEqual(code, 200, saved); self.assertTrue(saved['saved'])
        self.assertEqual(server.SITE['name'], 'Browser test home')
        self.assertEqual(server.SITE['doors'], ['Front', 'Back'])
        self.assertEqual(len(self.console.rules), 7)  # arm, arm_stay, arm_away, disarm, entry_alert, doors, panic
        self.assertEqual(self.console.profiles[0]['state'], 'disarmed')
        code, info, _ = self.request('/session')
        self.assertTrue(info['apiConfigured']); self.assertTrue(info['pinSet'])
        for secret in ('test-only-api-key', '927461', self.console.cookie):
            self.assertNotIn(secret, json.dumps(info))
        for path in browser_setup.ASSETS:
            self.assertEqual(self.request(path)[0], 200, path)
        old_cookie = self.cookies.copy()
        code, done, _ = self.request('/browser/end', {})
        self.assertEqual(code, 200); self.assertEqual(done['state'], 'complete')
        self.assertFalse(self.pair.authorized({'Cookie': '; '.join(k + '=' + v for k, v in old_cookie.items())}))
        self.assertEqual(credential_store.load(server.CREDENTIAL_FILE)['host'], self.console.host)
        self.start = self.pair.begin()  # Listener can be immediately reopened after Finish.
        self.assertEqual(self.request('/session')[0], 401)

    def test_remote_connection_cannot_switch_an_existing_home(self):
        self.claim()
        server.SITE['consoleHost'] = '192.168.0.1'
        self.assertEqual(self.request('/connect', {'host': '192.168.0.2', 'key': 'test'})[0], 400)
        server.read_status.assert_not_called()
        self.assertFalse(server.CREDENTIAL_FILE.exists())

    def test_idle_tls_connection_cannot_prevent_expiry_and_reopen(self):
        idle = socket.create_connection(('127.0.0.1', self.port), timeout=2)
        try:
            self.pair.end()
            self.start = self.pair.begin()
            self.assertEqual(self.request('/')[0], 200)
        finally:
            idle.close()


if __name__ == '__main__': unittest.main()


class NetworkAddressTests(unittest.TestCase):
    def test_auto_address_uses_private_route_without_sending_packets(self):
        with patch.object(browser_setup.socket, 'socket') as factory:
            route = factory.return_value.__enter__.return_value
            route.getsockname.return_value = ('192.168.0.21', 32123)
            self.assertEqual(browser_setup.lan_address(), '192.168.0.21')
            route.send.assert_not_called()
            route.sendto.assert_not_called()
            route.getsockname.return_value = ('8.8.8.8', 32123)
            self.assertEqual(browser_setup.lan_address(), '')
        with patch.object(browser_setup.socket, 'socket', side_effect=OSError('offline')):
            self.assertEqual(browser_setup.lan_address(), '')

    def test_no_loopback_or_guessed_url_is_advertised(self):
        pair = browser_setup.BrowserSetup(server.Handler, lambda: Mock(), lambda: {}, bind='auto')
        with patch.object(browser_setup, 'lan_address', return_value=''):
            self.assertEqual(pair.connection_info(), {'ipAddress': '', 'available': False, 'secureUrl': '', 'url': ''})
        with patch.object(browser_setup, 'lan_address', return_value='192.168.0.21'):
            info = pair.connection_info()
            self.assertEqual(info['ipAddress'], '192.168.0.21')
            self.assertEqual(info['url'], '')
            self.assertFalse(info['available'])
        for bind in ('0.0.0.0', '8.8.8.8', '169.254.169.254'):
            pair.bind = bind; pair.landing_port = 8080
            pair.start_landing()
            self.assertIsNone(pair.landing_server)


class LandingHTTPTests(unittest.TestCase):
    def setUp(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        self.pair = browser_setup.BrowserSetup(server.Handler, lambda: Mock(sessions={}), lambda: {},
            origin='https://setup.example.test:8767', bind='127.0.0.1', landing_port=port)
        self.pair.start_landing(); self.addCleanup(self.close)
        self.port = self.pair.landing_server.server_port

    def close(self):
        self.pair.landing_server.shutdown(); self.pair.landing_server.server_close()

    def request(self, path='/', method='GET', host=None):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        client.request(method, path, headers={'Host': host or f'127.0.0.1:{self.port}'})
        response = client.getresponse(); result = response.status, response.read().decode(), response.getheaders()
        client.close(); return result

    def test_landing_has_no_credentials_and_does_not_start_pairing(self):
        status, body, headers = self.request()
        self.assertEqual(status, 200)
        self.assertIn('not ready', body)
        self.assertNotIn('<form', body)
        self.assertIsNone(self.pair.current)
        with patch.object(self.pair, 'available', return_value=True):
            status, body, headers = self.request()
            self.assertIn('tap', body)
            self.pair.current = {'state': 'waiting', 'expires': self.pair.clock() + 100,
                                 'code': '87654321', 'token': 'secret-pair-token'}
            status, body, headers = self.request()
            self.assertIn('href="https://setup.example.test:8767"', body)
            self.assertNotIn('87654321', body); self.assertNotIn('secret-pair-token', body)
            self.assertFalse(any(name.lower() == 'set-cookie' for name, value in headers))
            self.pair.current['state'] = 'closed'
            self.assertNotIn('href=', self.request()[1])

    def test_http_rejects_credentials_private_routes_and_foreign_hosts(self):
        self.assertEqual(self.request(host='attacker.example')[0], 403)
        for path in ('/session', '/setup', '/connect', '/settings/unlock', '/browser-setup/start', '/.api-connection.json'):
            self.assertEqual(self.request(path)[0], 404)
            self.assertEqual(self.request(path, 'POST')[0], 405)
