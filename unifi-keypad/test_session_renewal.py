"""Exercise the real login/renewal transport using a synthetic console."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from local_auth import LocalSession, ConsoleRequestError, cookie_expiry
import session_store


class SessionRenewalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.unifi-session.json'
        self.host = '192.168.0.1'
        self.wall, self.tick = 1_900_000_000, 1000
        self.connection = MagicMock()
        for mocker in (patch('local_auth.time.time', side_effect=lambda: self.wall),
                       patch('local_auth.time.monotonic', side_effect=lambda: self.tick),
                       patch.dict('os.environ', {'UNIFI_CERT_SHA256': ''}),
                       patch('local_auth.http.client.HTTPSConnection', return_value=self.connection)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def token(self, expires=None, csrf='test-csrf'):
        claims = {'exp': expires or self.wall + 7200, 'csrfToken': csrf}
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        return 'TOKEN=synthetic.' + encoded + '.signature'

    def response(self, status=200, body=None, cookie=None, expires=None):
        response = MagicMock()
        response.status = status
        response.read.return_value = json.dumps({} if body is None else body).encode()
        headers = []
        if cookie:
            headers.append(('Set-Cookie', cookie + '; Secure; HttpOnly; SameSite=Strict; Partitioned'))
        if expires is not None:
            headers.append(('X-Token-Expire-Time', str(int(expires * 1000))))
        response.getheaders.return_value = headers
        return response

    def queue(self, *responses):
        self.connection.getresponse.side_effect = responses
        self.connection.request.reset_mock()

    def login(self, lifetime=7200):
        client = LocalSession(self.path)
        token = self.token(self.wall + lifetime)
        self.queue(self.response(cookie=token, expires=self.wall + lifetime), self.response(body=[]))
        client.login(self.host, 'synthetic-user', 'not-saved-password')
        return client

    def advance(self, seconds):
        self.wall += seconds
        self.tick += seconds

    def paths(self):
        return [call.args[:2] for call in self.connection.request.call_args_list]

    def test_remembered_login_and_expiry_schedule_survive_restart(self):
        client = self.login()
        payload = json.loads(self.connection.request.call_args_list[0].kwargs['body'])
        self.assertIs(payload['rememberMe'], True)
        self.assertEqual(client.expires_at, self.wall + 7200)
        self.assertEqual(client._refresh_at, self.tick + 3600)
        restarted = LocalSession(self.path)
        self.assertTrue(restarted.restore(self.host))
        self.assertEqual(restarted._refresh_at, client._refresh_at)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('not-saved-password', self.path.read_text())
        self.assertNotIn('synthetic-user', self.path.read_text())

    def test_renewal_uses_cookie_csrf_and_no_credentials_then_saves_immediately(self):
        client = self.login(lifetime=3700)
        previous, generation = client.cookie, client.generation
        self.advance(101)
        rotated = self.token(self.wall + 86400, 'rotated-csrf')
        self.queue(self.response(cookie=rotated, expires=self.wall + 86400))
        self.assertTrue(client.refresh_if_due())
        call = self.connection.request.call_args
        self.assertEqual(call.args[:2], ('POST', '/api/auth/refresh'))
        self.assertIsNone(call.kwargs['body'])
        self.assertEqual(call.kwargs['headers']['Cookie'], previous)
        self.assertEqual(call.kwargs['headers']['X-CSRF-Token'], 'test-csrf')
        self.assertEqual(client.generation, generation)
        self.assertEqual(session_store.load(self.path)['cookie'], rotated)
        restarted = LocalSession(self.path)
        self.assertTrue(restarted.restore(self.host))
        self.assertEqual(restarted.expires_at, self.wall + 86400)

    def test_due_refresh_precedes_read_and_read_uses_rotated_token(self):
        client = self.login(lifetime=3700)
        self.advance(101)
        rotated = self.token(csrf='next')
        self.queue(self.response(cookie=rotated), self.response(body=[{'state': 'disarmed'}]))
        result = client.profiles()
        self.assertEqual(self.paths(), [('POST', '/api/auth/refresh'), ('GET', '/api/v2/alarms/profiles')])
        self.assertEqual(self.connection.request.call_args.kwargs['headers']['Cookie'], rotated)
        self.assertEqual(result['profiles'][0]['state'], 'disarmed')
        self.assertNotIn(rotated, json.dumps(result))

    def test_early_401_renews_and_retries_only_the_read_once(self):
        client = self.login()
        rotated = self.token(csrf='next')
        self.queue(self.response(401), self.response(cookie=rotated), self.response(body=[]))
        self.assertTrue(client.profiles()['localConnected'])
        self.assertEqual(self.paths(), [('GET', '/api/v2/alarms/profiles'), ('POST', '/api/auth/refresh'),
                                       ('GET', '/api/v2/alarms/profiles')])
        self.assertEqual(session_store.load(self.path)['cookie'], rotated)

    def test_repeated_401_does_not_loop_or_storm_refresh_endpoint(self):
        client = self.login()
        self.queue(self.response(401), self.response(cookie=self.token(csrf='next')), self.response(401))
        with self.assertRaises(ConsoleRequestError):
            client.profiles()
        self.assertEqual(len(self.paths()), 3)
        self.assertTrue(self.path.exists())
        self.queue(self.response(401))
        with self.assertRaises(ConsoleRequestError):
            client.profiles()
        self.assertEqual(self.paths(), [('GET', '/api/v2/alarms/profiles')])

    def test_resource_403_does_not_delete_or_renew_the_login(self):
        client = self.login()
        stored = self.path.read_bytes()
        self.queue(self.response(403, {'private': 'do-not-show'}))
        with self.assertRaisesRegex(ConsoleRequestError, 'account permissions') as error:
            client.profiles()
        self.assertNotIn('do-not-show', str(error.exception))
        self.assertEqual(self.path.read_bytes(), stored)
        self.assertEqual(self.paths(), [('GET', '/api/v2/alarms/profiles')])

    def test_only_refresh_authentication_rejection_removes_the_saved_login(self):
        for status in (401, 403):
            client = self.login(lifetime=3700)
            self.advance(101)
            self.queue(self.response(status))
            with self.assertRaisesRegex(ConsoleRequestError, 'Sign in to reconnect'):
                client.profiles()
            self.assertIsNone(client.cookie)
            self.assertFalse(self.path.exists())
            self.assertEqual(self.paths(), [('POST', '/api/auth/refresh')])

    def test_transient_renewal_failure_keeps_session_and_backs_off(self):
        for status in (429, 500, 503):
            client = self.login(lifetime=3700)
            self.advance(101)
            self.queue(self.response(status), self.response(body=[]), self.response(body=[]))
            client.profiles()
            self.advance(10)
            client.profiles()
            self.assertEqual(self.paths(), [('POST', '/api/auth/refresh'), ('GET', '/api/v2/alarms/profiles'),
                                           ('GET', '/api/v2/alarms/profiles')])
            self.assertTrue(self.path.exists())
            self.advance(291)
            self.queue(self.response(cookie=self.token(csrf='new')), self.response(body=[]))
            client.profiles()
            self.assertEqual(self.paths()[0], ('POST', '/api/auth/refresh'))

    def test_network_outage_during_refresh_retains_cookie_and_does_not_spin(self):
        client = self.login(lifetime=3700)
        self.advance(101)
        saved = self.path.read_bytes()
        self.queue(self.response(body=[]), self.response(body=[]))
        self.connection.connect.side_effect = [OSError('private-error'), None, None]
        client.profiles()
        client.profiles()
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertEqual(self.paths(), [('GET', '/api/v2/alarms/profiles')] * 2)

    def test_writes_are_never_renewed_or_replayed(self):
        client = self.login(lifetime=3700)
        self.advance(101)
        self.queue(self.response(401))
        with self.assertRaises(ConsoleRequestError):
            client.request(self.host, 'PATCH', '/api/v2/alarms/profiles/test', {'state': 'armed'},
                           cookie=client.cookie, extra_headers={'X-CSRF-Token': 'test-csrf'})
        self.assertEqual(self.paths(), [('PATCH', '/api/v2/alarms/profiles/test')])
        self.assertTrue(self.path.exists())

    def test_event_cookie_rotation_is_persisted_without_waiting_for_next_poll(self):
        client = self.login()
        rotated = self.token(csrf='event-rotation')
        self.queue(self.response(body=[], cookie=rotated))
        client.door_events(0)
        self.assertEqual(session_store.load(self.path)['cookie'], rotated)

    def test_expired_restored_token_gets_one_renewal_opportunity(self):
        self.login(lifetime=3700)
        self.advance(4000)
        restarted = LocalSession(self.path)
        self.assertTrue(restarted.restore(self.host))
        self.queue(self.response(cookie=self.token()), self.response(body=[]))
        restarted.profiles()
        self.assertEqual(self.paths()[0], ('POST', '/api/auth/refresh'))

    def test_other_console_or_snapshot_cookie_cannot_clear_main_login(self):
        client = self.login()
        stored = self.path.read_bytes()
        self.queue(self.response(401))
        with self.assertRaises(ConsoleRequestError):
            client.request('192.168.0.2', 'GET', '/api/v2/alarms/profiles', cookie=self.token())
        self.assertEqual(self.path.read_bytes(), stored)
        self.assertEqual(len(self.paths()), 1)

    def test_logout_during_refresh_cannot_recreate_the_saved_login(self):
        client = self.login(lifetime=3700)
        self.advance(101)
        def logout(*args, **kwargs):
            client.clear()
            return {}, self.token(), self.wall + 7200
        with patch.object(client, '_request_once', side_effect=logout):
            self.assertFalse(client.refresh_if_due())
        self.assertFalse(self.path.exists())
        self.assertIsNone(client.cookie)

    def test_bad_expiry_is_only_a_hint_and_never_marks_authentication_valid(self):
        for cookie in ('TOKEN=opaque', 'TOKEN=bad.bad.bad', '', None):
            self.assertIsNone(cookie_expiry(cookie))
        client = LocalSession(self.path)
        client.host, client.cookie = self.host, self.token()
        self.assertFalse(client.refresh_if_due(force=True))
        self.connection.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
