"""Loopback-only setup experiment with its own temporary UniFi login.

Run separately from server.py to preserve the working alarm's in-memory session.
"""
import ipaddress
import importlib
import json
import os
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from rule_setup import RuleSetupSession, inspect_console
import rule_setup
from settings_access import SettingsAccess, SettingsLocked, protected_route
from device_config import paths

PORT = int(os.environ.get('KEYPAD_SETUP_PORT', '18766'))
ORIGIN = 'http://127.0.0.1:%d' % PORT
TOKEN = secrets.token_urlsafe(32)
SESSION = RuleSetupSession()
LOCK = threading.Lock()
ROOT = Path(__file__).parent
RESULT = {'connected': False}
PROBE_RESULT = None
INSTALLER_STATE = {}
SETTINGS_ACCESS = SettingsAccess(lambda: paths(root=ROOT)[0] / '.keypad-pin.json', PORT)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, kind='application/json'):
        data = json.dumps(body).encode() if kind == 'application/json' else body
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if getattr(self, 'settings_cookie', None):
            self.send_header('Set-Cookie', self.settings_cookie)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def trusted_host(self):
        return self.headers.get('Host') == '127.0.0.1:%d' % PORT

    def do_GET(self):
        if not self.trusted_host():
            return self.reply(403, {'error': 'Invalid host.'})
        assets = {'/': ('setup_lab.html', 'text/html; charset=utf-8'),
                  '/setup.js': ('setup_lab.js', 'text/javascript'),
                  '/setup.css': ('setup_lab.css', 'text/css'),
                  '/web-setup.css': ('web_setup.css', 'text/css'),
                  '/browser-pair.js': ('browser_pair_ui.js', 'text/javascript'),
                  '/phone-pair.js': ('phone_pair_ui.js', 'text/javascript')}
        if self.path in assets:
            path, kind = assets[self.path]
            return self.reply(200, (ROOT / path).read_bytes(), kind)
        if self.path == '/session':
            return self.reply(200, {'token': TOKEN})
        self.reply(404, {'error': 'Not found.'})

    def do_POST(self):
        global RESULT, PROBE_RESULT
        self.settings_cookie = None
        if not self.trusted_host() or self.headers.get('Origin') != ORIGIN or self.headers.get('X-Keypad-Token') != TOKEN:
            return self.reply(403, {'error': 'Reload the setup test to continue.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 8192:
                raise ValueError('Request too large.')
            body = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(body, dict):
                raise ValueError('Invalid request.')
            with LOCK:
                if self.path == '/settings/status':
                    return self.reply(200, SETTINGS_ACCESS.status(self.headers))
                if self.path == '/settings/unlock':
                    result, self.settings_cookie = SETTINGS_ACCESS.unlock(body.get('pin'), self.headers)
                    return self.reply(200, result)
                if self.path == '/settings/lock':
                    self.settings_cookie = SETTINGS_ACCESS.lock(self.headers)
                    return self.reply(200, {'settingsUnlocked': False})
                if protected_route(self.path):
                    SETTINGS_ACCESS.require(self.headers)
                if self.path == '/login':
                    host = str(ipaddress.IPv4Address(body.get('host', '')))
                    if host != os.environ.get('KEYPAD_SETUP_HOST'):
                        raise ValueError('Use the console address configured for this compatibility test.')
                    RESULT = {'connected': False}
                    result = SESSION.login(host, body.get('username'), body.get('password'), body.get('verificationCode'))
                    if result.get('localConnected'):
                        RESULT = inspect_console(SESSION)
                        result = RESULT
                elif self.path == '/inspect':
                    RESULT = inspect_console(SESSION)
                    result = RESULT
                elif self.path == '/result':
                    result = {**RESULT, 'probe': PROBE_RESULT}
                elif self.path == '/probe/run':
                    # This lab can load an updated probe without discarding the
                    # temporary sign-in. The live keypad never uses this route.
                    importlib.reload(rule_setup)
                    PROBE_RESULT = rule_setup.run_creation_probe(SESSION, ROOT / '.setup-rule-probe.json')
                    result = PROBE_RESULT
                elif self.path == '/probe/cleanup':
                    importlib.reload(rule_setup)
                    journal_path = ROOT / '.setup-rule-probe.json'
                    journal = json.loads(journal_path.read_text())
                    rule_setup.cleanup_probe(SESSION, journal, journal_path)
                    result = {'message': 'Temporary rule removal verified.'}
                elif self.path.startswith('/installer/'):
                    import onboarding
                    importlib.reload(onboarding)
                    result = onboarding.handle(self.path, body, SESSION, ROOT, INSTALLER_STATE)
                    if self.path == '/installer/pin/create':
                        access, self.settings_cookie = SETTINGS_ACCESS.unlock(body.get('pin'), self.headers)
                        result.update(access)
                elif self.path == '/logout':
                    SESSION.clear()
                    RESULT = result = {'connected': False, 'message': 'Setup test disconnected.'}
                else:
                    return self.reply(404, {'error': 'Not found.'})
            self.reply(200, {**result, 'probeAvailable': True})
        except SettingsLocked as error:
            self.reply(403, {'error': str(error), 'settingsLocked': True})
        except (ValueError, TypeError) as error:
            self.reply(400, {'error': str(error)})
        except Exception:
            self.reply(500, {'error': 'Setup test could not complete. No automatic retry was attempted.'})


if __name__ == '__main__':
    if not os.environ.get('UNIFI_CERT_SHA256') or not os.environ.get('KEYPAD_SETUP_HOST'):
        raise SystemExit('Configure the previously verified console fingerprint and address first.')
    print('Keypad setup test: ' + ORIGIN, flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
