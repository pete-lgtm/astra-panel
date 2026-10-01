"""One shared, persistent keypad PIN. Never return its verifier to the browser."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile

ITERATIONS = 600000
ALREADY_SET = 'A keypad PIN is already saved. Setup cannot replace it.'


def read_pin(path):
    path = Path(path)
    try:
        with path.open() as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return None
    try:
        if len(raw) > 4096:
            raise ValueError()
        saved = json.loads(raw)
        if not isinstance(saved, dict) or set(saved) != {'version', 'salt', 'digest'} or type(saved['version']) is not int or saved['version'] != 1:
            raise ValueError()
        salt, digest = bytes.fromhex(saved['salt']), bytes.fromhex(saved['digest'])
        if len(salt) != 16 or len(digest) != 32:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError('The saved keypad PIN could not be read. It has not been reset.') from None
    os.chmod(path, 0o600)
    return {'salt': salt, 'digest': digest}


def create_pin(path, pin, confirmation):
    path = Path(path)
    if os.path.lexists(path):
        raise ValueError(ALREADY_SET)
    if not isinstance(pin, str) or not pin.isascii() or not pin.isdigit() or not 4 <= len(pin) <= 8:
        raise ValueError('Choose a PIN containing 4 to 8 digits.')
    if not isinstance(confirmation, str) or confirmation != pin:
        raise ValueError('The PINs do not match. Enter the same PIN twice.')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', pin.encode(), salt, ITERATIONS)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish a complete private file atomically without ever replacing one
    # created by another setup page/process while hashing this request.
    fd, temporary = tempfile.mkstemp(prefix='.keypad-pin-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'version': 1, 'salt': salt.hex(), 'digest': digest.hex()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise ValueError(ALREADY_SET) from None
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)
    return {'salt': salt, 'digest': digest}
