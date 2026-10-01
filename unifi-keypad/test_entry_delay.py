import unittest
from entry_delay import EntryDelay

class EntryDelayTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.alarm = EntryDelay(clock=lambda: self.now)

    def start(self):
        self.alarm.update('confirmed_arm')
        self.alarm.update('door_open')

    def test_deadline_fires_at_sixty_seconds(self):
        self.start()
        self.now = 59
        self.assertEqual(self.alarm.snapshot()['remaining'], 1)
        self.now = 60
        self.assertEqual(self.alarm.snapshot()['state'], 'alarm_due')

    def test_repeated_door_does_not_extend_timer(self):
        self.start()
        self.now = 20
        self.alarm.update('door_open')
        self.assertEqual(self.alarm.snapshot()['remaining'], 40)

    def test_request_without_confirmation_does_not_cancel(self):
        self.start()
        self.now = 10
        self.alarm.update('disarm_requested')
        self.now = 60
        self.assertEqual(self.alarm.snapshot()['state'], 'alarm_due')

    def test_confirmed_disarm_cancels(self):
        self.start()
        self.now = 59
        self.alarm.update('confirmed_disarm')
        self.now = 65
        self.assertEqual(self.alarm.snapshot()['state'], 'disarmed')

    def test_connection_loss_preserves_deadline(self):
        self.start()
        self.alarm.update('connection_lost')
        with self.assertRaises(ValueError):
            self.alarm.update('confirmed_disarm')
        self.now = 60
        self.assertEqual(self.alarm.snapshot()['state'], 'alarm_due')

    def test_unknown_or_disarmed_door_does_not_start(self):
        self.alarm.update('door_open')
        self.assertIsNone(self.alarm.deadline)
        self.alarm.update('confirmed_disarm')
        self.alarm.update('door_open')
        self.assertIsNone(self.alarm.deadline)

    def test_deadline_logged_once(self):
        self.start()
        self.now = 60
        self.alarm.snapshot()
        count = len(self.alarm.events)
        self.alarm.snapshot()
        self.assertEqual(len(self.alarm.events), count)

    def test_restart_is_unknown(self):
        self.start()
        self.assertEqual(EntryDelay().state, 'unknown')
