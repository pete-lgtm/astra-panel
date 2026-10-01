"""Local Philips Hue v2 lights, rooms and scenes. No cloud or other providers.

Pairing uses physical bridge approval and explicit trust on first connection.
The captured TLS certificate is pinned before pairing and every authenticated
request. Bridge credentials stay in owner-only state, outside release packages.
"""
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
from pathlib import Path
import re
import secrets
import socket
import ssl
import struct
import threading
import time

from device_config import atomic_json

PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
SERVICE = '_hue._tcp.local.'


def host(value):
    try:
        address = ipaddress.IPv4Address(value)
        if any(address in network for network in PRIVATE):
            return str(address)
    except (ValueError, TypeError):
        pass
    raise ValueError('Enter the Hue Bridge’s private IPv4 address.')


def label(value, fallback):
    return ''.join(c for c in str(value or fallback) if ord(c) >= 32)[:100]


def request(address, method, path, *, fingerprint=None, key=None, body=None, authorize=lambda: None):
    """Fixed HTTPS transport; pin is checked before any key or request is sent."""
    address = host(address)
    if key and not fingerprint:
        raise ValueError('Pair the Hue Bridge before sending credentials.')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Hue bridges use device certificates. TOFU is limited to anonymous bridge
    # identification; subsequent calls enforce the exact captured certificate.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(address, 443, timeout=5, context=context)
    try:
        connection.connect()
        certificate = connection.sock.getpeercert(binary_form=True)
        if not certificate:
            raise ValueError('The Hue Bridge did not provide a certificate.')
        actual = hashlib.sha256(certificate).hexdigest()
        if fingerprint and not hmac.compare_digest(actual, fingerprint):
            raise ValueError('The Hue Bridge certificate changed. Reconnect it from Lighting setup.')
        authorize()
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if key:
            headers['hue-application-key'] = key
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        raw = response.read(2_000_001)
        if response.status in (401, 403):
            raise ValueError('Hue no longer accepts this connection. Reconnect your bridge.')
        if response.status != 200 or len(raw) > 2_000_000:
            raise ValueError('Hue could not complete the request. Refresh before trying again.')
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError('Hue returned an unreadable response.') from None
        return data, actual
    except (OSError, http.client.HTTPException):
        # Do not replay writes after a timeout or a lost response.
        message = ('The lighting result is unknown. Refresh before trying again.' if method != 'GET'
                   else 'Cannot reach your Hue Bridge. Check its power and network connection.')
        raise ValueError(message) from None
    finally:
        connection.close()


def dns_name(packet, offset):
    parts, end, visited = [], None, set()
    for _ in range(64):
        if offset >= len(packet) or offset in visited:
            raise ValueError('Invalid DNS name')
        visited.add(offset)
        size = packet[offset]
        if size & 0xc0 == 0xc0:
            if offset + 1 >= len(packet):
                raise ValueError('Invalid DNS pointer')
            end = end or offset + 2
            offset = ((size & 0x3f) << 8) | packet[offset + 1]
        elif size == 0:
            return '.'.join(parts) + '.', end or offset + 1
        else:
            if size > 63 or offset + 1 + size > len(packet):
                raise ValueError('Invalid DNS label')
            parts.append(packet[offset + 1:offset + 1 + size].decode('utf-8'))
            offset += size + 1
    raise ValueError('DNS name too long')


def dns_records(packet):
    if len(packet) < 12:
        return []
    _, flags, questions, answers, authorities, extra = struct.unpack('!6H', packet[:12])
    if not flags & 0x8000 or questions > 32 or answers + authorities + extra > 256:
        return []
    offset, result = 12, []
    try:
        for _ in range(questions):
            _, offset = dns_name(packet, offset)
            offset += 4
        for _ in range(answers + authorities + extra):
            name, offset = dns_name(packet, offset)
            kind, _, ttl, size = struct.unpack('!HHIH', packet[offset:offset + 10])
            offset += 10
            end = offset + size
            if end > len(packet):
                return []
            if ttl:
                if kind == 12:
                    value, _ = dns_name(packet, offset)
                    result.append((name.lower(), kind, value.lower()))
                elif kind == 33 and size >= 7:
                    target, _ = dns_name(packet, offset + 6)
                    port = struct.unpack('!H', packet[offset + 4:offset + 6])[0]
                    result.append((name.lower(), kind, (target.lower(), port)))
                elif kind == 1 and size == 4:
                    result.append((name.lower(), kind, socket.inet_ntoa(packet[offset:end])))
            offset = end
    except (ValueError, UnicodeError, struct.error):
        return []
    return result


def discover(seconds=3):
    """mDNS QU query; never use the cloud broker or scan unrelated LAN ports."""
    query = struct.pack('!6H', 0, 0, 1, 0, 0, 0)
    query += b''.join(bytes([len(part)]) + part.encode() for part in SERVICE.rstrip('.').split('.'))
    query += b'\0' + struct.pack('!HH', 12, 0x8001)
    records = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(.2)
        sock.sendto(query, ('224.0.0.251', 5353))
        deadline = time.monotonic() + seconds
        for _ in range(256):
            if time.monotonic() >= deadline:
                break
            try:
                packet, _ = sock.recvfrom(9000)
                records.extend(dns_records(packet))
            except socket.timeout:
                continue
    instances = {value for name, kind, value in records if name == SERVICE and kind == 12}
    targets = {name: value for name, kind, value in records if kind == 33 and name in instances}
    addresses = {name: value for name, kind, value in records if kind == 1}
    bridges = {}
    for instance, (target, port) in targets.items():
        if port not in (80, 443) or target not in addresses:
            continue
        try:
            address = host(addresses[target])
        except ValueError:
            continue
        bridges[address] = {'host': address, 'name': label(instance.split('._hue.')[0], 'Hue Bridge')}
    return sorted(bridges.values(), key=lambda b: b['name'])[:8]


def resources(data):
    if (not isinstance(data, dict) or data.get('errors') or not isinstance(data.get('data'), list)
            or len(data['data']) > 4000):
        raise ValueError('Could not read Hue lights. Check that your bridge supports the Hue v2 API.')
    return {row['id']: row for row in data['data'] if isinstance(row, dict)
            and isinstance(row.get('id'), str) and UUID.fullmatch(row['id'])}


def obj(value):
    return value if isinstance(value, dict) else {}


def refs(value):
    return [item for item in value if isinstance(item, dict) and isinstance(item.get('rid'), str)] if isinstance(value, list) else []


def normalize(rows):
    devices = {key: row for key, row in rows.items() if row.get('type') == 'device'}
    offline = {obj(row.get('owner')).get('rid') for row in rows.values()
               if isinstance(obj(row.get('owner')).get('rid'), str)
               if row.get('type') == 'zigbee_connectivity' and row.get('status') in ('disconnected', 'connectivity_issue')}
    lights = []
    for key, row in rows.items():
        if row.get('type') != 'light':
            continue
        owner = obj(row.get('owner')).get('rid')
        brightness = obj(row.get('dimming')).get('brightness')
        lights.append({'id': key, 'kind': 'light', 'name': label(obj(row.get('metadata')).get('name'), 'Light'),
                       'on': obj(row.get('on')).get('on') if type(obj(row.get('on')).get('on')) is bool else None,
                       'brightness': round(brightness) if type(brightness) in (int, float) and math.isfinite(brightness) and 0 <= brightness <= 100 else None,
                       'reachable': isinstance(owner, str) and owner not in offline, 'dimmable': isinstance(row.get('dimming'), dict),
                       'roomId': '', 'deviceId': owner})
    rooms = []
    for key, row in rows.items():
        if row.get('type') not in ('room', 'zone'):
            continue
        children = {item['rid'] for item in refs(row.get('children'))}
        member_ids = {service['rid'] for child in children for service in refs(devices.get(child, {}).get('services'))}
        members = [light for light in lights if light['id'] in member_ids or light['id'] in children]
        if row.get('type') == 'room':
            for light in members:
                light['roomId'] = key
        group = next((rows.get(s['rid']) for s in refs(row.get('services')) if s.get('rtype') == 'grouped_light'), None)
        rooms.append({'id': key, 'name': label(obj(row.get('metadata')).get('name'), 'Room'),
                      'kind': row['type'], 'groupId': group['id'] if group else None,
                      'lights': [light['id'] for light in members]})
    scenes = [{'id': key, 'name': label(obj(row.get('metadata')).get('name'), 'Scene'),
               'roomId': obj(row.get('group')).get('rid')} for key, row in rows.items() if row.get('type') == 'scene']
    for light in lights:
        light.pop('deviceId')
    return {'lights': sorted(lights, key=lambda x: x['name'].casefold()),
            'rooms': sorted(rooms, key=lambda x: x['name'].casefold()),
            'scenes': sorted(scenes, key=lambda x: x['name'].casefold())}


class HueLighting:
    def __init__(self, path):
        self.path = Path(path)
        self.io = threading.Lock()
        self.pending = {}

    def saved(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict) or set(data) != {'host', 'name', 'bridgeId', 'fingerprint', 'key'}:
                raise ValueError('Invalid saved fields')
            host(data['host'])
            if not (re.fullmatch(r'[0-9a-f]{64}', data['fingerprint'])
                    and re.fullmatch(r'[0-9a-fA-F]{16}', data['bridgeId'])
                    and re.fullmatch(r'[A-Za-z0-9_-]{10,128}', data['key'])
                    and isinstance(data['name'], str)):
                raise ValueError('Invalid saved identity')
            return data
        except (ValueError, OSError, TypeError, KeyError):
            raise ValueError('The saved Hue connection needs attention. Reconnect your bridge.') from None

    def fetch(self, saved, authorize=lambda: None):
        return resources(request(saved['host'], 'GET', '/clip/v2/resource', fingerprint=saved['fingerprint'],
                                 key=saved['key'], authorize=authorize)[0])

    def read(self, authorize=lambda: None):
        if not self.io.acquire(blocking=False):
            raise ValueError('Hue is updating. Try again in a moment.')
        try:
            saved = self.saved()
            if not saved:
                return {'state': 'not_connected', 'rooms': [], 'lights': [], 'scenes': []}
            rows = self.fetch(saved, authorize)
            authorize()
            return {'state': 'ready', 'bridge': {k: saved[k] for k in ('name', 'bridgeId', 'host')}, **normalize(rows)}
        finally:
            self.io.release()

    def prepare(self, address, grant, authorize):
        with self.io:
            authorize()
            data, fingerprint = request(host(address), 'GET', '/api/config', authorize=authorize)
            if (not isinstance(data, dict) or not re.fullmatch(r'[0-9a-fA-F]{16}', str(data.get('bridgeid', '')))
                    or not str(data.get('modelid', '')).startswith('BSB') or data.get('modelid') == 'BSB001'):
                raise ValueError('This is not a supported Philips Hue Bridge. Use a bridge with the Hue v2 API.')
            authorize()
            identifier = secrets.token_urlsafe(24)
            self.pending = {identifier: {'host': address, 'name': label(data.get('name'), 'Hue Bridge'),
                                        'bridgeId': data['bridgeid'], 'fingerprint': fingerprint,
                                        'grant': grant, 'expires': time.monotonic() + 120}}
            return {'pairId': identifier, 'name': self.pending[identifier]['name'], 'host': address}

    def pair(self, identifier, grant, authorize):
        with self.io:
            plan = self.pending.get(identifier) if isinstance(identifier, str) else None
            if not plan or plan['grant'] != grant or plan['expires'] <= time.monotonic():
                raise ValueError('Choose your Hue Bridge again to start pairing.')
            authorize()
            data, _ = request(plan['host'], 'POST', '/api', fingerprint=plan['fingerprint'],
                              body={'devicetype': 'astra#panel'}, authorize=authorize)
            if isinstance(data, list) and data and isinstance(data[0], dict):
                if obj(data[0].get('error')).get('type') == 101:
                    raise ValueError('Press the button on your Hue Bridge, then tap Connect again.')
                key = obj(data[0].get('success')).get('username')
            else:
                key = None
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,128}', key):
                raise ValueError('Hue pairing did not complete. Press the bridge button and try again.')
            saved = {k: plan[k] for k in ('host', 'name', 'bridgeId', 'fingerprint')}
            saved['key'] = key
            # Verify API support before replacing an existing working connection.
            rows = self.fetch(saved, authorize)
            authorize()
            atomic_json(self.path, saved)
            self.pending.clear()
            return {'state': 'ready', 'bridge': {k: saved[k] for k in ('name', 'bridgeId', 'host')}, **normalize(rows)}

    def disconnect(self, authorize):
        with self.io:
            authorize()
            self.path.unlink(missing_ok=True)
            self.pending.clear()
        return {'state': 'not_connected', 'rooms': [], 'lights': [], 'scenes': []}

    def control(self, data, authorize):
        if set(data) - {'id', 'kind', 'action', 'value'}:
            raise ValueError('Unsupported lighting command.')
        identifier, kind, action = data.get('id'), data.get('kind'), data.get('action')
        if kind not in ('light', 'grouped_light', 'scene') or not isinstance(identifier, str) or not UUID.fullmatch(identifier):
            raise ValueError('Choose a light, room or scene from your Hue Bridge.')
        with self.io:
            authorize()
            saved = self.saved()
            if not saved:
                raise ValueError('Connect your Hue Bridge first.')
            rows = self.fetch(saved, authorize)
            row = rows.get(identifier)
            if not row or row.get('type') != kind:
                raise ValueError('This light or scene changed. Refresh Lighting.')
            if kind == 'scene' and action == 'scene' and 'value' not in data:
                body = {'recall': {'action': 'active'}}
            elif kind != 'scene' and action == 'on' and type(data.get('value')) is bool and isinstance(row.get('on'), dict):
                body = {'on': {'on': data['value']}}
            elif kind != 'scene' and action == 'brightness' and isinstance(row.get('dimming'), dict):
                value = data.get('value')
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
                    raise ValueError('Choose brightness from 0 to 100 percent.')
                body = {'on': {'on': value > 0}}
                if value > 0:
                    body['dimming'] = {'brightness': max(.1, value)}
            else:
                raise ValueError('That control is not available for this light.')
            if kind == 'light':
                light = next(x for x in normalize(rows)['lights'] if x['id'] == identifier)
                if not light['reachable']:
                    raise ValueError('This light is unreachable. Check its power before trying again.')
            authorize()
            reply, _ = request(saved['host'], 'PUT', '/clip/v2/resource/' + kind + '/' + identifier,
                               fingerprint=saved['fingerprint'], key=saved['key'], body=body, authorize=authorize)
            if not isinstance(reply, dict) or reply.get('errors') or not isinstance(reply.get('data'), list) or not reply['data']:
                raise ValueError('Hue did not confirm the request. Refresh before trying again.')
            # Acceptance is not device confirmation. The UI reads fresh state next.
            return {'accepted': True}
