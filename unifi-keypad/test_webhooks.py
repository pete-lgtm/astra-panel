import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from test_fixtures import legacy_site
import server

class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.file_patch = patch.object(server, "PIN_FILE", Path(self.temp.name) / "pin.json")
        self.file_patch.start()
        site = legacy_site()
        self.site_patch=patch.object(server, 'SITE', site)
        self.site_patch.start()
        for name, value in [('WEBHOOKS', site['webhooks']), ('ENTRY_DOORS', set(site['doors']))]:
            fixture = patch.object(server, name, value)
            fixture.start()
            self.addCleanup(fixture.stop)
        server.PIN.clear()
        server.UNLOCKS.clear()
        server.EXIT_UNTIL = 0
    def grant(self, token, expired=False):
        if not server.PIN:
            server.set_pin("123456")
        server.UNLOCKS[token] = {"expires": server.time.monotonic() + (-1 if expired else 60), "user": server.check_pin("123456")}

    def tearDown(self):
        self.file_patch.stop()
        self.site_patch.stop()
        self.temp.cleanup()
        server.PIN.clear()
        server.UNLOCKS.clear()
        server.EXIT_UNTIL = 0
    def test_missing_pin_never_sends(self):
        with patch.object(server, 'request_unifi') as send:
            for action in ('arm', 'disarm'):
                with self.assertRaisesRegex(ValueError, 'Keypad locked'):
                    server.webhook_command(action, {'confirm': True})
            send.assert_not_called()
    def test_wrong_pin_and_lockout(self):
        server.set_pin('123456')
        with patch.object(server, 'request_unifi') as send:
            for _ in range(5):
                with self.assertRaises(ValueError):
                    server.check_pin('0000')
            with self.assertRaisesRegex(ValueError, 'Wait one minute'):
                server.check_pin('123456')
            send.assert_not_called()
    def test_accepted_does_not_claim_disarmed(self):
        server.set_pin('123456')
        with patch.object(server, 'request_unifi') as send:
            self.grant('test-session')
            result = server.webhook_command('disarm', {'confirm': True, 'unlock': 'test-session'})
            send.assert_called_once_with('POST', 'alarm-manager/webhook/' + server.WEBHOOKS['disarm'])
            self.assertIn('Unverified', result['state'])
            self.assertFalse(result['canControl'])
    def test_confirmation_required(self):
        server.set_pin('123456')
        with patch.object(server, 'request_unifi') as send:
            with self.assertRaises(ValueError):
                server.webhook_command('arm', {'pin': '123456'})
            send.assert_not_called()
    def test_disarm_allowed_during_exit_countdown(self):
        server.EXIT_UNTIL = server.time.monotonic() + 60
        self.grant('cancel-exit')
        with patch.object(server, 'request_unifi') as send:
            result = server.webhook_command('disarm', {'confirm': True, 'unlock': 'cancel-exit'})
            send.assert_called_once_with('POST', 'alarm-manager/webhook/' + server.WEBHOOKS['disarm'])
            self.assertEqual(server.EXIT_UNTIL, 0)
            self.assertFalse(result['canControl'])
            self.assertNotIn('cancel-exit', server.UNLOCKS)
    def test_disarm_does_not_require_door_event_or_entry_timer(self):
        self.grant('disarm-at-home')
        with patch.object(server.ENTRY, 'snapshot') as observation, patch.object(server, 'request_unifi') as send:
            result = server.webhook_command('disarm', {'confirm': True, 'unlock': 'disarm-at-home'})
            observation.assert_not_called()
            send.assert_called_once_with('POST', 'alarm-manager/webhook/' + server.WEBHOOKS['disarm'])
            self.assertFalse(result['canControl'])
    def test_no_pin_replacement(self):
        server.set_pin('123456')
        with self.assertRaises(ValueError):
            server.set_pin('9999')

    def test_expired_unlock_cannot_send(self):
        self.grant('expired', expired=True)
        with patch.object(server, 'request_unifi') as send:
            with self.assertRaises(ValueError):
                server.webhook_command('arm', {'confirm': True, 'unlock': 'expired'})
            send.assert_not_called()
    def test_unlock_is_single_use(self):
        self.grant('once')
        with patch.object(server, 'request_unifi') as send:
            server.webhook_command('disarm', {'confirm': True, 'unlock': 'once'})
            with self.assertRaises(ValueError):
                server.webhook_command('disarm', {'confirm': True, 'unlock': 'once'})
            self.assertEqual(send.call_count, 1)

    def test_pin_arm_and_duplicate_blocked(self):
        ready = {'fresh':True,'state':'disarmed','liveDelayEnabled':True,
                 'doors':{name:{'connected':True,'position':'closed'} for name in server.ENTRY_DOORS}}
        self.grant('arm-once')
        with patch.object(server.ENTRY,'snapshot',return_value=ready), patch.object(server, 'request_unifi') as send:
            result = server.webhook_command('arm', {'confirm': True, 'unlock':'arm-once'})
            self.assertGreater(result['exitSeconds'], 59)
            self.grant('arm-again')
            with self.assertRaisesRegex(ValueError, 'already counting down'):
                server.webhook_command('arm', {'confirm': True, 'unlock':'arm-again'})
            self.assertEqual(send.call_count, 1)

    def test_incomplete_coverage_or_active_state_cannot_rearm(self):
        for state, names, enabled in [('armed',server.ENTRY_DOORS,True),
                                      ('disarmed',{'Back Door'},True),
                                      ('disarmed',server.ENTRY_DOORS,False)]:
            snapshot={'fresh':True,'state':state,'liveDelayEnabled':enabled,
                      'doors':{name:{'connected':True,'position':'closed'} for name in names}}
            self.grant('coverage')
            with patch.object(server.ENTRY,'snapshot',return_value=snapshot), patch.object(server,'request_unifi') as send:
                with self.assertRaises(ValueError):
                    server.webhook_command('arm',{'confirm':True,'unlock':'coverage'})
                send.assert_not_called()

    def test_saved_hash_survives_restart(self):
        server.set_pin('864209')
        stored = server.PIN_FILE.read_text()
        self.assertNotIn('864209', stored)
        self.assertEqual(server.PIN_FILE.stat().st_mode & 0o777, 0o600)
        server.PIN.clear()
        server.load_pin()
        server.check_pin('864209')
        with self.assertRaises(ValueError):
            server.check_pin('0000')
    def test_corrupt_saved_pin_fails_closed(self):
        server.PIN_FILE.write_text('{broken')
        with self.assertRaises(ValueError):
            server.load_pin()
