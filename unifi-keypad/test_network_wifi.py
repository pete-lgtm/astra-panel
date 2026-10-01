import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import network_wifi
import server


def response(rows):
    return {'meta': {'rc': 'ok'}, 'data': rows}, None


class WifiReaderTests(unittest.TestCase):
    def test_fixed_gets_and_exact_password_without_unrelated_secrets(self):
        sites = [{'name': 'default', 'desc': 'Home', 'secret': 'not-for-ui'}]
        networks = [{'name': '<Home & family>', 'security': 'wpapsk', 'enabled': True,
                     'x_passphrase': ' spaced password ', 'x_iapp_key': 'never-return-this', 'radius_secret': 'neither-this'}]
        with patch.object(network_wifi.LocalSession, 'request', side_effect=[response(sites), response(networks)]) as request:
            result = network_wifi.reveal('192.168.1.1', 'session-cookie', 'default')
        self.assertEqual([call.args[1:3] for call in request.call_args_list], [
            ('GET', '/proxy/network/api/self/sites'), ('GET', '/proxy/network/api/s/default/rest/wlanconf')])
        self.assertEqual(result['networks'][0]['password'], ' spaced password ')
        self.assertEqual(result['networks'][0]['name'], '<Home & family>')
        for secret in ('not-for-ui', 'never-return-this', 'neither-this', 'session-cookie'):
            self.assertNotIn(secret, json.dumps(result))

    def test_open_enterprise_missing_and_masked_passwords_are_distinct(self):
        cases = [('open', 'open', 'do-not-return'), ('wpaeap', 'enterprise', 'radius-key'),
                 ('wpapsk', 'unavailable', None), ('wpapsk', 'unavailable', '********'),
                 ('wpapsk', 'unavailable', '••••••••'), ('unknown', 'unavailable', 'unknown-secret')]
        for security, expected, value in cases:
            with self.subTest(security=security, expected=expected):
                row = network_wifi.normalize({'name': 'Example', 'security': security, 'x_passphrase': value})
                self.assertEqual(row['passwordState'], expected)
                self.assertIsNone(row['password'])
        row = network_wifi.normalize({'name': 'Hidden', 'security': 'wpapsk', 'enabled': False,
                                      'hide_ssid': True, 'x_passphrase': 'a' * 64})
        self.assertFalse(row['enabled'])
        self.assertTrue(row['hidden'])
        self.assertEqual(len(row['password']), 64)

    def test_invalid_sites_and_arbitrary_routes_do_not_make_requests(self):
        with patch.object(network_wifi.LocalSession, 'request') as request:
            for selected in (None, 3, '../other', 'default/../../admin', ''):
                with self.assertRaises(network_wifi.WifiError):
                    network_wifi.reveal('192.168.1.1', 'cookie', selected)
            with self.assertRaises(network_wifi.WifiError):
                network_wifi.WifiReader('host', 'cookie').get('/proxy/network/api/s/default/rest/admin')
            request.assert_not_called()

    def test_unknown_site_does_not_read_wlan_list(self):
        with patch.object(network_wifi.LocalSession, 'request', return_value=response([{'name': 'home'}])) as request:
            with self.assertRaisesRegex(network_wifi.WifiError, 'available to this account'):
                network_wifi.reveal('192.168.1.1', 'cookie', 'default')
            self.assertEqual(request.call_count, 1)

    def test_denials_malformed_and_errors_never_echo_secrets(self):
        cases = [ValueError('SECRET transport'), network_wifi.ConsoleRequestError(403),
                 ({'meta': {'rc': 'error', 'message': 'SECRET'}, 'data': []}, None),
                 response(['SECRET malformed']), response([{}] * 257)]
        for result in cases:
            kwargs = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
            with self.subTest(result=type(result)), patch.object(network_wifi.LocalSession, 'request', **kwargs):
                with self.assertRaises(network_wifi.WifiError) as caught:
                    network_wifi.reveal('192.168.1.1', 'cookie', 'default')
                self.assertNotIn('SECRET', str(caught.exception))


class WifiAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = server.LocalSession()
        self.session.host, self.session.cookie = '192.168.1.1', 'live-cookie'
        for name, value in [('PIN_FILE', Path(self.temp.name) / 'pin.json'), ('PIN', {}),
                            ('UNLOCKS', {}), ('LOCAL_SESSION', self.session)]:
            item = patch.object(server, name, value)
            item.start()
            self.addCleanup(item.stop)
        server.set_pin('739126')
        self.actor = server.check_pin('739126')
        server.UNLOCKS['grant'] = {'expires': server.time.monotonic() + 60, 'user': self.actor}
        self.payload = {'site': {'id': 'default', 'name': 'Home'}, 'networks': [
            {'name': 'Private SSID', 'password': 'private-wifi-secret', 'passwordState': 'available'}]}
        reader_patch = patch.object(server.network_wifi, 'reveal', return_value=self.payload)
        self.reader = reader_patch.start()
        self.addCleanup(reader_patch.stop)

    def reveal(self, **kwargs):
        return server.reveal_wifi_passwords(dict(unlock='grant', site='default', pin='739126', **kwargs))

    def test_each_read_needs_current_unlock_and_a_fresh_pin(self):
        for data in ({'pin': '739126', 'site': 'default'}, {'unlock': 'grant', 'site': 'default'},
                     {'unlock': 'grant', 'pin': '000000', 'site': 'default'}):
            with self.assertRaises(ValueError):
                server.reveal_wifi_passwords(data)
        self.reader.assert_not_called()
        self.assertEqual(self.reveal()['hideAfterSeconds'], 30)
        with self.assertRaises(ValueError):
            server.reveal_wifi_passwords({'unlock': 'grant', 'site': 'default'})
        self.assertEqual(self.reader.call_count, 1, 'A previous successful reveal grants no bypass')

    def test_regular_user_and_other_administrator_pin_cannot_reveal(self):
        store = server.account_store()
        for role, pin in (('user', '628451'), ('admin', '517362')):
            user = store.save_user(self.actor, {'name': role, 'role': role, 'pin': pin, 'confirmation': pin})['user']
            if role == 'user':
                server.UNLOCKS['other'] = {'expires': server.time.monotonic() + 60, 'user': user}
                with self.assertRaisesRegex(ValueError, 'administrator'):
                    server.reveal_wifi_passwords({'unlock': 'other', 'site': 'default', 'pin': '628451'})
            else:
                with self.assertRaisesRegex(ValueError, 'currently using'):
                    server.reveal_wifi_passwords({'unlock': 'grant', 'site': 'default', 'pin': pin})
        self.reader.assert_not_called()

    def test_wrong_pin_uses_shared_persistent_attempt_limit(self):
        for _ in range(5):
            with self.assertRaisesRegex(ValueError, 'Incorrect PIN'):
                server.reveal_wifi_passwords({'unlock': 'grant', 'site': 'default', 'pin': '000000'})
        with self.assertRaisesRegex(ValueError, 'Too many'):
            self.reveal()
        with self.assertRaisesRegex(ValueError, 'Too many'):
            server.check_pin('739126')
        self.reader.assert_not_called()

    def test_reads_outside_alarm_lock_and_activity_contains_no_credentials(self):
        def read(*args):
            self.assertTrue(server.LOCK.acquire(blocking=False))
            server.LOCK.release()
            return self.payload
        self.reader.side_effect = read
        data = {'unlock': 'grant', 'site': 'default', 'pin': '739126'}
        self.assertEqual(server.reveal_wifi_passwords(data)['networks'], self.payload['networks'])
        self.assertNotIn('pin', data)
        events = server.account_store().history()['events']
        event = next(item for item in events if item['action'] == 'wifi_passwords_viewed')
        self.assertEqual(event['user_id'], self.actor['id'])
        for secret in ('739126', 'private-wifi-secret', 'Private SSID', 'live-cookie'):
            self.assertNotIn(secret, json.dumps(events))
        self.assertEqual(self.session.cookie, 'live-cookie')

    def test_lock_login_change_expiry_and_user_change_during_read_discard_passwords(self):
        for change in ('lock', 'login', 'expiry', 'user'):
            with self.subTest(change=change):
                server.UNLOCKS['grant'] = {'expires': server.time.monotonic() + 60, 'user': self.actor}
                def read(*args):
                    if change == 'lock':
                        server.UNLOCKS.clear()
                    elif change == 'login':
                        self.session.clear()
                    elif change == 'expiry':
                        server.UNLOCKS['grant']['expires'] = 0
                    else:
                        server.account_store().save_user(self.actor, {'id': self.actor['id'], 'name': 'New name', 'role': 'admin'})
                    return self.payload
                self.reader.side_effect = read
                with self.assertRaises(ValueError):
                    self.reveal()
        self.assertFalse(any(event['action'] == 'wifi_passwords_viewed' for event in server.account_store().history()['events']))

    def test_network_failure_does_not_clear_live_alarm_session(self):
        self.reader.side_effect = network_wifi.WifiError('Wi-Fi unavailable.')
        with self.assertRaises(ValueError):
            self.reveal()
        self.assertEqual(self.session.cookie, 'live-cookie')
        self.assertIn('grant', server.UNLOCKS)

    def test_http_csrf_fresh_pin_and_no_store(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        port = httpd.server_address[1]
        for name, value in [('PORT', port), ('ORIGIN', 'http://127.0.0.1:%s' % port), ('TOKEN', 'test-csrf')]:
            item = patch.object(server, name, value)
            item.start()
            self.addCleanup(item.stop)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        def post(data, csrf='test-csrf'):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('POST', '/hub/wifi/reveal', json.dumps(data), {'Content-Type': 'application/json',
                         'Origin': server.ORIGIN, 'X-Keypad-Token': csrf, 'Cookie': 'keypad_settings=forged'})
            reply = conn.getresponse()
            result = reply.status, json.loads(reply.read()), reply.getheader('Cache-Control')
            conn.close()
            return result
        payload = {'unlock': 'grant', 'site': 'default', 'pin': '739126'}
        self.assertEqual(post(payload, 'wrong')[0], 403)
        self.assertEqual(post({'unlock': 'grant', 'site': 'default'})[0], 400)
        self.reader.assert_not_called()
        status, result, cache = post(payload)
        self.assertEqual(status, 200)
        self.assertEqual(cache, 'no-store')
        self.assertEqual(result['networks'], self.payload['networks'])


if __name__ == '__main__':
    unittest.main()
