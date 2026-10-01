import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pin_store import create_pin, read_pin, ITERATIONS
import server


class PinStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.keypad-pin.json'

    def test_only_confirmed_ascii_digits_are_saved(self):
        for pin, confirmation in (('123', '123'), ('123456789', '123456789'), ('12a4', '12a4'),
                                  ('１２３４', '１２３４'), ('1234', '9999'), (' 1234', ' 1234'), (1234, 1234)):
            with self.assertRaises(ValueError):
                create_pin(self.path, pin, confirmation)
            self.assertFalse(self.path.exists())

    def test_compatible_salted_hash_loads_after_restart(self):
        create_pin(self.path, '927461', '927461')
        raw = self.path.read_text()
        self.assertNotIn('927461', raw)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        saved = read_pin(self.path)
        self.assertEqual(saved['digest'], hashlib.pbkdf2_hmac('sha256', b'927461', saved['salt'], ITERATIONS))
        with patch.object(server, 'PIN_FILE', self.path), patch.object(server, 'PIN', {}):
            # The main keypad recognizes an installer-created PIN immediately.
            server.check_pin('927461')
            with self.assertRaisesRegex(ValueError, 'Incorrect'):
                server.check_pin('0000')

    def test_saved_pin_is_never_replaced(self):
        create_pin(self.path, '927461', '927461')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'already saved'):
            create_pin(self.path, '1357', '1357')
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_creation_cannot_overwrite_a_winner(self):
        import os
        real_link = os.link
        def raced(source, target):
            Path(target).write_text('another process already saved a PIN')
            real_link(source, target)
        with patch('pin_store.os.link', side_effect=raced):
            with self.assertRaisesRegex(ValueError, 'already saved'):
                create_pin(self.path, '927461', '927461')
        self.assertEqual(self.path.read_text(), 'another process already saved a PIN')
        self.assertEqual(list(self.path.parent.glob('.keypad-pin-*')), [])

    def test_bad_existing_pin_is_not_interpreted_as_a_new_device(self):
        for value in ('{broken', '{}', json.dumps({'version': 1, 'salt': 3, 'digest': None})):
            self.path.write_text(value)
            with self.assertRaisesRegex(ValueError, 'not been reset'):
                read_pin(self.path)
            with self.assertRaisesRegex(ValueError, 'already saved'):
                create_pin(self.path, '927461', '927461')
            self.assertEqual(self.path.read_text(), value)

    def test_failed_write_does_not_publish_partial_pin(self):
        with patch('pin_store.json.dump', side_effect=OSError('Storage unavailable')):
            with self.assertRaises(OSError):
                create_pin(self.path, '927461', '927461')
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob('.keypad-pin-*')), [])


if __name__ == '__main__':
    unittest.main()
