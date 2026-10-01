"""Read saved Wi-Fi credentials only after the server's fresh PIN check.

Use a separate session snapshot and fixed GET routes. These responses must never
be put in the ordinary equipment inventory, a cache, or an activity log.
"""
import re
from local_auth import LocalSession, ConsoleRequestError
from network_inventory import SITE_NAME, boolean, label


class WifiError(ValueError):
    pass


def normalize(raw):
    name = raw.get('name')
    if not isinstance(name, str) or not name or len(name) > 128:
        raise WifiError('UniFi returned an unsupported Wi-Fi network name.')
    security = raw.get('security')
    result = {'name': name, 'enabled': boolean(raw.get('enabled')),
              'hidden': boolean(raw.get('hide_ssid')) is True,
              'password': None, 'passwordState': 'unavailable',
              'message': 'UniFi did not provide a shared password for this network.'}
    if security == 'open':
        result.update(passwordState='open', message='No Wi-Fi password required.')
    elif security == 'wpaeap':
        result.update(passwordState='enterprise', message='Individual sign-in · no shared Wi-Fi password.')
    elif security == 'wpapsk':
        value = raw.get('x_passphrase')
        # Keep the password exactly as configured, including spaces. Reject
        # missing/masked credentials instead of displaying them as a password.
        if (isinstance(value, str) and 8 <= len(value) <= 64 and
                all(c.isprintable() for c in value) and
                not set(value).issubset(set('*•●'))):
            result.update(password=value, passwordState='available', message='')
    return result


class WifiReader:
    def __init__(self, host, cookie):
        self.session = LocalSession()
        self.host, self.cookie = host, cookie

    def get(self, path):
        if path != '/proxy/network/api/self/sites' and not re.fullmatch(
                r'/proxy/network/api/s/[A-Za-z0-9_-]{1,64}/rest/wlanconf', path):
            raise WifiError('Unsupported Wi-Fi discovery route.')
        body, cookie = self.session.request(self.host, 'GET', path, cookie=self.cookie)
        if cookie:
            self.cookie = cookie
        if (not isinstance(body, dict) or not isinstance(body.get('meta'), dict) or
                body['meta'].get('rc') != 'ok' or not isinstance(body.get('data'), list) or
                len(body['data']) > 256 or any(not isinstance(row, dict) for row in body['data'])):
            raise WifiError('UniFi did not return a complete Wi-Fi list. Check this account’s Network permissions.')
        return body['data']

    def read(self, selected):
        sites = self.get('/proxy/network/api/self/sites')
        site = next((site for site in sites if site.get('name') == selected), None)
        if site is None:
            raise WifiError('Choose a Network site available to this account, then enter your PIN again.')
        networks = [normalize(raw) for raw in self.get('/proxy/network/api/s/' + selected + '/rest/wlanconf')]
        return {'site': {'id': selected, 'name': label(site.get('desc'), selected)},
                'networks': sorted(networks, key=lambda item: item['name'].casefold())}


def reveal(host, cookie, selected):
    if not host or not cookie:
        raise WifiError('Reconnect your UniFi account in Settings first.')
    if not isinstance(selected, str) or not SITE_NAME.fullmatch(selected):
        raise WifiError('Choose a Network site first.')
    try:
        return WifiReader(host, cookie).read(selected)
    except ConsoleRequestError as error:
        if error.status in (401, 403):
            raise WifiError('This UniFi account cannot read Wi-Fi passwords. Check its Network access or reconnect.') from None
        raise WifiError('Wi-Fi passwords are not available from this console right now.') from None
    except WifiError:
        raise
    except (ValueError, TypeError, OSError):
        # A console response or exception may contain credentials. Never echo it.
        raise WifiError('Could not read Wi-Fi passwords. Check the console connection and try again.') from None
