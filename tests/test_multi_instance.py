#!/usr/bin/env python3
"""
Multi-instance-per-cwd tests.

Covers resolve_instance() (the four-step label resolver) and the new
per-instance layout under ~/.cache/termpilot/cwd/<hash>/<instance>/.

These tests poke termpilot-wrap's internals directly without spawning
the relay — the goal is to lock down the resilience-slot semantics, not
to retest the full register/POST loop.

For the TTY-based cases we fabricate ptys via pty.openpty() and dup2
them onto fd 0 around the call. fd 0 is restored in a finally.
"""
from __future__ import annotations

import importlib.util
import os
import pty
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load termpilot-wrap as a module (no .py extension).
from importlib.machinery import SourceFileLoader  # noqa: E402

_loader = SourceFileLoader("ccwrap", str(ROOT / "termpilot-wrap"))
_spec = importlib.util.spec_from_loader("ccwrap", _loader)
ccwrap = importlib.util.module_from_spec(_spec)
_loader.exec_module(ccwrap)


# Helpers -------------------------------------------------------------------


class _Stdin:
    """Context manager: temporarily install `fd` as the process's stdin (fd 0)."""

    def __init__(self, fd: int):
        self.fd = fd
        self._saved = -1

    def __enter__(self):
        self._saved = os.dup(0)
        os.dup2(self.fd, 0)
        return self

    def __exit__(self, *exc):
        os.dup2(self._saved, 0)
        os.close(self._saved)
        return False


def _make_pty() -> tuple[int, int]:
    """Allocate a (master, slave) pty pair. Caller closes both."""
    return pty.openpty()


# resolve_instance() tests --------------------------------------------------


class ResolveInstanceTests(unittest.TestCase):
    """Resolution order: --instance arg > $TERMPILOT_INSTANCE > TTY > 'default'."""

    def setUp(self):
        # Strip the env var so test cases set it deliberately.
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("TERMPILOT_INSTANCE", None)

    def tearDown(self):
        self._env_patch.stop()

    def test_explicit_arg_wins_over_everything(self):
        os.environ["TERMPILOT_INSTANCE"] = "from-env"
        master, slave = _make_pty()
        try:
            with _Stdin(slave):
                self.assertEqual(ccwrap.resolve_instance("from-arg"), "from-arg")
        finally:
            os.close(master); os.close(slave)

    def test_env_var_wins_over_tty(self):
        os.environ["TERMPILOT_INSTANCE"] = "from-env"
        master, slave = _make_pty()
        try:
            with _Stdin(slave):
                self.assertEqual(ccwrap.resolve_instance(None), "from-env")
        finally:
            os.close(master); os.close(slave)

    def test_tty_derived_when_no_arg_no_env(self):
        master, slave = _make_pty()
        try:
            with _Stdin(slave):
                label = ccwrap.resolve_instance(None)
            # Linux: pts-N. macOS: ttysNNN. Either way, never raw '/dev/...'.
            self.assertNotIn("/", label)
            self.assertTrue(label.startswith("pts-") or label.startswith("ttys"),
                            f"unexpected tty label: {label!r}")
        finally:
            os.close(master); os.close(slave)

    def test_two_ptys_yield_different_labels(self):
        ma, sa = _make_pty()
        mb, sb = _make_pty()
        try:
            with _Stdin(sa):
                label_a = ccwrap.resolve_instance(None)
            with _Stdin(sb):
                label_b = ccwrap.resolve_instance(None)
            self.assertNotEqual(label_a, label_b,
                                "two distinct ptys must give two distinct slot labels")
        finally:
            for fd in (ma, sa, mb, sb):
                os.close(fd)

    def test_non_tty_stdin_falls_back_to_default(self):
        devnull = os.open("/dev/null", os.O_RDONLY)
        try:
            with _Stdin(devnull):
                self.assertEqual(ccwrap.resolve_instance(None), "default")
        finally:
            os.close(devnull)

    def test_validation_rejects_bad_labels(self):
        # NB: empty string is intentionally treated as "no value" and falls
        # through to the next resolution step (mirrors $VAR= ~ unset in shells).
        for bad in ("../escape", "name with space", "a/b", ".", "..", "x" * 65,
                    "with\nnewline", "tab\there"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ccwrap.resolve_instance(bad)

    def test_validation_accepts_typical_labels(self):
        for good in ("default", "pts-3", "ttys003", "work", "staging.eu",
                     "a_b-c.d", "X" * 64):
            with self.subTest(good=good):
                self.assertEqual(ccwrap.resolve_instance(good), good)


# Per-instance lock + active.json tests -------------------------------------


class PerInstanceSlotTests(unittest.TestCase):
    """Two wrappers in the same cwd with different instance labels must
    not collide on the lock or on active.json. Same (cwd, instance)
    behaves exactly like today's per-cwd lock."""

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="termpilot-mi-test-"))
        self._orig_cache = ccwrap.CACHE_BASE
        ccwrap.CACHE_BASE = str(self.cache)
        self.cwd = "/tmp/multi-instance-cwd"

    def tearDown(self):
        ccwrap.CACHE_BASE = self._orig_cache
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_two_instances_lock_independently(self):
        fd_a = ccwrap.acquire_wrapper_lock(self.cwd, "alpha")
        fd_b = ccwrap.acquire_wrapper_lock(self.cwd, "beta")
        try:
            self.assertIsNotNone(fd_a)
            self.assertIsNotNone(fd_b, "different instance must NOT collide on lock")
            # Lock dirs are siblings under the same cwd-hash dir.
            base = Path(self.cache) / "cwd" / ccwrap._encode_cwd(self.cwd)
            self.assertTrue((base / "alpha" / "wrapper.lock").is_file())
            self.assertTrue((base / "beta" / "wrapper.lock").is_file())
        finally:
            ccwrap.release_wrapper_lock(fd_a)
            ccwrap.release_wrapper_lock(fd_b)

    def test_same_instance_still_blocks(self):
        fd1 = ccwrap.acquire_wrapper_lock(self.cwd, "alpha")
        fd2 = ccwrap.acquire_wrapper_lock(self.cwd, "alpha")
        try:
            self.assertIsNotNone(fd1)
            self.assertIsNone(fd2, "same (cwd, instance) must collide")
        finally:
            ccwrap.release_wrapper_lock(fd1)
            if fd2 is not None:
                ccwrap.release_wrapper_lock(fd2)

    def test_active_json_is_independent_per_instance(self):
        ccwrap.write_active_json(self.cwd, "alpha", wrapper_sid="sid-alpha",
                                 marker_b64="ma")
        ccwrap.write_active_json(self.cwd, "beta", wrapper_sid="sid-beta",
                                 marker_b64="mb")
        a = ccwrap.read_active_json(self.cwd, "alpha")
        b = ccwrap.read_active_json(self.cwd, "beta")
        self.assertEqual(a.get("wrapper_sid"), "sid-alpha")
        self.assertEqual(a.get("marker_b64"), "ma")
        self.assertEqual(b.get("wrapper_sid"), "sid-beta")
        self.assertEqual(b.get("marker_b64"), "mb")
        # Clearing one must not touch the other.
        ccwrap.clear_active_json(self.cwd, "alpha")
        self.assertEqual(ccwrap.read_active_json(self.cwd, "alpha"), {})
        b2 = ccwrap.read_active_json(self.cwd, "beta")
        self.assertEqual(b2.get("wrapper_sid"), "sid-beta")

    def test_cwd_cache_dir_path_layout(self):
        # Sanity-check the path the rest of the system reads from.
        h = ccwrap._encode_cwd(self.cwd)
        self.assertEqual(
            ccwrap.cwd_cache_dir(self.cwd, "alpha"),
            os.path.join(str(self.cache), "cwd", h, "alpha"),
        )


# Legacy cleanup test -------------------------------------------------------


class LegacyCwdArtifactCleanupTests(unittest.TestCase):
    """The pre-instance layout put active.json + wrapper.lock directly under
    cwd/<hash>/. Sweep these once they're stale, but only then."""

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="termpilot-legacy-test-"))
        self._orig_cache = ccwrap.CACHE_BASE
        ccwrap.CACHE_BASE = str(self.cache)

    def tearDown(self):
        ccwrap.CACHE_BASE = self._orig_cache
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_stale_legacy_artifacts_removed(self):
        h = "deadbeefdeadbeef"
        legacy_dir = Path(self.cache) / "cwd" / h
        legacy_dir.mkdir(parents=True)
        legacy_active = legacy_dir / "active.json"
        legacy_lock = legacy_dir / "wrapper.lock"
        legacy_active.write_text("{}")
        legacy_lock.write_text("123\n")
        # Backdate both files past the 7-day cap.
        old = (legacy_dir.stat().st_mtime) - (10 * 24 * 3600)
        os.utime(legacy_active, (old, old))
        os.utime(legacy_lock, (old, old))

        ccwrap.cleanup_legacy_cwd_artifacts()

        self.assertFalse(legacy_active.exists())
        self.assertFalse(legacy_lock.exists())

    def test_fresh_legacy_artifacts_kept(self):
        # A wrapper from the previous version that just exited shouldn't
        # have its in-window crash-recovery state nuked by the sweeper.
        h = "cafebabecafebabe"
        legacy_dir = Path(self.cache) / "cwd" / h
        legacy_dir.mkdir(parents=True)
        legacy_active = legacy_dir / "active.json"
        legacy_active.write_text("{}")

        ccwrap.cleanup_legacy_cwd_artifacts()

        self.assertTrue(legacy_active.exists(),
                        "recently-modified legacy active.json must be kept")


if __name__ == "__main__":
    unittest.main(verbosity=2)
