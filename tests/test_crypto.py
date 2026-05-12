#!/usr/bin/env python3
"""
Tests for crypto.py.

Covers:
- KDF determinism + length.
- AES-GCM round-trip with AAD.
- Tag-mismatch rejection on tamper.
- AAD-mismatch rejection.
- Hex round-trip.
- Generates `tests/crypto_vectors.json` for the JS-side test page to consume.

Usage:
    python3 tests/test_crypto.py
    python3 tests/test_crypto.py --gen-vectors    # writes vectors file
"""
from __future__ import annotations

import base64
import json
import os
import sys
import unittest
from pathlib import Path

# Make `lib/` importable when run as a script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.crypto import (  # noqa: E402
    Crypto,
    SALT,
    PBKDF2_ITERATIONS,
    TOKEN_BYTES,
    TRIGGER_INFO,
    aad_marker,
    aad_meta,
    aad_record,
    derive_token,
    derive_trigger_secret,
    hex_to_token,
    token_to_hex,
    trigger_id_for,
)


class KdfTests(unittest.TestCase):
    def test_kdf_deterministic(self):
        a = derive_token("hunter2")
        b = derive_token("hunter2")
        self.assertEqual(a, b)
        self.assertEqual(len(a), TOKEN_BYTES)

    def test_kdf_different_passwords_different_tokens(self):
        self.assertNotEqual(derive_token("a"), derive_token("b"))

    def test_kdf_rejects_empty_password(self):
        with self.assertRaises(ValueError):
            derive_token("")

    def test_kdf_unicode_password(self):
        # Should not crash; should be deterministic.
        a = derive_token("p@ssw0rd 🔑")
        b = derive_token("p@ssw0rd 🔑")
        self.assertEqual(a, b)

    def test_constants_locked(self):
        # Guard against accidental constant changes — they'd break
        # interoperability with the JS port and any existing tokens.
        self.assertEqual(SALT, b"termpilot:v1")
        self.assertEqual(PBKDF2_ITERATIONS, 600_000)
        self.assertEqual(TOKEN_BYTES, 32)


class HexRoundTripTests(unittest.TestCase):
    def test_round_trip(self):
        t = derive_token("p")
        h = token_to_hex(t)
        self.assertEqual(len(h), 64)
        self.assertEqual(hex_to_token(h), t)

    def test_hex_case_insensitive(self):
        t = derive_token("p")
        upper = token_to_hex(t).upper()
        self.assertEqual(hex_to_token(upper), t)

    def test_hex_rejects_short(self):
        with self.assertRaises(ValueError):
            hex_to_token("00" * 31)


class AesGcmTests(unittest.TestCase):
    def setUp(self):
        self.key = derive_token("test-key")
        self.crypto = Crypto(self.key)

    def test_round_trip(self):
        pt = b"hello world"
        aad = b"test:v1:abc"
        blob = self.crypto.encrypt(pt, aad)
        self.assertGreaterEqual(len(blob), 12 + 16)
        self.assertNotIn(pt, blob)  # ciphertext doesn't trivially contain plaintext
        out = self.crypto.decrypt(blob, aad)
        self.assertEqual(out, pt)

    def test_round_trip_empty(self):
        blob = self.crypto.encrypt(b"", b"aad")
        self.assertEqual(self.crypto.decrypt(blob, b"aad"), b"")

    def test_round_trip_large(self):
        pt = os.urandom(64 * 1024)
        blob = self.crypto.encrypt(pt, b"aad")
        self.assertEqual(self.crypto.decrypt(blob, b"aad"), pt)

    def test_round_trip_b64(self):
        pt = b"\xff\x00\x01...binary..."
        b64 = self.crypto.encrypt_b64(pt, b"aad")
        self.assertEqual(self.crypto.decrypt_b64(b64, b"aad"), pt)

    def test_nonces_are_unique(self):
        b1 = self.crypto.encrypt(b"x", b"aad")
        b2 = self.crypto.encrypt(b"x", b"aad")
        self.assertNotEqual(b1[:12], b2[:12])

    def test_wrong_key_rejects(self):
        blob = self.crypto.encrypt(b"x", b"aad")
        other = Crypto(derive_token("not-the-key"))
        with self.assertRaises(Exception):
            other.decrypt(blob, b"aad")

    def test_wrong_aad_rejects(self):
        blob = self.crypto.encrypt(b"x", b"aad-A")
        with self.assertRaises(Exception):
            self.crypto.decrypt(blob, b"aad-B")

    def test_tamper_ciphertext_rejects(self):
        blob = bytearray(self.crypto.encrypt(b"hello", b"aad"))
        blob[14] ^= 0x01  # flip a bit in the ciphertext
        with self.assertRaises(Exception):
            self.crypto.decrypt(bytes(blob), b"aad")

    def test_tamper_tag_rejects(self):
        blob = bytearray(self.crypto.encrypt(b"hello", b"aad"))
        blob[-1] ^= 0x01  # flip a bit in the tag
        with self.assertRaises(Exception):
            self.crypto.decrypt(bytes(blob), b"aad")

    def test_tamper_nonce_rejects(self):
        blob = bytearray(self.crypto.encrypt(b"hello", b"aad"))
        blob[0] ^= 0x01
        with self.assertRaises(Exception):
            self.crypto.decrypt(bytes(blob), b"aad")

    def test_too_short_blob(self):
        with self.assertRaises(ValueError):
            self.crypto.decrypt(b"abc", b"aad")


class AadConstructorTests(unittest.TestCase):
    def test_marker_aad_shape(self):
        self.assertEqual(aad_marker("abc"), b"marker:v1:abc")

    def test_meta_aad_shape(self):
        self.assertEqual(aad_meta("abc"), b"meta:v1:abc")

    def test_record_aad_shape(self):
        self.assertEqual(aad_record("out", "abc", 7), b"out:v1:abc:7")
        self.assertEqual(aad_record("in", "abc", 0), b"in:v1:abc:0")

    def test_record_aad_invalid_stream(self):
        with self.assertRaises(ValueError):
            aad_record("foo", "abc", 0)
        # The "tx" (transcript) stream was removed when the chat view
        # was retired; it should now be rejected like any other unknown.
        with self.assertRaises(ValueError):
            aad_record("tx", "abc", 0)


class TriggerSecretTests(unittest.TestCase):
    def test_trigger_secret_deterministic_and_token_bound(self):
        t1 = derive_token("pwA")
        t2 = derive_token("pwB")
        s1 = derive_trigger_secret(t1)
        s1b = derive_trigger_secret(t1)
        s2 = derive_trigger_secret(t2)
        self.assertEqual(s1, s1b)
        self.assertNotEqual(s1, s2)
        self.assertEqual(len(s1), 32)

    def test_trigger_id_is_sha256_of_secret(self):
        import hashlib
        t = derive_token("pw")
        s = derive_trigger_secret(t)
        tid = trigger_id_for(t)
        self.assertEqual(tid, hashlib.sha256(s).digest())
        self.assertEqual(len(tid), 32)

    def test_trigger_secret_rejects_bad_token_length(self):
        with self.assertRaises(ValueError):
            derive_trigger_secret(b"short")
        with self.assertRaises(ValueError):
            trigger_id_for(b"")

    def test_trigger_info_constant_locked(self):
        # Changing this constant breaks cross-language interop. If you
        # change it, also update php/lib/crypto.js:TRIGGER_INFO and
        # regenerate vectors.
        self.assertEqual(TRIGGER_INFO, b"termpilot:trigger:v1")


def write_vectors():
    """Write a vectors file the JS test page consumes.

    The JS test:
      - Imports our hex token, decrypts each blob with its AAD, asserts
        plaintext matches.
      - Encrypts a fresh plaintext, then we round-trip it back here in
        a follow-up pass (or a Python tool reads the JS-produced
        vectors).

    This file is the canonical interop fixture: any change to KDF or
    cipher protocol must regenerate it.
    """
    pw = "interop-password"
    token = derive_token(pw)
    c = Crypto(token)

    cases = []
    # Fixed plaintexts so JS asserts can compare with .equals.
    cases.append({
        "name": "ascii_short",
        "aad_str": "marker:v1:sid_001",
        "plaintext_b64": base64.b64encode(b"termpilot:v1").decode("ascii"),
    })
    cases.append({
        "name": "json_meta",
        "aad_str": "meta:v1:sid_001",
        "plaintext_b64": base64.b64encode(
            json.dumps({"title": "demo", "cwd": "/tmp", "cmd": "bash", "cols": 80, "rows": 24}).encode()
        ).decode("ascii"),
    })
    cases.append({
        "name": "binary_512",
        "aad_str": "out:v1:sid_001:0",
        "plaintext_b64": base64.b64encode(bytes(range(256)) + bytes(range(256))).decode("ascii"),
    })
    cases.append({
        "name": "empty",
        "aad_str": "in:v1:sid_001:0",
        "plaintext_b64": base64.b64encode(b"").decode("ascii"),
    })

    for case in cases:
        pt = base64.b64decode(case["plaintext_b64"])
        aad = case["aad_str"].encode("ascii")
        case["ciphertext_b64"] = c.encrypt_b64(pt, aad)

    # Trigger derivation vector: same token, the JS side recomputes
    # and asserts byte-identical match.
    trigger_secret_hex = derive_trigger_secret(token).hex()
    trigger_id_hex = trigger_id_for(token).hex()

    out = {
        "kdf": {
            "password": pw,
            "salt_str": SALT.decode("ascii"),
            "iterations": PBKDF2_ITERATIONS,
            "token_hex": token_to_hex(token),
        },
        "trigger": {
            "info": TRIGGER_INFO.decode("ascii"),
            "secret_hex": trigger_secret_hex,
            "id_hex": trigger_id_hex,
        },
        "cases": cases,
    }
    path = ROOT / "tests" / "crypto_vectors.json"
    path.write_text(json.dumps(out, indent=2))
    print(f"wrote {path} with {len(cases)} cases")


if __name__ == "__main__":
    if "--gen-vectors" in sys.argv:
        write_vectors()
        sys.argv.remove("--gen-vectors")
    unittest.main(verbosity=2)
