import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from local_auth import LocalSession, ConsoleRequestError, MfaRequired
import session_store


class SavedSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.unifi-session.json'
        self.host = '192.168.0.1'

    def login(self, cookie='TOKEN=synthetic-session'):
        client = LocalSession(self.path)
        with patch.object(client, 'request', side_effect=[({}, cookie), ([], None)]):
            client.login(self.host, 'test-user', 'never-persist-password')
        return client

    def test_validated_login_survives_new_process_and_does_not_save_password(self):
        self.login()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        stored = self.path.read_text()
        self.assertNotIn('never-persist-password', stored)
        self.assertNotIn('test-user', stored)
        restarted = LocalSession(self.path)
        self.assertTrue(restarted.restore(self.host))
        with patch.object(restarted, 'request', return_value=([{'state': 'disarmed'}], None)) as read:
            result = restarted.profiles()
        self.assertEqual(read.call_args.args[:3], (self.host, 'GET', '/api/v2/alarms/profiles'))
        self.assertTrue(result['localConnected'])
        self.assertNotIn('synthetic-session', json.dumps(result))

    def test_rotation_is_saved_and_explicit_logout_removes_it(self):
        client = self.login()
        with patch.object(client, 'request', return_value=([], 'UOS_TOKEN=rotated')):
            client.profiles()
        self.assertEqual(session_store.load(self.path)['cookie'], 'UOS_TOKEN=rotated')
        client.clear()
        self.assertFalse(self.path.exists())
        self.assertFalse(LocalSession(self.path).restore(self.host))

    def test_resource_denial_keeps_saved_login_for_recovery(self):
        self.login()
        for code in (401, 403):
            self.login()
            client = LocalSession(self.path)
            self.assertTrue(client.restore(self.host))
            connection = MagicMock()
            response = connection.getresponse.return_value
            response.status, response.read.return_value = code, b'{}'
            with patch.dict('os.environ', {'UNIFI_CERT_SHA256': ''}), patch('local_auth.http.client.HTTPSConnection', return_value=connection):
                with self.assertRaises(ConsoleRequestError):
                    client.profiles()
            self.assertIsNotNone(client.cookie)
            self.assertTrue(self.path.exists())
            self.assertEqual(connection.request.call_count, 1)
            self.assertEqual(connection.request.call_args.args[0], 'GET')

    def test_network_outage_keeps_session_for_recovery(self):
        client = self.login()
        with patch('local_auth.http.client.HTTPSConnection') as connection:
            connection.return_value.connect.side_effect = OSError('offline')
            with self.assertRaises(ValueError):
                client.profiles()
        self.assertTrue(self.path.exists())
        self.assertIsNotNone(client.cookie)

    def test_changed_console_and_insecure_files_do_not_restore(self):
        self.login()
        self.assertFalse(LocalSession(self.path).restore('192.168.0.2'))
        self.assertFalse(self.path.exists())
        self.login()
        os.chmod(self.path, 0o644)
        self.assertFalse(session_store.load(self.path))
        os.chmod(self.path, 0o600)
        link = self.path.with_name('link')
        link.symlink_to(self.path)
        self.assertFalse(session_store.load(link))

    def test_invalid_format_and_headers_are_rejected(self):
        for value in ('bad json', '[]', '{"version":1,"session":{}}'):
            self.path.write_text(value)
            self.path.chmod(0o600)
            self.assertFalse(session_store.load(self.path))
        for cookie in ('other=value', 'TOKEN=', 'TOKEN=test\nheader', 'TOKEN=a;evil=b'):
            with self.assertRaises(ValueError):
                session_store.save(self.path, {'host': self.host, 'cookie': cookie})

    def test_challenge_or_unverified_login_is_never_persisted(self):
        client = LocalSession(self.path)
        with patch.object(client, 'request', side_effect=MfaRequired('mfa=private')):
            client.login(self.host, 'test-user', 'never-persist-password')
        self.assertFalse(self.path.exists())
        with patch.object(client, 'request', side_effect=[({}, 'TOKEN=unverified'), ({'unexpected': True}, None)]):
            with self.assertRaises(ValueError):
                client.login(self.host, 'test-user', 'never-persist-password')
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
