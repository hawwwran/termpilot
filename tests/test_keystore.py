#!/usr/bin/env python3
"""
Tests for keystore.py.

These run against the user's actual environment (real keyring if
available, real ~/.config/termpilot/token if not). We MUST NOT clobber a token the
user already has, so we sandbox by:
- Pointing TOKEN_FILE at a temp path inside this test.
- Using a different KEYRING_USERNAME ("__test__") so we don't touch
  the user's real "default" entry.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import keystore, crypto  # noqa: E402


class TokenStorageTests(unittest.TestCase):
    def setUp(self):
        self._orig_user = keystore.KEYRING_USERNAME
        self._orig_file = keystore.TOKEN_FILE
        keystore.KEYRING_USERNAME = "__test__"
        self._tmp = tempfile.NamedTemporaryFile(delete=False, prefix="termpilot-test-token-")
        self._tmp.close()
        os.unlink(self._tmp.name)  # save_token will recreate
        keystore.TOKEN_FILE = Path(self._tmp.name)
        # Ensure clean slate
        keystore.delete_token()

    def tearDown(self):
        keystore.delete_token()
        keystore.KEYRING_USERNAME = self._orig_user
        keystore.TOKEN_FILE = self._orig_file
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_no_token_initially(self):
        self.assertFalse(keystore.has_token())
        self.assertIsNone(keystore.load_token())
        self.assertEqual(keystore.backend_in_use(), "none")

    def test_save_and_load_round_trip(self):
        token = crypto.derive_token("test-pwd")
        backend = keystore.save_token(token)
        self.assertIn(backend, ("keyring", "file"))
        loaded = keystore.load_token()
        self.assertEqual(loaded, token)
        self.assertTrue(keystore.has_token())

    def test_save_overwrites(self):
        a = crypto.derive_token("a")
        b = crypto.derive_token("b")
        keystore.save_token(a)
        keystore.save_token(b)
        self.assertEqual(keystore.load_token(), b)

    def test_delete_clears(self):
        keystore.save_token(crypto.derive_token("p"))
        keystore.delete_token()
        self.assertIsNone(keystore.load_token())
        self.assertFalse(keystore.has_token())

    def test_save_rejects_wrong_size(self):
        with self.assertRaises(ValueError):
            keystore.save_token(b"too short")

    def test_file_fallback_perms_are_strict(self):
        # Force the file path explicitly, even if keyring is available.
        token = crypto.derive_token("strict-perms")
        keystore._file_set(token)
        st = os.stat(keystore.TOKEN_FILE)
        mode = st.st_mode & 0o777
        self.assertEqual(mode, 0o600, f"expected 0o600, got {oct(mode)}")

    def test_file_fallback_refuses_loose_perms(self):
        token = crypto.derive_token("loose-perms")
        keystore._file_set(token)
        os.chmod(keystore.TOKEN_FILE, 0o644)
        # _file_get should refuse and return None, with a warning to stderr.
        loaded = keystore._file_get()
        self.assertIsNone(loaded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
