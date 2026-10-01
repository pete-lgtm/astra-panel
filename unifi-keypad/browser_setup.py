"""Temporary, locally paired HTTPS access to the shared setup workflow only."""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from phone_pairing import qr_svg

PAIR_SECONDS = 600
SESSION_SECONDS = 1800
ALLOWED_POSTS = {'/settings/status', '/settings/unlock', '/settings/lock', '/connect',
                 '/installer/login', '/installer/context', '/installer/pin/status',
                 '/installer/pin/create', '/installer/review', '/installer/apply',
                 '/status', '/entry/status', '/settings/sensors/enable'}
ASSETS = {'/setup': ('setup_lab.html', 'text/html; charset=utf-8'),
          '/setup.js': ('setup_lab.js', 'text/javascript'), '/setup.css': ('setup_lab.css', 'text/css'),
          '/phone-pair.js': ('phone_pair_ui.js', 'text/javascript'),
          '/browser-pair.js': ('browser_pair_ui.js', 'text/javascript'),
          '/web-setup.css': ('web_setup.css', 'text/css')}


def private_address(value):
    try:
        address = ipaddress.ip_address(value)
        return address.version == 4 and (address.is_loopback or any(address in ipaddress.ip_network(n)
            for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
    except ValueError:
        return False


def lan_address():
    """Ask the routing table for the LAN address; UDP connect sends no packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(('192.0.2.1', 9))
            address = route.getsockname()[0]
            if private_address(address) and not ipaddress.ip_address(address).is_loopback:
                return address
    except OSError:
        pass
    return ''


class BrowserSetup:
    def __init__(self, handler, access_factory, session_info, origin='', bind='', cert='', key='', clock=time.monotonic, landing_port=0):
        self.handler, self.access_factory, self.session_info = handler, access_factory, session_info
        self.origin, self.bind, self.cert, self.key = origin, bind, cert, key
        self.clock = clock
        self.lock = threading.RLock()
        self.current = None
        self.httpd = None
        self.closing = None
        self.access = access_factory()
        self.cookie_name = '__Host-loxicom_setup'
        self.landing_port = landing_port
        self.landing_server = None

    def bind_address(self):
        return lan_address() if self.bind == 'auto' else self.bind

    def connection_info(self):
        address = self.bind_address() or lan_address()
        if not private_address(address) or ipaddress.ip_address(address).is_loopback:
            address = ''
        available = self.available()
        landing = self.landing_server
        landing_url = (f'http://{address}:{landing.server_port}'
                       if landing and landing.server_address[0] == address else '')
        return {'ipAddress': address, 'available': available,
                'secureUrl': self.origin if available else '', 'url': landing_url or (self.origin if available else '')}

    def start_landing(self):
        """LAN discovery only. Never accept credentials or expose the local API."""
        if not self.landing_port or self.landing_server:
            return
        address = self.bind_address()
        if not private_address(address):
            return
        manager = self

        class LandingHandler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def log_message(self, *args):
                pass

            def send_page(self, status, body, kind='text/html; charset=utf-8'):
                self.close_connection = True
                self.send_response(status)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                host = f'{address}:{self.server.server_port}'
                if (self.headers.get('Host') != host or not private_address(self.client_address[0])):
                    return self.send_page(403, b'Use the address displayed on your panel.', 'text/plain')
                if self.path != '/':
                    return self.send_page(404, b'This address provides setup directions only.', 'text/plain')
                # No code, token, settings, or device information is served over HTTP.
                state = manager.status()['state']
                ready = manager.available() and state in ('waiting', 'connected')
                detail = ('Continue to secure setup, then enter the pairing code shown on your panel.' if ready else
                          'On your panel, tap “Set up from a computer” to open a private setup session.' if manager.available() else
                          'Computer setup is not ready on this panel yet. You can continue setup on the touchscreen.')
                link = f'<a href="{escape(manager.origin, quote=True)}">Continue to secure setup →</a>' if ready else ''
                page = Path(__file__).with_name('browser_landing.html').read_text()
                body = page.replace('{{DETAIL}}', escape(detail)).replace('{{LINK}}', link).encode()
                self.send_page(200, body)

            def do_POST(self):
                self.send_page(405, b'Enter setup information only on the secure setup page.', 'text/plain')

        landing = ThreadingHTTPServer((address, self.landing_port), LandingHandler)
        landing.daemon_threads = True
        self.landing_server = landing
        threading.Thread(target=landing.serve_forever, daemon=True).start()

    def watch_landing(self):
        """Retry after Wi-Fi connects, and stop advertising an obsolete DHCP address."""
        if not self.landing_port:
            return
        while True:
            try:
                address = self.bind_address()
                if self.landing_server and self.landing_server.server_address[0] != address:
                    self.landing_server.shutdown()
                    self.landing_server.server_close()
                    self.landing_server = None
                self.start_landing()
            except OSError:
                pass
            threading.Event().wait(10)

    def available(self):
        parts = urlsplit(self.origin)
        try:
            return bool(parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password and
                        not parts.path and not parts.query and not parts.fragment and
                        private_address(self.bind_address()) and
                        Path(self.cert).is_file() and Path(self.key).is_file())
        except ValueError:
            return False

    def status(self):
        with self.lock:
            if not self.current:
                return {'state': 'idle', 'available': self.available()}
            if self.current['state'] in ('waiting', 'connected') and self.clock() >= self.current['expires']:
                self.end('expired')
            return {'state': self.current['state'], 'expiresIn': max(0, int(self.current['expires'] - self.clock())),
                    'available': self.available()}

    def begin(self):
        if not self.available():
            raise ValueError('Computer setup needs the panel’s trusted HTTPS address. Setup on the touchscreen is still available.')
        with self.lock:
            self.start_listener()
            self.access.sessions.clear()
            token, code = secrets.token_urlsafe(32), f'{secrets.randbelow(100000000):08d}'
            self.current = {'state': 'waiting', 'expires': self.clock() + PAIR_SECONDS, 'token': token,
                            'code': code, 'attempts': 0, 'session': '', 'csrf': '', 'saved': False}
            self.schedule_expiry(token, PAIR_SECONDS)
            return {'url': self.connection_info()['url'], 'secureUrl': self.origin,
                    'code': code[:4] + ' ' + code[4:], 'qr': qr_svg(self.origin + '/#' + token),
                    'expiresIn': PAIR_SECONDS}

    def schedule_expiry(self, token, seconds):
        def expire():
            with self.lock:
                if self.current and self.current.get('token') == token:
                    self.status()
        timer = threading.Timer(seconds + 1, expire)
        timer.daemon = True
        timer.start()

    def claim(self, data):
        with self.lock:
            if self.status()['state'] != 'waiting' or not isinstance(data, dict) or set(data) not in ({'code'}, {'token'}):
                raise ValueError('Start a new computer setup session on the panel.')
            self.current['attempts'] += 1
            supplied = data.get('token') if 'token' in data else str(data.get('code', '')).replace(' ', '').replace('-', '')
            expected = self.current['token'] if 'token' in data else self.current['code']
            if not isinstance(supplied, str) or not supplied.isascii() or not secrets.compare_digest(supplied, expected):
                if self.current['attempts'] >= 8:
                    self.end('locked')
                raise ValueError('Pairing code did not match. Check the code displayed on your panel.')
            self.current.update(state='connected', code='', session=secrets.token_urlsafe(32),
                                csrf=secrets.token_urlsafe(32), expires=self.clock() + SESSION_SECONDS)
            self.schedule_expiry(self.current['token'], SESSION_SECONDS)
            return self.current['session']

    def authorized(self, headers):
        if self.status()['state'] != 'connected':
            return False
        jar = SimpleCookie()
        try:
            jar.load(headers.get('Cookie', ''))
            value = jar.get(self.cookie_name)
            return bool(value and secrets.compare_digest(value.value, self.current['session']))
        except Exception:
            return False

    def end(self, state='closed'):
        with self.lock:
            if self.current:
                self.current.update(state=state, code='', token='', session='', csrf='', expires=self.clock())
            self.access.sessions.clear()
            # Keep the listener only until its current request has replied. No
            # paired session or credentials remain usable after this point.
            server, self.httpd = self.httpd, None
            if server:
                def close():
                    server.shutdown()
                    server.server_close()
                self.closing = threading.Thread(target=close, daemon=True)
                self.closing.start()
            return {'state': state}

    def start_listener(self):
        if self.httpd:
            return
        if self.closing:
            self.closing.join(timeout=3)
            if self.closing.is_alive():
                raise ValueError('The previous setup session is closing. Try again in a moment.')
            self.closing = None
        manager = self
        class RemoteHandler(self.handler):
            remote_setup = True
            def setup(self):
                super().setup()
                self.connection.settimeout(15)
                self.pair_cookie = None
            @property
            def access(self):
                return manager.access
            def trusted_host(self):
                return self.headers.get('Host') == urlsplit(manager.origin).netloc
            def trusted_post(self):
                return (self.trusted_host() and self.headers.get('Origin') == manager.origin and
                        manager.authorized(self.headers) and
                        secrets.compare_digest(self.headers.get('X-Keypad-Token', '').encode(), manager.current['csrf'].encode()))
            def reply(self, status, data, content_type='application/json'):
                if status == 200 and self.path == '/installer/apply' and data.get('saved'):
                    manager.current['saved'] = True
                body = json.dumps(data).encode() if content_type == 'application/json' else data
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
                for cookie in (self.pair_cookie, getattr(self, 'settings_cookie', None)):
                    if cookie:
                        self.send_header('Set-Cookie', cookie)
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                with manager.lock:
                    if not self.trusted_host():
                        return self.reply(403, {'error': 'Use the address shown on your panel.'})
                    if self.path in ('/', '/pair.js', '/pair.css'):
                        if manager.status()['state'] not in ('waiting', 'connected'):
                            return self.reply(410, {'error': 'Setup access is closed. Start again on your panel.'})
                        name, kind = {'/': ('browser_pair.html', 'text/html; charset=utf-8'),
                                      '/pair.js': ('browser_pair.js', 'text/javascript'),
                                      '/pair.css': ('browser_pair.css', 'text/css')}[self.path]
                        return self.reply(200, Path(__file__).with_name(name).read_bytes(), kind)
                    if not manager.authorized(self.headers):
                        return self.reply(401, {'error': 'Pair this computer from the panel before opening setup.', 'pairingRequired': True})
                    if self.path == '/session':
                        return self.reply(200, {**manager.session_info(), 'token': manager.current['csrf'], 'browserSetup': True,
                                                'browserExpiresIn': manager.status()['expiresIn']})
                    if self.path not in ASSETS:
                        return self.reply(404, {'error': 'This address provides setup only.'})
                    name, kind = ASSETS[self.path]
                    return self.reply(200, Path(__file__).with_name(name).read_bytes(), kind)
            def do_POST(self):
                with manager.lock:
                    if not self.trusted_host() or self.headers.get('Origin') != manager.origin:
                        return self.reply(403, {'error': 'Invalid setup request.'})
                    if self.path == '/claim':
                        try:
                            size = int(self.headers.get('Content-Length', '0'))
                            if not 0 < size <= 1024 or self.headers.get('Content-Type') != 'application/json':
                                raise ValueError('Invalid pairing request.')
                            value = manager.claim(json.loads(self.rfile.read(size)))
                            self.pair_cookie = f'{manager.cookie_name}={value}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}'
                            return self.reply(200, {'paired': True, 'next': '/setup'})
                        except (ValueError, TypeError):
                            return self.reply(400, {'error': 'Could not pair. Check the panel’s code or start a new session.'})
                    if not self.trusted_post():
                        return self.reply(401, {'error': 'Computer setup expired or was closed. Pair again from the panel.', 'pairingRequired': True})
                    if self.path == '/browser/end':
                        state = 'complete' if manager.current['saved'] else 'closed'
                        manager.end(state)
                        self.pair_cookie = f'{manager.cookie_name}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0'
                        return self.reply(200, {'state': state})
                    if self.path not in ALLOWED_POSTS:
                        return self.reply(403, {'error': 'This connection is limited to panel setup.'})
                    if self.path not in ('/settings/status', '/settings/unlock', '/settings/lock'):
                        try:
                            self.access.require(self.headers)
                        except ValueError:
                            return self.reply(403, {'error': 'Enter your administrator PIN to continue.', 'settingsLocked': True})
                    super().do_POST()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert, self.key)
        class SecureServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                # Disconnects and incomplete TLS handshakes are normal. Keep
                # request data and client addresses out of the console log.
                pass
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(15)
                # Handshake in the bounded request thread, never the accept loop.
                # An idle LAN client must not prevent expiry or cancellation.
                return context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False), address
        server = SecureServer((self.bind_address(), urlsplit(self.origin).port or 443), RemoteHandler)
        server.daemon_threads = True
        self.httpd = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
