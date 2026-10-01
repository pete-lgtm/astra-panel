"""Exercise the real poller and observer with separate native Stay/Away profiles."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import server
from live_entry import EntryObserver, configured_alarm_state


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class AlarmModeTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.site = {'profileId': 'main', 'stayProfileId': 'stay', 'awayProfileId': 'away',
                     'sensors': [{'id': 'front', 'name': 'Front'}]}
        self.profiles = [{'id': identifier, 'title': title, 'state': 'disarmed', 'state_set_at': timestamp(900)}
                         for identifier, title in [('main', 'Arm'), ('stay', 'Stay'), ('away', 'Away')]]
        self.observer = EntryObserver(clock=lambda: self.now, wall=lambda: self.now)
        self.events = []
        session = SimpleNamespace(cookie='synthetic', refresh_if_due=lambda: None,
                                  profiles=lambda: {'profiles': self.profiles}, door_events=lambda since: self.events)
        self.door = {'id': 'front', 'name': 'Front', 'state': 'CONNECTED', 'isOpened': False,
                     'openStatusChangedAt': 900000}
        self.store = Mock()
        self.request = Mock(return_value=[self.door])
        for target, name, value in [
                (server, 'SITE', self.site), (server, 'ENTRY_DOORS', {'Front'}),
                (server, 'CONFIG', {'host': 'test', 'key': 'synthetic'}),
                (server, 'ENTRY', self.observer), (server, 'LOCAL_SESSION', session),
                (server, 'account_store', lambda: self.store), (server, 'request_unifi', self.request),
                (server, 'selected_sensors', lambda *a, **k: [self.door]),
                (server.door_policy, 'read', lambda *a: {'modes': {}})]:
            p = patch.object(target, name, value); p.start(); self.addCleanup(p.stop)

    def poll(self):
        server.observe_entry_once()
        return self.observer.snapshot()

    def test_stay_arming_countdown_advances_and_then_becomes_armed(self):
        self.assertEqual(self.poll()['state'], 'disarmed')
        self.now = 1010
        self.profiles[1].update(state='arming', state_set_at=timestamp(1005))
        result = self.poll()
        self.assertEqual((result['state'], result['exitRemaining']), ('arming', 55))
        self.now += 20
        self.assertEqual(self.poll()['exitRemaining'], 35, 'Polling must not restart the countdown')
        self.profiles[1].update(state='armed', state_set_at=timestamp(1065))
        self.now = 1065
        result = self.poll()
        self.assertEqual(result['state'], 'armed')
        self.assertIsNone(result['exitRemaining'])
        self.store.confirm_commands.assert_called_with('armed', timestamp(1065))
        self.assertTrue(all(call.args[:2] == ('GET', 'sensors') for call in self.request.call_args_list))

    def test_away_entry_timer_not_cancelled_by_disarmed_main_or_stay(self):
        self.poll()
        self.now = 1010
        self.profiles[2].update(state='armed', state_set_at=timestamp(1010))
        self.poll()
        self.now = 1015
        self.events = [{'id': 'open-front', 'type': 'sensorOpened', 'sensor': 'front', 'start': 1015000}]
        self.assertEqual(self.poll()['remaining'], 60)
        self.now = 1025
        self.assertEqual(self.poll()['remaining'], 50)
        # Even starting another profile cannot reset an existing entry timer.
        self.profiles[1].update(state='arming', state_set_at=timestamp(1025))
        self.assertEqual(self.poll()['remaining'], 50)
        self.profiles[2].update(state='breached', state_set_at=timestamp(1015))
        self.assertEqual(self.poll()['state'], 'breached')
        for profile in self.profiles:
            profile.update(state='disarmed', state_set_at=timestamp(1025))
        self.assertIsNone(self.poll()['remaining'])
        self.assertEqual(self.observer.state, 'disarmed')

    def test_missing_mode_never_reports_disarmed_or_cancels_entry_timer(self):
        self.poll()
        self.profiles[1].update(state='armed', state_set_at=timestamp(1005))
        self.now = 1010
        self.events = [{'id': 'open-front', 'type': 'sensorOpened', 'sensor': 'front', 'start': 1010000}]
        self.assertEqual(self.poll()['remaining'], 60)
        self.profiles.pop()
        result = self.poll()
        self.assertFalse(result['fresh'])
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['remaining'], 60)

    def test_confirmed_disarm_cancels_even_when_event_history_is_down(self):
        self.poll(); self.observer.enabled=True
        self.profiles[1].update(state='armed',state_set_at=timestamp(1005))
        self.now=1010; self.events=[{'id':'entry','type':'sensorOpened','sensor':'front','start':1010000}]
        self.assertEqual(self.poll()['remaining'],60)
        self.now=1071
        for profile in self.profiles: profile.update(state='disarmed',state_set_at=timestamp(1070))
        with patch.object(server.LOCAL_SESSION,'door_events',side_effect=ValueError('History unavailable')):
            self.poll()
        self.assertIsNone(self.observer.deadline)
        self.assertEqual(self.observer.output,'not_sent')
        self.assertTrue(all(call.args[:2]==('GET','sensors') for call in self.request.call_args_list))

    def test_duplicate_and_unrecognized_profiles_fail_closed(self):
        for bad in ([*self.profiles, self.profiles[1]], [{**p, 'state': 'unexpected'} for p in self.profiles]):
            with self.assertRaises(ValueError):
                configured_alarm_state(bad, self.site)

    def test_missing_timing_does_not_invent_a_countdown(self):
        self.profiles[1].update(state='arming', state_set_at=None)
        result = self.poll()
        self.assertTrue(result['fresh'])
        self.assertEqual(result['state'], 'arming')
        self.assertIsNone(result['exitRemaining'])

    def test_oldest_active_time_and_latest_disarm_and_legacy_title(self):
        self.profiles[1].update(state='arming', state_set_at=timestamp(970))
        self.profiles[2].update(state='arming', state_set_at=timestamp(990))
        self.assertEqual(self.poll()['exitRemaining'], 30)
        for profile in self.profiles:
            profile['state'] = 'disarmed'
        self.assertEqual(configured_alarm_state(self.profiles, self.site)['state_set_at'], timestamp(990))
        self.assertEqual(configured_alarm_state(self.profiles, {'profile': 'Arm'})['state'], 'disarmed')


if __name__ == '__main__':
    unittest.main()
