"""Bounded, memory-only cover cache for artwork advertised by verified Sonos rooms.

The browser receives raster data, never arbitrary upstream URLs. Local reads are
limited to the reporting player's artwork endpoint; public image reads pin DNS
to a public address and keep normal HTTPS certificate verification.
"""
import base64
from collections import OrderedDict
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

MAX_IMAGE = 1024 * 1024


def public_address(address):
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def source(uri, player):
    if (not isinstance(uri, str) or not uri or len(uri) > 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in uri) or '\\' in uri):
        return None
    try:
        if uri.startswith('/') and not uri.startswith('//'):
            uri = f"http://{player['ip']}:1400" + uri
        url = urlsplit(uri)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.fragment:
            return None
        local = url.hostname == player['ip']
        if local:
            if url.scheme != 'http' or url.port != 1400 or url.path != '/getaa' or not url.query:
                return None
        else:
            if url.port not in (None, 443 if url.scheme == 'https' else 80):
                return None
            try:
                if not public_address(url.hostname):
                    return None
            except ValueError:
                if not re.fullmatch(r'[a-zA-Z0-9.-]{1,253}', url.hostname) or '.' not in url.hostname:
                    return None
        return {'url': uri, 'ip': player['ip'], 'id': player['id'], 'local': local}
    except (ValueError, KeyError):
        return None


def identifier(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()


def image_data(data, content_type):
    kind = content_type.split(';', 1)[0].strip().lower()
    valid = ((kind == 'image/jpeg' and data.startswith(b'\xff\xd8\xff'))
             or (kind == 'image/png' and data.startswith(b'\x89PNG\r\n\x1a\n'))
             or (kind == 'image/webp' and data.startswith(b'RIFF') and data[8:12] == b'WEBP'))
    if not valid or len(data) > MAX_IMAGE:
        raise ValueError('Artwork is not a supported image.')
    return 'data:' + kind + ';base64,' + base64.b64encode(data).decode('ascii')


def fetch(item):
    deadline = time.monotonic() + 6
    for _ in range(3):
        url = urlsplit(item['url'])
        port = url.port or (443 if url.scheme == 'https' else 80)
        if item['local']:
            address = item['ip']
        else:
            addresses = {a[4][0] for a in socket.getaddrinfo(url.hostname, port, socket.AF_INET, socket.SOCK_STREAM)}
            if not addresses or not all(public_address(a) for a in addresses):
                raise ValueError('Artwork address is unavailable.')
            address = sorted(addresses)[0]
        if time.monotonic() >= deadline:
            raise ValueError('Artwork timed out.')
        connection_type = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        connection = connection_type(url.hostname, port, timeout=2)
        # Avoid a second DNS lookup; HTTPS still verifies the original hostname.
        connection._create_connection = lambda _target, timeout, source_address=None: socket.create_connection(
            (address, port), timeout, source_address)
        try:
            connection.request('GET', (url.path or '/') + ('?' + url.query if url.query else ''),
                               headers={'Accept': 'image/jpeg, image/png, image/webp', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                item = source(urljoin(item['url'], response.getheader('Location', '')), item)
                if not item:
                    raise ValueError('Unsupported artwork redirect.')
                continue
            if response.status != 200 or response.getheader('Content-Encoding', 'identity') != 'identity':
                raise ValueError('Artwork is unavailable.')
            if int(response.getheader('Content-Length', '0')) > MAX_IMAGE:
                raise ValueError('Artwork is too large.')
            chunks, size = [], 0
            while size <= MAX_IMAGE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('Artwork timed out.')
                if connection.sock:
                    connection.sock.settimeout(min(2, remaining))
                chunk = response.read(min(16384, MAX_IMAGE + 1 - size))
                if not chunk:
                    return image_data(b''.join(chunks), response.getheader('Content-Type', ''))
                chunks.append(chunk)
                size += len(chunk)
            raise ValueError('Artwork is too large.')
        finally:
            connection.close()
    raise ValueError('Too many artwork redirects.')


class ArtworkCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(3)
        self.sources = {}
        self.images = OrderedDict()

    def replace(self, sources):
        with self.lock:
            self.sources = dict(sources)
            self.images = OrderedDict((key, val) for key, val in self.images.items() if key in sources)

    def get(self, key, verify):
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key):
            return None
        with self.lock:
            item = self.sources.get(key)
            if not item:
                return None
            cached = self.images.get(key)
            if cached and time.monotonic() < cached[0]:
                self.images.move_to_end(key)
                return cached[1]
        if not self.slots.acquire(blocking=False):
            return None
        try:
            verify(item)
            result = fetch(item)
        except (ValueError, OSError, http.client.HTTPException):
            result = None
        finally:
            self.slots.release()
        with self.lock:
            # A song, group or network change invalidates an in-flight cover.
            if self.sources.get(key) != item:
                return None
            self.images[key] = (time.monotonic() + (900 if result else 60), result)
            self.images.move_to_end(key)
            while len(self.images) > 12:
                self.images.popitem(last=False)
        return result
