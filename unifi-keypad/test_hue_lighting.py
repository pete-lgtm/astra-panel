import hashlib
import json
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import hue_lighting as hue

LIGHT, DEVICE, ROOM, GROUP, SCENE, RADIO = [f'00000000-0000-4000-8000-{i:012d}' for i in range(1, 7)]
FINGERPRINT = hashlib.sha256(b'test-device-certificate').hexdigest()
SAVED = {'host': '192.168.1.50', 'name': 'Test Hue', 'bridgeId': '001122fffe334455',
         'fingerprint': FINGERPRINT, 'key': 'example-test-application-key'}


def inventory():
    return {'errors': [], 'data': [
        {'id': LIGHT, 'type': 'light', 'metadata': {'name': '<Living lamp>'}, 'owner': {'rid': DEVICE},
         'on': {'on': True}, 'dimming': {'brightness': 64.4}},
        {'id': DEVICE, 'type': 'device', 'services': [{'rid': LIGHT, 'rtype': 'light'}]},
        {'id': RADIO, 'type': 'zigbee_connectivity', 'owner': {'rid': DEVICE}, 'status': 'connected'},
        {'id': ROOM, 'type': 'room', 'metadata': {'name': 'Living room'}, 'children': [{'rid': DEVICE}],
         'services': [{'rid': GROUP, 'rtype': 'grouped_light'}]},
        {'id': GROUP, 'type': 'grouped_light', 'on': {'on': True}, 'dimming': {'brightness': 64.4}},
        {'id': SCENE, 'type': 'scene', 'metadata': {'name': 'Relax'}, 'group': {'rid': ROOM}},
    ]}


class HueTransportTests(unittest.TestCase):
    def connection(self):
        connection = MagicMock()
        connection.sock.getpeercert.return_value = b'test-device-certificate'
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{"data": [], "errors": []}'
        return connection

    def test_only_private_ipv4_and_pinned_credentials(self):
        for value in ('https://192.168.1.50', '127.0.0.1', '8.8.8.8', '169.254.1.1', 'localhost', '::1', None, {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hue.host(value)
        with patch.object(hue.http.client, 'HTTPSConnection') as factory:
            with self.assertRaises(ValueError):
                hue.request(SAVED['host'], 'GET', '/clip/v2/resource', key=SAVED['key'])
            factory.assert_not_called()

    def test_certificate_change_or_revocation_sends_no_request(self):
        for pin, authorize in [('0' * 64, lambda: None), (FINGERPRINT, MagicMock(side_effect=ValueError('Locked')))]:
            connection = self.connection()
            with patch.object(hue.http.client, 'HTTPSConnection', return_value=connection), self.assertRaises(ValueError):
                hue.request(SAVED['host'], 'PUT', '/clip/v2/resource/light/' + LIGHT,
                            key=SAVED['key'], fingerprint=pin, authorize=authorize)
            connection.request.assert_not_called()
            connection.close.assert_called_once()

    def test_pinned_https_and_bounded_reply_no_redirect_or_write_retry(self):
        connection = self.connection()
        with patch.object(hue.http.client, 'HTTPSConnection', return_value=connection) as factory:
            hue.request(SAVED['host'], 'GET', '/clip/v2/resource', fingerprint=FINGERPRINT, key=SAVED['key'])
            self.assertEqual(factory.call_args.args, (SAVED['host'], 443))
            self.assertEqual(connection.request.call_args.kwargs['headers']['hue-application-key'], SAVED['key'])
            connection.getresponse.return_value.read.assert_called_once_with(2_000_001)
            for status, body in [(302, b'{}'), (401, b'secret response'), (200, b'invalid'), (200, b' ' * 2_000_001)]:
                connection.getresponse.return_value.status = status
                connection.getresponse.return_value.read.return_value = body
                with self.assertRaises(ValueError) as error:
                    hue.request(SAVED['host'], 'GET', '/api/config')
                self.assertNotIn('secret response', str(error.exception))
            connection.request.reset_mock()
            connection.getresponse.side_effect = socket.timeout()
            with self.assertRaisesRegex(ValueError, 'unknown'):
                hue.request(SAVED['host'], 'PUT', '/clip/v2/resource/light/' + LIGHT,
                            fingerprint=FINGERPRINT, key=SAVED['key'], body={'on': {'on': False}})
            connection.request.assert_called_once()

    def test_dns_compression_and_malformed_packets(self):
        service = b'\x04_hue\x04_tcp\x05local\0'
        packet = struct.pack('!6H', 0, 0x8400, 0, 1, 0, 0) + service
        packet += struct.pack('!HHIH', 12, 1, 120, 9) + b'\x06bridge\xc0\x0c'
        self.assertEqual(hue.dns_records(packet), [(hue.SERVICE, 12, 'bridge.' + hue.SERVICE)])
        for raw in (b'bad', packet[:-1], b'\0' * 12 + b'\xc0\x0c'):
            self.assertEqual(hue.dns_records(raw), [])
        with self.assertRaises(ValueError):
            hue.dns_name(b'\xc0\x00', 0)


class HueLightingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / '.lighting-hue.json'
        self.lighting = hue.HueLighting(self.path)

    def save(self):
        hue.atomic_json(self.path, SAVED)

    def prepare(self):
        with patch.object(hue, 'request', return_value=({'name': 'Test Hue', 'modelid': 'BSB002', 'bridgeid': SAVED['bridgeId']}, FINGERPRINT)):
            return self.lighting.prepare(SAVED['host'], 'grant', lambda: None)['pairId']

    def test_persistent_connection_and_public_inventory_contain_no_key(self):
        self.save()
        restored = hue.HueLighting(self.path)
        with patch.object(hue, 'request', return_value=(inventory(), FINGERPRINT)):
            result = restored.read()
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['lights'][0]['brightness'], 64)
        self.assertEqual(result['rooms'][0]['lights'], [LIGHT])
        self.assertEqual(result['rooms'][0]['groupId'], GROUP)
        self.assertEqual(result['scenes'][0]['roomId'], ROOM)
        self.assertNotIn(SAVED['key'], json.dumps(result))
        self.assertNotIn(FINGERPRINT, json.dumps(result))
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)
        with patch.object(hue, 'request', side_effect=ValueError('offline')), self.assertRaises(ValueError):
            restored.read()
        self.assertEqual(restored.saved(), SAVED, 'A lost connection never deletes saved credentials')

    def test_pairing_requires_physical_button_then_verifies_and_saves(self):
        identifier = self.prepare()
        with patch.object(hue, 'request', return_value=([{'error': {'type': 101}}], FINGERPRINT)):
            with self.assertRaisesRegex(ValueError, 'Press the button'):
                self.lighting.pair(identifier, 'grant', lambda: None)
        self.assertFalse(self.path.exists())
        with patch.object(hue, 'request', side_effect=[([{'success': {'username': SAVED['key']}}], FINGERPRINT), (inventory(), FINGERPRINT)]) as request:
            result = self.lighting.pair(identifier, 'grant', lambda: None)
        self.assertEqual(request.call_args_list[0].args[1:], ('POST', '/api'))
        self.assertEqual(request.call_args_list[0].kwargs['fingerprint'], FINGERPRINT)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(self.lighting.saved(), SAVED)
        self.assertFalse(self.lighting.pending)

    def test_failed_repair_preserves_existing_connection_and_expired_grant_never_pairs(self):
        self.save()
        identifier = self.prepare()
        with patch.object(hue, 'request', side_effect=[([{'success': {'username': 'new-example-application-key'}}], FINGERPRINT), ValueError('Unsupported API')]):
            with self.assertRaises(ValueError):
                self.lighting.pair(identifier, 'grant', lambda: None)
        self.assertEqual(self.lighting.saved(), SAVED)
        with patch.object(hue, 'request') as request:
            with self.assertRaises(ValueError):
                self.lighting.pair(identifier, 'other-grant', lambda: None)
            self.lighting.pending[identifier]['expires'] = 0
            with self.assertRaises(ValueError):
                self.lighting.pair(identifier, 'grant', lambda: None)
            request.assert_not_called()

    def test_wrong_bridge_and_malformed_saved_connection(self):
        with patch.object(hue, 'request', return_value=({'modelid': 'BSB001', 'bridgeid': SAVED['bridgeId']}, FINGERPRINT)):
            with self.assertRaises(ValueError):
                self.lighting.prepare(SAVED['host'], 'grant', lambda: None)
        for raw in ('[]', '{}', '{', json.dumps({**SAVED, 'host': '8.8.8.8'}), json.dumps({**SAVED, 'key': None})):
            self.path.write_text(raw)
            with self.assertRaises(ValueError):
                self.lighting.saved()

    def test_malformed_optional_resources_do_not_break_lighting(self):
        raw = inventory()
        for row in raw['data']:
            for field in ('metadata', 'owner', 'dimming', 'on', 'services', 'children', 'group'):
                if field in row:
                    row[field] = None
        result = hue.normalize(hue.resources(raw))
        self.assertIsNone(result['lights'][0]['on'])
        self.assertFalse(result['lights'][0]['reachable'])
        self.assertIsNone(result['rooms'][0]['groupId'])
        for value in ([], {'data': [] , 'errors': ['failed']}, {'data': 'bad'}, {'data': [{}] * 4001}):
            with self.assertRaises(ValueError):
                hue.resources(value)

    def test_on_brightness_off_and_scene_use_fixed_targets_and_fresh_capabilities(self):
        self.save()
        commands = [
            ({'id': LIGHT, 'kind': 'light', 'action': 'on', 'value': False}, {'on': {'on': False}}),
            ({'id': LIGHT, 'kind': 'light', 'action': 'brightness', 'value': 35}, {'on': {'on': True}, 'dimming': {'brightness': 35}}),
            ({'id': LIGHT, 'kind': 'light', 'action': 'brightness', 'value': 0}, {'on': {'on': False}}),
            ({'id': GROUP, 'kind': 'grouped_light', 'action': 'on', 'value': True}, {'on': {'on': True}}),
            ({'id': SCENE, 'kind': 'scene', 'action': 'scene'}, {'recall': {'action': 'active'}}),
        ]
        for command, expected in commands:
            with patch.object(hue, 'request', side_effect=[(inventory(), FINGERPRINT), ({'data': [{'rid': command['id']}], 'errors': []}, FINGERPRINT)]) as request:
                self.assertEqual(self.lighting.control(command, lambda: None), {'accepted': True})
                self.assertEqual(request.call_args_list[0].args[1:], ('GET', '/clip/v2/resource'))
                self.assertEqual(request.call_args.args[1:], ('PUT', '/clip/v2/resource/' + command['kind'] + '/' + command['id']))
                self.assertEqual(request.call_args.kwargs['body'], expected)

    def test_invalid_unsupported_offline_and_revoked_commands_never_write(self):
        self.save()
        command = {'id': LIGHT, 'kind': 'light', 'action': 'brightness', 'value': 35}
        bad = [{**command, 'value': x} for x in (True, -1, 101, float('nan'), '35')]
        bad += [{**command, 'id': '../config'}, {**command, 'host': '192.168.1.2'}, {**command, 'id': ROOM}]
        with patch.object(hue, 'request', return_value=(inventory(), FINGERPRINT)) as request:
            for data in bad:
                with self.assertRaises(ValueError): self.lighting.control(data, lambda: None)
            self.assertTrue(all(c.args[1] == 'GET' for c in request.call_args_list))
        raw = inventory(); raw['data'][2]['status'] = 'disconnected'
        with patch.object(hue, 'request', return_value=(raw, FINGERPRINT)) as request:
            with self.assertRaisesRegex(ValueError, 'unreachable'): self.lighting.control(command, lambda: None)
            self.assertEqual(request.call_count, 1)
        with patch.object(hue, 'request', return_value=(inventory(), FINGERPRINT)) as request:
            with self.assertRaisesRegex(ValueError, 'Locked'):
                self.lighting.control(command, MagicMock(side_effect=[None, ValueError('Locked')]))
            self.assertEqual(request.call_count, 1)

    def test_disconnect_only_removes_local_state(self):
        self.save(); self.prepare()
        with patch.object(hue, 'request') as request:
            self.lighting.disconnect(lambda: None)
            request.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.lighting.pending)
        self.assertEqual(self.lighting.read()['state'], 'not_connected')

    def test_in_progress_bridge_call_does_not_queue_polls(self):
        with self.lighting.io, self.assertRaisesRegex(ValueError, 'updating'):
            self.lighting.read()


class HueServerTests(unittest.TestCase):
    def test_pin_and_admin_scope_and_network_outside_alarm_lock(self):
        import server
        admin = {'id': 'one', 'role': 'admin'}
        user = {'id': 'two', 'role': 'user'}
        def read(authorize):
            self.assertFalse(server.LOCK.locked())
            authorize()
            return {'state': 'not_connected'}
        with patch.object(server, 'unlocked_user', return_value=user), patch.object(server.HUE_LIGHTING, 'read', side_effect=read):
            self.assertFalse(server.hub_lighting({'unlock': 'fixture'})['canManage'])
            for action in ('discover', 'prepare', 'pair', 'disconnect'):
                with self.assertRaisesRegex(ValueError, 'administrator'):
                    server.hub_lighting({'unlock': 'fixture'}, action)
        with patch.object(server, 'unlocked_user', side_effect=ValueError('Locked')), patch.object(server.HUE_LIGHTING, 'read') as read_call:
            with self.assertRaises(ValueError): server.hub_lighting({})
            read_call.assert_not_called()
        with patch.object(server, 'unlocked_user', side_effect=[admin, user]), patch.object(server.HUE_LIGHTING, 'read', side_effect=read):
            with self.assertRaises(ValueError): server.hub_lighting({'unlock': 'fixture'})
        with patch.object(server, 'unlocked_user', return_value=admin), patch.object(server.HUE_LIGHTING, 'disconnect') as disconnect:
            with self.assertRaises(ValueError): server.hub_lighting({'unlock': 'fixture'}, 'disconnect')
            disconnect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
