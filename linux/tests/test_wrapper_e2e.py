#!/usr/bin/env python3
"""
Live wrapper test: launches termpilot-wrap run with `bash -i` as the
child, then drives it via the relay (encrypted input → wrapper → bash;
bash output → wrapper → encrypted server records → decrypted client).

Storage and config are sandboxed so this never touches the user's real
~/.config/termpilot/token, real RELAY_SECRET, or live deployment.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
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

PHP_PORT = 6020
RELAY_BASE = f"http://127.0.0.1:{PHP_PORT}"
SECRET = "wrapper-e2e-secret"
PASSWORD = "wrapper-e2e-pwd"


# The startup banner is one line of the form (post-ANSI-strip):
#     termpilot  session <12-hex>
# Match the bare hex after the literal "session" so the regex stays
# stable across cosmetic banner tweaks (colour, padding, etc.).
_ANSI_RE = re.compile(rb"\x1b\[[0-9;]*m")
_SID_BANNER_RE = re.compile(rb"session\s+([0-9a-f]{12})")


def _read_sid_from_banner(stderr, timeout: float) -> str | None:
    sid, _ = _read_sid_from_banner_with_dump(stderr, timeout)
    return sid


def _read_sid_from_banner_with_dump(stderr, timeout: float) -> tuple[str | None, bytes]:
    """Pull the SID out of the wrapper's startup banner on stderr.

    Returns (sid, accumulated_bytes). The dump is useful in the test
    assertion message so a regression points at what the wrapper
    actually wrote instead of just "didn't see banner".
    """
    deadline = time.time() + timeout
    dump = b""
    while time.time() < deadline:
        line = stderr.readline()
        if not line:
            time.sleep(0.05)
            continue
        dump += line
        m = _SID_BANNER_RE.search(_ANSI_RE.sub(b"", dump))
        if m:
            return m.group(1).decode("ascii"), dump
    return None, dump


def wait_port(host, port, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.3): return True
        except OSError: time.sleep(0.1)
    return False


class HTTPClient:
    def __init__(self, base, secret):
        self.base = base; self.secret = secret
    def _req(self, method, op, *, body=None, params=None):
        from urllib.parse import urlencode, urlsplit
        u = urlsplit(self.base)
        q = {"op": op, **(params or {})}
        path = f"/relay.php?{urlencode(q)}"
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
        h = {"Authorization": f"Bearer {self.secret}"}
        d = None
        if body is not None: d = json.dumps(body).encode(); h["Content-Type"] = "application/json"
        c.request(method, path, body=d, headers=h)
        r = c.getresponse(); body_bytes = r.read(); c.close()
        try: return r.status, json.loads(body_bytes)
        except Exception: return r.status, {"raw": body_bytes.decode("utf-8", errors="replace")}
    def get(self, op, **p):  return self._req("GET",  op, params=p)
    def post(self, op, b):   return self._req("POST", op, body=b)


class WrapperE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docroot = ROOT / "relay"
        cls._orig = cls.docroot / "config.php"
        cls._bak = cls._orig.with_suffix(".bak.wraptest") if cls._orig.exists() else None
        if cls._bak: shutil.move(str(cls._orig), str(cls._bak))
        (cls.docroot / "config.php").write_text(f"<?php\ndefine('RELAY_SECRET', '{SECRET}');\n")
        cls._data = cls.docroot / "data"
        if cls._data.exists(): shutil.rmtree(cls._data)

        cls.php_proc = subprocess.Popen(
            ["php", "-S", f"127.0.0.1:{PHP_PORT}", "-t", str(cls.docroot)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not wait_port("127.0.0.1", PHP_PORT, 5):
            cls.php_proc.terminate(); raise RuntimeError("php -S didn't start")

        # Sandbox the token: write a token file in a temp HOME so wrapper
        # uses our test password, not the user's real keyring.
        cls.tmphome = tempfile.mkdtemp(prefix="termpilot-wraptest-home-")
        cls.token = crypto.derive_token(PASSWORD)
        token_path = os.path.join(cls.tmphome, ".config/termpilot/token")
        os.makedirs(os.path.dirname(token_path), exist_ok=True)
        with open(token_path, "w") as f:
            f.write(crypto.token_to_hex(cls.token) + "\n")
        os.chmod(token_path, 0o600)
        cls.crypto = crypto.Crypto(cls.token)
        cls.http = HTTPClient(RELAY_BASE, SECRET)

    @classmethod
    def tearDownClass(cls):
        cls.php_proc.terminate()
        try: cls.php_proc.wait(timeout=2)
        except subprocess.TimeoutExpired: cls.php_proc.kill()
        cfg = cls.docroot / "config.php"
        if cfg.exists(): cfg.unlink()
        if cls._bak: shutil.move(str(cls._bak), str(cls._orig))
        if cls._data.exists(): shutil.rmtree(cls._data)
        shutil.rmtree(cls.tmphome, ignore_errors=True)

    def _spawn_wrapper(self):
        env = os.environ.copy()
        env["TERMPILOT_RELAY"] = f"{RELAY_BASE}/relay.php"
        env["TERMPILOT_SECRET"] = SECRET
        env["HOME"] = self.tmphome
        # Hard-pin the token via env so we don't accidentally pick up the
        # user's real keyring entry.
        env["TERMPILOT_TOKEN_HEX"] = crypto.token_to_hex(self.token)
        # Unbuffered stderr so failures surface in the test's pipe reader
        # immediately, not after the wrapper process flushes on exit.
        env["PYTHONUNBUFFERED"] = "1"
        # Run wrapper without local stdin (--no-local) so we drive purely via the relay.
        # Use `bash -i` as the spawned child.
        proc = subprocess.Popen(
            [str(ROOT / "linux" / "termpilot-wrap"), "run", "--no-local", "--insecure",
             "--", "bash", "-i"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Pull the SID out of the startup banner. The line looks like
        # `termpilot  session <12-hex-chars>` with ANSI escapes around
        # the label, so we just match on the trailing hex.
        sid, dumped = _read_sid_from_banner_with_dump(proc.stderr, timeout=10)
        if not sid:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill()
            try:
                rest = proc.stderr.read() or b""
                dumped += rest
            except Exception as e:
                dumped += f"\n[error reading remaining stderr: {e}]".encode()
            try:
                stdout_dump = proc.stdout.read() or b""
            except Exception:
                stdout_dump = b""
            self.fail(
                "wrapper didn't print the startup banner within timeout.\n"
                f"stderr ({len(dumped)} bytes):\n{dumped.decode('utf-8', errors='replace')}\n"
                f"stdout ({len(stdout_dump)} bytes):\n{stdout_dump.decode('utf-8', errors='replace')}\n"
                f"exit code: {proc.returncode}"
            )
        return proc, sid

    def test_register_then_inject_input_then_read_output(self):
        proc, sid = self._spawn_wrapper()
        try:
            # Confirm the session shows up on the relay
            time.sleep(0.5)
            status, body = self.http.get("sessions")
            self.assertEqual(status, 200)
            ids = [s["id"] for s in body["sessions"]]
            self.assertIn(sid, ids, f"expected sid {sid} in {ids}")

            my = next(s for s in body["sessions"] if s["id"] == sid)
            # Marker decrypts cleanly with our test token
            self.crypto.decrypt_b64(my["marker"], crypto.aad_marker(sid))

            # Decrypt meta and verify it's our wrapper's view
            status, body = self.http.get("meta", session=sid)
            self.assertEqual(status, 200)
            meta = json.loads(self.crypto.decrypt_b64(
                body["encrypted_meta"], crypto.aad_meta(sid)))
            self.assertIn("bash -i", meta["cmd"])

            # Send an input record (encrypted, seq=0): an `echo` command
            cmd = b"echo HELLO_FROM_E2E\n"
            blob = self.crypto.encrypt_b64(cmd, crypto.aad_record("in", sid, 0))
            status, _ = self.http.post("input", {
                "session_id": sid,
                "records": [{"seq": 0, "blob": blob}],
            })
            self.assertEqual(status, 200)

            # Wait for the output to flow back through the relay
            deadline = time.time() + 8
            decrypted_so_far = b""
            seen_marker = False
            while time.time() < deadline and not seen_marker:
                status, body = self.http.get("output",
                                             session=sid, since_seq=0)
                self.assertEqual(status, 200)
                for rec in body["records"]:
                    seq = int(rec["seq"])
                    pt = self.crypto.decrypt_b64(rec["blob"], crypto.aad_record("out", sid, seq))
                    decrypted_so_far += pt
                if b"HELLO_FROM_E2E" in decrypted_so_far:
                    seen_marker = True; break
                time.sleep(0.5)
            self.assertTrue(seen_marker, f"expected HELLO_FROM_E2E in output; got {decrypted_so_far[:200]}")

            # Confirm the on-disk records are opaque (no plaintext leak)
            out_records = (self._data / sid / "out.records").read_bytes()
            self.assertNotIn(b"HELLO_FROM_E2E", out_records,
                             "plaintext leaked into out.records!")
            in_records = (self._data / sid / "in.records").read_bytes()
            self.assertNotIn(b"HELLO_FROM_E2E", in_records)
        finally:
            proc.terminate()
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired: proc.kill()

if __name__ == "__main__":
    unittest.main(verbosity=2)
