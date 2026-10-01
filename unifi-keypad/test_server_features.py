"""Integration tests for server.py's duress + arm-mode webhook paths.

Heavy UniFi/hardware modules are stubbed in sys.modules before server is
imported; keypad_store, settings_access, pin_store, power_monitor and
fleet_heartbeat are the real modules. request_unifi is faked to record calls.
Nothing touches the network.
"""
import hashlib
import importlib.util
import secrets
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import keypad_store

STATE_DIR = Path(tempfile.mkdtemp(prefix='server-test-'))
SITE = {'name': 'Test Site', 'doors': [], 'webhooks': {}, 'version': 1,
        'consoleHost': '', 'panel_id': 'panel-test-1'}


def _install_stubs():
    device_config = types.ModuleType('device_config')
    device_config.settings = lambda: (STATE_DIR, SITE)
    device_config.selected_sensors = lambda *a, **k: []
    sys.modules['device_config'] = device_config

    entry_delay = types.ModuleType('entry_delay')
    entry_delay.EntryDelay = type('EntryDelay', (), {})
    sys.modules['entry_delay'] = entry_delay

    local_auth = types.ModuleType('local_auth')

    class LocalSession:
        def __init__(self, path):
            self.path = path
            self.host = None
            self.cookie = None
            self.generation = 0

        def restore(self, host):
            pass

    local_auth.LocalSession = LocalSession
    sys.modules['local_auth'] = local_auth

    live_entry = types.ModuleType('live_entry')

    class EntryObserver:
        def checkpoint(self):
            pass

        def snapshot(self):
            return {'state': 'disarmed', 'fresh': True,
                    'liveDelayEnabled': True, 'doors': {}}

    live_entry.configured_alarm_state = lambda *args: None
    live_entry.EntryObserver = EntryObserver
    sys.modules['live_entry'] = live_entry

    for name in ('onboarding', 'sensor_settings', 'network_inventory',
                 'network_actions', 'network_wifi', 'protection_state',
                 'door_policy', 'arm_coverage', 'credential_store'):
        sys.modules[name] = types.ModuleType(name)

    phone_pairing = types.ModuleType('phone_pairing')
    phone_pairing.Pairing = type('Pairing', (), {'__init__': lambda self, *a, **k: None})
    sys.modules['phone_pairing'] = phone_pairing

    browser_setup = types.ModuleType('browser_setup')
    browser_setup.SESSION_SECONDS = 300
    browser_setup.BrowserSetup = type('BrowserSetup', (), {'__init__': lambda self, *a, **k: None})
    sys.modules['browser_setup'] = browser_setup


# Keep this fixture's fake modules and server separate from the other test files.
# Discovery imports every test before running them; global stubs or a changed PIN
# work factor otherwise corrupt the real-module tests that run in the same process.
_stub_names = ('device_config', 'entry_delay', 'local_auth', 'live_entry',
               'onboarding', 'sensor_settings', 'network_inventory',
               'network_actions', 'network_wifi', 'protection_state',
               'door_policy', 'arm_coverage', 'credential_store',
               'phone_pairing', 'browser_setup')
_previous = {name: sys.modules.get(name) for name in _stub_names}
try:
    _install_stubs()
    _spec = importlib.util.spec_from_file_location(
        '_server_features_subject', Path(__file__).with_name('server.py'))
    server = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(server)
finally:
    for _name, _module in _previous.items():
        if _module is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _module


def seed_store():
    from keypad_store import KeypadStore
    store = KeypadStore(STATE_DIR / '.keypad-pin.json')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', b'1111', salt, keypad_store.ITERATIONS)
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO users (id,name,role,enabled,salt,digest,version,created_at)'
                   ' VALUES (?,?,?,?,?,?,?,?)',
                   ('admin1', 'Owner', 'admin', 1, salt, digest, 1, keypad_store.stamp()))
    admin = store.authenticate('1111')
    store.save_user(admin, {'id': 'admin1', 'name': 'Owner', 'role': 'admin',
                            'pin': '1111', 'confirmation': '1111',
                            'duress_pin': '9999', 'duress_confirmation': '9999'})
    return store


class WebhookCommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = seed_store()

    def setUp(self):
        self.calls = []
        server.request_unifi = lambda method, path, config=None: self.calls.append((method, path))
        server.WEBHOOKS.clear()
        server.WEBHOOKS.update({'arm': 'wh-arm', 'disarm': 'wh-disarm',
                                'arm_stay': 'wh-stay', 'panic': 'wh-panic'})
        server.UNLOCKS.clear()
        server.EXIT_UNTIL = 0

    def unlock_with(self, pin):
        claim = self.store.authenticate(pin)
        token = secrets.token_urlsafe(16)
        server.UNLOCKS[token] = {'expires': time.monotonic() + 60, 'user': claim}
        return token

    def latest_activity(self):
        events = self.store.history()['events']
        return events[0]

    def test_duress_disarm_fires_panic_silently(self):
        token = self.unlock_with('9999')  # duress PIN
        result = server.webhook_command('disarm', {'unlock': token, 'confirm': True})
        self.assertEqual(self.calls, [('POST', 'alarm-manager/webhook/wh-disarm'),
                                      ('POST', 'alarm-manager/webhook/wh-panic')])
        self.assertNotIn(token, server.UNLOCKS)  # grant consumed
        event = self.latest_activity()
        self.assertEqual(event['action'], 'disarm')
        self.assertEqual(event['status'], 'accepted')
        # The visible log must read as an ordinary disarm.
        self.assertNotIn('duress', (event['detail'] + event['action']).lower())
        self.assertNotIn('panic', (event['detail'] + event['action']).lower())
        self.assertIn('Disarm webhook accepted', result['message'])

    def test_normal_disarm_fires_no_panic(self):
        token = self.unlock_with('1111')
        server.webhook_command('disarm', {'unlock': token, 'confirm': True})
        self.assertEqual(self.calls, [('POST', 'alarm-manager/webhook/wh-disarm')])

    def test_duress_disarm_without_panic_webhook_still_disarms(self):
        del server.WEBHOOKS['panic']
        token = self.unlock_with('9999')
        server.webhook_command('disarm', {'unlock': token, 'confirm': True})
        self.assertEqual(self.calls, [('POST', 'alarm-manager/webhook/wh-disarm')])

    def test_duress_arm_does_not_fire_panic(self):
        token = self.unlock_with('9999')
        server.webhook_command('arm', {'unlock': token, 'confirm': True})
        self.assertEqual(self.calls, [('POST', 'alarm-manager/webhook/wh-arm')])
        self.assertGreater(server.EXIT_UNTIL, time.monotonic())

    def test_arm_stay_uses_stay_webhook(self):
        token = self.unlock_with('1111')
        result = server.webhook_command('arm_stay', {'unlock': token, 'confirm': True})
        self.assertEqual(self.calls, [('POST', 'alarm-manager/webhook/wh-stay')])
        self.assertTrue(result['message'].startswith('Arm stay webhook accepted'))
        event = self.latest_activity()
        self.assertEqual(event['action'], 'arm_stay')

    def test_arm_away_unconfigured_is_rejected(self):
        token = self.unlock_with('1111')
        with self.assertRaisesRegex(ValueError, 'installer must configure'):
            server.webhook_command('arm_away', {'unlock': token, 'confirm': True})
        self.assertEqual(self.calls, [])

    def test_duress_flag_helper(self):
        self.assertFalse(server.duress_flag({}))
        self.assertFalse(server.duress_flag({'unlock': 'nope'}))
        token = self.unlock_with('9999')
        self.assertTrue(server.duress_flag({'unlock': token}))
        token2 = self.unlock_with('1111')
        self.assertFalse(server.duress_flag({'unlock': token2}))

    def test_fire_panic_webhook_never_raises(self):
        server.request_unifi = lambda *a, **k: (_ for _ in ()).throw(OSError('down'))
        server.fire_panic_webhook()  # must not raise
        del server.WEBHOOKS['panic']
        server.fire_panic_webhook()  # unconfigured: also fine

    def test_duress_disarm_fires_panic_even_when_disarm_errors(self):
        def fail_on_disarm(method, path, config=None):
            self.calls.append((method, path))
            if path.endswith('wh-disarm'):
                raise OSError('console unreachable')
        server.request_unifi = fail_on_disarm
        token = self.unlock_with('9999')
        with self.assertRaises(OSError):
            server.webhook_command('disarm', {'unlock': token, 'confirm': True})
        # The silent alert is still attempted after the disarm failure.
        self.assertEqual(self.calls, [('POST', 'alarm-manager/webhook/wh-disarm'),
                                      ('POST', 'alarm-manager/webhook/wh-panic')])

    def test_duress_disarm_indistinguishable_when_panic_missing_or_failing(self):
        normal = server.webhook_command('disarm', {'unlock': self.unlock_with('1111'), 'confirm': True})
        # No panic webhook configured: the panel shows an ordinary disarm.
        del server.WEBHOOKS['panic']
        missing = server.webhook_command('disarm', {'unlock': self.unlock_with('9999'), 'confirm': True})
        self.assertEqual(missing['message'], normal['message'])
        event = self.latest_activity()
        self.assertEqual(event['action'], 'disarm')
        self.assertNotIn('panic', (event['detail'] + event['action']).lower())
        # Panic delivery failing: still an ordinary disarm at the panel.
        server.WEBHOOKS['panic'] = 'wh-panic'
        def fail_panic_only(method, path, config=None):
            self.calls.append((method, path))
            if path.endswith('wh-panic'):
                raise OSError('down')
        server.request_unifi = fail_panic_only
        failing = server.webhook_command('disarm', {'unlock': self.unlock_with('9999'), 'confirm': True})
        self.assertEqual(failing['message'], normal['message'])
        self.assertEqual(self.latest_activity()['action'], 'disarm')


class PowerEventTest(unittest.TestCase):
    """Server-level power-monitor integration: the outage webhook path."""

    def setUp(self):
        self.calls = []
        server.request_unifi = lambda method, path, config=None: self.calls.append((method, path))
        server.WEBHOOKS.clear()
        server.WEBHOOKS.update({'power': 'wh-power'})

    def latest_activity(self):
        return server.account_store().history()['events'][0]

    def test_outage_fires_configured_power_webhook(self):
        server.power_event('outage', {'source': 'test'})
        self.assertEqual(self.calls, [('POST', 'alarm-manager/webhook/wh-power')])
        self.assertEqual(self.latest_activity()['action'], 'power_outage')

    def test_outage_without_power_webhook_is_silent(self):
        del server.WEBHOOKS['power']
        server.power_event('outage', {'source': 'test'})  # must not raise
        self.assertEqual(self.calls, [])
        self.assertEqual(self.latest_activity()['action'], 'power_outage')

    def test_power_webhook_failure_never_raises(self):
        server.request_unifi = lambda *a, **k: (_ for _ in ()).throw(OSError('down'))
        server.power_event('outage', {'source': 'test'})  # must not raise
        server.power_event('restored', {})
        server.power_event('low_battery', {})

    def test_restored_and_low_battery_fire_no_webhook(self):
        server.power_event('restored', {})
        server.power_event('low_battery', {})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.latest_activity()['action'], 'power_low_battery')


if __name__ == '__main__':
    unittest.main()
