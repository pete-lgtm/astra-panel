"""Narrow Network mutations with a fresh, read-only review before every write.

Compatibility API references: aiounifi/models/device.py and models/outlet.py.
Raw device records and complete override arrays remain inside this module.
"""
from copy import deepcopy
import hashlib
import json

from local_auth import ConsoleRequestError
from network_inventory import NetworkReader, SITE_NAME, DEVICE_ID, entries, normalize_device
from protect_setup_api import csrf_from_cookie

ACTIONS = {'rename_device', 'rename_port', 'rename_outlet', 'restart_device', 'cycle_poe', 'outlet_on', 'outlet_off'}
DISRUPTIVE = {'restart_device', 'cycle_poe', 'outlet_on', 'outlet_off'}


class ActionError(ValueError):
    pass


class UncertainAction(ActionError):
    pass


def clean_request(data):
    if not isinstance(data, dict) or set(data) - {'action', 'site', 'deviceId', 'index', 'name'}:
        raise ActionError('Unsupported equipment request.')
    request = deepcopy(data)
    action = request.get('action')
    if action not in ACTIONS:
        raise ActionError('Choose a supported equipment action.')
    if not isinstance(request.get('site'), str) or not SITE_NAME.fullmatch(request['site']):
        raise ActionError('Choose a Network site.')
    if not isinstance(request.get('deviceId'), str) or not DEVICE_ID.fullmatch(request['deviceId']):
        raise ActionError('Refresh and choose a device from this console.')
    if action in ('rename_port', 'rename_outlet', 'cycle_poe', 'outlet_on', 'outlet_off'):
        if type(request.get('index')) is not int or not 0 <= request['index'] <= 512:
            raise ActionError('Choose a valid port or outlet.')
    elif 'index' in request:
        raise ActionError('A device action does not take a port or outlet.')
    if action.startswith('rename_'):
        name = request.get('name')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64 or any(not c.isprintable() for c in name):
            raise ActionError('Enter a name with 1–64 printable characters.')
        request['name'] = name.strip()
    elif 'name' in request:
        raise ActionError('This action does not take a new name.')
    return request


def overrides(raw, field, key, idx, change, fallback):
    # PUT replaces the entire array. Preserve every unrelated setting, including
    # fields this adapter does not understand. Reject malformed/duplicate rows.
    items = deepcopy(raw.get(field, []))
    if (not isinstance(items, list) or len(items) > 512 or
            any(not isinstance(item, dict) or type(item.get(key)) is not int for item in items) or
            len({item[key] for item in items}) != len(items)):
        raise ActionError('UniFi returned incomplete port settings. No changes were made.')
    match = next((item for item in items if item[key] == idx), None)
    if match is None:
        match = {key: idx, **fallback}
        items.append(match)
    match.update(change)
    return items


def prepare(host, cookie, data):
    request = clean_request(data)
    reader = NetworkReader(host, cookie)
    try:
        sites = entries(reader.get('/proxy/network/api/self/sites'), 256)
        if request['site'] not in [site.get('name') for site in sites]:
            raise ActionError('This account no longer has access to that Network site.')
        base = '/proxy/network/api/s/' + request['site']
        devices = entries(reader.get(base + '/stat/device'), 500)
        found = [raw for raw in devices if raw.get('_id') == request['deviceId']]
        if len(found) != 1:
            raise ActionError('This device changed or is unavailable. Refresh the list.')
        raw = found[0]
        device = normalize_device(raw)
        if device['state'] != 'online':
            raise ActionError('Wait for this device to be online before changing it.')
        action = request['action']
        target = device
        if action in ('rename_port', 'cycle_poe'):
            target = next((item for item in device['ports'] if item['index'] == request['index']), None)
        elif action in ('rename_outlet', 'outlet_on', 'outlet_off'):
            target = next((item for item in device['outlets'] if item['index'] == request['index']), None)
        capability = 'rename' if action.startswith('rename_') else 'restart' if action == 'restart_device' else 'cycle' if action == 'cycle_poe' else 'toggle'
        if not target or not target['controls'].get(capability):
            raise ActionError('This device does not support that control here. Refresh or use UniFi Network.')
        title = device['name'] if target is device else device['name'] + ' · ' + target['name']
        payload, method, path = {}, 'PUT', base + '/rest/device/' + request['deviceId']
        effect = ''
        if action == 'rename_device':
            payload = {'name': request['name']}
        elif action == 'rename_port':
            port = next(item for item in raw['port_table'] if item.get('port_idx') == request['index'])
            fallback = {k: port[k] for k in ('portconf_id', 'poe_mode') if k in port}
            payload = {'port_overrides': overrides(raw, 'port_overrides', 'port_idx', request['index'], {'name': request['name']}, fallback)}
        elif action in ('rename_outlet', 'outlet_on', 'outlet_off'):
            change = {'name': request['name']} if action == 'rename_outlet' else {'relay_state': action == 'outlet_on'}
            payload = {'outlet_overrides': overrides(raw, 'outlet_overrides', 'index', request['index'], change, {'name': target['name']})}
            if action != 'rename_outlet':
                if target['on'] is None:
                    raise ActionError('The outlet’s current power state is unknown. Refresh before switching it.')
                if target['on'] == (action == 'outlet_on'):
                    raise ActionError('That outlet is already ' + ('on.' if target['on'] else 'off.'))
                effect = 'Power will turn ' + ('on.' if action == 'outlet_on' else 'off and stay off until you turn it on. Connected equipment will lose power.')
        else:
            method, path = 'POST', base + '/cmd/devmgr'
            if action == 'restart_device':
                payload = {'cmd': 'restart', 'mac': raw['mac'], 'reboot_type': 'soft'}
                effect = 'Connected devices will temporarily lose their network connection while this equipment restarts.'
            else:
                payload = {'cmd': 'power-cycle', 'mac': raw['mac'], 'port_idx': request['index']}
                effect = 'UniFi will cycle PoE power on this port. Its connected equipment will restart.'
        if action.startswith('rename_'):
            if request['name'] == target['name']:
                raise ActionError('Enter a different name.')
            effect = 'Rename “' + target['name'] + '” to “' + request['name'] + '” in UniFi Network.'
        stable = {'identity': {k: raw.get(k) for k in ('_id', 'mac', 'type', 'model', 'name', 'state')},
                  'target': {k: target.get(k) for k in ('index', 'name', 'controls', 'on')},
                  'port_overrides': raw.get('port_overrides'), 'outlet_overrides': raw.get('outlet_overrides'),
                  'payload': payload}
        digest = hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'request': request, 'digest': digest, 'title': title, 'effect': effect, 'method': method,
                'path': path, 'payload': payload, 'reader': reader, 'base': base}
    except ActionError:
        raise
    except (ValueError, TypeError, KeyError, StopIteration):
        raise ActionError('Could not review this equipment. Check the Network connection and refresh.') from None


def execute(plan):
    reader = plan['reader']
    try:
        headers = {'X-CSRF-Token': csrf_from_cookie(reader.cookie)}
    except ValueError:
        raise ActionError('Reconnect your UniFi account before changing equipment.') from None
    try:
        body, cookie = reader.session.request(reader.host, plan['method'], plan['path'], plan['payload'],
                                              cookie=reader.cookie, extra_headers=headers)
        if cookie:
            reader.cookie = cookie
    except ConsoleRequestError as error:
        if error.status in (400, 401, 403, 404, 405, 409, 422):
            raise ActionError('UniFi rejected this change. Check the account permissions and device support.') from None
        raise UncertainAction('The result is unknown. Check this equipment in UniFi before trying again.') from None
    except (ValueError, OSError):
        raise UncertainAction('The connection ended before the result was confirmed. Check UniFi before trying again.') from None
    if not isinstance(body, dict) or not isinstance(body.get('meta'), dict) or body['meta'].get('rc') != 'ok':
        raise UncertainAction('UniFi did not confirm the result. Check the device before trying again.')
    action = plan['request']['action']
    result = {'status': 'submitted', 'message': 'Request accepted by UniFi. Refresh to check the equipment; completion is not yet confirmed.'}
    if action in ('restart_device', 'cycle_poe'):
        return result
    try:
        raw = next(item for item in entries(reader.get(plan['base'] + '/stat/device'), 500)
                   if item.get('_id') == plan['request']['deviceId'])
        device = normalize_device(raw)
        target = device
        if action == 'rename_port':
            target = next(item for item in device['ports'] if item['index'] == plan['request']['index'])
        elif action in ('rename_outlet', 'outlet_on', 'outlet_off'):
            target = next(item for item in device['outlets'] if item['index'] == plan['request']['index'])
        verified = target['name'] == plan['request']['name'] if action.startswith('rename_') else target['on'] == (action == 'outlet_on')
        if verified:
            result = {'status': 'verified', 'message': 'Change confirmed by UniFi Network.'}
    except (ValueError, KeyError, TypeError, StopIteration):
        pass  # A successful write followed by a failed read must never be retried.
    return result
