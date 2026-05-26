"""Unit tests for termpilot_mcp/core.py.

Each TestCase builds a fake CACHE_BASE under a tmp dir and rebinds the
module-level constant for the duration. No live wrapper is required;
`collect_wrapper_inventory` is monkeypatched where needed.
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

from termpilot_mcp import core


def _write_frame(fh, payload):
    fh.write(struct.pack("<I", len(payload)) + payload)


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class _CacheBaseFixture(unittest.TestCase):
    """Builds a fake CACHE_BASE under tmp and exposes a fake sid dir."""

    sid = "abc123def456"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tp_mcp_test_")
        self.sid_dir = os.path.join(self.tmp, "sid", self.sid)
        os.makedirs(self.sid_dir)
        self._orig_cache = core.CACHE_BASE
        core.CACHE_BASE = self.tmp
        self.addCleanup(self._restore)

    def _restore(self):
        core.CACHE_BASE = self._orig_cache
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spool(self):
        return os.path.join(self.sid_dir, "out.spool")

    def in_local(self):
        return os.path.join(self.sid_dir, "in.local")


class TestSpoolReading(_CacheBaseFixture):

    def test_missing_spool_returns_empty(self):
        data, off, n = core._scan_tail(self.spool(), 32768)
        self.assertEqual((data, off, n), (b"", 0, 0))
        data, off, n = core._read_spool_range(self.sid, 0, 32768)
        self.assertEqual((data, off, n), (b"", 0, 0))

    def test_read_single_frame(self):
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"hello world")
        data, off, n = core._read_spool_range(self.sid, 0, 32768)
        self.assertEqual(data, b"hello world")
        self.assertEqual(n, 1)
        self.assertEqual(off, 4 + 11)

    def test_offset_tracking_resumes(self):
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"first")
            _write_frame(f, b"second")
        data1, off1, _ = core._read_spool_range(self.sid, 0, 32768)
        self.assertEqual(data1, b"firstsecond")
        # Append another frame
        with open(self.spool(), "ab") as f:
            _write_frame(f, b"third")
        data2, off2, n2 = core._read_spool_range(self.sid, off1, 32768)
        self.assertEqual(data2, b"third")
        self.assertEqual(n2, 1)

    def test_scan_tail_trims_old_frames(self):
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"X" * 300)
            _write_frame(f, b"keep me")
        data, _, n = core._scan_tail(self.spool(), max_bytes=10)
        self.assertEqual(data, b"keep me")
        self.assertEqual(n, 1)

    def test_scan_tail_keeps_at_least_one(self):
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"X" * 100)
        data, _, n = core._scan_tail(self.spool(), max_bytes=10)
        # Single oversized frame is kept rather than returning empty
        self.assertEqual(len(data), 100)
        self.assertEqual(n, 1)

    def test_truncated_tail_stops_cleanly(self):
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"complete")
            f.write(struct.pack("<I", 100))  # claims 100 bytes
            f.write(b"only-5")                # writes 6
        data, off, n = core._read_spool_range(self.sid, 0, 32768)
        self.assertEqual(data, b"complete")
        self.assertEqual(n, 1)
        # Offset stops at end of the last complete frame
        self.assertEqual(off, 4 + 8)


class TestAnsiStrip(unittest.TestCase):

    def test_csi_color(self):
        self.assertEqual(core._ansi_strip(b"\x1b[31mhello\x1b[0m"), b"hello")

    def test_cursor_position(self):
        self.assertEqual(core._ansi_strip(b"a\x1b[2;5Hb"), b"ab")

    def test_osc_title_bel_terminated(self):
        self.assertEqual(core._ansi_strip(b"\x1b]0;title\x07after"), b"after")

    def test_osc_title_st_terminated(self):
        self.assertEqual(core._ansi_strip(b"\x1b]0;title\x1b\\after"), b"after")

    def test_keeps_newlines_tabs_cr(self):
        self.assertEqual(core._ansi_strip(b"a\nb\tc\rd"), b"a\nb\tc\rd")

    def test_strips_other_control_chars(self):
        self.assertEqual(core._ansi_strip(b"a\x00b\x07c\x7fd"), b"abcd")


class _FakeInventoryMixin:
    """Pretend a wrapper is alive at the test's own pid for sid `self.sid`."""

    def install_fake_inventory(self, alive=True):
        self._orig_inv = core.collect_wrapper_inventory
        sid = self.sid
        pid = os.getpid()

        def fake():
            return [{
                "sid": sid, "pid": pid, "alive": alive, "ts": int(time.time()),
                "instance": "test", "cwd_hash": "x" * 16, "cwd": "/tmp",
                "cmdline": "fake", "from_cache": True,
            }]
        core.collect_wrapper_inventory = fake
        self.addCleanup(self._restore_inv)

    def _restore_inv(self):
        core.collect_wrapper_inventory = self._orig_inv


class TestSendInput(_CacheBaseFixture, _FakeInventoryMixin):

    def setUp(self):
        super().setUp()
        open(self.in_local(), "wb").close()
        self.install_fake_inventory(alive=True)

    def test_appends_text_plus_cr(self):
        r = core.send_input(self.sid, "hello")
        self.assertEqual(_read_bytes(self.in_local()), b"hello\r")
        self.assertEqual(r["bytes_written"], 6)

    def test_no_newline_skips_cr(self):
        core.send_input(self.sid, "raw", newline=False)
        self.assertEqual(_read_bytes(self.in_local()), b"raw")

    def test_idempotent_cr_not_duplicated(self):
        core.send_input(self.sid, "already-cr\r")
        self.assertEqual(_read_bytes(self.in_local()), b"already-cr\r")

    def test_unknown_sid_raises(self):
        with self.assertRaises(RuntimeError):
            core.send_input("doesnotexist", "hi")

    def test_dead_wrapper_raises(self):
        self._restore_inv()
        self.install_fake_inventory(alive=False)
        with self.assertRaises(RuntimeError):
            core.send_input(self.sid, "hi")

    def test_missing_in_local_raises(self):
        os.unlink(self.in_local())
        with self.assertRaises(RuntimeError) as cm:
            core.send_input(self.sid, "hi")
        self.assertIn("local-input support", str(cm.exception))


class TestSendKey(_CacheBaseFixture, _FakeInventoryMixin):

    def setUp(self):
        super().setUp()
        open(self.in_local(), "wb").close()
        self.install_fake_inventory(alive=True)

    def test_named_key_esc_writes_escape_byte(self):
        core.send_key(self.sid, "Esc")
        self.assertEqual(_read_bytes(self.in_local()), b"\x1b")

    def test_named_key_shifttab(self):
        core.send_key(self.sid, "ShiftTab")
        self.assertEqual(_read_bytes(self.in_local()), b"\x1b[Z")

    def test_named_key_backspace_no_trailing_cr(self):
        core.send_key(self.sid, "Backspace")
        self.assertEqual(_read_bytes(self.in_local()), b"\x7f")

    def test_ctrl_c_writes_03(self):
        core.send_key(self.sid, "Ctrl-C")
        self.assertEqual(_read_bytes(self.in_local()), b"\x03")

    def test_case_insensitive_and_separators(self):
        core.send_key(self.sid, "shift_tab")
        self.assertEqual(_read_bytes(self.in_local()), b"\x1b[Z")

    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            core.send_key(self.sid, "Mehkey")

    def test_function_key_f5(self):
        core.send_key(self.sid, "F5")
        self.assertEqual(_read_bytes(self.in_local()), b"\x1b[15~")


class TestKeyComboResolution(unittest.TestCase):
    """resolve_key() handles modifier prefixes without touching the filesystem."""

    def test_ctrl_left_uses_csi_modifier(self):
        self.assertEqual(core.resolve_key("Ctrl-Left"), "\x1b[1;5D")

    def test_ctrl_right(self):
        self.assertEqual(core.resolve_key("Ctrl-Right"), "\x1b[1;5C")

    def test_ctrl_up_and_down(self):
        self.assertEqual(core.resolve_key("Ctrl-Up"),   "\x1b[1;5A")
        self.assertEqual(core.resolve_key("Ctrl-Down"), "\x1b[1;5B")

    def test_shift_arrow(self):
        # mod_code = 1 + shift(1) = 2
        self.assertEqual(core.resolve_key("Shift-Left"), "\x1b[1;2D")

    def test_ctrl_shift_up_stacks(self):
        # mod_code = 1 + shift(1) + ctrl(4) = 6
        self.assertEqual(core.resolve_key("Ctrl-Shift-Up"), "\x1b[1;6A")

    def test_modifier_order_does_not_matter(self):
        self.assertEqual(
            core.resolve_key("Shift-Ctrl-Up"),
            core.resolve_key("Ctrl-Shift-Up"),
        )

    def test_alt_letter_uses_esc_prefix(self):
        self.assertEqual(core.resolve_key("Alt-a"), "\x1ba")

    def test_alt_uppercase_letter(self):
        # Alt + Shift + a applies shift to letter first → A, then alt prefix.
        self.assertEqual(core.resolve_key("Alt-Shift-a"), "\x1bA")

    def test_alt_arrow(self):
        # mod_code = 1 + alt(2) = 3
        self.assertEqual(core.resolve_key("Alt-Left"), "\x1b[1;3D")

    def test_ctrl_alt_letter(self):
        # Ctrl on letter first → 0x01, then alt prefix
        self.assertEqual(core.resolve_key("Ctrl-Alt-A"), "\x1b\x01")

    def test_modifier_on_csi_tilde(self):
        # PgUp with Ctrl = `\e[5;5~`
        self.assertEqual(core.resolve_key("Ctrl-PgUp"), "\x1b[5;5~")
        # F5 with Alt = `\e[15;3~`
        self.assertEqual(core.resolve_key("Alt-F5"), "\x1b[15;3~")

    def test_f1_with_modifier_upgrades_from_ss3_to_csi(self):
        # F1 plain = `\eOP`; F1 with Shift = `\e[1;2P`
        self.assertEqual(core.resolve_key("Shift-F1"), "\x1b[1;2P")

    def test_direct_hit_wins_over_parser(self):
        # Ctrl-A is in NAMED_KEYS directly; parser would also produce
        # 0x01 but the direct hit must return same.
        self.assertEqual(core.resolve_key("Ctrl-A"), "\x01")

    def test_case_insensitive_modifier_names(self):
        self.assertEqual(core.resolve_key("ctrl-LEFT"), "\x1b[1;5D")
        self.assertEqual(core.resolve_key("CTRL-shift-left"), "\x1b[1;6D")

    def test_unknown_base_returns_none(self):
        self.assertIsNone(core.resolve_key("Ctrl-FloofKey"))

    def test_no_modifier_lookup_falls_through(self):
        # 'Foo-Bar' has neither part as a modifier → None
        self.assertIsNone(core.resolve_key("Foo-Bar"))


class TestSendKeyCombo(_CacheBaseFixture, _FakeInventoryMixin):
    """End-to-end: send_key with combos actually writes the right bytes."""

    def setUp(self):
        super().setUp()
        open(self.in_local(), "wb").close()
        self.install_fake_inventory(alive=True)

    def test_send_ctrl_left(self):
        core.send_key(self.sid, "Ctrl-Left")
        self.assertEqual(_read_bytes(self.in_local()), b"\x1b[1;5D")

    def test_send_alt_a(self):
        core.send_key(self.sid, "Alt-a")
        self.assertEqual(_read_bytes(self.in_local()), b"\x1ba")

    def test_send_ctrl_shift_up(self):
        core.send_key(self.sid, "Ctrl-Shift-Up")
        self.assertEqual(_read_bytes(self.in_local()), b"\x1b[1;6A")

    def test_unknown_combo_raises(self):
        with self.assertRaises(ValueError):
            core.send_key(self.sid, "Ctrl-Floofy")


class TestSendSignal(_CacheBaseFixture, _FakeInventoryMixin):

    def setUp(self):
        super().setUp()
        open(self.in_local(), "wb").close()
        self.install_fake_inventory(alive=True)

    def test_sigint_writes_ctrl_c(self):
        r = core.send_signal(self.sid, "SIGINT")
        self.assertEqual(_read_bytes(self.in_local()), b"\x03")
        self.assertEqual(r["method"], "ctrl_char")

    def test_sigquit_writes_ctrl_backslash(self):
        core.send_signal(self.sid, "SIGQUIT")
        self.assertEqual(_read_bytes(self.in_local()), b"\x1c")

    def test_sigtstp_writes_ctrl_z(self):
        core.send_signal(self.sid, "SIGTSTP")
        self.assertEqual(_read_bytes(self.in_local()), b"\x1a")

    def test_lowercase_signal_name_accepted(self):
        core.send_signal(self.sid, "sigint")
        self.assertEqual(_read_bytes(self.in_local()), b"\x03")

    def test_unsupported_signal_raises(self):
        with self.assertRaises(ValueError):
            core.send_signal(self.sid, "SIGKILL")


class TestTailEvents(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tp_mcp_test_")
        self.log = os.path.join(self.tmp, "events.log")
        recs = [
            {"ts": "2026-05-26T10:00:00Z", "cat": "wrapper_start", "sid": "a", "pid": 1},
            {"ts": "2026-05-26T10:01:00Z", "cat": "local_input", "sid": "a", "n": 5},
            {"ts": "2026-05-26T10:02:00Z", "cat": "wrapper_start", "sid": "b", "pid": 2},
        ]
        with open(self.log, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        self._orig_log = core.EVENT_LOG
        core.EVENT_LOG = self.log
        self.addCleanup(self._restore)

    def _restore(self):
        core.EVENT_LOG = self._orig_log
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_filter_by_sid(self):
        recs = core.tail_events(sid="a")
        self.assertEqual(len(recs), 2)
        self.assertTrue(all(r["sid"] == "a" for r in recs))

    def test_filter_by_cat(self):
        recs = core.tail_events(cat=["wrapper_start"])
        self.assertEqual({r["sid"] for r in recs}, {"a", "b"})

    def test_filter_by_ts(self):
        recs = core.tail_events(since_ts="2026-05-26T10:01:30Z")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["sid"], "b")

    def test_limit_keeps_most_recent(self):
        recs = core.tail_events(limit=1)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["sid"], "b")

    def test_missing_log_returns_empty(self):
        os.unlink(self.log)
        self.assertEqual(core.tail_events(), [])


class TestRenderScreen(_CacheBaseFixture):
    """pyte-rendered screen view of a spool."""

    def _skip_if_no_pyte(self):
        try:
            import pyte  # noqa: F401
        except ImportError:
            self.skipTest("pyte not installed in this environment")

    def test_plain_text_renders_unchanged(self):
        self._skip_if_no_pyte()
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"hello\r\nworld\r\n")
        r = core.render_screen(self.sid, cols=40, rows=4)
        self.assertEqual(r["lines"][0], "hello")
        self.assertEqual(r["lines"][1], "world")
        self.assertEqual(r["size"], {"cols": 40, "rows": 4})

    def test_in_place_redraw_collapses_to_latest(self):
        self._skip_if_no_pyte()
        # Three writes of "spinner X" at the same line position via \r.
        # Stream-mode reading would see all three; screen mode shows only
        # the last.
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"spinner 1\r")
            _write_frame(f, b"spinner 2\r")
            _write_frame(f, b"spinner 3")
        r = core.render_screen(self.sid, cols=20, rows=2)
        self.assertEqual(r["lines"][0], "spinner 3")

    def test_cursor_position_tracked(self):
        self._skip_if_no_pyte()
        with open(self.spool(), "wb") as f:
            # Move cursor to row 2 col 5 (1-indexed in CSI H)
            _write_frame(f, b"\x1b[2;5H")
        r = core.render_screen(self.sid, cols=20, rows=4)
        # pyte uses 0-indexed cursor coords internally
        self.assertEqual(r["cursor"], {"x": 4, "y": 1})

    def test_ansi_colors_dropped_from_text(self):
        self._skip_if_no_pyte()
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"\x1b[31mRED\x1b[0m text")
        r = core.render_screen(self.sid, cols=20, rows=2)
        self.assertEqual(r["lines"][0], "RED text")

    def test_missing_spool_returns_blank_screen(self):
        self._skip_if_no_pyte()
        # spool file doesn't exist for a fresh sid_dir
        r = core.render_screen(self.sid, cols=10, rows=2)
        self.assertEqual(r["lines"], ["", ""])
        self.assertEqual(r["frames_fed"], 0)

    def test_screen_as_text_joins_lines(self):
        rendered = {"lines": ["a", "b", "c"], "cursor": {"x": 0, "y": 0},
                    "size": {"cols": 1, "rows": 3}, "bytes_fed": 0,
                    "frames_fed": 0, "spool_end": 0}
        self.assertEqual(core.screen_as_text(rendered), "a\nb\nc")

    def test_keep_color_true_includes_lines_ansi(self):
        self._skip_if_no_pyte()
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"\x1b[31mRED\x1b[0m plain\r\n")
        r = core.render_screen(self.sid, cols=20, rows=2, keep_color=True)
        self.assertIn("lines_ansi", r)
        # Plain "lines" still has no ANSI codes
        self.assertEqual(r["lines"][0], "RED plain")
        # lines_ansi has the red SGR somewhere
        self.assertIn("\x1b[31m", r["lines_ansi"][0])
        self.assertIn("RED", r["lines_ansi"][0])

    def test_keep_color_false_omits_lines_ansi(self):
        self._skip_if_no_pyte()
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"\x1b[31mRED\x1b[0m\r\n")
        r = core.render_screen(self.sid, cols=20, rows=2)
        self.assertNotIn("lines_ansi", r)

    def test_screen_as_text_prefers_lines_ansi_when_present(self):
        rendered = {
            "lines": ["plain"],
            "lines_ansi": ["\x1b[31mplain\x1b[0m"],
            "cursor": {"x": 0, "y": 0},
            "size": {"cols": 5, "rows": 1},
            "bytes_fed": 0, "frames_fed": 0, "spool_end": 0,
        }
        self.assertEqual(core.screen_as_text(rendered), "\x1b[31mplain\x1b[0m")

    def test_hex_color_maps_to_truecolor_sgr(self):
        self._skip_if_no_pyte()
        # 38;2;215;119;87 = Claude's spinner orange (ff7755-ish hex)
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"\x1b[38;2;215;119;87mx\x1b[0m")
        r = core.render_screen(self.sid, cols=5, rows=1, keep_color=True)
        self.assertIn("38;2;215;119;87", r["lines_ansi"][0])


class TestReadOutput(_CacheBaseFixture):

    def test_tail_then_resume(self):
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"old line\n")
            _write_frame(f, b"newer line\n")
        r1 = core.read_output(self.sid, since=None, max_bytes=32, strip_ansi=False)
        self.assertEqual(r1["bytes"], "old line\nnewer line\n")
        self.assertEqual(r1["frames_read"], 2)
        with open(self.spool(), "ab") as f:
            _write_frame(f, b"third\n")
        r2 = core.read_output(self.sid, since=r1["new_offset"], strip_ansi=False)
        self.assertEqual(r2["bytes"], "third\n")
        self.assertEqual(r2["frames_read"], 1)

    def test_strips_ansi_by_default(self):
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"\x1b[31mred\x1b[0m\n")
        r = core.read_output(self.sid, since=0)
        self.assertEqual(r["bytes"], "red\n")

    def test_wait_secs_polls_then_returns(self):
        # No data; wait 0.3s; should return empty without error.
        t0 = time.monotonic()
        r = core.read_output(self.sid, since=0, wait_secs=0.3)
        elapsed = time.monotonic() - t0
        self.assertEqual(r["bytes"], "")
        self.assertGreater(elapsed, 0.25)


class TestWaiters(_CacheBaseFixture):

    def test_wait_for_idle_returns_on_silence(self):
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"working...\n")
        t0 = time.monotonic()
        r = core.wait_for_idle(self.sid, quiet_secs=0.5, timeout=5.0, since=0)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertEqual(r["bytes"], "working...\n")

    def test_wait_for_idle_timeout(self):
        import threading
        # Background writer that keeps the spool noisy
        stop = threading.Event()
        def noisy():
            while not stop.is_set():
                with open(self.spool(), "ab") as f:
                    _write_frame(f, b"x")
                time.sleep(0.05)
        t = threading.Thread(target=noisy, daemon=True)
        t.start()
        try:
            with self.assertRaises(TimeoutError):
                core.wait_for_idle(self.sid, quiet_secs=0.5, timeout=0.8, since=0)
        finally:
            stop.set()
            t.join(timeout=1)

    def test_wait_for_output_matches(self):
        with open(self.spool(), "wb") as f:
            _write_frame(f, b"loading\n")
            _write_frame(f, b"all done!\n")
        r = core.wait_for_output(self.sid, pattern=r"all done", timeout=2.0, since=0)
        self.assertEqual(r["match"], "all done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
