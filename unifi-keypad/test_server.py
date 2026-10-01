import unittest
from unittest.mock import patch
import hashlib
import server

class StatusTests(unittest.TestCase):
    def test_global_mode_retains_connection_without_trusting_local_state(self):
        with patch.object(server, 'request_unifi', side_effect=[{'armMode': {'status': 'disabled'}}, ValueError('This operation is not available when global alarm manager is enabled')]):
            result = server.read_status()
            self.assertTrue(result['connected'])
            self.assertFalse(result['canControl'])
            self.assertEqual(result['state'], 'Unverified (Global mode)')

    def test_pin_mismatch_never_sends_key(self):
        with patch.dict(server.CONFIG, {'host': '192.168.0.1', 'key': 'test-only'}), patch.dict(server.os.environ, {'UNIFI_CERT_SHA256': '0' * 64}), patch.object(server.http.client, 'HTTPSConnection') as connection:
            connection.return_value.sock.getpeercert.return_value = b'test certificate'
            with self.assertRaisesRegex(ValueError, 'certificate changed'):
                server.request_unifi('GET', 'nvrs')
            connection.return_value.request.assert_not_called()
            connection.return_value.close.assert_called_once()

    def test_matching_pin_permits_request(self):
        certificate = b'test certificate'
        with patch.dict(server.CONFIG, {'host': '192.168.0.1', 'key': 'test-only'}), patch.dict(server.os.environ, {'UNIFI_CERT_SHA256': hashlib.sha256(certificate).hexdigest()}), patch.object(server.http.client, 'HTTPSConnection') as connection:
            connection.return_value.sock.getpeercert.return_value = certificate
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{}'
            self.assertEqual(server.request_unifi('GET', 'nvrs'), {})
            connection.return_value.request.assert_called_once()

    def status(self, state):
        with patch.object(server, 'request_unifi', side_effect=[{'armMode': {'status': state, 'armProfileId': 'p'}}, [{'id': 'p', 'name': 'Home'}]]):
            return server.read_status()

    def test_unknown_is_never_disarmed(self):
        for state in (None, 'unknown', 'future-value'):
            result = self.status(state)
            self.assertEqual(result['state'], 'Unknown')
            self.assertFalse(result['canControl'])

    def test_real_states_and_profile(self):
        for raw, label in server.STATES.items():
            result = self.status(raw)
            self.assertEqual(result['state'], label)
            self.assertEqual(result['profile'], 'Home')
            self.assertTrue(result['canControl'])

    def test_failed_read_does_not_return_state(self):
        with patch.object(server, 'request_unifi', side_effect=ValueError('Offline')):
            with self.assertRaises(ValueError):
                server.read_status()

    def test_no_profiles_disables_controls(self):
        with patch.object(server, 'request_unifi', side_effect=[{'armMode': {'status': 'disabled'}}, []]):
            self.assertFalse(server.read_status()['canControl'])

    def test_only_private_ipv4_console_targets(self):
        self.assertEqual(server.validate_host('192.168.1.1'), '192.168.1.1')
        for host in ('127.0.0.1', '169.254.169.254', '8.8.8.8', 'localhost', '192.168.1.1/path'):
            with self.assertRaises(ValueError):
                server.validate_host(host)

if __name__ == '__main__':
    unittest.main()
