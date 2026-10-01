"""Owner-only storage of a console-issued session; never passwords or MFA codes.

A loaded session must still be validated with the console before alarm state is
trusted. Expiration and revocation are enforced by UniFi, not inferred locally.
"""
import os
import re
import stat
import json
from pathlib import Path
from credential_store import validate as validate_connection
from device_config import atomic_json


def validate(session):
    if not isinstance(session, dict) or set(session) != {'host', 'cookie'}:
        raise ValueError('Invalid saved UniFi session.')
    validate_connection({'host': session['host'], 'key': 'host-validation-only'})
    cookie = session['cookie']
    if (not isinstance(cookie, str) or len(cookie) > 8192 or
            not re.fullmatch(r'(?:TOKEN|UOS_TOKEN)=[!-~]+', cookie) or
            any(c in cookie for c in ';,\"\\')):
        raise ValueError('Invalid saved UniFi session.')
    return dict(session)


def save(path, session):
    atomic_json(path, {'version': 1, 'session': validate(session)})


def load(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 16384):
                return {}
            data = json.load(stream)
        if set(data) != {'version', 'session'} or data['version'] != 1:
            return {}
        return validate(data['session'])
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def forget(path):
    Path(path).unlink(missing_ok=True)
