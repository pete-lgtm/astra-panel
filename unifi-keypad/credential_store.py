"""Private device-local API credentials; never included in application releases.

This is permission-restricted storage, not encryption. UniFi passwords and MFA
codes are deliberately not persisted.
"""
import ipaddress
import json
import os
from pathlib import Path
import stat
from device_config import atomic_json


def validate(config):
    if not isinstance(config, dict) or set(config) != {'host', 'key'}:
        raise ValueError('Invalid saved API connection.')
    address = ipaddress.ip_address(config['host'])
    if address.version != 4 or not any(address in ipaddress.ip_network(n) for n in
            ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('Invalid saved console address.')
    key = config['key']
    if not isinstance(key, str) or not 1 <= len(key) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('Invalid saved API key.')
    return dict(config)


def save(path, config):
    config = validate(config)
    atomic_json(path, {'version': 1, 'connection': config})


def load(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 8192):
                return {}
            data = json.load(stream)
        if set(data) != {'version', 'connection'} or data['version'] != 1:
            return {}
        return validate(data['connection'])
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def forget(path):
    Path(path).unlink(missing_ok=True)
