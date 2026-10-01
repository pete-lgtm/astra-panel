import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class HubTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.patches = [patch.object(server, 'PIN_FILE', Path(self.directory.name) / 'pin.json'),
                        patch.object(server, 'PIN', {}), patch.object(server, 'UNLOCKS', {}),
                        patch.object(server, 'CONFIG', {'host': '192.168.1.1', 'key': 'test-key'})]
        for item in self.patches:
            item.start()
        server.set_pin('753159')
        self.user = server.check_pin('753159')
        server.UNLOCKS['session'] = {'expires': server.time.monotonic() + 60, 'user': self.user}

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def test_locked_cannot_discover_cameras(self):
        with patch.object(server, 'request_unifi') as request:
            with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                server.hub_capabilities({})
            request.assert_not_called()

    def test_audio_requires_pin_and_does_not_hold_alarm_lock_or_renew_grant(self):
        expires = server.UNLOCKS['session']['expires']
        def read(**kwargs):
            self.assertTrue(server.LOCK.acquire(blocking=False))
            server.LOCK.release()
            return {'zones': []}
        with patch.object(server.SONOS_AUDIO, 'read', side_effect=read) as request:
            with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                server.hub_audio({})
            request.assert_not_called()
            self.assertEqual(server.hub_audio({'unlock': 'session', 'rescan': True}), {'zones': []})
            request.assert_called_once_with(rescan=True)
            self.assertEqual(server.UNLOCKS['session']['expires'], expires)

    def test_audio_result_discarded_if_panel_locks_during_read(self):
        def read(**kwargs):
            server.UNLOCKS.clear()
            return {'zones': [{'name': 'Private room'}]}
        with patch.object(server.SONOS_AUDIO, 'read', side_effect=read):
            with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                server.hub_audio({'unlock': 'session'})

    def test_artwork_requires_pin_before_and_after_read_without_alarm_lock(self):
        expires = server.UNLOCKS['session']['expires']
        def artwork(key):
            self.assertTrue(server.LOCK.acquire(blocking=False))
            server.LOCK.release()
            return 'data:image/png;base64,AA=='
        with patch.object(server.SONOS_AUDIO, 'artwork', side_effect=artwork) as request:
            with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                server.hub_audio({'artworkId': 'a'*64}, artwork=True)
            request.assert_not_called()
            result = server.hub_audio({'unlock': 'session', 'artworkId': 'a'*64}, artwork=True)
            self.assertEqual(result['image'], 'data:image/png;base64,AA==')
            self.assertEqual(server.UNLOCKS['session']['expires'], expires)
        def lock_during_read(key):
            server.UNLOCKS.clear()
            return 'private cover'
        with patch.object(server.SONOS_AUDIO, 'artwork', side_effect=lock_during_read):
            with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                server.hub_audio({'unlock': 'session', 'artworkId': 'a'*64}, artwork=True)

    def test_audio_control_requires_current_pin_without_holding_alarm_lock(self):
        expires = server.UNLOCKS['session']['expires']
        def control(data, authorize):
            self.assertTrue(server.LOCK.acquire(blocking=False));server.LOCK.release()
            self.assertNotIn('unlock', data)
            authorize()
            return {'accepted': True}
        with patch.object(server.SONOS_AUDIO, 'control', side_effect=control) as write:
            with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                server.hub_audio_control({'action': 'pause'})
            write.assert_not_called()
            self.assertEqual(server.hub_audio_control({'unlock': 'session', 'action': 'pause'}), {'accepted': True})
            self.assertEqual(server.UNLOCKS['session']['expires'], expires)
        def lock_during_read(data, authorize):
            server.UNLOCKS.clear();authorize()
            self.fail('Control must not continue with an expired grant')
        with patch.object(server.SONOS_AUDIO, 'control', side_effect=lock_during_read):
            with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                server.hub_audio_control({'unlock': 'session', 'action': 'pause'})

    def test_discovery_is_read_only_sanitized_and_does_not_hold_alarm_lock(self):
        def cameras(method, path, config):
            self.assertEqual((method, path), ('GET', 'cameras'))
            self.assertEqual(config, server.CONFIG)
            self.assertIsNot(config, server.CONFIG)
            self.assertTrue(server.LOCK.acquire(blocking=False))
            server.LOCK.release()
            return [{'id': 'front', 'name': 'Front camera', 'state': 'CONNECTED',
                     'stream': 'private-stream-secret'}, {'name': 'Malformed'}]

        with patch.object(server, 'request_unifi', side_effect=cameras):
            result = server.hub_capabilities({'unlock': 'session'})
        self.assertEqual(result, {'camerasDiscovered': True, 'cameras': [
            {'id': 'front', 'name': 'Front camera', 'state': 'CONNECTED'}]})
        self.assertIn('session', server.UNLOCKS, 'Discovery does not consume the alarm command grant')

    def test_grouping_requires_pin_rechecks_grant_and_never_holds_alarm_lock(self):
        expires = server.UNLOCKS['session']['expires']
        def grouping(data, authorize):
            self.assertTrue(server.LOCK.acquire(blocking=False));server.LOCK.release()
            self.assertNotIn('unlock', data)
            authorize()
            return {'confirmed': True}
        with patch.object(server.SONOS_AUDIO, 'group', side_effect=grouping) as write:
            with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                server.hub_audio_control({}, grouping=True)
            write.assert_not_called()
            self.assertEqual(server.hub_audio_control({'unlock': 'session'}, grouping=True), {'confirmed': True})
            self.assertEqual(server.UNLOCKS['session']['expires'], expires)
        def revoke(data, authorize):
            server.UNLOCKS.clear();authorize()
            self.fail('No further grouping with a locked grant')
        with patch.object(server.SONOS_AUDIO, 'group', side_effect=revoke):
            with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                server.hub_audio_control({'unlock': 'session'}, grouping=True)

    def test_lock_during_discovery_discards_result(self):
        def cameras(*args, **kwargs):
            server.UNLOCKS.clear()
            return [{'id': 'front', 'name': 'Private camera'}]

        with patch.object(server, 'request_unifi', side_effect=cameras):
            with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                server.hub_capabilities({'unlock': 'session'})

    def test_connection_switch_discards_old_console_inventory(self):
        def cameras(*args, **kwargs):
            server.CONFIG.clear()
            return [{'id': 'front', 'name': 'Old console camera'}]
        with patch.object(server, 'request_unifi', side_effect=cameras):
            self.assertEqual(server.hub_capabilities({'unlock': 'session'}),
                             {'cameras': [], 'camerasDiscovered': False})

    def test_missing_or_failed_camera_connection_does_not_revoke_alarm_access(self):
        with patch.object(server, 'request_unifi', side_effect=ValueError('API unavailable')):
            self.assertEqual(server.hub_capabilities({'unlock': 'session'})['cameras'], [])
            self.assertIn('session', server.UNLOCKS)
        server.CONFIG.clear()
        with patch.object(server, 'request_unifi') as request:
            self.assertFalse(server.hub_capabilities({'unlock': 'session'})['camerasDiscovered'])
            request.assert_not_called()

    def test_network_inventory_requires_an_unlocked_administrator(self):
        with patch.object(server.network_inventory, 'discover') as discover:
            with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                server.hub_network({})
            with patch.object(server, 'unlocked_user', return_value={'role': 'user'}):
                self.assertEqual(server.hub_network({'unlock': 'session'})['state'], 'admin_required')
            discover.assert_not_called()

    def test_network_reads_outside_alarm_lock_and_preserves_live_login_on_denial(self):
        session = server.LocalSession()
        session.host, session.cookie = '192.168.1.1', 'alarm-cookie'
        def denied(reader, host, method, path, **kwargs):
            self.assertIsNot(reader, session)
            self.assertTrue(server.LOCK.acquire(blocking=False))
            server.LOCK.release()
            reader.clear()
            raise server.network_inventory.ConsoleRequestError(403)
        with patch.object(server, 'LOCAL_SESSION', session), patch.object(server.LocalSession, 'request', new=denied):
            result = server.hub_network({'unlock': 'session'})
        self.assertEqual(result['state'], 'denied')
        self.assertEqual(session.cookie, 'alarm-cookie')
        self.assertIn('session', server.UNLOCKS)

    def test_lock_and_logout_during_network_discovery_discard_results(self):
        for change in ('lock', 'logout'):
            session = server.LocalSession()
            session.host, session.cookie = '192.168.1.1', 'cookie'
            server.UNLOCKS['session'] = {'expires': server.time.monotonic() + 60, 'user': self.user}
            def discovery(*args):
                if change == 'lock':
                    server.UNLOCKS.clear()
                else:
                    session.clear()
                return {'state': 'ready', 'devices': [{'name': 'Private device'}]}
            with patch.object(server, 'LOCAL_SESSION', session), patch.object(server.network_inventory, 'discover', side_effect=discovery):
                with self.assertRaises(ValueError):
                    server.hub_network({'unlock': 'session'})

    def test_normal_alarm_cookie_rotation_does_not_discard_network_inventory(self):
        session = server.LocalSession()
        session.host, session.cookie = '192.168.1.1', 'cookie'
        def discovery(*args):
            session.cookie = 'rotated-cookie'
            return {'state': 'ready', 'devices': []}
        with patch.object(server, 'LOCAL_SESSION', session), patch.object(server.network_inventory, 'discover', side_effect=discovery):
            self.assertEqual(server.hub_network({'unlock': 'session'})['state'], 'ready')
