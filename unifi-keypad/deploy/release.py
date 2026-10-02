#!/usr/bin/env python3
"""Build, verify and stage application releases. No cloud account or network required.

Automatic activation remains restricted to empty bench devices. Commissioned
prototype updates use a separate attended, disarmed verification procedure.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.request
import zipfile

APP_FILES = ('VERSION', 'server.py', 'device_config.py', 'credential_store.py', 'session_store.py', 'phone_pairing.py', 'qrcodegen.py', 'phone_setup.html', 'phone_setup.js', 'phone_setup.css', 'phone_pair_ui.js', 'local_auth.py', 'network_inventory.py', 'network_actions.py', 'network_wifi.py', 'protection_state.py', 'door_policy.py', 'arm_coverage.py',
             'alarm_outputs.py', 'siren_runtime.py', 'entry_recovery.py', 'live_entry.py', 'entry_delay.py', 'index.html', 'app.js', 'management.js', 'style.css', 'astra.css',
             'lab.html', 'lab.js', 'onboarding.py', 'protect_setup_api.py', 'pin_store.py', 'settings_access.py', 'keypad_store.py', 'sensor_settings.py',
             'setup_lab.html', 'setup_lab.js', 'setup_lab.css',
             'browser_setup.py', 'browser_landing.html', 'browser_pair.html', 'browser_pair.js', 'browser_pair.css', 'browser_pair_ui.js', 'web_setup.css',
             'power_monitor.py', 'fleet_heartbeat.py', 'network_speedtest.py', 'sonos_audio.py', 'sonos_artwork.py', 'audio.js',
             'hue_lighting.py', 'lighting.js', 'lighting.css', 'level-control.js')
MAX_PACKAGE = 8_000_000
MAX_FILE = 1_000_000


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,6}\.\d{1,6}\.\d{1,6}', value):
        raise ValueError('Use a numeric major.minor.patch version.')
    return tuple(map(int, value.split('.')))


def run(*args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode:
        raise ValueError('Release signature command failed. Check the signing or verification key.')
    return result.stdout


def build(source, output, signing_key=None):
    version = (source / 'VERSION').read_text().strip()
    version_tuple(version)
    output.mkdir(parents=True, exist_ok=False)
    payload = output / 'app.zip'
    files = {}
    with zipfile.ZipFile(payload, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(APP_FILES):
            path = source / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('Only regular application files may enter a release.')
            data = path.read_bytes()
            if len(data) > MAX_FILE:
                raise ValueError('Application file exceeds release size limit.')
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
            files[name] = hashlib.sha256(data).hexdigest()
    manifest = {'format': 1, 'product': 'loxicom-keypad', 'version': version,
                'target': 'raspberry-pi-os', 'stateSchema': 1,
                'sha256': hashlib.sha256(payload.read_bytes()).hexdigest(), 'files': files}
    (output / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    if signing_key:
        run('openssl', 'dgst', '-sha256', '-sign', str(signing_key),
            '-out', str(output / 'manifest.sig'), str(output / 'manifest.json'))
    return manifest


def verified_files(package, public_key):
    # Copy bounded bytes first: verify and unpack exactly the same data, avoiding
    # download races. Never extract a ZIP directly into the filesystem.
    limits = {'manifest.json': 16_384, 'manifest.sig': 8192, 'app.zip': MAX_PACKAGE}
    blobs = {}
    for name, limit in limits.items():
        with (package / name).open('rb') as stream:
            blobs[name] = stream.read(limit + 1)
        if len(blobs[name]) > limit:
            raise ValueError('Release exceeds size limit.')
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        for name in ('manifest.json', 'manifest.sig'):
            (temp / name).write_bytes(blobs[name])
        run('openssl', 'dgst', '-sha256', '-verify', str(public_key),
            '-signature', str(temp / 'manifest.sig'), str(temp / 'manifest.json'))
    manifest = json.loads(blobs['manifest.json'])
    if (manifest.get('format') != 1 or manifest.get('product') != 'loxicom-keypad' or
            manifest.get('target') not in ('raspberry-pi-os', 'raspberry-pi-os-arm64') or manifest.get('stateSchema') != 1):
        raise ValueError('Incompatible release.')
    version_tuple(manifest.get('version'))
    if hashlib.sha256(blobs['app.zip']).hexdigest() != manifest['sha256']:
        raise ValueError('Release payload digest mismatch.')
    if set(manifest.get('files', {})) != set(APP_FILES):
        raise ValueError('Unexpected application file list.')
    files = {}
    with zipfile.ZipFile(io.BytesIO(blobs['app.zip'])) as archive:
        members = archive.infolist()
        if len(members) != len(APP_FILES) or {x.filename for x in members} != set(APP_FILES):
            raise ValueError('Archive contains unexpected or duplicate paths.')
        for member in members:
            mode = member.external_attr >> 16
            if not stat.S_ISREG(mode) or member.file_size > MAX_FILE or member.flag_bits & 1:
                raise ValueError('Archive contains a link, oversized file, or encrypted member.')
            data = archive.read(member)
            if hashlib.sha256(data).hexdigest() != manifest['files'][member.filename]:
                raise ValueError('Application file digest mismatch.')
            files[member.filename] = data
    if files['VERSION'].decode().strip() != manifest['version']:
        raise ValueError('Version file does not match signed manifest.')
    return manifest, files


def stage(package, public_key, root):
    manifest, files = verified_files(package, public_key)
    releases = root / 'releases'
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / manifest['version']
    if target.exists():
        raise ValueError('This version is already staged. Use a new version number.')
    temporary = Path(tempfile.mkdtemp(prefix='.staging-', dir=releases))
    try:
        for name, data in files.items():
            (temporary / name).write_bytes(data)
            (temporary / name).chmod(0o644)
        (temporary / 'release.json').write_text(json.dumps(manifest, sort_keys=True))
        (temporary / 'release.json').chmod(0o644)
        temporary.chmod(0o755)
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return target


def switch(root, target):
    # Call only with our verified immutable release directory.
    temporary = root / '.next'
    if temporary.is_symlink():
        temporary.unlink()
    os.symlink(str(target), temporary)
    os.replace(temporary, root / 'current')


def health():
    with urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3) as response:
        return json.load(response)


def restart():
    subprocess.run(['systemctl', 'restart', 'loxicom-keypad.service'], check=True, timeout=20)


def empty_bench_state(state_dir):
    """Allow only an empty directory or the DB a never-configured service creates.

    Never remove private state to make an update eligible. Even disabled users,
    migration markers, saved credentials or past alarm activity block activation.
    """
    entries = list(state_dir.iterdir())
    if not entries:
        return True
    if len(entries) != 1 or entries[0].name != '.keypad-data.sqlite3':
        return False
    path = entries[0]
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            db.execute('BEGIN')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != {'users', 'meta', 'attempts', 'activity', 'sqlite_sequence'}:
                return False
            if db.execute('SELECT 1 FROM users LIMIT 1').fetchone() or db.execute('SELECT 1 FROM meta LIMIT 1').fetchone():
                return False
            if db.execute('SELECT 1 FROM attempts WHERE failures IS NOT 0 OR blocked IS NOT 0 LIMIT 1').fetchone():
                return False
            return db.execute("SELECT 1 FROM activity WHERE user_id IS NOT 'system' OR action IS NOT 'controller_shutdown' OR status IS NOT 'saved' LIMIT 1").fetchone() is None
    except sqlite3.Error:
        return False


def activate_bench(root, target, state_dir, read_health=health, restart_service=restart, wait=time.sleep):
    # No override for a commissioned installation. Disarmed is insufficient:
    # automatic live-site rollout/rollback has not been commissioned.
    if not empty_bench_state(state_dir):
        raise ValueError('Bench activation requires an empty state directory; live sites cannot be updated yet.')
    before = read_health()
    if before.get('service') != 'loxicom-keypad' or before.get('benchUpdateAllowed') is not True:
        raise ValueError('This device has a connection or site setup. Activation blocked until restart recovery is implemented.')
    previous = (root / 'current').resolve(strict=True)
    releases = (root / 'releases').resolve()
    if previous.parent != releases or target.resolve().parent != releases or target.is_symlink():
        raise ValueError('Release must belong to this installation.')
    target = target.resolve()
    wanted = (target / 'VERSION').read_text().strip()
    current = (previous / 'VERSION').read_text().strip()
    if version_tuple(wanted) <= version_tuple(current):
        raise ValueError('Update must advance the version; automatic downgrades are blocked.')
    manifest = json.loads((target / 'release.json').read_text())
    for name in APP_FILES:
        if (target / name).is_symlink() or hashlib.sha256((target / name).read_bytes()).hexdigest() != manifest['files'][name]:
            raise ValueError('Staged release was modified.')
    try:
        switch(root, target)
        restart_service()
        for _ in range(15):
            try:
                after = read_health()
                if (after.get('service') == 'loxicom-keypad' and after.get('processHealthy') is True and
                        after.get('version') == wanted and after.get('instance') != before.get('instance')):
                    return
            except (OSError, ValueError):
                pass
            wait(1)
        raise ValueError('Updated service failed its startup health check.')
    except Exception:
        switch(root, previous)
        restart_service()
        raise ValueError('Update failed; previous application restored. Check service health.') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build_cmd = commands.add_parser('build')
    build_cmd.add_argument('--source', type=Path, default=Path(__file__).resolve().parent.parent)
    build_cmd.add_argument('--output', type=Path, required=True)
    build_cmd.add_argument('--signing-key', type=Path)
    for name in ('verify', 'stage'):
        command = commands.add_parser(name)
        command.add_argument('--package', type=Path, required=True)
        command.add_argument('--public-key', type=Path, required=True)
        if name == 'stage':
            command.add_argument('--root', type=Path, default=Path('/opt/loxicom'))
    command = commands.add_parser('activate-bench')
    command.add_argument('--version', required=True)
    args = parser.parse_args()
    if args.command == 'build':
        result = build(args.source, args.output, args.signing_key)
        print(f"Built {result['version']} ({'signed' if args.signing_key else 'UNSIGNED: sign before installation'}).")
    elif args.command == 'verify':
        manifest, _ = verified_files(args.package, args.public_key)
        print('Signature and payload verified:', manifest['version'])
    elif args.command == 'stage':
        print('Staged only; running service unchanged:', stage(args.package, args.public_key, args.root.resolve()))
    else:
        if os.geteuid() != 0:
            raise ValueError('Bench activation requires an installer running as root.')
        import fcntl
        version_tuple(args.version)
        with open('/run/lock/loxicom-update.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            root = Path('/opt/loxicom')
            activate_bench(root, root / 'releases' / args.version, Path('/var/lib/loxicom/keypad'))
        print('Bench application updated. This does not verify alarm operation.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile, KeyError) as error:
        raise SystemExit(str(error))
