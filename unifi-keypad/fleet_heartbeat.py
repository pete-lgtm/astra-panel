"""Optional heartbeat for a future Loxicom fleet console.

Disabled unless KEYPAD_FLEET_URL and KEYPAD_FLEET_SECRET are both set. POSTs a
small JSON payload (panel id, software version, alarm state, timestamp) so a
cloud console can notice offline panels and track health across customer sites.

The shared secret never travels: the body is HMAC-SHA256 signed and the
signature rides in an X-Signature header for the console to verify. The panel
never sends PINs, credentials, or user data. Plain HTTP URLs are refused.

This is the panel side only. The cloud console itself is a separate service.
"""
import hashlib
import hmac
import json
import time
import urllib.request
from urllib.parse import urlsplit


def signed_body(secret, payload):
    body = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, signature


def send(url, secret, panel_id, version, alarm_state, timeout=10):
    """POST one heartbeat. Raises on any failure; the caller decides retries."""
    scheme = urlsplit(url).scheme.lower()
    if scheme != 'https':
        raise ValueError('Fleet heartbeat requires an https URL.')
    if not secret or not panel_id:
        raise ValueError('Fleet heartbeat needs a secret and a panel id.')
    payload = {'panel_id': panel_id, 'version': version,
               'alarm_state': alarm_state, 'timestamp': int(time.time())}
    body, signature = signed_body(secret, payload)
    request = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'X-Panel-Id': panel_id,
        'X-Signature': 'sha256=' + signature,
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status >= 400:
            raise ValueError('Fleet console returned HTTP %s.' % response.status)
    return True
