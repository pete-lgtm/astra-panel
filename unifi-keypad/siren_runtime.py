"""Stop only the panel's selected alarm sirens after confirmed disarm.

A short private journal survives restart and uncertain webhook responses. Sound
starts in Protect's delayed alarm rule; this module never sends play commands.
"""
import json
import math
import time
from alarm_outputs import siren_output, validate_output
from device_config import atomic_json, paths


def journal_path(root):
    return paths(root=root)[0] / '.siren-alert.json'


def remember(root, site, now=None):
    output = siren_output(site)
    if not output:
        return
    now = time.time() if now is None else now
    atomic_json(journal_path(root), {'version': 1, 'host': site['consoleHost'],
                                    'output': output, 'until': now + output['durationSeconds'] + 15})


def stop_after_disarm(root, site, state, request, now=None):
    path = journal_path(root)
    if state != 'disarmed' or not path.exists():
        return
    now = time.time() if now is None else now
    if path.stat().st_size > 16000:
        raise ValueError('Siren stop status needs review in Settings.')
    record = json.loads(path.read_text())
    output = validate_output(record.get('output'))
    if record.get('version') != 1 or record.get('host') != site.get('consoleHost'):
        raise ValueError('Siren stop status belongs to another console.')
    until = record.get('until')
    if not isinstance(until, (int, float)) or isinstance(until, bool) or not math.isfinite(until) or until > now + 60:
        raise ValueError('Siren stop status has an invalid deadline.')
    if now > until:
        path.unlink(missing_ok=True)
        return
    failed = []
    for device in output['devices']:
        try:
            # Read first: an uncertain stop response is never blindly replayed.
            current = request('GET', 'sirens/' + device['id'])
            if (current.get('id') != device['id'] or
                    str(current.get('mac', '')).replace(':', '').upper() != device['mac']):
                raise ValueError('Siren identity could not be verified.')
            active = current.get('sirenStatus', {}).get('isActive')
            if type(active) is not bool:
                raise ValueError('Siren sound status could not be verified.')
            if active:
                request('POST', 'sirens/' + device['id'] + '/stop')
                current = request('GET', 'sirens/' + device['id'])
                if current.get('sirenStatus', {}).get('isActive') is not False:
                    raise ValueError('Siren stop is not confirmed yet.')
        except Exception:
            failed.append(device)
    if failed:
        atomic_json(path, {**record, 'output': {**output, 'devices': failed}})
        raise ValueError('Disarmed; a siren stop is not confirmed. Check Protect. Its timed sound will end automatically.')
    path.unlink(missing_ok=True)
