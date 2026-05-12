#!/usr/bin/env python3
"""
Config-gate tests for relay.php.

RELAY_SECRET is OPTIONAL. When unset / empty / "CHANGE_ME", the relay
runs OPEN — every request is treated as unauthenticated. Content
secrecy still holds (per-token AES-GCM); op_close and op_push_notify
are still token-bound via trigger_secret. The only loss is the spam
gate: anyone who finds the URL can register fake sessions and write
encrypted noise.

When RELAY_SECRET *is* set, missing/wrong Bearer headers must 401.

Usage:
    python3 tests/test_config_gate.py
"""
from __future__ import annotations

import http.client
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


PHP_PORT = 6020  # distinct from test_e2e.py


def wait_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class RelayConfigGateTest(unittest.TestCase):
    def _spawn_with_config(self, config_body):
        # Build a self-contained docroot copying the real php/ tree so the
        # relay sees its actual sources, but with our chosen config.php.
        work = Path(tempfile.mkdtemp(prefix="termpilot-cfg-"))
        docroot = work / "php"
        shutil.copytree(ROOT / "php", docroot)
        cfg = docroot / "config.php"
        if config_body is None:
            if cfg.exists(): cfg.unlink()
        else:
            cfg.write_text(config_body)
        # Wipe any data/ that came along — start clean.
        data_dir = docroot / "data"
        if data_dir.exists():
            shutil.rmtree(data_dir)
        proc = subprocess.Popen(
            ["php", "-S", f"127.0.0.1:{PHP_PORT}", "-t", str(docroot)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not wait_port("127.0.0.1", PHP_PORT, 5):
            proc.terminate()
            shutil.rmtree(work, ignore_errors=True)
            self.fail("php -S didn't start")
        return proc, work

    def _stop(self, proc, work):
        proc.terminate()
        try: proc.wait(timeout=2)
        except subprocess.TimeoutExpired: proc.kill()
        shutil.rmtree(work, ignore_errors=True)

    def _hit(self, op: str = "auth_required", bearer: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", PHP_PORT, timeout=5)
        headers = {}
        if bearer is not None:
            headers["Authorization"] = "Bearer " + bearer
        conn.request("GET", f"/relay.php?op={op}", headers=headers)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body

    # ---- Test cases ------------------------------------------------------

    def test_unset_secret_runs_open(self):
        # No config.php at all → RELAY_SECRET undefined → relay runs open.
        proc, work = self._spawn_with_config(None)
        try:
            st, body = self._hit("auth_required")
            self.assertEqual(st, 200)
            self.assertIn(b'"required":false', body)
            # Even with no bearer, sessions endpoint succeeds.
            st, _ = self._hit("sessions")
            self.assertEqual(st, 200)
        finally:
            self._stop(proc, work)

    def test_change_me_placeholder_runs_open(self):
        # 'CHANGE_ME' literal counts as "not configured" for backwards
        # compatibility with the example config file.
        proc, work = self._spawn_with_config("<?php\ndefine('RELAY_SECRET', 'CHANGE_ME');\n")
        try:
            st, body = self._hit("auth_required")
            self.assertEqual(st, 200)
            self.assertIn(b'"required":false', body)
        finally:
            self._stop(proc, work)

    def test_empty_string_runs_open(self):
        proc, work = self._spawn_with_config("<?php\ndefine('RELAY_SECRET', '');\n")
        try:
            st, body = self._hit("auth_required")
            self.assertEqual(st, 200)
            self.assertIn(b'"required":false', body)
        finally:
            self._stop(proc, work)

    def test_real_secret_requires_bearer(self):
        proc, work = self._spawn_with_config(
            "<?php\ndefine('RELAY_SECRET', 'a' . str_repeat('b', 31));\n"
        )
        try:
            # auth_required is the only pre-auth endpoint.
            st, body = self._hit("auth_required")
            self.assertEqual(st, 200)
            self.assertIn(b'"required":true', body)
            # No bearer on sessions → 401.
            st, _ = self._hit("sessions")
            self.assertEqual(st, 401)
            # Wrong bearer → 401.
            st, _ = self._hit("sessions", bearer="wrong")
            self.assertEqual(st, 401)
            # Right bearer → 200.
            st, _ = self._hit("sessions", bearer="a" + ("b" * 31))
            self.assertEqual(st, 200)
        finally:
            self._stop(proc, work)


if __name__ == "__main__":
    unittest.main(verbosity=2)
