"""Track confirmed Protect state and dispatch one delayed notification per arm cycle."""
from datetime import datetime
import math
import time


def configured_alarm_state(profiles, site):
    """Only report Disarmed when every configured alarm mode confirms it."""
    if not isinstance(profiles, list) or any(not isinstance(p, dict) for p in profiles):
        raise ValueError('Alarm profiles could not be verified. Check Protect.')
    selected = []
    for field in ('profileId', 'stayProfileId', 'awayProfileId'):
        identifier = site.get(field)
        if field != 'profileId' and not identifier:
            continue
        if not identifier and not site.get('profile'):
            raise ValueError('The alarm profile has not been configured.')
        matches = [p for p in profiles if p.get('id') == identifier] if identifier else [
            p for p in profiles if p.get('title') == site.get('profile')]
        if len(matches) != 1 or matches[0].get('state') not in ('disarmed', 'arming', 'armed', 'breached'):
            raise ValueError('A configured alarm mode could not be verified. Check Protect.')
        selected.append(matches[0])
    # An inactive main profile must never hide Stay/Away, and a newly arming
    # profile must not cancel protection already active in another mode.
    priority = {'disarmed': 0, 'arming': 1, 'armed': 2, 'breached': 3}
    state = max((p['state'] for p in selected), key=priority.get)
    candidates = [p for p in selected if p['state'] == state]
    dated = []
    for profile in candidates:
        try:
            when = datetime.fromisoformat(profile['state_set_at'].replace('Z', '+00:00'))
            if when.tzinfo is None:
                raise ValueError()
            dated.append((when.timestamp(), profile['state_set_at']))
        except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
            # Keep the confirmed state, but do not invent a countdown or a
            # command-confirmation timestamp when Protect omitted timing.
            return {'state': state, 'state_set_at': None}
    changed = (max(dated) if state == 'disarmed' else min(dated))[1]
    return {'state': state, 'state_set_at': changed}


class EntryObserver:
    def __init__(self, clock=time.monotonic, wall=time.time):
        self.clock, self.wall = clock, wall
        self.started_ms = wall()*1000
        self.state = 'unknown'
        self.enabled = False
        self.output = 'not_sent'
        self.verified_event = False
        self.verified_doors = set()
        self.sensor_baseline = {}
        self.doors = {}
        self.deadline = None
        self.seen = set()
        self.last_ok = None
        self.error = 'Sign in to start live observation.'
        self.last_event = None
        self.result = None
        self.last_disarmed_ms = self.started_ms
        self.armed_since_ms = None
        self.exit_until = None
        self.active_names = None
        self.bypassed = []
        self.entry_started_ms = None
        self.entry_due_ms = None
        self.entry_due_monotonic = None
        self.last_confirmed_ms = None
        self.event_window_seconds = 120
        self.checkpoint = lambda: None

    def ingest(self, profile_state, events, sensor_ids, state_set_at=None):
        if profile_state not in ('armed', 'disarmed', 'arming', 'breached'):
            raise ValueError('Global alarm state is not recognized.')
        now = self.clock()
        previous_state = self.state
        changed_ms = None
        if isinstance(state_set_at, str):
            try:
                stamp = datetime.fromisoformat(state_set_at.replace('Z', '+00:00'))
                if stamp.tzinfo is not None:
                    candidate = stamp.timestamp()*1000
                    if 0 < candidate <= self.wall()*1000 + 2000:
                        changed_ms = candidate
            except ValueError:
                pass
        self.state = profile_state
        self.last_ok = now
        self.last_confirmed_ms = self.wall()*1000
        self.error = None
        if profile_state == 'disarmed':
            self.last_disarmed_ms = self.wall()*1000
            self.armed_since_ms = None
            if self.deadline is not None:
                self.result = 'Disarm confirmed; entry countdown cancelled.'
            elif previous_state != 'disarmed':
                self.result = 'Disarmed. No entry countdown is pending.'
            self.deadline = None
            self.entry_started_ms = self.entry_due_ms = self.entry_due_monotonic = None
            self.output = 'not_sent'
            self.exit_until = None
        elif profile_state == 'arming':
            self.armed_since_ms = None
            self.deadline = None
            self.entry_started_ms = self.entry_due_ms = self.entry_due_monotonic = None
            self.output = 'not_sent'
            self.result = 'Exit delay in progress. Door openings during this delay do not start the entry timer.'
            self.exit_until = now + max(0, 60 - (self.wall() - changed_ms/1000)) if changed_ms is not None else None
        elif self.armed_since_ms is None:
            # A baseline armed read is necessary before accepting door events.
            self.armed_since_ms = changed_ms if changed_ms is not None else self.wall()*1000
        if profile_state in ('armed', 'breached'):
            self.exit_until = None
            if previous_state == 'arming' and self.deadline is None:
                self.result = 'Exit delay finished; Protect reports armed.'
        for event in sorted(events, key=lambda e:e.get('start',0)):
            event_id, stamp = event.get('id'), event.get('start')
            if not isinstance(event_id,str) or not isinstance(stamp,(int,float)) or isinstance(stamp,bool):
                continue
            if event_id in self.seen:
                continue
            self.seen.add(event_id)
            device = event.get('sensor') or event.get('device')
            if event.get('type') != 'sensorOpened' or device not in sensor_ids or stamp < self.started_ms:
                continue
            self.last_event = (sensor_ids.get(device, 'Door') if isinstance(sensor_ids,dict) else 'Back Door') + ' opened'
            self.verified_event = True
            if 0 <= self.wall()*1000 - stamp <= 120000:
                self.verified_doors.add(sensor_ids.get(device, 'Back Door') if isinstance(sensor_ids, dict) else 'Back Door')
            if profile_state not in ('armed', 'breached') or stamp < max(self.last_disarmed_ms, self.armed_since_ms or 0):
                continue
            if self.active_names is not None and sensor_ids.get(device) not in self.active_names:
                continue
            age = self.wall() - stamp/1000
            if age < -2 or age > self.event_window_seconds:
                raise ValueError('Door event timestamp is outside the observation window.')
            if self.deadline is None and self.output == 'not_sent' and self.result != 'Observed deadline expired; no live alarm sent.':
                self.deadline = now + max(0, 60 - max(0, age))
                self.entry_started_ms = stamp
                self.entry_due_ms = stamp + 60000
                self.entry_due_monotonic = self.deadline
                self.result = 'Entry countdown started.' if self.enabled else 'Entry countdown observed; delayed notification is not enabled.'
        if len(self.seen) > 2000:
            self.seen = {e.get('id') for e in events if isinstance(e.get('id'),str)}
        if profile_state == 'disarmed' and self.result == 'Observed deadline expired; no live alarm sent.':
            self.result = 'Disarmed.'
        return self.snapshot()

    def sensor_update(self, sensor):
        name = sensor.get('name') or 'Back Door'
        previous = self.sensor_baseline.get(name)
        current = (sensor.get('isOpened'), sensor.get('openStatusChangedAt'))
        self.sensor_baseline[name] = current
        self.doors[name] = {'connected':sensor.get('state','CONNECTED') == 'CONNECTED', 'position':'open' if current[0] is True else 'closed' if current[0] is False else 'unknown'}
        # Supplement event history with state changes. A closed-to-closed timestamp
        # change records a rapid door cycle; it cannot reset an existing deadline.
        if not self.doors[name]['connected'] or previous is None or current == previous or type(current[0]) is not bool:
            return
        changed = current[0] is True or (previous[0] is False and current[0] is False and current[1] != previous[1])
        if changed:
            self.verified_event = True
            self.last_event = name + (' opened' if current[0] else ' open/close cycle observed')
            stamp = current[1]
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and 0 <= self.wall()*1000 - stamp <= 120000:
                self.verified_doors.add(name)
            # An update delivered just after exit delay may describe an opening
            # that happened during exit. Use the event time, not its delivery time.
            if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
                return
            age = self.wall() - stamp/1000
            if stamp < max(self.last_disarmed_ms, self.armed_since_ms or 0) or age < -2 or age > self.event_window_seconds:
                return
            if self.active_names is not None and name not in self.active_names:
                return
            if self.state in ('armed', 'breached') and not self.error and self.deadline is None and self.output == 'not_sent' and self.result != 'Observed deadline expired; no live alarm sent.':
                self.deadline = self.clock()+max(0,60-max(0,age))
                self.entry_started_ms = stamp
                self.entry_due_ms = stamp + 60000
                self.entry_due_monotonic = self.deadline
                self.result = 'Entry countdown started.' if self.enabled else 'Entry countdown observed; delayed notification is not enabled.'

    def enable(self):
        if not self.snapshot()['fresh'] or self.state != 'disarmed' or not self.verified_event:
            raise ValueError('Confirm disarmed and verify a real door event before enabling the delay.')
        self.enabled = True
        self.result = 'Live one-minute entry delay enabled for configured doors.'

    def dispatch(self, send):
        self.snapshot()
        if not self.enabled or self.result != 'Observed deadline expired; no live alarm sent.' or self.output != 'not_sent':
            return
        # The console webhook is also restricted to When Armed. Never treat a
        # timeout as confirmed disarm; never blindly retry an uncertain POST.
        self.output = 'sending'
        try:
            # Commit intent before the POST. A crash after this point must not
            # silently replay a notification or sound the siren a second time.
            self.checkpoint()
        except Exception:
            self.output = 'not_sent'
            raise ValueError('Could not save alarm delivery state. Check panel storage and Protect.') from None
        try:
            send()
        except Exception:
            self.output = 'uncertain'
            self.result = 'Delayed notification outcome uncertain. Check Protect immediately.'
        else:
            self.output = 'accepted'
            self.result = 'Delayed notification request accepted by Protect.'
        self.checkpoint()

    def fail(self, message):
        self.error = message
        return self.snapshot()

    def snapshot(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            self.deadline = None
            self.result = 'Observed deadline expired; no live alarm sent.'
        fresh = self.last_ok is not None and self.clock()-self.last_ok < 8 and not self.error
        return {'mode':'live' if self.enabled else 'observation', 'liveDelayEnabled':self.enabled, 'eventVerified':self.verified_event, 'verifiedDoors':sorted(self.verified_doors), 'notification':self.output, 'doors':self.doors.copy(),
                'activeDoors': sorted(self.active_names) if self.active_names is not None else None, 'bypassedDoors': list(self.bypassed),
                'state':self.state if fresh else 'unknown', 'fresh':bool(fresh),
                'exitRemaining':max(0,math.ceil(self.exit_until-self.clock())) if fresh and self.state == 'arming' and self.exit_until is not None else None,
                'remaining':max(0,math.ceil(self.deadline-self.clock())) if self.deadline is not None else None,
                'lastEvent':self.last_event, 'message':self.error or self.result or 'Watching configured doors. Entry delay has not been enabled.'}
