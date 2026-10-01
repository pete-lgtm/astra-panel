"""Durable entry timing and delivery intent; never a source of live alarm truth.

The caller must re-read Protect and verify the saved rules/scope before restoring
an active cycle. This file contains no credentials, PINs or webhook URLs.
"""
import copy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import stat

from device_config import atomic_json
from live_entry import configured_alarm_state
from protection_state import fingerprint

ACTIVE = ('arming', 'armed', 'breached')
OUTPUTS = ('not_sent', 'sending', 'accepted', 'uncertain')
MAX_GAP_SECONDS = 86400


def number(value, nullable=False):
    return value is None and nullable or type(value) in (int, float) and math.isfinite(value) and value >= 0


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.timestamp()*1000 if result.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def profile_marks(profiles, site):
    configured_alarm_state(profiles, site)
    ids = {site.get(k) for k in ('profileId', 'stayProfileId', 'awayProfileId')} - {None, ''}
    return {p['id']: {'state': p['state'], 'changedMs': timestamp(p.get('state_set_at'))}
            for p in profiles if p.get('id') in ids}


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        # A desktop process restart uses wall time, never an invented boot match.
        return ''


class EntryRecovery:
    def __init__(self, path, site, boot=None, clock_ready=lambda: True):
        self.path, self.site = Path(path), copy.deepcopy(site)
        self.boot = boot_id() if boot is None else boot
        self.clock_ready = clock_ready
        self.record = None
        self.read_error = ''
        self.pending = True
        self.profiles = {}
        self.replay_start_ms = None
        self.last_written = None
        self.last_key = None
        self.storage_error = ''
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 120000 or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('Unsafe recovery record')
            record = json.loads(self.path.read_text())
            self.validate(record)
            self.record = record
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            self.read_error = 'Saved alarm recovery could not be verified. Confirm disarmed to reset it.'

    def validate(self, record):
        keys = {'version', 'configuration', 'boot', 'writtenAt', 'monotonicAt', 'profiles', 'observer'}
        if (not isinstance(record, dict) or set(record) != keys or record['version'] != 1 or
                record['configuration'] != fingerprint(self.site) or not isinstance(record['boot'], str) or
                not number(record['writtenAt']) or not number(record['monotonicAt'])):
            raise ValueError('Recovery record does not match this installation')
        data = record['observer']
        expected = {'state', 'enabled', 'output', 'startedMs', 'lastDisarmedMs', 'armedSinceMs',
                    'lastConfirmedMs', 'entryStartedMs', 'entryDueMs', 'entryDueMonotonic', 'seen', 'baseline'}
        if (not isinstance(data, dict) or set(data) != expected or data['state'] not in ('unknown', 'disarmed', *ACTIVE) or
                type(data['enabled']) is not bool or data['output'] not in OUTPUTS or
                not all(number(data[k]) for k in ('startedMs', 'lastDisarmedMs')) or
                not all(number(data[k], True) for k in ('armedSinceMs', 'lastConfirmedMs', 'entryStartedMs', 'entryDueMs', 'entryDueMonotonic')) or
                not isinstance(data['seen'], list) or len(data['seen']) > 2000 or
                any(not isinstance(x, str) or len(x) > 256 for x in data['seen'])):
            raise ValueError('Invalid saved entry state')
        if ((data['entryStartedMs'] is None) != (data['entryDueMs'] is None) or
                (data['entryDueMs'] is None) != (data['entryDueMonotonic'] is None) or
                data['entryStartedMs'] is not None and data['entryDueMs'] != data['entryStartedMs'] + 60000 or
                data['output'] != 'not_sent' and data['entryStartedMs'] is None):
            raise ValueError('Inconsistent saved deadline')
        baseline = data['baseline']
        if (not isinstance(baseline, dict) or not set(baseline) <= set(self.site['doors']) or
                any(not isinstance(v, list) or len(v) != 2 or v[0] is not None and type(v[0]) is not bool or
                    not number(v[1], True) for v in baseline.values())):
            raise ValueError('Invalid saved sensor baseline')
        expected_ids = {self.site.get(k) for k in ('profileId', 'stayProfileId', 'awayProfileId')} - {None, ''}
        if (not isinstance(record['profiles'], dict) or set(record['profiles']) != expected_ids or
                any(not isinstance(v, dict) or set(v) != {'state', 'changedMs'} or
                    v['state'] not in ('disarmed', *ACTIVE) or not number(v['changedMs'], True)
                    for v in record['profiles'].values())):
            raise ValueError('Invalid saved alarm profiles')

    def set_profiles(self, profiles):
        self.profiles = profile_marks(profiles, self.site)

    def continuous(self, current):
        previous = self.record['profiles']
        data = self.record['observer']
        for identifier, value in current.items():
            old = previous.get(identifier, {})
            if value['state'] not in ACTIVE or old.get('state') not in ACTIVE:
                continue
            if value == old:
                return True
            if old['state'] == 'armed' and value['state'] == 'breached':
                trigger = data['entryStartedMs']
                if trigger is None or abs(value['changedMs'] - trigger) <= 2000:
                    return True
        return False

    def restore(self, observer, profiles, scope=None):
        """scope is supplied only after independent, current rule verification."""
        selected = configured_alarm_state(profiles, self.site)
        current = profile_marks(profiles, self.site)
        now = observer.wall()
        if selected['state'] == 'disarmed':
            # Saved armed/breach state never overrides a real confirmed disarm.
            self.record = None
            self.read_error = ''
        else:
            if self.read_error:
                raise ValueError(self.read_error)
            if not self.record:
                raise ValueError('No saved alarm cycle is available. Confirm disarmed before resuming protection.')
            if scope is None:
                raise ValueError('Checking saved alarm rules before resuming protection.')
            active = [v for v in current.values() if v['state'] in ACTIVE]
            if any(v['changedMs'] is None or not 0 <= v['changedMs'] <= now*1000 + 2000 for v in active):
                raise ValueError('Protect must confirm alarm timing before restart recovery can finish.')
            data = self.record['observer']
            same_boot = bool(self.boot) and self.boot == self.record['boot']
            if not same_boot and not self.clock_ready():
                raise ValueError('Synchronizing the panel clock before restoring the alarm countdown.')
            elapsed = observer.clock() - self.record['monotonicAt'] if same_boot else now - self.record['writtenAt']
            if elapsed < -2 or not same_boot and now*1000 < (data['lastConfirmedMs'] or 0) - 2000:
                raise ValueError('Panel time changed. Waiting for a verified clock before alarm recovery.')
            continuity = self.continuous(current)
            if continuity:
                observer.state = data['state']
                observer.output = 'uncertain' if data['output'] == 'sending' else data['output']
                observer.last_disarmed_ms = data['lastDisarmedMs']
                observer.armed_since_ms = data['armedSinceMs']
                observer.seen = set(data['seen'])
                observer.sensor_baseline = {k: tuple(v) for k, v in data['baseline'].items()}
                observer.entry_started_ms = data['entryStartedMs']
                observer.entry_due_ms = data['entryDueMs']
                if data['entryDueMs'] is not None:
                    due = data['entryDueMonotonic'] if same_boot else observer.clock() + max(0, data['entryDueMs']/1000 - now)
                    observer.entry_due_monotonic = due
                    observer.deadline = due if observer.output == 'not_sent' else None
                    observer.result = {'not_sent': 'Entry countdown restored after restart.',
                                       'accepted': 'Delayed notification request was already accepted by Protect.',
                                       'uncertain': 'Delayed notification outcome uncertain after restart. Check Protect immediately.'}[observer.output]
                floor = max(data['lastDisarmedMs'], data['armedSinceMs'] or 0, (data['lastConfirmedMs'] or self.record['writtenAt']*1000) - 2000)
            else:
                # A fresh native arm timestamp represents a new cycle. Old
                # delivery markers and old sensor events cannot leak into it.
                observer.state = 'unknown'
                observer.output = 'not_sent'
                observer.deadline = None
                observer.entry_started_ms = observer.entry_due_ms = observer.entry_due_monotonic = None
                observer.seen.clear()
                observer.sensor_baseline.clear()
                observer.result = None
                changed = timestamp(selected['state_set_at'])
                floor = changed - (2000 if selected['state'] == 'breached' else 0)
                observer.last_disarmed_ms = max(0, floor)
                observer.armed_since_ms = max(0, floor) if selected['state'] != 'arming' else None
            self.replay_start_ms = max(0, floor)
            if now*1000 - self.replay_start_ms > MAX_GAP_SECONDS*1000:
                # An already-recorded deadline or delivery intent is complete
                # evidence. A long gap without one requires an explicit check.
                if not continuity or data['entryDueMs'] is None:
                    raise ValueError('Alarm history gap is too long to verify. Confirm disarmed before resuming protection.')
                self.replay_start_ms = now*1000 - 120000
            observer.started_ms = self.replay_start_ms
            observer.event_window_seconds = max(120, now - self.replay_start_ms/1000 + 5)
            observer.active_names, observer.bypassed = set(scope['names']), copy.deepcopy(scope['bypassed'])
            observer.enabled = True
        self.profiles = current
        self.pending = False
        observer.checkpoint = lambda: self.save(observer)

    def history_start(self, observer):
        return self.replay_start_ms if self.replay_start_ms is not None else max(observer.started_ms, (observer.wall()-120)*1000)

    def history_complete(self, observer):
        self.replay_start_ms = None
        observer.event_window_seconds = 120

    def save(self, observer, force=False):
        if self.pending or not self.profiles:
            raise ValueError('Alarm recovery is not verified yet.')
        data = {'state': observer.state, 'enabled': observer.enabled, 'output': observer.output,
                'startedMs': observer.started_ms, 'lastDisarmedMs': observer.last_disarmed_ms,
                'armedSinceMs': observer.armed_since_ms, 'lastConfirmedMs': observer.last_confirmed_ms,
                'entryStartedMs': observer.entry_started_ms, 'entryDueMs': observer.entry_due_ms,
                'entryDueMonotonic': observer.entry_due_monotonic,
                'seen': sorted(observer.seen), 'baseline': {k: list(v) for k, v in observer.sensor_baseline.items()}}
        record = {'version': 1, 'configuration': fingerprint(self.site), 'boot': self.boot,
                  'writtenAt': observer.wall(), 'monotonicAt': observer.clock(),
                  'profiles': copy.deepcopy(self.profiles), 'observer': data}
        self.validate(record)
        # Write meaningful transitions immediately; a quiet panel checkpoints
        # at most every 30 seconds instead of wearing the SD card on every poll.
        key = {**data, 'profiles': self.profiles}
        key.pop('lastDisarmedMs'); key.pop('lastConfirmedMs')
        if not force and key == self.last_key and self.last_written is not None and observer.clock()-self.last_written < 30:
            return
        try:
            atomic_json(self.path, record)
        except OSError:
            self.storage_error = 'Alarm recovery could not be saved. Check panel storage.'
            raise ValueError(self.storage_error) from None
        self.record = record
        self.last_key = copy.deepcopy(key)
        self.last_written = observer.clock()
        self.storage_error = ''
