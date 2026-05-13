#!/usr/bin/env python3
"""
Push-notification relay tests (improvements.md §5).

Covers:
  - op=push_pubkey: auto-generates a 65-byte P-256 uncompressed key and
    persists it across requests.
  - op=push_subscribe: validates token_hash / endpoint, stores under
    data/push/<token_hash>/<sub_id>.json, dedupes on identical endpoint.
  - op=push_unsubscribe: removes the file.
  - op=push_notify: iterates subscriptions, POSTs to the endpoint URL with
    a VAPID Authorization header (decoded and validated against the public
    key returned by push_pubkey).

The "FCM endpoint" is a local HTTP server that captures the request, so no
real push service is involved.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "linux"))

from shared import crypto  # noqa: E402

PHP_PORT = 6029
RELAY_SECRET = "push-test-secret"


def wait_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def b64url_decode(s: str) -> bytes:
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


# ---------- mock push service ------------------------------------------------


class MockPushHandler(http.server.BaseHTTPRequestHandler):
    """Captures POSTs to a fake FCM endpoint URL."""
    captured: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        MockPushHandler.captured.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "ttl": self.headers.get("TTL", ""),
            "body_len": len(body),
        })
        self.send_response(201)
        self.end_headers()

    def log_message(self, *_a, **_kw):
        pass


class MockPushServer:
    def __init__(self):
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), MockPushHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path="/push/abc"):
        return f"http://127.0.0.1:{self.port}{path}"

    def shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------- tests ------------------------------------------------------------


class PushRelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docroot = ROOT / "relay"
        cls._orig_config = cls.docroot / "config.php"
        cls._backup_config = (
            cls._orig_config.with_suffix(".bak.push")
            if cls._orig_config.exists() else None
        )
        if cls._backup_config:
            shutil.move(str(cls._orig_config), str(cls._backup_config))
        # PUSH_ALLOW_INSECURE_DEV bypasses the strict push-endpoint
        # allowlist so this suite can subscribe a local http://127.0.0.1
        # mock push server. Production deploys never set this flag — the
        # strict SSRF defense is exercised separately in
        # PushEndpointStrictTest below.
        (cls.docroot / "config.php").write_text(
            f"<?php\n"
            f"define('RELAY_SECRET', '{RELAY_SECRET}');\n"
            f"define('PUSH_ALLOW_INSECURE_DEV', true);\n"
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

    def _request(self, method, op, *, body=None, params=None, no_auth=False):
        q = {"op": op, **(params or {})}
        path = "/relay.php?" + urlencode(q)
        h = {}
        if not no_auth:
            h["Authorization"] = "Bearer " + RELAY_SECRET
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", PHP_PORT, timeout=10)
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
            try: return r.status, json.loads(raw)
            except Exception: return r.status, {"raw": raw.decode("utf-8", errors="replace")}
        finally:
            c.close()

    # ----- pubkey -----

    def test_push_pubkey_format_and_persistence(self):
        st, body = self._request("GET", "push_pubkey")
        self.assertEqual(st, 200, body)
        pk = body["public_b64u"]
        raw = b64url_decode(pk)
        self.assertEqual(len(raw), 65, "P-256 uncompressed point must be 65 bytes")
        self.assertEqual(raw[0], 0x04, "uncompressed point prefix must be 0x04")
        # Calling again must return the SAME key (persisted in data/vapid.json).
        st2, body2 = self._request("GET", "push_pubkey")
        self.assertEqual(st2, 200)
        self.assertEqual(body["public_b64u"], body2["public_b64u"])

    # ----- subscribe / unsubscribe -----

    def _fake_token_hash(self, seed: bytes = b"test-token") -> str:
        return hashlib.sha256(seed).hexdigest()

    def _fake_trigger_pair(self, seed: bytes = b"test-token"):
        # Treat the seed-hash as the token's bytes; derive the matching
        # trigger pair via the same primitives the real code uses, so
        # the relay's verify_trigger_secret accepts our notify calls.
        tok = hashlib.sha256(seed).digest()
        return (
            crypto.trigger_id_for(tok).hex(),
            crypto.derive_trigger_secret(tok).hex(),
        )

    def test_subscribe_rejects_bad_token_hash(self):
        tid, _ = self._fake_trigger_pair()
        st, _ = self._request("POST", "push_subscribe", body={
            "token_hash": "nothex",
            "trigger_id_hex": tid,
            "endpoint": "https://fcm.googleapis.com/abc",
            "keys": {"p256dh": "x" * 88, "auth": "y" * 22},
        })
        self.assertEqual(st, 400)

    def test_subscribe_rejects_at_smuggling(self):
        # parse_url is host-ambiguous on "https://user@evil@fcm.../..."
        # across PHP versions; any '@' in the endpoint is a structural
        # bypass attempt regardless of dev mode.
        tid, _ = self._fake_trigger_pair()
        st, _ = self._request("POST", "push_subscribe", body={
            "token_hash": self._fake_token_hash(),
            "trigger_id_hex": tid,
            "endpoint": "https://user@fcm.googleapis.com/abc",
            "keys": {"p256dh": "x" * 88, "auth": "y" * 22},
        })
        self.assertEqual(st, 400)

    def test_subscribe_rejects_endpoint_over_1024_chars(self):
        tid, _ = self._fake_trigger_pair()
        st, _ = self._request("POST", "push_subscribe", body={
            "token_hash": self._fake_token_hash(),
            "trigger_id_hex": tid,
            "endpoint": "https://fcm.googleapis.com/" + "a" * 1100,
            "keys": {"p256dh": "x" * 88, "auth": "y" * 22},
        })
        self.assertEqual(st, 400)

    def test_subscribe_rejects_missing_keys(self):
        tid, _ = self._fake_trigger_pair()
        st, _ = self._request("POST", "push_subscribe", body={
            "token_hash": self._fake_token_hash(),
            "trigger_id_hex": tid,
            "endpoint": "https://fcm.googleapis.com/abc",
            "keys": {},
        })
        self.assertEqual(st, 400)

    def test_subscribe_rejects_missing_trigger_id(self):
        # No trigger_id_hex in body → 400.
        st, _ = self._request("POST", "push_subscribe", body={
            "token_hash": self._fake_token_hash(),
            "endpoint": "https://fcm.googleapis.com/abc",
            "keys": {"p256dh": "p" * 88, "auth": "a" * 22},
        })
        self.assertEqual(st, 400)

    def test_subscribe_rejects_mismatched_trigger_id(self):
        # First subscribe binds the token_hash to trigger_id A. A second
        # subscribe with the same hash but a different trigger_id is
        # squatting (different device claiming the same hash) → 401.
        th = self._fake_token_hash(b"mismatch")
        tid_a, _ = self._fake_trigger_pair(b"mismatch-tok-a")
        tid_b, _ = self._fake_trigger_pair(b"mismatch-tok-b")
        st, _ = self._request("POST", "push_subscribe", body={
            "token_hash": th, "trigger_id_hex": tid_a,
            "endpoint": "https://example-a.test/", "keys": {"p256dh": "p" * 88, "auth": "a" * 22},
        })
        self.assertEqual(st, 200)
        st, _ = self._request("POST", "push_subscribe", body={
            "token_hash": th, "trigger_id_hex": tid_b,
            "endpoint": "https://example-b.test/", "keys": {"p256dh": "p" * 88, "auth": "a" * 22},
        })
        self.assertEqual(st, 401)

    def test_subscribe_then_unsubscribe(self):
        th = self._fake_token_hash(b"sub1")
        tid, _ = self._fake_trigger_pair(b"sub1")
        st, body = self._request("POST", "push_subscribe", body={
            "token_hash": th,
            "trigger_id_hex": tid,
            "endpoint": "https://example.test/sub1",
            "keys": {"p256dh": "p" * 88, "auth": "a" * 22},
        })
        self.assertEqual(st, 200, body)
        sub_id = body["id"]
        self.assertRegex(sub_id, r"^[a-f0-9]{32}$")
        f = self._data / "push" / th / f"{sub_id}.json"
        self.assertTrue(f.exists())
        rec = json.loads(f.read_text())
        self.assertEqual(rec["endpoint"], "https://example.test/sub1")
        # Same endpoint subscribed again → same id.
        st, body2 = self._request("POST", "push_subscribe", body={
            "token_hash": th,
            "trigger_id_hex": tid,
            "endpoint": "https://example.test/sub1",
            "keys": {"p256dh": "p" * 88, "auth": "a" * 22},
        })
        self.assertEqual(st, 200)
        self.assertEqual(body2["id"], sub_id)
        # Unsubscribe.
        st, _ = self._request("POST", "push_unsubscribe", body={
            "token_hash": th, "id": sub_id,
        })
        self.assertEqual(st, 200)
        self.assertFalse(f.exists())

    # ----- notify (with mock push service) -----

    def test_notify_sends_to_subscribed_endpoints(self):
        MockPushHandler.captured.clear()
        push_srv = MockPushServer()
        try:
            th = self._fake_token_hash(b"notify-test")
            tid, secret = self._fake_trigger_pair(b"notify-test")
            # Under PUSH_ALLOW_INSECURE_DEV the local http://127.0.0.1
            # mock is accepted, so we can use the real subscribe path
            # end-to-end rather than dropping files into the data dir.
            for path in ["/p1", "/p2"]:
                st, _ = self._request("POST", "push_subscribe", body={
                    "token_hash": th,
                    "trigger_id_hex": tid,
                    "endpoint": push_srv.url(path),
                    "keys": {"p256dh": "p" * 88, "auth": "a" * 22},
                })
                self.assertEqual(st, 200)

            st, body = self._request("POST", "push_notify",
                                     body={"token_hash": th,
                                           "trigger_secret_hex": secret})
            self.assertEqual(st, 200, body)
            self.assertEqual(body.get("sent"), 2, body)

            # Each captured request must carry a VAPID Authorization header.
            self.assertEqual(len(MockPushHandler.captured), 2)
            for cap in MockPushHandler.captured:
                self.assertTrue(cap["auth"].startswith("vapid t="),
                                f"missing VAPID auth: {cap['auth']!r}")
                # Header form: "vapid t=<jwt>, k=<base64url-pubkey>"
                self.assertIn(", k=", cap["auth"])
                self.assertEqual(cap["body_len"], 0,
                                 "v1 sends content-free pushes")
        finally:
            push_srv.shutdown()

    def test_notify_no_subs_returns_sent_zero(self):
        th = self._fake_token_hash(b"empty")
        _, secret = self._fake_trigger_pair(b"empty")
        st, body = self._request("POST", "push_notify",
                                 body={"token_hash": th,
                                       "trigger_secret_hex": secret})
        self.assertEqual(st, 200, body)
        self.assertEqual(body.get("sent", 0), 0)

    def test_notify_rejects_bad_token_hash(self):
        _, secret = self._fake_trigger_pair()
        st, _ = self._request("POST", "push_notify",
                              body={"token_hash": "bad",
                                    "trigger_secret_hex": secret})
        self.assertEqual(st, 400)

    def test_notify_rejects_wrong_trigger_secret(self):
        # Subscribe with one token's trigger pair, then notify with a
        # different token's secret → 401.
        th = self._fake_token_hash(b"wrong-trigger")
        tid_real, _ = self._fake_trigger_pair(b"wrong-trigger-real")
        _, secret_wrong = self._fake_trigger_pair(b"wrong-trigger-fake")
        st, _ = self._request("POST", "push_subscribe", body={
            "token_hash": th, "trigger_id_hex": tid_real,
            "endpoint": "https://example.test/x",
            "keys": {"p256dh": "p" * 88, "auth": "a" * 22},
        })
        self.assertEqual(st, 200)
        st, _ = self._request("POST", "push_notify",
                              body={"token_hash": th,
                                    "trigger_secret_hex": secret_wrong})
        self.assertEqual(st, 401)

    def test_notify_rejects_missing_trigger_secret(self):
        # No trigger_secret_hex in body → 400 (require_hex_64 fails).
        th = self._fake_token_hash(b"missing")
        st, _ = self._request("POST", "push_notify",
                              body={"token_hash": th})
        self.assertEqual(st, 400)


class PushEndpointStrictTest(unittest.TestCase):
    """SSRF defense: with PUSH_ALLOW_INSECURE_DEV unset, only real
    push-service hostnames + public IPs are accepted by subscribe.

    Runs on a separate port from PushRelayTest so the two configs
    don't collide. Skipped if DNS isn't available (gethostbynamel on
    fcm.googleapis.com must succeed for the one positive test)."""

    STRICT_PORT = 6030

    @classmethod
    def setUpClass(cls):
        cls.docroot = ROOT / "relay"
        cls._orig_config = cls.docroot / "config.php"
        cls._backup_config = (
            cls._orig_config.with_suffix(".bak.push_strict")
            if cls._orig_config.exists() else None
        )
        if cls._backup_config:
            shutil.move(str(cls._orig_config), str(cls._backup_config))
        (cls.docroot / "config.php").write_text(
            f"<?php\n"
            f"define('RELAY_SECRET', '{RELAY_SECRET}');\n"
            # Deliberately NO PUSH_ALLOW_INSECURE_DEV — strict mode.
        )
        cls._data = cls.docroot / "data"
        if cls._data.exists():
            shutil.rmtree(cls._data)
        cls.php_proc = subprocess.Popen(
            ["php", "-S", f"127.0.0.1:{cls.STRICT_PORT}", "-t", str(cls.docroot)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not wait_port("127.0.0.1", cls.STRICT_PORT, 5):
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

    def _subscribe(self, endpoint: str) -> int:
        th = hashlib.sha256(b"ssrf-test").hexdigest()
        tok = hashlib.sha256(b"ssrf-test-token").digest()
        tid = crypto.trigger_id_for(tok).hex()
        body = json.dumps({
            "token_hash": th,
            "trigger_id_hex": tid,
            "endpoint": endpoint,
            "keys": {"p256dh": "p" * 88, "auth": "a" * 22},
        }).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.STRICT_PORT, timeout=10)
        try:
            c.request("POST", "/relay.php?op=push_subscribe", body=body, headers={
                "Authorization": "Bearer " + RELAY_SECRET,
                "Content-Type": "application/json",
            })
            r = c.getresponse()
            r.read()
            return r.status
        finally:
            c.close()

    def test_rejects_http_scheme(self):
        self.assertEqual(self._subscribe("http://fcm.googleapis.com/abc"), 400)

    def test_rejects_unknown_host(self):
        self.assertEqual(self._subscribe("https://evil.example.com/abc"), 400)

    def test_rejects_at_smuggling(self):
        self.assertEqual(
            self._subscribe("https://user@evil.com@fcm.googleapis.com/abc"), 400)

    def test_rejects_non_443_port(self):
        self.assertEqual(self._subscribe("https://fcm.googleapis.com:8080/abc"), 400)

    def test_rejects_link_local_literal(self):
        # gethostbynamel('169.254.169.254') returns the literal IP; the
        # IP filter then rejects link-local.
        self.assertEqual(self._subscribe("https://169.254.169.254/abc"), 400)

    def test_rejects_loopback_literal(self):
        self.assertEqual(self._subscribe("https://127.0.0.1/abc"), 400)

    def test_accepts_fcm_endpoint(self):
        # Requires DNS. If it fails (offline CI), skip rather than fail.
        try:
            socket.gethostbyname("fcm.googleapis.com")
        except OSError:
            self.skipTest("DNS unavailable; skipping positive subscribe test")
        # Real fcm endpoint URL format.
        st = self._subscribe(
            "https://fcm.googleapis.com/fcm/send/ABC123_test")
        self.assertEqual(st, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
