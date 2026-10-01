"""Short-lived, PIN-authenticated settings access, separate from alarm unlocks."""
from http.cookies import SimpleCookie
import secrets
import time

from keypad_store import KeypadStore

LIFETIME = 300


class SettingsLocked(ValueError):
    pass


class SettingsAccess:
    def __init__(self, pin_file, port, verifier=None, lifetime=LIFETIME, secure=False):
        self.pin_file = pin_file
        self.cookie_name = 'keypad_settings_' + str(port)
        self.verifier = verifier
        self.lifetime = lifetime
        self.secure = '; Secure' if secure else ''
        if secure:
            self.cookie_name = '__Host-' + self.cookie_name
        self.sessions = {}

    def store(self):
        return KeypadStore(self.pin_file() if callable(self.pin_file) else self.pin_file)

    def saved_pin(self):
        return {'configured': True} if self.store().has_users() else None

    def token(self, headers):
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get('Cookie', ''))
            value = cookie.get(self.cookie_name)
            return value.value if value else ''
        except Exception:
            return ''

    def status(self, headers):
        now = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items() if value['expires'] > now}
        pin_set = self.saved_pin() is not None
        session = self.sessions.get(self.token(headers))
        user = None
        if session:
            try:
                user = self.store().current(session['user'], admin=True)
            except ValueError:
                self.sessions.pop(self.token(headers), None)
        seconds = max(0, session['expires'] - now) if user else 0
        return {'pinSet': pin_set, 'settingsUnlocked': not pin_set or seconds > 0,
                'expiresIn': seconds, 'user': user}

    def require(self, headers):
        status = self.status(headers)
        if not status['settingsUnlocked']:
            raise SettingsLocked('Enter an administrator PIN to open settings.')
        return status['user']

    def verify(self, pin):
        user = self.verifier(pin) if self.verifier else self.store().authenticate(pin)
        return self.store().current(user, admin=True)

    def unlock(self, pin, headers):
        user = self.verify(pin)
        self.sessions.pop(self.token(headers), None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {'expires': time.monotonic() + self.lifetime, 'user': user}
        return ({'pinSet': True, 'settingsUnlocked': True, 'expiresIn': self.lifetime, 'user': user},
                f'{self.cookie_name}={token}; Path=/{self.secure}; HttpOnly; SameSite=Strict; Max-Age={self.lifetime}')

    def lock(self, headers):
        self.sessions.pop(self.token(headers), None)
        return f'{self.cookie_name}=; Path=/{self.secure}; HttpOnly; SameSite=Strict; Max-Age=0'


def protected_route(path):
    return path.startswith(('/installer/', '/settings/', '/phone-pair/', '/browser-setup/')) or path in {
        '/connect', '/disconnect', '/local/login', '/local/logout', '/local/status',
        '/diagnostics', '/entry/enable', '/pin/setup', '/arm', '/disarm',
        '/login', '/inspect', '/result', '/probe/run', '/probe/cleanup', '/logout',
    }
