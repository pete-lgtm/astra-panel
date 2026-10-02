import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

from deploy import release
from device_config import load_site, settings
from keypad_store import KeypadStore
from test_fixtures import legacy_site
import server

ROOT = Path(__file__).resolve().parent


class DeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.private = Path(cls.keys.name) / 'test-private.pem'
        cls.public = Path(cls.keys.name) / 'test-public.pem'
        # Test-only key: never published or installed on a device.
        subprocess.run(['openssl', 'genrsa', '-out', str(cls.private), '3072'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['openssl', 'rsa', '-in', str(cls.private), '-pubout', '-out', str(cls.public)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.package = self.root / 'package'
        release.build(ROOT, self.package, self.private)

    def resign(self):
        release.run('openssl', 'dgst', '-sha256', '-sign', str(self.private),
                    '-out', str(self.package / 'manifest.sig'), str(self.package / 'manifest.json'))

    def rewrite_zip(self, mutation):
        path = self.package / 'app.zip'
        with zipfile.ZipFile(path) as archive:
            members = [(item, archive.read(item)) for item in archive.infolist()]
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            for item, content in members:
                archive.writestr(item, content)
            mutation(archive)
        path.write_bytes(data.getvalue())
        manifest = json.loads((self.package / 'manifest.json').read_text())
        manifest['sha256'] = hashlib.sha256(data.getvalue()).hexdigest()
        (self.package / 'manifest.json').write_text(json.dumps(manifest))
        self.resign()

    def test_build_excludes_site_and_credentials(self):
        _, files = release.verified_files(self.package, self.public)
        self.assertEqual(set(files), set(release.APP_FILES))
        for name in ('prototype-site.json', '.keypad-pin.json', '.protection-state.json', '.keypad-data.sqlite3', '.keypad-data.sqlite3-journal', '.env', 'site.json', 'test-private.pem'):
            self.assertNotIn(name, files)
        self.assertNotIn(b'00000000-0000-4000-8000-000000000099', b''.join(files.values()))
        self.assertNotIn(b'EXAMPLE HOME', files['index.html'])

    def test_staged_server_imports_without_development_files(self):
        staged = release.stage(self.package, self.public, self.root / 'install')
        state = self.root / 'empty-device-state'
        state.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('KEYPAD_', 'UNIFI_', 'PYTHON'))}
        env.update(KEYPAD_APPLIANCE='1', KEYPAD_STATE_DIR=str(state),
                   PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        result = subprocess.run(
            [sys.executable, '-c', 'import server; assert server.SITE["doors"] == []; assert not server.CONFIG'],
            cwd=staged, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unsigned_is_rejected(self):
        (self.package / 'manifest.sig').unlink()
        with self.assertRaises(FileNotFoundError):
            release.verified_files(self.package, self.public)

    def test_manifest_tampering_is_rejected(self):
        path = self.package / 'manifest.json'
        path.write_text(path.read_text().replace((ROOT / 'VERSION').read_text().strip(), '9.9.9'))
        with self.assertRaisesRegex(ValueError, 'signature'):
            release.verified_files(self.package, self.public)

    def test_payload_tampering_is_rejected(self):
        path = self.package / 'app.zip'
        path.write_bytes(path.read_bytes() + b'tampering')
        with self.assertRaisesRegex(ValueError, 'digest'):
            release.verified_files(self.package, self.public)

    def test_traversal_in_signed_archive_is_rejected(self):
        self.rewrite_zip(lambda archive: archive.writestr('../site.json', '{}'))
        with self.assertRaisesRegex(ValueError, 'paths'):
            release.verified_files(self.package, self.public)

    def test_symlink_in_signed_archive_is_rejected(self):
        path = self.package / 'app.zip'
        with zipfile.ZipFile(path) as archive:
            members = [(item, archive.read(item)) for item in archive.infolist()]
        with zipfile.ZipFile(path, 'w') as archive:
            for item, data in members:
                if item.filename == 'app.js':
                    item.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(item, data)
        manifest = json.loads((self.package / 'manifest.json').read_text())
        manifest['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.package / 'manifest.json').write_text(json.dumps(manifest))
        self.resign()
        with self.assertRaisesRegex(ValueError, 'link'):
            release.verified_files(self.package, self.public)

    def test_staging_preserves_current_and_private_state(self):
        root = self.root / 'install'
        root.mkdir()
        state = self.root / 'private'
        state.mkdir()
        (state / '.keypad-pin.json').write_text('test-only-sentinel')
        (root / 'current').symlink_to('/some/previous/release')
        staged = release.stage(self.package, self.public, root)
        self.assertEqual(staged.name, (ROOT / 'VERSION').read_text().strip())
        self.assertEqual((root / 'current').readlink(), Path('/some/previous/release'))
        self.assertEqual((state / '.keypad-pin.json').read_text(), 'test-only-sentinel')

    def bench(self):
        root = self.root / 'install'
        target = release.stage(self.package, self.public, root)
        previous = root / 'releases' / '0.1.0'
        previous.mkdir()
        (previous / 'VERSION').write_text('0.1.0')
        (root / 'current').symlink_to(previous)
        state = self.root / 'private'
        state.mkdir()
        return root, target, previous, state

    def test_configured_site_cannot_activate(self):
        root, target, previous, state = self.bench()
        (state / 'site.json').write_text('{}')
        restart = Mock()
        with self.assertRaisesRegex(ValueError, 'empty state'):
            release.activate_bench(root, target, state, Mock(), restart)
        restart.assert_not_called()
        self.assertEqual((root / 'current').resolve(), previous)

    def test_connected_backend_cannot_activate(self):
        root, target, previous, state = self.bench()
        restart = Mock()
        with self.assertRaisesRegex(ValueError, 'blocked'):
            release.activate_bench(root, target, state, lambda: {'service': 'loxicom-keypad', 'benchUpdateAllowed': False}, restart)
        restart.assert_not_called()

    def test_failed_start_restores_previous_release(self):
        root, target, previous, state = self.bench()
        restart = Mock()
        before = {'service': 'loxicom-keypad', 'benchUpdateAllowed': True, 'instance': 'old'}
        with self.assertRaisesRegex(ValueError, 'previous application restored'):
            release.activate_bench(root, target, state, lambda: before, restart, lambda _: None)
        self.assertEqual((root / 'current').resolve(), previous)
        self.assertEqual(restart.call_count, 2)

    def test_success_requires_new_instance_and_version(self):
        root, target, _, state = self.bench()
        before = {'service': 'loxicom-keypad', 'benchUpdateAllowed': True, 'instance': 'old'}
        after = {'service': 'loxicom-keypad', 'processHealthy': True, 'version': (ROOT / 'VERSION').read_text().strip(), 'instance': 'new'}
        restart = Mock()
        release.activate_bench(root, target, state, Mock(side_effect=[before, after]), restart, lambda _: None)
        self.assertEqual((root / 'current').resolve(), target)
        restart.assert_called_once()

    def test_downgrade_is_rejected(self):
        root, target, previous, state = self.bench()
        (previous / 'VERSION').write_text('0.3.0')
        restart = Mock()
        with self.assertRaisesRegex(ValueError, 'advance'):
            release.activate_bench(root, target, state, lambda: {'service':'loxicom-keypad','benchUpdateAllowed':True}, restart)
        restart.assert_not_called()


class DeviceConfigTests(unittest.TestCase):
    def test_countdown_sound_asset_and_kiosk_permission(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port)
            connection.request('GET', '/countdown-sound.js', headers={'Host': f'127.0.0.1:{server.PORT}'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), (ROOT / 'countdown-sound.js').read_bytes())
            connection.request('GET', '/countdown-sound.js', headers={'Host': 'untrusted.example'})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()
        policy = json.loads((ROOT / 'deploy/pi/chromium-policy.json').read_text())
        self.assertEqual(policy['AutoplayAllowlist'], ['http://127.0.0.1:8764', 'http://127.0.0.1:8765'])
        self.assertNotIn('AutoplayAllowed', policy)
        self.assertIn('allow="autoplay http://127.0.0.1:8765"', (ROOT / 'deploy/kiosk/index.html').read_text())

    def test_root_opens_setup_until_site_and_pin_are_saved(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            for doors, pin_set in (([], False), ([], True), (['Front'], False), (['Front'], True)):
                with self.subTest(doors=doors, pin=pin_set), \
                        patch.object(server, 'SITE', {'name': 'Test home', 'doors': doors, 'webhooks': {}}), \
                        patch.object(server.SETTINGS_ACCESS, 'saved_pin', return_value={'configured': True} if pin_set else None), \
                        patch.dict(server.CONFIG, {}, clear=True):
                    connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port)
                    connection.request('GET', '/', headers={'Host': f'127.0.0.1:{server.PORT}'})
                    response = connection.getresponse()
                    body = response.read().decode()
                    required = not doors or not pin_set
                    self.assertEqual(response.status, 200)
                    self.assertEqual('id="api-page"' in body, required)
                    self.assertEqual('id="sleep"' in body, not required)
                    connection.request('GET', '/session', headers={'Host': f'127.0.0.1:{server.PORT}'})
                    session = json.loads(connection.getresponse().read())
                    self.assertEqual(session['setupRequired'], required)
                    self.assertFalse(session['apiConfigured'])
                    connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()

    def test_factory_configuration_never_loads_home_site(self):
        with tempfile.TemporaryDirectory() as directory:
            state, site = settings({'KEYPAD_APPLIANCE': '1', 'KEYPAD_STATE_DIR': directory})
            self.assertEqual(state, Path(directory))
            self.assertEqual(site['doors'], [])
            self.assertEqual(site['webhooks'], {})

    def test_bad_site_does_not_fall_back_to_home(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'site.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                load_site(path)

    def test_duplicate_sensors_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'site.json'
            site = legacy_site()
            site['doors'] = ['same', 'same']
            path.write_text(json.dumps(site))
            with self.assertRaisesRegex(ValueError, 'unique'):
                load_site(path)

    def test_health_is_liveness_only_and_contains_no_auth(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(server, 'KIOSK_ORIGIN', 'http://127.0.0.1:8764'), patch.object(server, 'SITE', legacy_site()):
                connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port)
                connection.request('GET', '/health', headers={'Host': f'127.0.0.1:{server.PORT}'})
                response = connection.getresponse()
                data = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader('Access-Control-Allow-Origin'), 'http://127.0.0.1:8764')
                self.assertFalse(data['automaticUpdatesReady'])
                self.assertFalse(data['benchUpdateAllowed'])
                self.assertTrue(data['processHealthy'])
                self.assertEqual(set(data), {'service','version','instance','processHealthy','automaticUpdatesReady','benchUpdateAllowed'})
                connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()


class BenchStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = KeypadStore(self.root / '.keypad-pin.json')
        self.store.recover()

    def test_generated_database_and_shutdown_log_are_preserved(self):
        self.store.record({'id': 'system', 'name': 'System'}, 'controller_shutdown', 'Panel is shutting down.')
        before = self.store.path.read_bytes()
        self.assertTrue(release.empty_bench_state(self.root))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_a_user_or_migration_marker_blocks_activation(self):
        with self.store.db() as db:
            db.execute("INSERT INTO meta VALUES ('pin_migrated', '1')")
        self.assertFalse(release.empty_bench_state(self.root))

    def test_existing_activity_blocks_even_if_users_were_removed(self):
        self.store.record({'id': 'owner', 'name': 'Owner'}, 'disarm', 'Past alarm activity')
        self.assertFalse(release.empty_bench_state(self.root))

    def test_other_state_corruption_and_symlinks_block_activation(self):
        extra = self.root / '.api-connection.json'
        extra.write_text('{}')
        self.assertFalse(release.empty_bench_state(self.root))
        extra.unlink()
        self.store.path.write_text('invalid database')
        self.assertFalse(release.empty_bench_state(self.root))
        self.store.path.unlink()
        self.store.path.symlink_to(self.root / 'missing')
        self.assertFalse(release.empty_bench_state(self.root))


if __name__ == '__main__':
    unittest.main()
