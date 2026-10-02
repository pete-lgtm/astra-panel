"""Local Sonos discovery, current playback and narrowly scoped room controls.

Discovery and playback have their own locks, timeouts and small worker pools;
they never use the alarm controller lock. Writes recheck room identity, grouping,
capabilities and the caller's PIN grant. Listening history is not persisted.
"""
import copy
import fcntl
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
import sonos_artwork
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
IDENTIFIER = re.compile(r'RINCON_[A-Z0-9]{8,40}')
READS = {
    'GetZoneGroupState': ('ZoneGroupTopology', '/ZoneGroupTopology/Control', ''),
    'GetTransportInfo': ('AVTransport', '/MediaRenderer/AVTransport/Control', '<InstanceID>0</InstanceID>'),
    'GetPositionInfo': ('AVTransport', '/MediaRenderer/AVTransport/Control', '<InstanceID>0</InstanceID>'),
    'GetCurrentTransportActions': ('AVTransport', '/MediaRenderer/AVTransport/Control', '<InstanceID>0</InstanceID>'),
    'GetVolume': ('RenderingControl', '/MediaRenderer/RenderingControl/Control', '<InstanceID>0</InstanceID><Channel>Master</Channel>'),
    'GetMute': ('RenderingControl', '/MediaRenderer/RenderingControl/Control', '<InstanceID>0</InstanceID><Channel>Master</Channel>'),
    'GetOutputFixed': ('RenderingControl', '/MediaRenderer/RenderingControl/Control', '<InstanceID>0</InstanceID>'),
}
PLAYBACK = {'PLAYING': 'playing', 'PAUSED_PLAYBACK': 'paused', 'STOPPED': 'stopped',
            'NO_MEDIA_PRESENT': 'idle', 'TRANSITIONING': 'loading'}


def label(value, limit=160):
    return ''.join(c for c in (value or '') if c.isprintable())[:limit]


def xml(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    if len(data) > 262144 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Invalid Sonos response.')
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise ValueError('Invalid Sonos response.') from None


def value(root, name):
    return next((n.text or '' for n in root.iter() if n.tag.split('}')[-1] == name), '')


def interfaces():
    """Read Linux interface addresses using the service's allowed AF_INET socket.

    `ip addr` needs AF_NETLINK, which the appliance deliberately disallows.
    These GET-only ioctls need no capabilities and retain the real subnet mask.
    The ifreq fields used here have the same offsets on 32/64-bit Linux.
    """
    if not sys.platform.startswith('linux'):
        return []
    found = []
    try:
        names = sorted(p.name for p in Path('/sys/class/net').iterdir())[:64]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for name in names:
                encoded = name.encode('utf-8')
                if not 0 < len(encoded) < 16:
                    continue
                request = struct.pack('256s', encoded)
                try:
                    flags = fcntl.ioctl(sock.fileno(), 0x8913, request)  # SIOCGIFFLAGS
                    if not struct.unpack_from('H', flags, 16)[0] & 1:  # IFF_UP
                        continue
                    address = fcntl.ioctl(sock.fileno(), 0x8915, request)  # SIOCGIFADDR
                    mask = fcntl.ioctl(sock.fileno(), 0x891b, request)  # SIOCGIFNETMASK
                    if any(struct.unpack_from('H', result, 16)[0] != socket.AF_INET for result in (address, mask)):
                        continue
                    interface = ipaddress.ip_interface(socket.inet_ntoa(address[20:24]) + '/' + socket.inet_ntoa(mask[20:24]))
                    if any(interface.ip in n for n in PRIVATE) and interface.network.prefixlen >= 8:
                        found.append(interface)
                except (OSError, ValueError, struct.error):
                    # An unplugged Ethernet interface must not hide working Wi-Fi.
                    continue
    except OSError:
        pass
    return sorted(set(found), key=str)[:4]


def local_ip(candidate, nets):
    try:
        ip = ipaddress.ip_address(candidate)
        return ip.version == 4 and any(ip in n for n in PRIVATE) and any(
            ip in n and ip not in (n.network_address, n.broadcast_address) for n in nets)
    except (ValueError, TypeError):
        return False


def location_ip(location, nets):
    try:
        url = urlsplit(location)
        if (url.scheme == 'http' and url.port == 1400 and not url.username and not url.password
                and url.path == '/xml/device_description.xml' and not url.query and not url.fragment
                and local_ip(url.hostname, nets)):
            return url.hostname
    except ValueError:
        pass
    return None


def request(ip, nets, action=None):
    if action is not None and action not in READS:
        raise ValueError('Unsupported Sonos read.')
    return exchange(ip, nets, action, READS.get(action))


def exchange(ip, nets, action, definition, writing=False):
    if not local_ip(ip, nets):
        raise ValueError('Sonos is not on the panel network.')
    method, path, body, headers = 'GET', '/xml/device_description.xml', None, {}
    if action:
        service, path, arguments = definition
        service = 'urn:schemas-upnp-org:service:' + service + ':1'
        body = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{service}">{arguments}</u:{action}>'
                '</s:Body></s:Envelope>').encode()
        method = 'POST'
        headers = {'Content-Type': 'text/xml; charset=utf-8', 'SOAPACTION': f'"{service}#{action}"'}
    connection = http.client.HTTPConnection(ip, 1400, timeout=1.5)
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Sonos did not confirm the change. Refresh before trying again.' if writing else 'Sonos did not return playback information.')
        root = xml(response.read(262145))
        if writing and (any(node.tag.rsplit('}', 1)[-1] == 'Fault' for node in root.iter())
                        or not any(node.tag.rsplit('}', 1)[-1] == action + 'Response' for node in root.iter())):
            raise ValueError('Sonos did not confirm the change. Refresh before trying again.')
        return root
    except (OSError, http.client.HTTPException):
        raise ValueError('The result is unknown. Refresh Sonos before trying again.' if writing else 'Sonos is not responding.') from None
    finally:
        connection.close()


def control_request(ip, nets, action, setting=None):
    """No URI, grouping, queue or arbitrary service/path writes are accepted."""
    service, args = 'AVTransport', '<InstanceID>0</InstanceID>'
    if action in ('play', 'pause', 'stop', 'next') and setting is None:
        soap_action = action.capitalize()
        if action == 'play':
            args += '<Speed>1</Speed>'
    elif action in ('volume_up', 'volume_down') and setting is None:
        service, soap_action = 'RenderingControl', 'SetRelativeVolume'
        args += '<Channel>Master</Channel><Adjustment>' + ('2' if action == 'volume_up' else '-2') + '</Adjustment>'
    elif action == 'volume' and type(setting) is int and 0 <= setting <= 100:
        service, soap_action = 'RenderingControl', 'SetVolume'
        args += f'<Channel>Master</Channel><DesiredVolume>{setting}</DesiredVolume>'
    elif action == 'mute' and type(setting) is bool:
        service, soap_action = 'RenderingControl', 'SetMute'
        args += f'<Channel>Master</Channel><DesiredMute>{int(setting)}</DesiredMute>'
    else:
        raise ValueError('Unsupported Sonos control.')
    return exchange(ip, nets, soap_action, (service, '/MediaRenderer/' + service + '/Control', args), writing=True)


def group_request(ip, nets, coordinator=None):
    """Only a verified Sonos coordinator may be used as a grouping URI."""
    args = '<InstanceID>0</InstanceID>'
    if coordinator is None:
        action = 'BecomeCoordinatorOfStandaloneGroup'
    elif isinstance(coordinator, str) and IDENTIFIER.fullmatch(coordinator):
        action = 'SetAVTransportURI'
        args += f'<CurrentURI>x-rincon:{coordinator}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>'
    else:
        raise ValueError('Invalid Sonos group.')
    return exchange(ip, nets, action, ('AVTransport', '/MediaRenderer/AVTransport/Control', args), writing=True)


def identity(ip, nets):
    root = request(ip, nets)
    identifier = value(root, 'UDN').removeprefix('uuid:')
    if ('sonos' not in value(root, 'manufacturer').lower()
            or not IDENTIFIER.fullmatch(identifier)
            or 'ZonePlayer' not in value(root, 'deviceType')):
        raise ValueError('Device is not a Sonos player.')
    return {'id': identifier, 'ip': ip, 'name': label(value(root, 'roomName')) or 'Sonos room',
            'model': label(value(root, 'modelName'), 80)}


def discover(attached):
    nets = [a.network for a in attached]
    found = set()
    packet = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
              'MAN: "ssdp:discover"\r\nMX: 1\r\n'
              'ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n').encode()
    sockets = []
    try:
        for interface in attached:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sockets.append(sock)
            sock.bind((str(interface.ip), 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(str(interface.ip)))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            for _ in range(2):
                sock.sendto(packet, ('239.255.255.250', 1900))
        deadline = time.monotonic() + 1.6
        while time.monotonic() < deadline:
            for sock in sockets:
                sock.settimeout(.08)
                try:
                    data, sender = sock.recvfrom(16384)
                except socket.timeout:
                    continue
                headers = {}
                for line in data.decode('utf-8', 'replace').splitlines()[1:]:
                    if ':' in line:
                        key, item = line.split(':', 1)
                        headers[key.lower()] = item.strip()
                ip = location_ip(headers.get('location', ''), nets)
                if ip == sender[0] and 'sonos' in headers.get('server', '').lower():
                    found.add(ip)
    except OSError:
        pass
    finally:
        for sock in sockets:
            sock.close()
    def probe(ip):
        try:
            if not found:
                with socket.create_connection((ip, 1400), timeout=.3):
                    pass
            return identity(ip, nets)
        except (ValueError, OSError):
            return None
    candidates = sorted(found)
    if not candidates:
        # Bounded fallback on the panel's /24 segment (or smaller attached net).
        candidates = sorted({str(ip) for a in attached
                             for ip in ipaddress.ip_network(f'{a.ip}/{max(24, a.network.prefixlen)}', strict=False).hosts()
                             if ip != a.ip})[:512]
    with ThreadPoolExecutor(max_workers=12) as pool:
        return {p['id']: p for p in pool.map(probe, candidates) if p}


def topology(root, nets):
    groups = []
    for group in xml(value(root, 'ZoneGroupState')).iter('ZoneGroup'):
        coordinator = group.get('Coordinator', '')
        if not IDENTIFIER.fullmatch(coordinator):
            continue
        members = []
        for member in group.findall('ZoneGroupMember'):
            uid, ip = member.get('UUID', ''), location_ip(member.get('Location', ''), nets)
            if ip and IDENTIFIER.fullmatch(uid):
                members.append({'id': uid, 'ip': ip, 'name': label(member.get('ZoneName')) or 'Sonos room',
                                'visible': member.get('Invisible') != '1' and member.get('IsZoneBridge') != '1'})
        if members and any(m['id'] == coordinator for m in members):
            groups.append({'id': coordinator, 'members': members})
        if len(groups) >= 64:
            break
    return groups


def group_shape(groups):
    return {g['id']: sorted(m['id'] for m in g['members'] if m['visible']) for g in groups
            if any(m['visible'] for m in g['members'])}


def group_revision(shape):
    return hashlib.sha256(json.dumps(shape, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def group_scope(shape):
    return group_revision(sorted(uid for members in shape.values() for uid in members))


def grouping_plan(shape, coordinator, wanted):
    """Preserve the source coordinator; move other linked groups followers first."""
    if coordinator not in shape or coordinator not in shape[coordinator] or coordinator not in wanted:
        raise ValueError('Keep the source room selected.')
    available = {uid for members in shape.values() for uid in members}
    if not wanted <= available:
        raise ValueError('A room is no longer available. Find rooms and try again.')
    actions = []
    for leader, members in sorted(shape.items()):
        if leader == coordinator:
            continue
        if leader in wanted and not set(members) <= wanted:
            raise ValueError('Select all rooms in an existing linked group before moving its source room.')
        actions.extend((uid, coordinator) for uid in sorted(set(members) & wanted, key=lambda uid: (uid == leader, uid)))
    actions.extend((uid, None) for uid in sorted(set(shape[coordinator]) - wanted))
    return actions


def changed_group_shape(shape, uid, coordinator):
    updated = {leader: [member for member in members if member != uid] for leader, members in shape.items()}
    updated = {leader: members for leader, members in updated.items() if members}
    updated.setdefault(coordinator or uid, []).append(uid)
    return {leader: sorted(members) for leader, members in updated.items()}


def metadata(position):
    raw = value(position, 'TrackMetaData')
    if not raw or raw == 'NOT_IMPLEMENTED':
        return {}
    try:
        root = xml(raw)
        result = {key: label(value(root, name)) for key, name in
                (('title', 'title'), ('artist', 'creator'), ('album', 'album'), ('stream', 'streamContent'))}
        result['artwork'] = value(root, 'albumArtURI')
        return result
    except ValueError:
        return {}


def source_name(uri):
    if uri.startswith('x-sonos-htastream:'):
        return 'TV'
    if uri.startswith('x-rincon-stream:'):
        return 'Line-in'
    if uri.startswith('x-sonos-vli:'):
        if ',airplay:' in uri:
            return 'AirPlay'
        if ',spotify:' in uri:
            return 'Spotify Connect'
        return 'Music'
    if uri.startswith(('x-sonosapi-stream:', 'x-sonosapi-radio:', 'x-rincon-mp3radio:', 'aac:', 'hls-radio:')):
        return 'Radio'
    return 'Music' if uri else ''


def transport_actions(player, nets):
    try:
        raw = value(request(player['ip'], nets, 'GetCurrentTransportActions'), 'Actions')
        return sorted({a.strip().split('_')[-1].lower() for a in raw.split(',')
                       if a.strip().split('_')[-1] in ('Play', 'Pause', 'Stop', 'Next')})
    except ValueError:
        return []


def room_levels(player, nets):
    result = {'volume': None, 'muted': None, 'fixedVolume': False}
    try:
        if identity(player['ip'], nets)['id'] != player['id']:
            return result
    except ValueError:
        return result
    for action, field, tag in (('GetVolume', 'volume', 'CurrentVolume'), ('GetMute', 'muted', 'CurrentMute'),
                               ('GetOutputFixed', 'fixedVolume', 'CurrentFixed')):
        try:
            raw = value(request(player['ip'], nets, action), tag)
            if field == 'volume' and raw.isdigit() and 0 <= int(raw) <= 100:
                result[field] = int(raw)
            elif field != 'volume' and raw in ('0', '1'):
                result[field] = raw == '1'
        except ValueError:
            pass
    return result


def playback(player, nets):
    try:
        current = identity(player['ip'], nets)
        if current['id'] != player['id']:
            raise ValueError('Sonos address changed.')
        state = PLAYBACK.get(value(request(player['ip'], nets, 'GetTransportInfo'), 'CurrentTransportState'), 'unknown')
        position = request(player['ip'], nets, 'GetPositionInfo')
        track = metadata(position)
        uri = value(position, 'TrackURI')
        source = source_name(uri)
        if source == 'TV':
            source, track = 'TV', {'title': 'TV audio'}
        elif source == 'Line-in':
            source, track = 'Line-in', {'title': 'Line-in audio'}
        elif source == 'Radio':
            if track.get('stream'):
                track['title'] = track['stream']
        if not track.get('title') and state in ('playing', 'loading'):
            track['title'] = 'Audio playing' if state == 'playing' else 'Connecting…'
        return {'playback': state, 'model': current['model'], 'title': track.get('title', ''), 'artist': track.get('artist', ''),
                'album': track.get('album', ''), 'source': source if uri else '', 'observedAt': time.time(),
                'transportActions': transport_actions(current, nets) if source not in ('TV', 'Line-in', '') else [],
                '_artwork': sonos_artwork.source(track.get('artwork'), current) if uri and state != 'idle' else None}
    except ValueError:
        return {'playback': 'offline', 'title': '', 'artist': '', 'album': '', 'source': '', 'observedAt': None}


class AudioInventory:
    def __init__(self):
        self.lock = threading.Lock()
        self.control_lock = threading.Lock()
        self.players = {}
        self.last_discovery = -1000
        self.last_read = -1000
        self.networks = ()
        self.cached = None
        self.covers = sonos_artwork.ArtworkCache()

    def artwork(self, key):
        def verify(item):
            nets = [a.network for a in interfaces()]
            if identity(item['ip'], nets)['id'] != item['id']:
                raise ValueError('Sonos address changed.')
        return self.covers.get(key, verify)

    def control(self, data, authorize):
        if not isinstance(data, dict) or set(data) - {'roomId', 'groupId', 'action', 'volume', 'muted'}:
            raise ValueError('Unsupported Sonos control.')
        room_id, group_id, action = data.get('roomId'), data.get('groupId'), data.get('action')
        if not all(isinstance(v, str) and IDENTIFIER.fullmatch(v) for v in (room_id, group_id)):
            raise ValueError('Refresh and select a Sonos room.')
        if action not in ('play', 'pause', 'stop', 'next', 'volume', 'volume_up', 'volume_down', 'mute'):
            raise ValueError('Unsupported Sonos control.')
        extras = set(data) - {'roomId', 'groupId', 'action'}
        if extras != ({'volume'} if action == 'volume' else {'muted'} if action == 'mute' else set()):
            raise ValueError('Unsupported Sonos control.')
        if action == 'volume' and (type(data['volume']) is not int or not 0 <= data['volume'] <= 100):
            raise ValueError('Choose a volume from 0 to 100.')
        if action == 'mute' and type(data['muted']) is not bool:
            raise ValueError('Choose mute or unmute.')
        if not self.control_lock.acquire(blocking=False):
            raise ValueError('Wait for the previous audio change to finish.')
        try:
            authorize()
            with self.lock:
                known = copy.deepcopy(self.players.get(room_id))
            if not known:
                raise ValueError('Refresh and select a Sonos room.')
            nets = [a.network for a in interfaces()]
            if identity(known['ip'], nets)['id'] != room_id:
                raise ValueError('The room address changed. Find rooms again.')
            groups = topology(request(known['ip'], nets, 'GetZoneGroupState'), nets)
            group = next((g for g in groups if any(m['id'] == room_id and m['visible'] for m in g['members'])), None)
            if not group or group['id'] != group_id:
                raise ValueError('The Sonos group changed. Refresh before trying again.')
            if action in ('play', 'pause', 'stop', 'next'):
                target = next(m for m in group['members'] if m['id'] == group_id)
                info = playback(target, nets)
                if action not in info.get('transportActions', []):
                    raise ValueError('This source does not support that playback control. Use its source app.')
            else:
                target = known
                levels = room_levels(target, nets)
                if action == 'mute' and levels['muted'] is None:
                    raise ValueError('Mute is unavailable for this room. Refresh and try again.')
                if action != 'mute' and (levels['volume'] is None or levels['fixedVolume']):
                    raise ValueError('Volume is unavailable or fixed for this room.')
            authorize()  # Recheck after the reads, immediately before the write.
            control_request(target['ip'], nets, action, data.get('volume') if action == 'volume' else data.get('muted'))
            return {'accepted': True, 'roomId': room_id, 'action': action}
        finally:
            with self.lock:
                self.last_read = -1000  # Includes uncertain outcomes; never retry writes automatically.
            self.control_lock.release()

    def group(self, data, authorize):
        if not isinstance(data, dict) or set(data) != {'roomId', 'groupId', 'revision', 'roomIds'}:
            raise ValueError('Invalid room selection.')
        room_id, coordinator, selected = data['roomId'], data['groupId'], data['roomIds']
        if (not isinstance(selected, list) or not 1 <= len(selected) <= 64
                or not all(isinstance(uid, str) and IDENTIFIER.fullmatch(uid) for uid in [room_id, coordinator] + selected)
                or len(set(selected)) != len(selected)
                or not isinstance(data['revision'], str) or not re.fullmatch('[a-f0-9]{64}', data['revision'])):
            raise ValueError('Invalid room selection.')
        if not self.control_lock.acquire(blocking=False):
            raise ValueError('Wait for the previous audio change to finish.')
        started = False
        try:
            authorize()
            with self.lock:
                known = copy.deepcopy(self.players)
            if room_id not in known:
                raise ValueError('Refresh and select a Sonos room.')
            nets = [a.network for a in interfaces()]
            if identity(known[room_id]['ip'], nets)['id'] != room_id:
                raise ValueError('The room address changed. Find rooms again.')
            groups = topology(request(known[room_id]['ip'], nets, 'GetZoneGroupState'), nets)
            shape = group_shape(groups)
            if group_revision(shape) != data['revision'] or room_id not in shape.get(coordinator, []):
                raise ValueError('Room links changed. Open Link rooms again to review them.')
            actions = grouping_plan(shape, coordinator, set(selected))
            members = {m['id']: m for g in groups for m in g['members'] if m['visible']}
            if not set(selected) <= set(known):
                raise ValueError('Find rooms again before linking new rooms.')
            source = members[coordinator]
            if identity(source['ip'], nets)['id'] != coordinator:
                raise ValueError('The source room address changed. Find rooms again.')
            deadline = time.monotonic() + 20
            for uid, target in actions:
                # Verify both sides and the entire reviewed topology before each write.
                if identity(members[uid]['ip'], nets)['id'] != uid or identity(source['ip'], nets)['id'] != coordinator:
                    raise ValueError('A room address changed.')
                observed = group_shape(topology(request(source['ip'], nets, 'GetZoneGroupState'), nets))
                if observed != shape:
                    raise ValueError('Room links changed elsewhere.')
                if time.monotonic() >= deadline:
                    raise ValueError('Linking took too long.')
                authorize()
                started = True
                group_request(members[uid]['ip'], nets, target)
                expected = changed_group_shape(shape, uid, target)
                # Only retry reads; a timeout must never replay a grouping command.
                for attempt in range(5):
                    current = group_shape(topology(request(source['ip'], nets, 'GetZoneGroupState'), nets))
                    if current == expected:
                        shape = current
                        break
                    if current != shape or time.monotonic() >= deadline:
                        raise ValueError('Room links changed while confirming the update.')
                    if attempt < 4:
                        time.sleep(.2)
                else:
                    raise ValueError('Sonos has not confirmed the new room links.')
            authorize()
            return {'accepted': True, 'confirmed': True, 'groupId': coordinator, 'roomIds': shape[coordinator]}
        except ValueError as error:
            if started:
                raise ValueError('Some room links may have changed. Refresh and review Link rooms before trying again.') from None
            raise
        finally:
            with self.lock:
                self.last_read = -1000
            self.control_lock.release()

    def read(self, rescan=False):
        with self.lock:
            now = time.monotonic()
            if self.cached is not None and now - self.last_read < 5 and (not rescan or now - self.last_discovery < 15):
                return copy.deepcopy(self.cached)
            attached = interfaces()
            networks = tuple(map(str, attached))
            if networks != self.networks:
                self.players, self.last_discovery = {}, -1000
                self.networks = networks
                self.covers.replace({})
            nets = [a.network for a in attached]
            if attached and (now - self.last_discovery > 180 or (rescan and now - self.last_discovery >= 15)):
                self.players.update(discover(attached))
                self.last_discovery = now
            groups, covered, grouping = {}, set(), {}
            # One topology read discovers even players that missed multicast.
            # Read another seed only if it belongs to another household.
            for player in list(self.players.values())[:64]:
                if player['id'] in covered:
                    continue
                try:
                    household = topology(request(player['ip'], nets, 'GetZoneGroupState'), nets)
                    shape = group_shape(household)
                    for group in household:
                        groups[group['id']] = group
                        for member in group['members']:
                            covered.add(member['id'])
                            old = self.players.get(member['id'], {})
                            self.players[member['id']] = {**old, **member, 'model': old.get('model', '')}
                            grouping[member['id']] = {'groupRevision': group_revision(shape), 'groupScope': group_scope(shape)}
                except ValueError:
                    pass
            # A failed topology read must not claim old grouping is still valid.
            # Keep known rooms visible with explicit unavailable state instead.
            unavailable = [p for p in self.players.values() if p['id'] not in covered
                           and p.get('visible', True) and not any(x in p.get('model', '') for x in ('Sub', 'Boost', 'Bridge'))]
            def read_group(group):
                coordinator = self.players[group['id']]
                info = playback(coordinator, nets)
                if info.get('model'):
                    coordinator['model'] = info['model']
                rooms = [m['name'] for m in group['members'] if m['visible']]
                return [{**info, 'id': m['id'], 'name': m['name'], 'model': self.players[m['id']].get('model', ''),
                         'groupId': group['id'], 'groupRooms': rooms, **grouping.get(m['id'], {}),
                         'groupMembers': [{'id': member['id'], 'name': member['name']} for member in group['members'] if member['visible']],
                         'groupName': coordinator['name']} for m in group['members'] if m['visible']]
            with ThreadPoolExecutor(max_workers=4) as pool:
                zones = [z for rows in pool.map(read_group, list(groups.values())[:64]) for z in rows]
            def read_levels(zone):
                return room_levels(self.players[zone['id']], nets) if zone['playback'] != 'offline' else {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                for zone, levels in zip(zones, pool.map(read_levels, zones)):
                    zone.update(levels)
            zones.extend({'id': p['id'], 'name': p['name'], 'model': p.get('model', ''), 'playback': 'offline',
                          'groupId': None, 'groupRooms': [], 'title': '', 'artist': '', 'album': '', 'source': '',
                          'observedAt': None} for p in unavailable)
            zones.sort(key=lambda z: (z['name'].casefold(), z['id']))
            sources = {}
            for zone in zones:
                item = zone.pop('_artwork', None)
                zone['artworkId'] = sonos_artwork.identifier(item) if item else None
                if item:
                    sources[zone['artworkId']] = item
            self.covers.replace(sources)
            state = 'ready' if zones else 'empty' if attached else 'unavailable'
            message = ('Connect the panel to the same local network as your Sonos system.' if not attached else
                       'No Sonos rooms found. Set up your speakers in the Sonos app, then tap Find rooms.' if not zones else '')
            self.cached = {'state': state, 'zones': zones, 'message': message, 'observedAt': time.time()}
            self.last_read = time.monotonic()
            return copy.deepcopy(self.cached)
