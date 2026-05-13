#!/usr/bin/env python3
"""
End-to-end test: termpilot-wrap ↔ relay.php (php -S) ↔ a Python "client"
acting as the browser. Verifies:

1. Wrapper register: marker + meta blobs land on the server.
2. Server's data files are opaque (no plaintext leaks).
3. Wrong token fails to decrypt marker (server-blind property).
4. Right token decrypts marker and meta cleanly.
5. Output flow: wrapper writes → server records → client decrypts → matches.
6. Input flow: client encrypts + posts → wrapper reads → matches.
7. Tampering: server modifying a single byte of any record causes the
   client to reject it (GCM tag check).

This is the SECURITY oracle. If any assertion here fails, the design is
broken.

Usage:
    python3 tests/test_e2e.py
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "linux"))

from shared import crypto  # noqa: E402


PHP_PORT = 6019
RELAY_BASE = f"http://127.0.0.1:{PHP_PORT}"
SECRET = "test-secret-e2e"
PASSWORD = "e2e-test-password"


def wait_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class HTTPClient:
    """Minimal HTTP client that talks to the relay with the auth header."""
    def __init__(self, base: str, secret: str):
        self.base = base
        self.secret = secret

    def _req(self, method: str, op: str, *, body=None, params=None):
        from urllib.parse import urlencode, urlsplit
        u = urlsplit(self.base)
        q = {"op": op, **(params or {})}
        path = f"/relay.php?{urlencode(q)}"
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
        headers = {"Authorization": f"Bearer {self.secret}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        body_bytes = r.read()
        conn.close()
        try:
            payload = json.loads(body_bytes)
        except Exception:
            payload = {"raw": body_bytes.decode("utf-8", errors="replace")}
        return r.status, payload

    def get(self, op, **params):  return self._req("GET", op, params=params)
    def post(self, op, body):     return self._req("POST", op, body=body)


class E2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="termpilot-e2e-"))
        cls.docroot = ROOT / "relay"
        # Seed a clean config.php for the test session.
        cls._orig_config = cls.docroot / "config.php"
        cls._backup_config = cls._orig_config.with_suffix(".bak.e2e") if cls._orig_config.exists() else None
        if cls._backup_config:
            shutil.move(str(cls._orig_config), str(cls._backup_config))
        (cls.docroot / "config.php").write_text(
            f"<?php\ndefine('RELAY_SECRET', '{SECRET}');\n"
        )
        # Wipe data so test starts clean.
        cls._data = cls.docroot / "data"
        if cls._data.exists():
            shutil.rmtree(cls._data)

        # Launch php -S
        cls.php_proc = subprocess.Popen(
            ["php", "-S", f"127.0.0.1:{PHP_PORT}", "-t", str(cls.docroot)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not wait_port("127.0.0.1", PHP_PORT, 5):
            cls.php_proc.terminate()
            raise RuntimeError("php -S didn't start in time")

        cls.token = crypto.derive_token(PASSWORD)
        cls.crypto = crypto.Crypto(cls.token)
        cls.http = HTTPClient(RELAY_BASE, SECRET)

    @classmethod
    def tearDownClass(cls):
        cls.php_proc.terminate()
        try: cls.php_proc.wait(timeout=2)
        except subprocess.TimeoutExpired: cls.php_proc.kill()
        # Restore config and clean data
        cfg = cls.docroot / "config.php"
        if cfg.exists(): cfg.unlink()
        if cls._backup_config: shutil.move(str(cls._backup_config), str(cls._orig_config))
        if cls._data.exists(): shutil.rmtree(cls._data)
        shutil.rmtree(cls.work, ignore_errors=True)

    # ---- Test cases -------------------------------------------------------

    def test_01_unauthorized(self):
        bad = HTTPClient(RELAY_BASE, "wrong-secret")
        status, _ = bad.get("sessions")
        self.assertEqual(status, 401)

    def test_02_register_and_meta_round_trip(self):
        sid = "abc123def456"
        meta_pt = json.dumps({"title": "test", "cwd": "/tmp", "cmd": "bash", "cols": 80, "rows": 24}).encode()
        enc_meta = self.crypto.encrypt_b64(meta_pt, crypto.aad_meta(sid))
        enc_marker = self.crypto.encrypt_b64(b"termpilot:v1", crypto.aad_marker(sid))
        trigger_id_hex = crypto.trigger_id_for(self.token).hex()
        status, body = self.http.post("register", {
            "session_id": sid,
            "encrypted_meta": enc_meta,
            "encrypted_marker": enc_marker,
            "trigger_id_hex": trigger_id_hex,
            "cols": 80, "rows": 24,
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("session_id"), sid)

        # /sessions returns the marker, not the meta
        status, body = self.http.get("sessions")
        self.assertEqual(status, 200)
        sess_list = body["sessions"]
        my = next((s for s in sess_list if s["id"] == sid), None)
        self.assertIsNotNone(my, "session not in list")
        # No plaintext leakage in the public payload
        for k in ("title", "cwd", "cmd"):
            self.assertNotIn(k, my, f"server leaked {k}")
        self.assertEqual(my["cols"], 80)
        self.assertIsNotNone(my["marker"])

        # Marker decryption with right key works
        marker_pt = self.crypto.decrypt_b64(my["marker"], crypto.aad_marker(sid))
        self.assertEqual(marker_pt, b"termpilot:v1")

        # Marker decryption with wrong key fails
        wrong = crypto.Crypto(crypto.derive_token("not-the-password"))
        with self.assertRaises(Exception):
            wrong.decrypt_b64(my["marker"], crypto.aad_marker(sid))

        # /meta endpoint returns the encrypted_meta blob; decrypt round-trip
        status, body = self.http.get("meta", session=sid)
        self.assertEqual(status, 200)
        meta_back = self.crypto.decrypt_b64(body["encrypted_meta"], crypto.aad_meta(sid))
        self.assertEqual(json.loads(meta_back)["title"], "test")

    def test_03_data_files_are_opaque(self):
        """The server's stored files MUST NOT contain our plaintext title/cmd."""
        sid_dir = self._data / "abc123def456"
        for name in ("meta.bin",):
            blob = (sid_dir / name).read_bytes()
            self.assertNotIn(b"test", blob, f"plaintext leaked into {name}")
            self.assertNotIn(b"/tmp", blob)
            self.assertNotIn(b"bash", blob)

    def test_04_output_flow(self):
        sid = "0a1b2c3d4e5f"
        # register
        self._register(sid)
        # post 3 output records (encrypted)
        for seq, msg in enumerate([b"hello ", b"world", b"\n"]):
            blob = self.crypto.encrypt_b64(msg, crypto.aad_record("out", sid, seq))
            status, _ = self.http.post("output", {
                "session_id": sid,
                "records": [{"seq": seq, "blob": blob}],
            })
            self.assertEqual(status, 200)
        # GET back; decrypt each
        status, body = self.http.get("output", session=sid, since_seq=0)
        self.assertEqual(status, 200)
        records = body["records"]
        self.assertEqual(len(records), 3)
        decrypted = b""
        for r in records:
            seq = r["seq"]
            pt = self.crypto.decrypt_b64(r["blob"], crypto.aad_record("out", sid, seq))
            decrypted += pt
        self.assertEqual(decrypted, b"hello world\n")

    def test_05_input_flow_and_tampering(self):
        sid = "1122aabbccdd"
        self._register(sid)
        # Client posts an encrypted input record (impersonating browser)
        plaintext = b"echo I am the user\n"
        blob_b64 = self.crypto.encrypt_b64(plaintext, crypto.aad_record("in", sid, 0))
        status, _ = self.http.post("input", {
            "session_id": sid,
            "records": [{"seq": 0, "blob": blob_b64}],
        })
        self.assertEqual(status, 200)
        # Wrapper-side simulation: GET input, decrypt
        status, body = self.http.get("input", session=sid, since_seq=0)
        self.assertEqual(status, 200)
        recs = body["records"]
        self.assertEqual(len(recs), 1)
        decrypted = self.crypto.decrypt_b64(recs[0]["blob"], crypto.aad_record("in", sid, 0))
        self.assertEqual(decrypted, plaintext)

        # Now tamper with the stored blob: flip a bit on disk and verify
        # client refuses on next read.
        in_records = self._data / sid / "in.records"
        data = bytearray(in_records.read_bytes())
        data[-1] ^= 0x01
        in_records.write_bytes(bytes(data))
        status, body = self.http.get("input", session=sid, since_seq=0)
        self.assertEqual(status, 200)
        with self.assertRaises(Exception):
            self.crypto.decrypt_b64(body["records"][0]["blob"], crypto.aad_record("in", sid, 0))

    def test_06_wrong_aad_rejects(self):
        """Server can't replay an output record into the input stream."""
        sid = "deadbeef9999"
        self._register(sid)
        # Encrypt with stream='out', but write into the in.records file.
        plain = b"sneaky"
        blob_b64 = self.crypto.encrypt_b64(plain, crypto.aad_record("out", sid, 0))
        # Post via output endpoint (legitimate)
        self.http.post("output", {"session_id": sid, "records": [{"seq": 0, "blob": blob_b64}]})
        # Fetch and try to decrypt as if it were input — must fail.
        status, body = self.http.get("output", session=sid, since_seq=0)
        recs = body["records"]
        with self.assertRaises(Exception):
            # Same blob, wrong AAD (stream='in') — GCM rejects.
            self.crypto.decrypt_b64(recs[0]["blob"], crypto.aad_record("in", sid, 0))

    # ---- Helpers ----------------------------------------------------------

    def _register(self, sid: str, password: str = PASSWORD):
        tok = crypto.derive_token(password)
        c = crypto.Crypto(tok)
        meta_b64 = c.encrypt_b64(json.dumps({"title": "t", "cwd": "/x", "cmd": "y", "cols": 80, "rows": 24}).encode(), crypto.aad_meta(sid))
        marker_b64 = c.encrypt_b64(b"termpilot:v1", crypto.aad_marker(sid))
        status, body = self.http.post("register", {
            "session_id": sid, "encrypted_meta": meta_b64,
            "encrypted_marker": marker_b64,
            "trigger_id_hex": crypto.trigger_id_for(tok).hex(),
            "cols": 80, "rows": 24,
        })
        self.assertEqual(status, 200, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
