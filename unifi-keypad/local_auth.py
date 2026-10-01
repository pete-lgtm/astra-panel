"""UniFi console sessions with optional private device storage.

No browser cookie access, password storage, or credential logging.
"""
import hashlib
import base64
import hmac
import http.client
from http.cookies import SimpleCookie
import json
import os
import ssl
import time
import re
import session_store
from protect_setup_api import csrf_from_cookie

LOGIN = '/api/auth/login'
REFRESH = '/api/auth/refresh'
REFRESH_LEAD = 3600
REFRESH_RETRY = 300


def cookie_expiry(cookie):
    """Scheduling hint from our console-issued token, never proof of login."""
    try:
        part = cookie.split('=', 1)[1].split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        expires = claims.get('exp')
        if type(expires) in (int, float) and 0 < expires < 100_000_000_000:
            return float(expires)
    except (ValueError, TypeError, IndexError, AttributeError):
        pass
    return None

class MfaRequired(Exception):
    def __init__(self, cookie):
        super().__init__('UniFi verification required.')
        self.cookie = cookie

class ConsoleRequestError(ValueError):
    """A console failure without exposing its response body or credentials."""
    def __init__(self, status, message=None):
        self.status = status
        super().__init__(message or 'Console request failed (HTTP %s). No alarm or device action was repeated.' % status)

class LocalSession:
    def __init__(self, storage_path=None):
        self.storage_path = None
        self.clear()
        self.storage_path = storage_path

    def clear(self):
        self.generation = getattr(self, 'generation', 0) + 1
        self.host = None
        self.cookie = None
        self.pending = None
        self._saved_session = None
        self._validated = False
        self.expires_at = None
        self._login_expiry = None
        self._refresh_at = 0
        self._refresh_retry_at = 0
        if self.storage_path is not None:
            session_store.forget(self.storage_path)

    def restore(self, expected_host):
        """Restore only for the configured console; profiles() verifies it live."""
        if self.storage_path is None:
            return False
        saved = session_store.load(self.storage_path)
        if not saved or saved['host'] != expected_host:
            self.clear()
            return False
        self.generation += 1
        self.host, self.cookie = saved['host'], saved['cookie']
        self.pending = None
        self._saved_session = saved
        self._validated = True
        self._schedule_refresh(cookie_expiry(self.cookie), restored=True)
        return True

    def save(self):
        if self._validated and self.storage_path is not None and self.host and self.cookie:
            current = {'host': self.host, 'cookie': self.cookie}
            if current != self._saved_session:
                session_store.save(self.storage_path, current)
                self._saved_session = current

    def _schedule_refresh(self, expires, restored=False):
        self.expires_at = expires
        # UniFi OS refreshes an hour before x-token-expire-time (milliseconds).
        # Old files keep their format: the JWT exp claim restores this schedule.
        delay = expires - time.time() - REFRESH_LEAD if expires else REFRESH_RETRY
        self._refresh_at = time.monotonic() + max(0 if restored else 60, delay)

    def _accept_response(self, cookie, expires, *, renewed=False):
        changed = bool(cookie and cookie != self.cookie)
        if cookie:
            self.cookie = cookie
        if expires is None and changed:
            expires = cookie_expiry(cookie)
        if renewed or changed or (expires is not None and expires != self.expires_at):
            self._schedule_refresh(expires if expires is not None else self.expires_at)
        self.save()

    def refresh_if_due(self, force=False):
        """Renew the console-issued login, without passwords or MFA replay.

        Failed renewal backs off while retaining the last token. Only an explicit
        authentication rejection from the renewal endpoint discards it.
        """
        now = time.monotonic()
        if (not self.cookie or not self._validated or now < self._refresh_retry_at or
                (not force and now < self._refresh_at)):
            return False
        host, cookie, generation = self.host, self.cookie, self.generation
        self._refresh_retry_at = now + REFRESH_RETRY
        try:
            headers = {'X-CSRF-Token': csrf_from_cookie(cookie)}
            _, rotated, expires = self._request_once(host, 'POST', REFRESH,
                                                     cookie=cookie, extra_headers=headers)
        except ConsoleRequestError as error:
            if error.status in (401, 403):
                if (self.host, self.cookie, self.generation) == (host, cookie, generation):
                    self.clear()
                raise ConsoleRequestError(error.status, 'UniFi no longer accepts the saved login. Sign in to reconnect.') from None
            return False
        except ValueError:
            # Connectivity, certificate changes, or unsupported token formats are
            # not evidence of revocation. Retain the private file for recovery.
            return False
        if (self.host, self.cookie, self.generation) != (host, cookie, generation):
            return False
        self._accept_response(rotated, expires, renewed=True)
        # A still-denied feature must not trigger a renewal on every 1s poll.
        self._refresh_retry_at = time.monotonic() + 60
        return True

    def request(self, host, method, path, payload=None, cookie=None, extra_headers=None):
        owns_cookie = bool(cookie and host == self.host and cookie == self.cookie)
        generation = self.generation
        # Reads may renew first. Writes use exactly their original cookie/CSRF
        # pair and are never replayed, including when authentication fails.
        retry_read = owns_cookie and self._validated and method == 'GET'
        renewed = False
        if retry_read:
            renewed = self.refresh_if_due()
            cookie = self.cookie
        try:
            body, rotated, expires = self._request_once(host, method, path, payload,
                                                        cookie, extra_headers)
        except ConsoleRequestError as error:
            if error.status != 401 or not retry_read or renewed or not self.refresh_if_due(force=True):
                raise
            cookie = self.cookie
            # Exactly one retry of a read, never login, refresh, or a mutation.
            body, rotated, expires = self._request_once(host, method, path, payload,
                                                        cookie, extra_headers)
        if path == LOGIN:
            self._login_expiry = expires
        if owns_cookie and (self.host != host or self.generation != generation):
            raise ValueError('UniFi connection changed during the request. Try again.')
        if owns_cookie and (self.host, self.cookie, self.generation) == (host, cookie, generation):
            self._accept_response(rotated, expires)
        return body, rotated

    def _request_once(self, host, method, path, payload=None, cookie=None, extra_headers=None):
        pin = os.environ.get('UNIFI_CERT_SHA256', '').replace(':', '').lower()
        context = ssl.create_default_context(cafile=os.environ.get('UNIFI_CA_FILE') or None)
        if pin:
            if len(pin) != 64 or any(c not in '0123456789abcdef' for c in pin):
                raise ValueError('Invalid configured console certificate fingerprint.')
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(host, timeout=8, context=context)
        try:
            connection.connect()
            if pin and not hmac.compare_digest(hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest(), pin):
                raise ValueError('Console certificate changed. Credentials were not sent.')
            headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
            if extra_headers:
                if set(extra_headers) != {'X-CSRF-Token'} or not isinstance(extra_headers['X-CSRF-Token'], str) or not re.fullmatch(r'[!-~]{1,8192}', extra_headers['X-CSRF-Token']):
                    raise ValueError('Unsupported console request headers.')
                headers.update(extra_headers)
            if cookie:
                headers['Cookie'] = cookie
            connection.request(method, path, body=json.dumps(payload).encode() if payload else None, headers=headers)
            response = connection.getresponse()
            body = response.read(2_000_000)
            if response.status == 499 and path == '/api/auth/login':
                challenge = json.loads(body)
                data = challenge.get('data', {}) if isinstance(challenge, dict) else {}
                mfa_cookie = data.get('mfaCookie') if isinstance(data, dict) else None
                if not isinstance(mfa_cookie, str) or not mfa_cookie or len(mfa_cookie) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in mfa_cookie):
                    raise ValueError('UniFi requested verification, but its challenge format is unsupported.')
                raise MfaRequired(mfa_cookie)
            if response.status in (401, 403):
                if path == '/api/auth/login':
                    if payload and payload.get('token'):
                        raise ValueError('UniFi rejected the verification sign-in. The code may be expired, belong to another account, or this verification method may be incompatible. Start again with a fresh code for the same UniFi account. Alarm-profile access was not tested.')
                    raise ValueError('UniFi rejected the username/password sign-in. Alarm-profile access was not tested.')
                message = ('UniFi denied access to this feature. Check the account permissions. Alarm state remains unverified.'
                           if response.status == 403 else
                           'UniFi could not verify this request. Connection recovery will retry automatically; no alarm or device action was repeated.')
                raise ConsoleRequestError(response.status, message)
            if response.status >= 300:
                raise ConsoleRequestError(response.status)
            cookies = SimpleCookie()
            expires = None
            for name, value in response.getheaders():
                if name.lower() == 'x-token-expire-time' and re.fullmatch(r'[0-9]{1,14}', value):
                    expires = int(value) / 1000 or None
                if name.lower() == 'set-cookie':
                    # Older Python drops the entire cookie when it sees newer
                    # attributes such as Partitioned. Only the cookie pair belongs
                    # in a request Cookie header; attributes are never forwarded.
                    pair = value.split(';', 1)[0]
                    parsed = SimpleCookie()
                    parsed.load(pair)
                    for cookie_name in ('TOKEN', 'UOS_TOKEN'):
                        if cookie_name in parsed:
                            cookie_value = parsed[cookie_name].value
                            if not cookie_value or any(ord(c) < 33 or ord(c) > 126 or c in ';,\"\\' for c in cookie_value):
                                raise ValueError('Console returned an unsupported session-cookie format.')
                            cookies[cookie_name] = cookie_value
            session_cookie = next((f'{name}={cookies[name].value}' for name in ('TOKEN', 'UOS_TOKEN') if name in cookies), None)
            return json.loads(body) if body else None, session_cookie, expires
        except ssl.SSLError:
            raise ValueError('Console certificate verification failed; credentials were not sent.') from None
        except (OSError, http.client.HTTPException):
            raise ValueError('Local console connection failed. Check its address and network.') from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError('Console returned an unexpected response.') from None
        finally:
            connection.close()

    def login(self, host, username, password, token=None):
        pending = self.pending
        self.clear()
        if not isinstance(username, str) or not username.strip() or len(username)>256 or not isinstance(password,str) or not password or len(password)>4096:
            raise ValueError('Enter your local username and password.')
        username = username.strip()
        payload = {'username':username, 'password':password, 'rememberMe':True}
        challenge_cookie = None
        if token:
            if not isinstance(token, str) or not re.fullmatch(r'[0-9]{6}', token):
                raise ValueError('Enter the six-digit authenticator code, then start sign-in again.')
            if not pending or pending['host'] != host or pending['username'] != username or time.monotonic() >= pending['expires']:
                raise ValueError('Verification expired or account changed. Start sign-in again.')
            payload['token'] = token
            challenge_cookie = pending['cookie']
        try:
            _, cookie = self.request(host, 'POST', '/api/auth/login', payload, cookie=challenge_cookie)
        except MfaRequired as challenge:
            if token:
                raise ValueError('Verification was not accepted. Start sign-in again with a fresh code.') from None
            self.pending = {'host':host, 'username':username, 'cookie':challenge.cookie, 'expires':time.monotonic()+180}
            return {'localConnected':False, 'mfaRequired':True, 'message':'UniFi requires verification. Re-enter your password and enter the six-digit code from your authenticator app. This is not your keypad PIN. Phone push approval is not supported here yet.'}
        if not cookie:
            raise ValueError('Console did not issue a supported local session. Login compatibility is unverified.')
        self.host, self.cookie = host, cookie
        self._schedule_refresh(self._login_expiry or cookie_expiry(cookie))
        return self.profiles()

    def profiles(self):
        if not self.cookie:
            raise ValueError('Sign in to the local console first.')
        profiles, cookie = self.request(self.host, 'GET', '/api/v2/alarms/profiles', cookie=self.cookie)
        if cookie:
            self.cookie = cookie
        if not isinstance(profiles,list):
            raise ValueError('Global profile response was not recognized. Alarm status remains unverified.')
        self._validated = True
        self.save()
        return {'localConnected':True, 'profiles':[{k:p.get(k) for k in ('id','title','state','state_set_at')} for p in profiles if isinstance(p,dict)],
                'message':'Local login and Global profile read succeeded. Compare these reported states with Protect before enabling automatic entry-delay control.'}

    def door_events(self, start_ms):
        if not self.cookie:
            raise ValueError('Sign in to the local console first.')
        from urllib.parse import urlencode
        query = urlencode({'start':int(start_ms), 'end':int(time.time()*1000)+1000,
                           'types':'sensorOpened', 'limit':100, 'orderDirection':'ASC'})
        events, cookie = self.request(self.host, 'GET', '/proxy/protect/api/events?' + query, cookie=self.cookie)
        if cookie:
            self.cookie = cookie
        if not isinstance(events,list) or len(events) >= 100:
            raise ValueError('Door event history is incomplete or unsupported.')
        return [{k:e.get(k) for k in ('id','type','start','sensor','device')} for e in events if isinstance(e,dict)]
