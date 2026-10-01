"""Read-only Network inventory using a snapshot of the local console session.

Only two fixed GET routes are used. Never return raw device documents: they can
contain adoption credentials. The compatibility API exposes outlet_table and
port_table, which are not both available in Network's integration API.
"""
import ipaddress
import math
import re
import time
from local_auth import LocalSession, ConsoleRequestError

SITE_NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')
DEVICE_ID = re.compile(r'[0-9a-f]{24}')
MAC = re.compile(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}')
KINDS = {'uap': 'accessPoint', 'usw': 'switch', 'ugw': 'gateway', 'udm': 'gateway',
         'ucg': 'gateway', 'uxg': 'gateway', 'usp': 'power', 'pdu': 'power',
         'ubb': 'bridge', 'uck': 'console', 'uas': 'console'}
STATES = {0: 'offline', 1: 'online', 2: 'pending', 3: 'firmwareMismatch',
          4: 'updating', 5: 'provisioning', 6: 'unreachable', 7: 'adopting',
          8: 'removing', 9: 'connectionError', 10: 'adoptionFailed', 11: 'isolated'}


class InventoryError(ValueError):
    def __init__(self, state, message):
        self.state = state
        super().__init__(message)


def label(value, fallback='', limit=120):
    return ''.join(c for c in value if c.isprintable())[:limit] if isinstance(value, str) and value else fallback


def number(value, maximum=1000000):
    if type(value) is bool or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and 0 <= result <= maximum else None
    except (ValueError, TypeError, OverflowError):
        return None


def index(value):
    result = number(value, 512)
    return int(result) if result is not None and result.is_integer() else None


def boolean(value):
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ('true', 'false', '1', '0'):
        return value.lower() in ('true', '1')
    return None


def address(value):
    try:
        return str(ipaddress.ip_address(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def entries(value, limit):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise InventoryError('unavailable', 'Network returned an incomplete or unsupported device list. Check UniFi Network.')
    return [item for item in value if isinstance(item, dict)]


def normalize_device(raw):
    identifier = label(raw.get('_id') or raw.get('mac'), limit=100)
    if not identifier:
        return None
    raw_state = index(raw.get('state'))
    state = STATES.get(raw_state, 'unknown')
    online = state == 'online'
    model = label(raw.get('model'), 'Unknown model', 80)
    name = label(raw.get('name'), model)
    editable = online and bool(DEVICE_ID.fullmatch(str(raw.get('_id', ''))))
    commandable = editable and bool(MAC.fullmatch(str(raw.get('mac', ''))))
    ports, outlets = [], []
    port_ids, outlet_ids = set(), set()
    port_names = {index(item.get('port_idx')): label(item.get('name'))
                  for item in entries(raw.get('port_overrides'), 256)}
    for port in entries(raw.get('port_table'), 256):
        idx = index(port.get('port_idx'))
        if idx is None or idx in port_ids:
            continue
        port_ids.add(idx)
        caps = number(port.get('poe_caps'))
        poe = boolean(port.get('port_poe')) is True or (caps is not None and caps > 0)
        ports.append({'index': idx, 'name': port_names.get(idx) or label(port.get('name'), 'Port %s' % idx),
                      'linkUp': boolean(port.get('up')) if online else None,
                      'speedMbps': number(port.get('speed')) if online else None,
                      'poe': poe,
                      'poeEnabled': boolean(port.get('poe_enable')) if poe and online else None,
                      'poePowered': boolean(port.get('poe_good')) if poe and online else None,
                      'watts': number(port.get('poe_power')) if poe and online else None,
                      'controls': {'rename': editable and raw.get('type') in ('usw', 'udm', 'ugw', 'ucg') and not port.get('attr_no_edit'),
                                   'cycle': commandable and poe and boolean(port.get('poe_enable')) is True and
                                            boolean(port.get('is_uplink')) is False and not port.get('attr_no_edit')}})
    # Overrides supply a user-assigned name, never an observed relay state.
    names = {index(item.get('index')): label(item.get('name'))
             for item in entries(raw.get('outlet_overrides'), 512)}
    for outlet in entries(raw.get('outlet_table'), 512):
        idx = index(outlet.get('index'))
        if idx is None or idx in outlet_ids:
            continue
        outlet_ids.add(idx)
        relay = boolean(outlet.get('has_relay'))
        if relay is None:
            caps = number(outlet.get('outlet_caps'), 4294967295)
            relay = bool(int(caps) & 1) if caps is not None and caps.is_integer() else False
        grouped = outlet.get('relay_group') is not None
        outlets.append({'index': idx, 'name': names.get(idx) or label(outlet.get('name'), 'Outlet %s' % idx),
                        'on': boolean(outlet.get('relay_state')) if online else None,
                        'watts': number(outlet.get('outlet_power')) if online else None,
                        'controls': {'rename': editable, 'toggle': editable and relay and not grouped},
                        'controlNote': 'Shared power group · switching is not available here yet.' if grouped else
                                       '' if relay else 'This outlet does not report an individual power relay.'})
    kind = KINDS.get(raw.get('type'), 'other')
    if outlets and kind in ('other', 'switch'):
        kind = 'power'
    return {'id': identifier, 'name': name, 'model': model, 'kind': kind, 'state': state,
            'ip': address(raw.get('ip')), 'ports': sorted(ports, key=lambda p: p['index']),
            'outlets': sorted(outlets, key=lambda p: p['index']),
            'controls': {'rename': editable, 'restart': commandable and kind in ('accessPoint', 'switch') and not outlets}}


class NetworkReader:
    def __init__(self, host, cookie):
        # A Network 401/403 may clear THIS snapshot, never the live alarm session.
        self.session = LocalSession()
        self.host, self.cookie = host, cookie

    def get(self, path):
        if path != '/proxy/network/api/self/sites' and not re.fullmatch(
                r'/proxy/network/api/s/[A-Za-z0-9_-]{1,64}/stat/device', path):
            raise ValueError('Unsupported Network inventory route.')
        body, cookie = self.session.request(self.host, 'GET', path, cookie=self.cookie)
        if cookie:
            self.cookie = cookie
        if not isinstance(body, dict) or not isinstance(body.get('meta'), dict):
            raise InventoryError('unavailable', 'Network returned an unsupported inventory response.')
        if body['meta'].get('rc') != 'ok':
            raise InventoryError('denied', 'UniFi Network did not allow device discovery. Check this account’s Network access.')
        if not isinstance(body.get('data'), list):
            raise InventoryError('unavailable', 'Network did not return a complete device list.')
        return body['data']


def discover(host, cookie, selected=None):
    result = {'state': 'needs_login', 'message': 'Reconnect your UniFi account in Settings to find Network devices.',
              'sites': [], 'site': None, 'devices': [], 'observedAt': None}
    if not host or not cookie:
        return result
    try:
        reader = NetworkReader(host, cookie)
        sites = []
        for site in entries(reader.get('/proxy/network/api/self/sites'), 256):
            site_name = site.get('name')
            if isinstance(site_name, str) and SITE_NAME.fullmatch(site_name):
                if not any(item['id'] == site_name for item in sites):
                    sites.append({'id': site_name, 'name': label(site.get('desc'), site_name)})
        result['sites'] = sites
        if not sites:
            raise InventoryError('denied', 'No Network sites are accessible to this UniFi account.')
        if selected is not None and not any(site['id'] == selected for site in sites):
            raise InventoryError('choose_site', 'Choose a Network site available to this account.')
        if selected is None and len(sites) != 1:
            raise InventoryError('choose_site', 'Choose the Network site for this panel.')
        result['site'] = next(site for site in sites if site['id'] == selected) if selected else sites[0]
        devices = reader.get('/proxy/network/api/s/' + result['site']['id'] + '/stat/device')
        normalized, seen = [], set()
        for raw in entries(devices, 500):
            device = normalize_device(raw)
            if device and device['id'] not in seen:
                seen.add(device['id'])
                normalized.append(device)
        result.update(state='ready', message='', observedAt=int(time.time() * 1000),
                      devices=sorted(normalized, key=lambda device: (device['kind'], device['name'].casefold())))
    except ConsoleRequestError as error:
        state, message = {
            401: ('needs_login', 'UniFi Network needs a new sign-in. Reconnect your UniFi account in Settings.'),
            403: ('denied', 'This UniFi account cannot read Network devices. Check its Network permissions.'),
            404: ('unavailable', 'Network discovery is not available on this console. Check that UniFi Network is installed.'),
        }.get(error.status, ('unavailable', 'UniFi Network is unavailable. Try refreshing shortly.'))
        result.update(state=state, message=message)
    except InventoryError as error:
        result.update(state=error.state, message=str(error))
    except (ValueError, TypeError, OSError):
        # No response bodies, exception strings, session cookies, or secrets leave this adapter.
        result.update(state='unavailable', message='Could not read Network devices. Check the console connection and try Refresh.')
    return result
