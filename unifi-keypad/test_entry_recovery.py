"""Crash/restart alarm scenarios with simulated Protect and deterministic clocks."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from device_config import atomic_json
from entry_recovery import EntryRecovery
from live_entry import EntryObserver, configured_alarm_state
import protection_state
import server


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.path = self.root / '.entry-state.json'
        self.site = {'version': 2, 'consoleHost': '192.0.2.1', 'name': 'Test', 'profile': 'Home',
                     'profileId': 'main', 'stayProfileId': 'stay', 'awayProfileId': 'away',
                     'doors': ['Front', 'Back'], 'sensors': [{'id': 'front', 'name': 'Front'}, {'id': 'back', 'name': 'Back'}]}
        self.profiles = [{'id': key, 'state': 'disarmed', 'state_set_at': stamp(900)} for key in ('main', 'stay', 'away')]
        self.now, self.mono = 1000., 500.
        self.boot_count = 0
        self.scope = {'names': {'Front'}, 'bypassed': [{'name': 'Back', 'reason': 'Unreachable'}]}
        self.observer = self.observer_new()
        self.recovery = EntryRecovery(self.path, self.site, boot='boot-0')
        self.recovery.restore(self.observer, self.profiles)
        self.ingest()
        self.observer.sensor_update({'name': 'Front', 'isOpened': False, 'openStatusChangedAt': 900000})
        self.observer.verified_doors = {'Front', 'Back'}; self.observer.verified_event = True
        self.observer.enabled = True
        self.observer.active_names = {'Front'}
        self.observer.checkpoint()

    def observer_new(self):
        return EntryObserver(clock=lambda: self.mono, wall=lambda: self.now)

    def advance(self, seconds):
        self.now += seconds; self.mono += seconds

    def ingest(self, events=()):
        selected = configured_alarm_state(self.profiles, self.site)
        self.recovery.set_profiles(self.profiles)
        self.observer.ingest(selected['state'], events, {'front': 'Front', 'back': 'Back'}, selected['state_set_at'])

    def arm(self):
        self.advance(10)
        self.profiles[1].update(state='armed', state_set_at=stamp(self.now))
        self.ingest(); self.observer.checkpoint()

    def opened(self, sensor='front', seconds=None, identifier='event-1'):
        return {'id': identifier, 'type': 'sensorOpened', 'sensor': sensor,
                'start': (self.now if seconds is None else seconds)*1000}

    def entry(self):
        self.arm(); self.advance(5)
        self.profiles[1].update(state='breached', state_set_at=stamp(self.now))
        self.event = self.opened()
        self.ingest([self.event]); self.observer.checkpoint()

    def reboot(self, seconds=15, same_boot=False, ready=True):
        self.advance(seconds)
        if not same_boot:
            self.mono = 12.
            self.boot_count += 1
        self.recovery = EntryRecovery(self.path, self.site, boot='boot-'+str(self.boot_count), clock_ready=lambda: ready)
        self.observer = self.observer_new()
        self.recovery.restore(self.observer, self.profiles, self.scope)
        self.ingest()

    def test_reboot_keeps_original_deadline_and_bypass_scope(self):
        self.entry(); self.reboot(23)
        self.assertEqual(self.observer.snapshot()['remaining'], 37)
        self.assertEqual(self.observer.active_names, {'Front'})
        self.assertEqual(self.observer.bypassed[0]['name'], 'Back')
        self.ingest([self.event, self.opened(identifier='second')])
        self.assertEqual(self.observer.snapshot()['remaining'], 37)
        calls=[]; self.advance(36); self.observer.dispatch(lambda:calls.append(1)); self.assertEqual(calls, [])
        self.advance(1); self.observer.dispatch(lambda:calls.append(1)); self.assertEqual(calls, [1])
        self.reboot(10); self.observer.dispatch(lambda:calls.append(2)); self.assertEqual(calls, [1])

    def test_overdue_countdown_fires_once_after_live_reconciliation(self):
        self.entry(); self.reboot(90)
        self.assertIsNone(self.observer.snapshot()['remaining'])
        calls=[]; self.observer.dispatch(lambda:calls.append(1)); self.observer.dispatch(lambda:calls.append(2))
        self.assertEqual(calls,[1])

    def test_same_boot_restart_uses_monotonic_deadline(self):
        self.entry(); self.reboot(15,same_boot=True)
        self.assertEqual(self.observer.snapshot()['remaining'],45)
        self.observer.checkpoint(); self.reboot(10,same_boot=True)
        self.assertEqual(self.observer.snapshot()['remaining'],35)

    def test_crash_after_delivery_intent_never_replays_webhook(self):
        self.entry(); self.advance(60)
        def crash():
            self.assertEqual(json.loads(self.path.read_text())['observer']['output'],'sending')
            raise SystemExit('Simulated sudden power loss')
        with self.assertRaises(SystemExit): self.observer.dispatch(crash)
        self.reboot(3)
        self.assertEqual(self.observer.output,'uncertain')
        send=Mock(); self.observer.dispatch(send); send.assert_not_called()
        self.assertIn('uncertain',self.observer.snapshot()['message'])

    def test_timeout_outcome_survives_multiple_restarts(self):
        self.entry(); self.advance(60)
        self.observer.dispatch(Mock(side_effect=OSError('timeout')))
        self.reboot(1); self.observer.checkpoint(); self.reboot(2,same_boot=False)
        self.assertEqual(self.observer.output,'uncertain')
        send=Mock(); self.observer.dispatch(send); send.assert_not_called()

    def test_storage_failure_prevents_unjournaled_delivery_and_can_retry(self):
        self.entry(); self.advance(60); send=Mock()
        with patch('entry_recovery.atomic_json',side_effect=OSError('full')):
            with self.assertRaisesRegex(ValueError,'storage'): self.observer.dispatch(send)
        send.assert_not_called(); self.assertEqual(self.observer.output,'not_sent')
        self.observer.dispatch(send); send.assert_called_once()

    def test_disarm_while_power_is_off_cancels_without_sending(self):
        self.entry()
        self.profiles[1].update(state='disarmed',state_set_at=stamp(self.now+10))
        self.reboot(90)
        self.assertIsNone(self.observer.deadline); self.assertEqual(self.observer.output,'not_sent')
        send=Mock(); self.observer.dispatch(send); send.assert_not_called()
        self.observer.checkpoint(); self.assertEqual(json.loads(self.path.read_text())['observer']['state'],'disarmed')

    def test_exit_countdown_reads_original_protect_timestamp(self):
        self.profiles[1].update(state='arming',state_set_at=stamp(self.now))
        self.ingest(); self.observer.checkpoint(); self.reboot(28)
        self.assertEqual(self.observer.snapshot()['exitRemaining'],32)
        self.assertIsNone(self.observer.deadline)

    def test_opening_during_downtime_is_recovered_from_real_history(self):
        self.arm(); event=self.opened(seconds=self.now+8)
        self.profiles[1].update(state='breached',state_set_at=stamp(self.now+8))
        self.reboot(30)
        self.assertLessEqual(self.recovery.history_start(self.observer),event['start'])
        self.ingest([event]); self.observer.checkpoint(); self.recovery.history_complete(self.observer)
        self.assertEqual(self.observer.snapshot()['remaining'],38)
        self.assertEqual(self.observer.event_window_seconds,120)

    def test_bypassed_door_during_downtime_does_not_start_entry(self):
        self.arm(); event=self.opened('back',self.now+3)
        self.reboot(30); self.ingest([event]); self.assertIsNone(self.observer.deadline)

    def test_new_native_arm_cycle_does_not_reuse_old_alert_or_event(self):
        self.entry(); self.advance(60); self.observer.dispatch(lambda:None)
        old=self.event
        self.profiles[1].update(state='armed',state_set_at=stamp(self.now+10))
        self.reboot(20); self.ingest([old])
        self.assertEqual(self.observer.output,'not_sent'); self.assertIsNone(self.observer.deadline)
        self.ingest([self.opened(identifier='new-cycle')]); self.assertEqual(self.observer.snapshot()['remaining'],60)

    def test_clock_not_ready_or_rollback_waits_without_touching_record(self):
        self.entry(); original=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'Synchronizing'): self.reboot(10,ready=False)
        self.assertEqual(self.path.read_bytes(),original); self.assertTrue(self.recovery.pending)
        self.now=900
        with self.assertRaises(ValueError): self.recovery.restore(self.observer,self.profiles,self.scope)
        self.assertEqual(self.path.read_bytes(),original)

    def test_corrupt_changed_site_or_unsafe_record_never_arms_itself(self):
        self.entry(); original=self.path.read_bytes()
        variants=[b'not json',json.dumps({**json.loads(original),'configuration':'wrong'}).encode()]
        for contents in variants:
            self.path.write_bytes(contents)
            record=EntryRecovery(self.path,self.site,boot='boot-b')
            fresh=self.observer_new()
            with self.assertRaises(ValueError): record.restore(fresh,self.profiles,self.scope)
            self.assertFalse(fresh.enabled); self.assertTrue(record.pending)
        self.path.write_bytes(original); self.path.chmod(0o644)
        record=EntryRecovery(self.path,self.site)
        self.assertTrue(record.read_error)

    def test_save_is_private_bounded_and_no_secrets(self):
        self.entry(); data=json.loads(self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        self.assertNotIn('webhooks',data); self.assertNotIn('cookie',self.path.read_text())
        with patch('entry_recovery.atomic_json',wraps=atomic_json) as write:
            self.advance(1); self.ingest(); self.observer.checkpoint(); write.assert_not_called()
            self.advance(30); self.ingest(); self.observer.checkpoint(); write.assert_called_once()

    def test_fresh_disarm_recovers_from_a_corrupt_file(self):
        self.path.write_text('broken')
        recovery=EntryRecovery(self.path,self.site); observer=self.observer_new()
        recovery.restore(observer,self.profiles)
        observer.ingest('disarmed',[],{},stamp(self.now)); observer.checkpoint()
        self.assertFalse(recovery.pending); self.assertFalse(recovery.read_error)
        self.assertEqual(json.loads(self.path.read_text())['observer']['state'],'disarmed')

    def test_infinite_or_inconsistent_deadline_is_rejected(self):
        self.entry(); data=json.loads(self.path.read_text())
        for key,value in [('entryDueMs',float('inf')),('entryStartedMs',1),('output','invented')]:
            changed=copy.deepcopy(data); changed['observer'][key]=value; atomic_json(self.path,changed)
            self.assertTrue(EntryRecovery(self.path,self.site).read_error)

    def server_recovery_fixture(self):
        self.entry(); self.advance(20)
        recovery=EntryRecovery(self.path,self.site,boot='boot-b'); fresh=self.observer_new()
        selected=configured_alarm_state(self.profiles,self.site)
        fresh.ingest(selected['state'],[],{},selected['state_set_at']); recovery.set_profiles(self.profiles)
        session=server.LocalSession(); session.host=self.site['consoleHost']; session.cookie='synthetic'
        runtime={'phase':'sent','activeIds':['front'],'bypassed':self.scope['bypassed'],'mode':'arm_stay'}
        def verify(*args,**kwargs):
            self.assertTrue(server.LOCK.acquire(False)); server.LOCK.release()
            return {'current':{},'payload':{}}
        patches=[patch.object(server,'SITE',self.site),patch.object(server,'ROOT',self.root),
                 patch.object(server,'PIN_FILE',self.root/'pin.json'),patch.object(server,'ENTRY',fresh),
                 patch.object(server,'ENTRY_RECOVERY',recovery),patch.object(server,'CONFIG',{'host':session.host}),
                 patch.object(server,'LOCAL_SESSION',session),patch.object(server.arm_coverage,'saved',return_value=runtime),
                 patch.object(server.protection_state,'read',return_value={'verifiedDoors':['Front','Back']}),
                 patch.object(server.arm_coverage,'prepare',side_effect=verify),
                 patch.object(server.onboarding,'equivalent',return_value=True),patch.object(server,'account_store')]
        for p in patches:p.start();self.addCleanup(p.stop)
        return recovery,fresh,session

    def test_server_recovery_verifies_rules_outside_lock_and_keeps_pin_access(self):
        recovery,fresh,session=self.server_recovery_fixture()
        self.assertTrue(server.restore_entry_runtime_once())
        self.assertFalse(recovery.pending); self.assertTrue(fresh.enabled)
        self.assertEqual(fresh.snapshot()['remaining'],40)
        self.assertEqual(fresh.active_names,{'Front'})

    def test_server_rule_mismatch_does_not_restore_or_replay(self):
        recovery,fresh,session=self.server_recovery_fixture()
        original=self.path.read_bytes()
        with patch.object(server.arm_coverage,'prepare',side_effect=ValueError('Rules changed')):
            self.assertTrue(server.restore_entry_runtime_once())
        self.assertTrue(recovery.pending); self.assertFalse(fresh.enabled)
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(fresh.snapshot()['state'],'breached')

    def test_server_disarm_during_rule_verification_wins_over_saved_timer(self):
        recovery,fresh,session=self.server_recovery_fixture()
        def verify(*a,**k):
            for profile in self.profiles:profile.update(state='disarmed',state_set_at=stamp(self.now))
            recovery.set_profiles(self.profiles);fresh.ingest('disarmed',[],{},stamp(self.now))
            return {'current':{},'payload':{}}
        with patch.object(server.arm_coverage,'prepare',side_effect=verify):
            server.restore_entry_runtime_once()
        self.assertFalse(recovery.pending);self.assertIsNone(fresh.deadline)
        self.assertFalse(fresh.enabled);self.assertEqual(fresh.output,'not_sent')

    def test_changed_console_session_invalidates_recovery_verification(self):
        recovery,fresh,session=self.server_recovery_fixture()
        def verify(*a,**k):
            session.clear();return {'current':{},'payload':{}}
        with patch.object(server.arm_coverage,'prepare',side_effect=verify):
            server.restore_entry_runtime_once()
        self.assertTrue(recovery.pending);self.assertFalse(fresh.enabled)
        self.assertIn('changed',server.RECOVERY_MESSAGE)


if __name__=='__main__': unittest.main()
