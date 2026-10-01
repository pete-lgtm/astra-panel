"""Private local users and activity. PINs never leave salted verifiers in storage."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time
from uuid import uuid4

from pin_store import read_pin, ITERATIONS


def stamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


# Temporary PINs may not outlive five years. Expiry is enforced by the panel on
# every authentication and on every session-claim check, never trusted from input.
MAX_EXPIRY_SECONDS = 5 * 365 * 24 * 3600

# Arm/disarm action names recorded in activity. arm_stay/arm_away are separate
# installer-configured Protect webhooks; confirmation treats them like arm.
ARM_ACTIONS = ('arm', 'arm_stay', 'arm_away')
AUTO_LOCK_CHOICES = (60, 120, 180)


def parse_expiry(value):
    """Normalize a temporary-PIN expiry to epoch seconds, or None for no expiry.

    Accepts epoch seconds or an ISO-8601 date/time. Naive date/times are read
    in the panel's local timezone. Raises ValueError for anything unusable.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        epoch = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            epoch = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
            except ValueError:
                raise ValueError('Enter the expiry as a date and time, or leave it blank.')
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
            epoch = parsed.timestamp()
    else:
        raise ValueError('Enter the expiry as a date and time, or leave it blank.')
    now = time.time()
    if epoch <= now:
        raise ValueError('The temporary PIN must expire in the future.')
    if epoch > now + MAX_EXPIRY_SECONDS:
        raise ValueError('The temporary PIN expiry is too far ahead.')
    return epoch


class KeypadStore:
    def __init__(self, pin_file):
        self.pin_file = Path(pin_file)
        self.path = self.pin_file.parent / '.keypad-data.sqlite3'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        os.chmod(self.path, 0o600)
        db = sqlite3.connect(self.path, timeout=8)
        db.row_factory = sqlite3.Row
        try:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                  role TEXT NOT NULL, enabled INTEGER NOT NULL, salt BLOB NOT NULL,
                  digest BLOB NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, failures INTEGER, blocked REAL);
                INSERT OR IGNORE INTO attempts VALUES (1, 0, 0);
                CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  occurred_at TEXT NOT NULL, epoch REAL NOT NULL, user_id TEXT, user_name TEXT NOT NULL,
                  action TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, confirmed_at TEXT);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
            if 'expires_at' not in columns:
                db.execute('ALTER TABLE users ADD COLUMN expires_at REAL')
            if 'duress_salt' not in columns:
                db.execute('ALTER TABLE users ADD COLUMN duress_salt BLOB')
            if 'duress_digest' not in columns:
                db.execute('ALTER TABLE users ADD COLUMN duress_digest BLOB')
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM meta WHERE key='pin_migrated'").fetchone():
                saved = read_pin(self.pin_file)
                if saved:
                    db.execute('INSERT INTO users (id, name, role, enabled, salt, digest, version, created_at)'
                               ' VALUES (?,?,?,?,?,?,?,?)',
                               ('owner', 'Owner', 'admin', 1, saved['salt'], saved['digest'], 1, stamp()))
                    db.execute("INSERT INTO meta VALUES ('pin_migrated','1')")
            if db.execute("SELECT 1 FROM meta WHERE key='pin_migrated'").fetchone() and not db.execute("SELECT 1 FROM users WHERE role='admin' AND enabled=1").fetchone():
                raise ValueError('No active administrator is available. Settings remain locked.')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def expired(row, now=None):
        """A temporary PIN is expired once its time passes. Never-expiring PINs return False."""
        try:
            expires_at = row['expires_at']
        except (IndexError, KeyError, TypeError):
            return False
        if expires_at is None:
            return False
        return float(expires_at) <= (time.time() if now is None else now)

    @staticmethod
    def public(row, now=None):
        info = {key: (bool(row[key]) if key == 'enabled' else row[key])
                for key in ('id', 'name', 'role', 'enabled', 'version')}
        try:
            expires_at = row['expires_at']
        except (IndexError, KeyError, TypeError):
            expires_at = None
        info['expires_at'] = expires_at
        info['expired'] = expires_at is not None and float(expires_at) <= (time.time() if now is None else now)
        try:
            info['has_duress_pin'] = row['duress_digest'] is not None
        except (IndexError, KeyError, TypeError):
            info['has_duress_pin'] = False
        return info

    def has_users(self):
        with self.db() as db:
            return db.execute('SELECT 1 FROM users').fetchone() is not None

    def users(self):
        with self.db() as db:
            return [self.public(row) for row in db.execute('SELECT * FROM users ORDER BY created_at, id')]

    @staticmethod
    def resolve(db, claim, admin=False):
        if not isinstance(claim, dict):
            raise ValueError('Enter your PIN again.')
        row = db.execute('SELECT * FROM users WHERE id=?', (claim.get('id'),)).fetchone()
        if not row or not row['enabled'] or row['version'] != claim.get('version'):
            raise ValueError('This user or PIN has changed. Enter your PIN again.')
        if KeypadStore.expired(row):
            raise ValueError('This temporary PIN has expired.')
        if admin and row['role'] != 'admin':
            raise ValueError('An administrator PIN is required for settings.')
        return KeypadStore.public(row)

    def current(self, claim, admin=False):
        with self.db() as db:
            return self.resolve(db, claim, admin)

    def panel_settings(self):
        with self.db() as db:
            saved = db.execute("SELECT value FROM meta WHERE key='auto_lock_seconds'").fetchone()
            seconds = int(saved['value']) if saved and saved['value'] in ('60', '120', '180') else 60
            return {'autoLockSeconds': seconds}

    def save_panel_settings(self, actor, data):
        if (not isinstance(data, dict) or set(data) != {'autoLockSeconds'} or
                type(data['autoLockSeconds']) is not int or data['autoLockSeconds'] not in AUTO_LOCK_CHOICES):
            raise ValueError('Choose an auto-lock time of 1, 2, or 3 minutes.')
        seconds = data['autoLockSeconds']
        with self.db() as db:
            user = self.resolve(db, actor, admin=True)
            db.execute("INSERT OR REPLACE INTO meta VALUES ('auto_lock_seconds', ?)", (str(seconds),))
            self.event(db, user, 'panel_settings_updated', 'saved',
                       'Auto-lock after %d minute%s of inactivity.' % (seconds // 60, '' if seconds == 60 else 's'))
        return {'autoLockSeconds': seconds}

    @staticmethod
    def matches(pin, row):
        if len(row['salt']) != 16 or len(row['digest']) != 32:
            raise ValueError('A saved PIN could not be read. Access remains locked.')
        return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256', pin.encode(), row['salt'], ITERATIONS), row['digest'])

    @staticmethod
    def matches_duress(pin, row):
        """Check the silent duress PIN. Missing duress credentials never match."""
        try:
            salt, digest = row['duress_salt'], row['duress_digest']
        except (IndexError, KeyError, TypeError):
            return False
        if salt is None or digest is None:
            return False
        if len(salt) != 16 or len(digest) != 32:
            raise ValueError('A saved PIN could not be read. Access remains locked.')
        return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256', pin.encode(), salt, ITERATIONS), digest)

    def authenticate(self, pin):
        error = None
        user = None
        with self.db() as db:
            attempts = db.execute('SELECT * FROM attempts WHERE id=1').fetchone()
            now = time.time()
            if now < attempts['blocked']:
                raise ValueError('Too many incorrect PINs. Wait one minute.')
            candidate = pin if isinstance(pin, str) and len(pin) <= 8 else ''
            live = [row for row in db.execute('SELECT * FROM users WHERE enabled=1')
                    if not self.expired(row, now)]
            matches = [row for row in live if self.matches(candidate, row)]
            duress = []
            if len(matches) != 1:
                duress = [row for row in live if self.matches_duress(candidate, row)]
            if len(matches) == 1:
                db.execute('UPDATE attempts SET failures=0, blocked=0 WHERE id=1')
                user = self.public(matches[0], now)
            elif len(duress) == 1:
                # A duress PIN authenticates as its user but is flagged so the
                # caller can raise a silent alert. It never matches when the
                # normal PIN already did, and ambiguous matches stay locked.
                db.execute('UPDATE attempts SET failures=0, blocked=0 WHERE id=1')
                user = dict(self.public(duress[0], now), duress=True)
            else:
                failures = attempts['failures'] + 1
                db.execute('UPDATE attempts SET failures=?, blocked=? WHERE id=1',
                           (0 if failures >= 5 else failures, now + 60 if failures >= 5 else 0))
                error = 'Incorrect PIN.'
        if error:
            raise ValueError(error)
        return user

    @staticmethod
    def event(db, user, action, status, detail='', epoch=None):
        when = time.time() if epoch is None else epoch
        return db.execute('INSERT INTO activity (occurred_at,epoch,user_id,user_name,action,status,detail) VALUES (?,?,?,?,?,?,?)',
                          (stamp(when), when, user.get('id') if user else None,
                           user['name'] if user else 'Quick arm · no PIN', action, status, detail)).lastrowid

    def save_user(self, actor, data):
        allowed = {'id', 'name', 'role', 'enabled', 'pin', 'confirmation',
                   'expires_at', 'duress_pin', 'duress_confirmation'}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError('Invalid user settings.')
        name, role = data.get('name'), data.get('role')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 for c in name):
            raise ValueError('Enter a user name of 1–60 characters.')
        if role not in ('admin', 'user') or type(data.get('enabled', True)) is not bool:
            raise ValueError('Choose a valid role and access status.')
        user_id = data.get('id')
        pin, confirmation = data.get('pin', ''), data.get('confirmation', '')
        with self.db() as db:
            actor = self.resolve(db, actor, True)
            rows = list(db.execute('SELECT * FROM users'))
            old = next((row for row in rows if row['id'] == user_id), None)
            if user_id and not old:
                raise ValueError('This user no longer exists. Refresh the list.')
            if not old and len(rows) >= 20:
                raise ValueError('This keypad supports up to 20 users.')
            if any(row['id'] != user_id and row['name'].casefold() == name.strip().casefold() for row in rows):
                raise ValueError('Choose a different user name.')
            enabled = data.get('enabled', True)
            if old and old['role'] == 'admin' and old['enabled'] and (role != 'admin' or not enabled) and not any(
                    row['id'] != user_id and row['role'] == 'admin' and row['enabled'] for row in rows):
                raise ValueError('Keep at least one active administrator.')
            if not old or pin or confirmation:
                if not isinstance(pin, str) or not pin.isascii() or not pin.isdigit() or not 4 <= len(pin) <= 8:
                    raise ValueError('Choose a PIN containing 4 to 8 digits.')
                if pin != confirmation:
                    raise ValueError('The PINs do not match.')
                if any(row['id'] != user_id and self.matches(pin, row) for row in rows):
                    raise ValueError('That PIN is already assigned. Choose a different PIN.')
                salt = secrets.token_bytes(16)
                digest = hashlib.pbkdf2_hmac('sha256', pin.encode(), salt, ITERATIONS)
            else:
                salt, digest = old['salt'], old['digest']
            if 'expires_at' in data:
                expires_at = parse_expiry(data['expires_at'])
            elif old:
                expires_at = old['expires_at']
            else:
                expires_at = None
            if 'duress_pin' in data or 'duress_confirmation' in data:
                dpin, dconfirmation = data.get('duress_pin', ''), data.get('duress_confirmation', '')
                if not dpin and not dconfirmation:
                    duress_salt, duress_digest = None, None
                else:
                    if not isinstance(dpin, str) or not dpin.isascii() or not dpin.isdigit() or not 4 <= len(dpin) <= 8:
                        raise ValueError('Choose a duress PIN containing 4 to 8 digits.')
                    if dpin != dconfirmation:
                        raise ValueError('The duress PINs do not match.')
                    if hmac.compare_digest(hashlib.pbkdf2_hmac('sha256', dpin.encode(), salt, ITERATIONS), digest):
                        raise ValueError('The duress PIN must differ from the normal PIN.')
                    if any(row['id'] != user_id and (self.matches(dpin, row) or self.matches_duress(dpin, row))
                           for row in rows):
                        raise ValueError('That duress PIN is already assigned. Choose a different PIN.')
                    duress_salt = secrets.token_bytes(16)
                    duress_digest = hashlib.pbkdf2_hmac('sha256', dpin.encode(), duress_salt, ITERATIONS)
            elif old:
                duress_salt, duress_digest = old['duress_salt'], old['duress_digest']
            else:
                duress_salt, duress_digest = None, None
            user_id = user_id or str(uuid4())
            version = old['version'] + 1 if old else 1
            db.execute('INSERT OR REPLACE INTO users (id, name, role, enabled, salt, digest, version, created_at,'
                       ' expires_at, duress_salt, duress_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (user_id, name.strip(), role, int(enabled), salt, digest, version,
                        old['created_at'] if old else stamp(), expires_at, duress_salt, duress_digest))
            detail = name.strip() + ' · ' + ('Administrator' if role == 'admin' else 'User') + (' · Disabled' if not enabled else '')
            if expires_at:
                detail += ' · Temporary until ' + stamp(expires_at)
            if duress_salt:
                detail += ' · Duress PIN set'
            self.event(db, actor, 'user_updated' if old else 'user_created', 'saved', detail)
            return {'user': self.public(db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()),
                    'reauthenticate': user_id == actor['id']}

    def start_command(self, actor, action):
        with self.db() as db:
            db.execute("UPDATE activity SET status='unverified', detail='Another request was made before confirmation.' WHERE status IN ('requested','accepted') AND action IN ('arm','arm_stay','arm_away','disarm')")
            return self.event(db, actor, action, 'requested')

    def start_equipment_command(self, actor, action, detail):
        with self.db() as db:
            return self.event(db, actor, action, 'requested', detail)

    def command_result(self, event_id, status, detail=''):
        with self.db() as db:
            db.execute('UPDATE activity SET status=?, detail=? WHERE id=?', (status, detail, event_id))

    def confirm_commands(self, state, changed_at):
        changed_epoch = None
        try:
            changed = datetime.fromisoformat(changed_at.replace('Z', '+00:00'))
            if changed.tzinfo is not None:
                changed_epoch = changed.timestamp()
        except (AttributeError, TypeError, ValueError):
            pass
        now = time.time()
        with self.db() as db:
            for row in db.execute("SELECT * FROM activity WHERE status='accepted' AND action IN ('arm','arm_stay','arm_away','disarm')").fetchall():
                expected = 'disarmed' if row['action'] == 'disarm' else 'armed'
                if changed_epoch is not None and state == expected and row['epoch'] <= changed_epoch <= now + 2:
                    db.execute("UPDATE activity SET status='confirmed', confirmed_at=?, detail='Protect confirmed the requested state.' WHERE id=?", (changed_at, row['id']))
                elif now - row['epoch'] > 120:
                    db.execute("UPDATE activity SET status='unverified', detail='No matching state change was confirmed in time.' WHERE id=?", (row['id'],))

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE activity SET status='unverified', detail='The controller restarted before confirmation.' WHERE status IN ('requested','accepted')")

    def record(self, actor, action, detail):
        with self.db() as db:
            self.event(db, actor, action, 'saved', detail)

    def history(self, before=None):
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError('Invalid activity page.')
        with self.db() as db:
            db.execute("UPDATE activity SET status='unverified', detail='No matching state change was confirmed in time.' WHERE status IN ('requested','accepted') AND epoch < ?", (time.time()-120,))
            rows = db.execute('SELECT id,occurred_at,user_id,user_name,action,status,detail,confirmed_at FROM activity WHERE id < ? ORDER BY id DESC LIMIT 26',
                              (before if before is not None else 9223372036854775807,)).fetchall()
            return {'events': [dict(row) for row in rows[:25]], 'nextBefore': rows[24]['id'] if len(rows) > 25 else None}
