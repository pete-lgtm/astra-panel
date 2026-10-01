import os
from pathlib import Path
import tempfile
import unittest
import credential_store


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / '.api-connection.json'
        self.config = {'host': '192.168.0.1', 'key': 'synthetic-test-key'}

    def test_save_load_replace_and_forget(self):
        credential_store.save(self.path, self.config)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(credential_store.load(self.path), self.config)
        changed = dict(self.config, key='replacement-test-key')
        credential_store.save(self.path, changed)
        self.assertEqual(credential_store.load(self.path), changed)
        credential_store.forget(self.path)
        self.assertEqual(credential_store.load(self.path), {})
        credential_store.forget(self.path)

    def test_insecure_permissions_and_symlinks_are_rejected(self):
        credential_store.save(self.path, self.config)
        os.chmod(self.path, 0o644)
        self.assertEqual(credential_store.load(self.path), {})
        os.chmod(self.path, 0o600)
        link = self.path.with_name('link')
        link.symlink_to(self.path)
        self.assertEqual(credential_store.load(link), {})

    def test_invalid_file_does_not_prevent_startup(self):
        self.path.write_text('broken JSON')
        os.chmod(self.path, 0o600)
        self.assertEqual(credential_store.load(self.path), {})
        self.path.write_text('[]')
        self.assertEqual(credential_store.load(self.path), {})

    def test_rejects_public_hosts_and_secrets_in_headers(self):
        for config in (dict(self.config, host='8.8.8.8'), dict(self.config, key='bad\nkey'),
                       dict(self.config, password='must-not-save')):
            with self.assertRaises(ValueError):
                credential_store.save(self.path, config)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
