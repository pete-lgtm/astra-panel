import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import protection_state as saved
from live_entry import EntryObserver
import server


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.protection-state.json'
        self.site = {'version': 2, 'doors': ['Front', 'Back', 'Garage'], 'profileId': 'test-profile',
                     'sensors': [{'id': 'one', 'name': 'Front'}], 'appliedAt': 'first'}
        self.observer = EntryObserver()
        self.observer.ingest('disarmed', [], {})
        self.observer.doors = {name: {'connected': True, 'position': 'closed'} for name in self.site['doors']}

    def verify_all(self):
        self.observer.verified_doors.update(self.site['doors'])
        self.observer.verified_event = True
        saved.save(self.path, self.site, self.observer.snapshot())

    def test_partial_progress_survives_restart_without_enabling_protection(self):
        self.observer.verified_doors = {'Front'}
        saved.save_progress(self.path, self.site, self.observer.snapshot())
        restored = EntryObserver()
        saved.restore_progress(self.path, self.site, restored)
        self.assertEqual(restored.verified_doors, {'Front'})
        self.assertTrue(restored.verified_event)
        self.assertFalse(restored.enabled)
        self.assertFalse(saved.matches(self.path, self.site))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads(self.path.read_text())), {'version', 'siteFingerprint', 'verifiedDoors', 'enabled'})

    def test_incomplete_stale_or_open_checks_cannot_authorize_activation(self):
        with self.assertRaises(ValueError):
            saved.save(self.path, self.site, self.observer.snapshot())
        self.observer.verified_doors.update(self.site['doors'])
        for change in ({'fresh': False}, {'state': 'armed'}, {'doors': {}},
                       {'doors': {name: {'connected': True, 'position': 'open'} for name in self.site['doors']}}):
            with self.assertRaises(ValueError):
                saved.save(self.path, self.site, {**self.observer.snapshot(), **change})
        self.assertFalse(self.path.exists())

    def test_config_change_invalidates_progress_but_identical_resave_does_not(self):
        self.verify_all()
        self.assertTrue(saved.matches(self.path, {**self.site, 'appliedAt': 'second'}))
        for field, value in [('profileId', 'other'), ('doors', ['Front']), ('sensors', [{'id': 'replacement', 'name': 'Front'}]), ('host', '192.168.9.1')]:
            self.assertFalse(saved.matches(self.path, {**self.site, field: value}))

    def test_progress_does_not_revoke_prior_enable_and_ignores_unclosed_checks(self):
        self.observer.verified_doors = {'Front', 'Back'}
        self.observer.doors['Back']['position'] = 'open'
        saved.save_progress(self.path, self.site, self.observer.snapshot())
        self.assertEqual(saved.read(self.path, self.site)['verifiedDoors'], ['Front'])
        self.observer.doors['Back']['position'] = 'closed'
        self.verify_all()
        saved.save_progress(self.path, self.site, self.observer.snapshot())
        self.assertTrue(saved.matches(self.path, self.site))

    def test_corrupt_legacy_or_inconsistent_state_fails_closed(self):
        for contents in ('not json', '[]', 'x' * 9000, json.dumps({'version': 1}),
                         json.dumps({'version': 2, 'siteFingerprint': saved.fingerprint(self.site), 'verifiedDoors': ['Front'], 'enabled': True})):
            self.path.write_text(contents)
            self.assertFalse(saved.matches(self.path, self.site))

    def patches(self):
        session = server.LocalSession()
        session.host, session.cookie = '192.168.1.1', 'test-cookie'
        for patcher in (patch.object(server, 'PIN_FILE', self.path.parent / 'pin.json'),
                        patch.object(server, 'SITE', self.site), patch.object(server, 'ENTRY', self.observer),
                        patch.object(server, 'CONFIG', {'host': session.host}),
                        patch.object(server, 'LOCAL_SESSION', session), patch.object(server, 'RECOVERY_MESSAGE', '')):
            patcher.start()
            self.addCleanup(patcher.stop)
        return session

    def test_resume_rechecks_live_rules_outside_alarm_lock_and_logs_once(self):
        self.verify_all()
        self.patches()
        def verify(*args):
            self.assertTrue(server.LOCK.acquire(False))
            server.LOCK.release()
            return copy.deepcopy(self.site)
        with patch.object(server.sensor_settings, 'verify_for_activation', side_effect=verify) as verify_rules, patch.object(server, 'account_store') as store:
            server.restore_protection_once()
            server.restore_protection_once()
            verify_rules.assert_called_once()
            store.return_value.record.assert_called_once()
        self.assertTrue(self.observer.enabled)

    def test_failed_rules_reconnect_or_armed_state_never_resume(self):
        self.verify_all()
        session = self.patches()
        with patch.object(server.sensor_settings, 'verify_for_activation', side_effect=ValueError('Rules changed')):
            server.restore_protection_once()
        self.assertFalse(self.observer.enabled)
        self.assertEqual(server.RECOVERY_MESSAGE, 'Rules changed')
        def changed(*args):
            session.clear()
            return self.site
        with patch.object(server.sensor_settings, 'verify_for_activation', side_effect=changed):
            server.restore_protection_once()
        self.assertFalse(self.observer.enabled)
        session.cookie = 'test-cookie'
        self.observer.ingest('armed', [], {})
        with patch.object(server.sensor_settings, 'verify_for_activation') as verify:
            server.restore_protection_once()
            verify.assert_not_called()
        self.assertFalse(self.observer.enabled)

    def test_storage_failure_does_not_leave_new_protection_enabled(self):
        self.patches()
        self.observer.verified_doors.update(self.site['doors'])
        self.observer.verified_event = True
        with patch.object(saved, 'save', side_effect=OSError('Disk full')):
            with self.assertRaisesRegex(ValueError, 'storage'):
                server.enable_saved_protection()
        self.assertFalse(self.observer.enabled)
