#!/usr/bin/env python3
"""
Resilience tests: §3 of improvements.md.

Covers:
  - OutputSpool: append → pending → confirm → cursor advance.
  - OutputSpool: replay across instance recreation (simulates wrapper crash
    + restart with the same sid).
  - OutputSpool: truncated tail (interrupted append) is ignored.
  - acquire_wrapper_lock: only one wrapper per cwd holds the fcntl lock.
  - active.json: write + read + merge across calls.
  - relay.php /sessions: alive flag flips correctly with $ALIVE_TTL_SECS.
  - relay.php /sessions: stale (alive=false) sessions remain visible.
  - relay.php auto-GC: every op_close triggers a cleanup pass that
    removes closed-stale, last-seen-stale, and orphan dirs while keeping
    live ones and non-session siblings (data/push/, co-tenant junk).

Designed to run alongside test_e2e.py / test_wrapper_e2e.py without
touching the user's real ~/.cache/termpilot or live keyring.
"""
from __future__ import annotations

import http.client
import importlib.util
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

# Load termpilot-wrap (no .py extension) as a module so we can poke at the
# resilience helpers directly. Importing under a non-__main__ name avoids
# triggering main(). spec_from_file_location returns None for unknown
# extensions, so hand it an explicit SourceFileLoader.
from importlib.machinery import SourceFileLoader  # noqa: E402

_loader = SourceFileLoader("ccwrap", str(ROOT / "linux" / "termpilot-wrap"))
_spec = importlib.util.spec_from_loader("ccwrap", _loader)
ccwrap = importlib.util.module_from_spec(_spec)
_loader.exec_module(ccwrap)


PHP_PORT = 6021
RELAY_BASE = f"http://127.0.0.1:{PHP_PORT}"
RELAY_SECRET = "resilience-test-secret"


def wait_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# ============================================================================
#  Pure-Python unit tests (no relay needed)
# ============================================================================


class OutputSpoolTests(unittest.TestCase):
    def setUp(self):
        # Sandbox CACHE_BASE so we don't touch the user's real cache dir.
        self.cache = Path(tempfile.mkdtemp(prefix="termpilot-spool-test-"))
        self._orig_cache = ccwrap.CACHE_BASE
        ccwrap.CACHE_BASE = str(self.cache)
        self.sid = "abcdef012345"

    def tearDown(self):
        ccwrap.CACHE_BASE = self._orig_cache
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_append_then_pending_returns_entries_in_order(self):
        s = ccwrap.OutputSpool(self.sid, "out")
        s.append(b"hello")
        s.append(b"world")
        s.append(b"!")
        pending = s.pending_chunks()
        self.assertEqual([p["plain"] for p in pending], [b"hello", b"world", b"!"])
        # end_off is monotonically increasing
        self.assertTrue(pending[0]["end_off"] < pending[1]["end_off"] < pending[2]["end_off"])
        s.close()

    def test_confirm_through_advances_cursor_and_hides_entries(self):
        s = ccwrap.OutputSpool(self.sid, "out")
        e1 = s.append(b"AAA")
        e2 = s.append(b"BBB")
        s.append(b"CCC")
        s.confirm_through(e1)
        # pending now skips the first entry
        self.assertEqual([p["plain"] for p in s.pending_chunks()], [b"BBB", b"CCC"])
        s.confirm_through(e2)
        self.assertEqual([p["plain"] for p in s.pending_chunks()], [b"CCC"])
        s.close()

    def test_replay_after_recreate_simulates_crash_recovery(self):
        s = ccwrap.OutputSpool(self.sid, "out")
        end1 = s.append(b"unsent-1")
        s.append(b"unsent-2")
        s.append(b"unsent-3")
        s.confirm_through(end1)  # cursor past entry-1; entries 2 and 3 remain
        s.write_next_seq(7)
        s.close()
        # Open fresh — should see entries 2 and 3, and remember next_seq.
        s2 = ccwrap.OutputSpool(self.sid, "out")
        self.assertEqual([p["plain"] for p in s2.pending_chunks()],
                         [b"unsent-2", b"unsent-3"])
        self.assertEqual(s2.next_seq(), 7)
        s2.close()

    def test_truncated_tail_is_ignored(self):
        s = ccwrap.OutputSpool(self.sid, "out")
        s.append(b"complete")
        s.close()
        # Manually corrupt the spool by appending a header that promises more
        # bytes than follow — simulates a power-loss mid-append.
        spool_path = Path(ccwrap.sid_cache_dir(self.sid)) / "out.spool"
        with open(spool_path, "ab") as f:
            f.write(b"\x10\x00\x00\x00")  # claim 16 bytes
            f.write(b"only5")             # provide 5
        s2 = ccwrap.OutputSpool(self.sid, "out")
        # Only the well-formed entry is returned; truncated tail dropped.
        self.assertEqual([p["plain"] for p in s2.pending_chunks()], [b"complete"])
        s2.close()

    def test_next_seq_default_zero_and_round_trip(self):
        s = ccwrap.OutputSpool(self.sid, "out")
        self.assertEqual(s.next_seq(), 0)
        s.write_next_seq(42)
        self.assertEqual(s.next_seq(), 42)
        s.close()
        s2 = ccwrap.OutputSpool(self.sid, "out")
        self.assertEqual(s2.next_seq(), 42)
        s2.close()


class WrapperLockTests(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="termpilot-lock-test-"))
        self._orig_cache = ccwrap.CACHE_BASE
        ccwrap.CACHE_BASE = str(self.cache)
        self.cwd = "/tmp/test-cwd"

    def tearDown(self):
        ccwrap.CACHE_BASE = self._orig_cache
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_acquire_twice_in_same_process_blocks_second(self):
        fd1 = ccwrap.acquire_wrapper_lock(self.cwd, "default")
        self.assertIsNotNone(fd1)
        fd2 = ccwrap.acquire_wrapper_lock(self.cwd, "default")
        self.assertIsNone(fd2, "second acquire should fail while first holds")
        ccwrap.release_wrapper_lock(fd1)
        # After release, acquire works again.
        fd3 = ccwrap.acquire_wrapper_lock(self.cwd, "default")
        self.assertIsNotNone(fd3)
        ccwrap.release_wrapper_lock(fd3)

    def test_lock_is_per_cwd(self):
        fd1 = ccwrap.acquire_wrapper_lock("/tmp/cwd-a", "default")
        fd2 = ccwrap.acquire_wrapper_lock("/tmp/cwd-b", "default")
        try:
            self.assertIsNotNone(fd1)
            self.assertIsNotNone(fd2)
        finally:
            ccwrap.release_wrapper_lock(fd1)
            ccwrap.release_wrapper_lock(fd2)


class ActiveJsonTests(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="termpilot-aj-test-"))
        self._orig_cache = ccwrap.CACHE_BASE
        ccwrap.CACHE_BASE = str(self.cache)
        self.cwd = "/tmp/aj-cwd"

    def tearDown(self):
        ccwrap.CACHE_BASE = self._orig_cache
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_read_missing_returns_empty(self):
        self.assertEqual(ccwrap.read_active_json(self.cwd, "default"), {})

    def test_write_then_read_round_trip(self):
        ccwrap.write_active_json(self.cwd, "default",
                                 wrapper_sid="abc123", marker_b64="m1")
        d = ccwrap.read_active_json(self.cwd, "default")
        self.assertEqual(d.get("wrapper_sid"), "abc123")
        self.assertEqual(d.get("marker_b64"), "m1")
        self.assertIsInstance(d.get("ts"), int)

    def test_writes_merge_fields(self):
        ccwrap.write_active_json(self.cwd, "default", wrapper_sid="sid-1")
        ccwrap.write_active_json(self.cwd, "default", marker_b64="m1")
        d = ccwrap.read_active_json(self.cwd, "default")
        self.assertEqual(d.get("wrapper_sid"), "sid-1")
        self.assertEqual(d.get("marker_b64"), "m1")

    def test_clear_removes_file(self):
        ccwrap.write_active_json(self.cwd, "default", wrapper_sid="x")
        ccwrap.clear_active_json(self.cwd, "default")
        self.assertEqual(ccwrap.read_active_json(self.cwd, "default"), {})


# ============================================================================
#  Relay endpoint tests (alive flag + GC)
# ============================================================================


class RelayResilienceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docroot = ROOT / "relay"
        cls._orig_config = cls.docroot / "config.php"
        cls._backup_config = cls._orig_config.with_suffix(".bak.resilience") \
            if cls._orig_config.exists() else None
        if cls._backup_config:
            shutil.move(str(cls._orig_config), str(cls._backup_config))
        # Small ALIVE_TTL_SECS so the alive flag flips fast. Tight
        # auto-GC thresholds so a few-seconds-old session counts as
        # closed/last-seen-stale and we can assert removal in one test
        # without waiting hours.
        (cls.docroot / "config.php").write_text(
            f"<?php\n"
            f"define('RELAY_SECRET', '{RELAY_SECRET}');\n"
            f"define('ALIVE_TTL_SECS', 2);\n"
            f"define('AUTO_GC_CLOSED_SECS', 1);\n"
            f"define('AUTO_GC_STALE_SECS', 1);\n"
        )
        cls._data = cls.docroot / "data"
        if cls._data.exists():
            shutil.rmtree(cls._data)
        cls.php_proc = subprocess.Popen(
            ["php", "-S", f"127.0.0.1:{PHP_PORT}", "-t", str(cls.docroot)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not wait_port("127.0.0.1", PHP_PORT, 5):
            cls.php_proc.terminate()
            raise RuntimeError("php -S didn't start")

    @classmethod
    def tearDownClass(cls):
        cls.php_proc.terminate()
        try: cls.php_proc.wait(timeout=2)
        except subprocess.TimeoutExpired: cls.php_proc.kill()
        cfg = cls.docroot / "config.php"
        if cfg.exists(): cfg.unlink()
        if cls._backup_config:
            shutil.move(str(cls._backup_config), str(cls._orig_config))
        if cls._data.exists():
            shutil.rmtree(cls._data)

    # ---- helpers ----------------------------------------------------------

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", PHP_PORT, timeout=10)

    def _request(self, method, op, *, body=None, params=None, no_auth=False):
        from urllib.parse import urlencode
        q = {"op": op, **(params or {})}
        path = "/relay.php?" + urlencode(q)
        h = {}
        if not no_auth:
            h["Authorization"] = "Bearer " + RELAY_SECRET
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c = self._conn()
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
            try: return r.status, json.loads(raw)
            except Exception: return r.status, {"raw": raw.decode("utf-8", errors="replace")}
        finally:
            c.close()

    def _close(self, sid, password: str = "p"):
        tok = crypto.derive_token(password)
        secret_hex = crypto.derive_trigger_secret(tok).hex()
        return self._request("POST", "close", body={
            "session_id": sid,
            "trigger_secret_hex": secret_hex,
        })

    def _register(self, sid: str, password: str = "p"):
        tok = crypto.derive_token(password)
        cr = crypto.Crypto(tok)
        meta_b64 = cr.encrypt_b64(
            json.dumps({"title": "t", "cwd": "/x", "cmd": "y",
                        "cols": 80, "rows": 24}).encode(),
            crypto.aad_meta(sid))
        marker_b64 = cr.encrypt_b64(b"termpilot:v1", crypto.aad_marker(sid))
        st, _ = self._request("POST", "register", body={
            "session_id": sid, "encrypted_meta": meta_b64,
            "encrypted_marker": marker_b64,
            "trigger_id_hex": crypto.trigger_id_for(tok).hex(),
            "cols": 80, "rows": 24,
        })
        self.assertEqual(st, 200)

    def _sessions(self):
        st, body = self._request("GET", "sessions")
        self.assertEqual(st, 200, body)
        return body["sessions"]

    def _session(self, sid):
        return next((s for s in self._sessions() if s["id"] == sid), None)

    # ---- tests ------------------------------------------------------------

    def test_alive_flag_flips_with_ttl(self):
        sid = "feed00aa1234"
        self._register(sid)
        s = self._session(sid)
        self.assertIsNotNone(s)
        self.assertTrue(s["alive"], "freshly-registered session must be alive")
        self.assertEqual(s["offline_secs"], 0)
        # Wait > ALIVE_TTL_SECS (2) without a heartbeat.
        time.sleep(3)
        s2 = self._session(sid)
        self.assertIsNotNone(s2, "stale session must REMAIN visible (not hidden)")
        self.assertFalse(s2["alive"], "after TTL the session should be alive=false")
        self.assertGreaterEqual(s2["offline_secs"], 2)
        # Heartbeat → alive again. Heartbeat is trigger-secret gated to
        # stop a RELAY_SECRET-holder from artificially keeping someone
        # else's session alive; pass the matching secret here.
        tok = crypto.derive_token("p")
        secret_hex = crypto.derive_trigger_secret(tok).hex()
        st, _ = self._request("POST", "heartbeat", body={
            "session_id": sid,
            "trigger_secret_hex": secret_hex,
        })
        self.assertEqual(st, 200)
        s3 = self._session(sid)
        self.assertTrue(s3["alive"], "heartbeat must restore alive=true")
        # Negative: heartbeat without the trigger_secret must be rejected
        # (the new gate added alongside op_close's existing one).
        st, _ = self._request("POST", "heartbeat", body={"session_id": sid})
        self.assertEqual(st, 400, "missing trigger_secret_hex must be 400 (require_hex_64)")
        # Wrong trigger_secret → 401.
        bad = crypto.derive_trigger_secret(crypto.derive_token("other")).hex()
        st, _ = self._request("POST", "heartbeat", body={
            "session_id": sid, "trigger_secret_hex": bad,
        })
        self.assertEqual(st, 401, "wrong trigger_secret_hex must be 401")

    def test_auto_gc_removes_closed_stale_on_next_close(self):
        # Two sessions. Backdate the first's closed_at past the (tiny)
        # AUTO_GC_CLOSED_SECS, then close the second — its op_close
        # triggers the cleanup pass that sweeps the first.
        sid_old = "aaaa00000001"
        sid_new = "aaaa00000002"
        self._register(sid_old)
        self._register(sid_new)
        meta_p = self._data / sid_old / "meta.public.json"
        meta = json.loads(meta_p.read_text())
        meta["closed"] = True
        meta["closed_at"] = int(time.time()) - 3600  # 1h ago ≫ threshold
        meta_p.write_text(json.dumps(meta))

        st, _ = self._close(sid_new)
        self.assertEqual(st, 200)
        self.assertFalse((self._data / sid_old).exists(),
                         "closed-stale session must be removed by auto-GC")
        self.assertTrue((self._data / sid_new).exists(),
                        "just-closed session is fresh; survives this pass")

    def test_auto_gc_removes_last_seen_stale_on_next_close(self):
        # Wrapper-silent path: session has no closed flag but last_seen
        # is old (e.g. wrapper died without an op_close).
        sid_old = "bbbb00000001"
        sid_new = "bbbb00000002"
        self._register(sid_old)
        self._register(sid_new)
        meta_p = self._data / sid_old / "meta.public.json"
        meta = json.loads(meta_p.read_text())
        meta.pop("closed", None)
        meta.pop("closed_at", None)
        meta["last_seen"] = int(time.time()) - 3600  # 1h ago ≫ threshold
        meta_p.write_text(json.dumps(meta))

        st, _ = self._close(sid_new)
        self.assertEqual(st, 200)
        self.assertFalse((self._data / sid_old).exists(),
                         "last-seen-stale session must be removed by auto-GC")

    def test_auto_gc_removes_orphan_dirs(self):
        # Orphan (no meta.public.json) — created when register half-runs.
        # Only swept when older than the 1h hardcoded orphan grace.
        sid_orphan = "cccc00000001"
        sid_trigger = "cccc00000002"
        self._register(sid_trigger)
        orphan_dir = self._data / sid_orphan
        orphan_dir.mkdir()
        old = time.time() - 7200  # 2h ago
        os.utime(orphan_dir, (old, old))

        st, _ = self._close(sid_trigger)
        self.assertEqual(st, 200)
        self.assertFalse(orphan_dir.exists(),
                         "old orphan dir must be removed by auto-GC")

    def test_auto_gc_keeps_live_sessions_and_non_session_dirs(self):
        # Live session + push subscription tree + co-tenant junk all
        # must survive a close that triggers auto-GC.
        sid_live = "dddd00000001"
        sid_trigger = "dddd00000002"
        self._register(sid_live)
        self._register(sid_trigger)
        # Push subscription tree (no meta, non-hex basename in the chain
        # from data/push/<token_hash>/).
        push_dir = self._data / "push" / ("a" * 64)
        push_dir.mkdir(parents=True)
        sub_file = push_dir / "deadbeefdeadbeefdeadbeefdeadbeef.json"
        sub_file.write_text(json.dumps({"id": "x", "endpoint": "https://fcm.googleapis.com/abc"}))
        # Co-tenant-style sibling (non-hex name).
        sibling = self._data / "wat"
        sibling.mkdir()
        old = time.time() - 7200
        os.utime(push_dir, (old, old))
        os.utime(self._data / "push", (old, old))
        os.utime(sibling, (old, old))

        st, _ = self._close(sid_trigger)
        self.assertEqual(st, 200)
        self.assertTrue((self._data / sid_live).is_dir(),
                        "live session must survive")
        self.assertTrue(push_dir.is_dir(), "push/<hash>/ must survive")
        self.assertTrue(sub_file.exists(), "push subscription file must survive")
        self.assertTrue(sibling.is_dir(), "non-hex sibling must survive")

    def test_gc_endpoint_is_removed(self):
        # Defense against accidentally re-adding the admin endpoint:
        # ?op=gc must not be routable.
        st, body = self._request("POST", "gc", body={})
        self.assertEqual(st, 404, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
