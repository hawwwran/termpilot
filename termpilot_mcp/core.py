"""Local data plane for termpilot orchestration.

Pure functions used by both the CLI adapter (`termpilot_mcp.cli`) and the
stdio MCP server (`termpilot_mcp.server`). Stays stdlib-only; the `mcp`
PyPI package is needed only by `server.py`.

Reuses helpers from `linux/termpilot-wrap` (loaded once via importlib) so
the data-plane stays a single source of truth: when the wrapper's session
state layout or relay protocol changes, this file inherits the new
behaviour for free.
"""

import importlib.machinery
import importlib.util
import json
import os
import pathlib
import re
import signal as _signal
import struct
import time

# Load the wrapper script (no .py extension) as the `tp_wrap` module to
# re-use its Relay client, session-inventory walker, and path constants.
# The wrapper has `if __name__ == "__main__":` at the bottom, so importing
# it is a pure-side-effect-free operation; module-level code only sets up
# constants and sys.path probes that we benefit from.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
# Dev tree keeps the wrapper under linux/; the release zip flattens it to
# the install root next to termpilot_mcp/. Probe both, matching the
# wrapper's own shared/ sys.path probe.
for _cand in (_REPO_ROOT / "linux" / "termpilot-wrap", _REPO_ROOT / "termpilot-wrap"):
    if _cand.is_file():
        _WRAP_PATH = _cand
        break
else:
    raise FileNotFoundError(
        f"termpilot-wrap not found in dev layout ({_REPO_ROOT}/linux/) "
        f"or flat layout ({_REPO_ROOT}/)"
    )
_loader = importlib.machinery.SourceFileLoader("tp_wrap", str(_WRAP_PATH))
_spec = importlib.util.spec_from_loader("tp_wrap", _loader)
_tp_wrap = importlib.util.module_from_spec(_spec)
_loader.exec_module(_tp_wrap)

# Re-exports. Rebound at module level so tests can monkeypatch
# `termpilot_mcp.core.CACHE_BASE` etc. without reaching into `tp_wrap`.
Relay = _tp_wrap.Relay
collect_wrapper_inventory = _tp_wrap.collect_wrapper_inventory
CACHE_BASE = _tp_wrap.CACHE_BASE
EVENT_LOG = _tp_wrap.EVENT_LOG


# ---------------------------------------------------------------------------
# Session listing
# ---------------------------------------------------------------------------

def list_sessions():
    """Return all termpilot wrappers currently registered on this machine.

    Each entry includes the fields produced by `collect_wrapper_inventory`
    plus `spool_size` (output bytes available locally) and `in_local_size`
    (whether the wrapper supports local input; None means no in.local
    file, i.e. a pre-MCP wrapper).
    """
    out = []
    for entry in collect_wrapper_inventory():
        sid = entry.get("sid", "")
        if not sid:
            continue
        e = dict(entry)
        sid_dir = os.path.join(CACHE_BASE, "sid", sid)
        try:
            e["spool_size"] = os.path.getsize(os.path.join(sid_dir, "out.spool"))
        except OSError:
            e["spool_size"] = None
        try:
            e["in_local_size"] = os.path.getsize(os.path.join(sid_dir, "in.local"))
        except OSError:
            e["in_local_size"] = None
        out.append(e)
    return out


def _entry_for_sid(sid):
    for e in collect_wrapper_inventory():
        if e.get("sid") == sid:
            return e
    return None


def _spool_path(sid):
    return os.path.join(CACHE_BASE, "sid", sid, "out.spool")


def _in_local_path(sid):
    return os.path.join(CACHE_BASE, "sid", sid, "in.local")


def _safe_getsize(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Output-spool reading
# ---------------------------------------------------------------------------
# Spool format is `[u32 LE length][payload bytes]` frames, append-only,
# never truncated by the wrapper in normal operation (see
# linux/termpilot-wrap:93). External readers track an absolute byte offset
# and walk frames sequentially. Truncated-tail (mid-append) is handled by
# stopping at the incomplete frame and returning the offset just before it.

def _scan_tail(path, max_bytes):
    """Read entire spool, keep at most `max_bytes` of payload from the end.

    Frame-boundary trimming: drops whole frames from the front until the
    accumulated payload fits. Always keeps at least one frame, even if it
    alone exceeds `max_bytes`. Returns (bytes, end_offset, frames_kept).
    """
    chunks = []
    total = 0
    end_off = 0
    try:
        with open(path, "rb") as f:
            while True:
                h = f.read(4)
                if len(h) < 4:
                    break
                n = struct.unpack("<I", h)[0]
                p = f.read(n)
                if len(p) < n:
                    break
                end_off = f.tell()
                chunks.append((n, p))
                total += n
                while total > max_bytes and len(chunks) > 1:
                    total -= chunks[0][0]
                    chunks.pop(0)
    except FileNotFoundError:
        return b"", 0, 0
    return b"".join(c[1] for c in chunks), end_off, len(chunks)


def _read_spool_range(sid, start, max_bytes):
    """Read frames starting at byte offset `start` up to `max_bytes` of payload.

    Returns (bytes, end_offset, frames_read). `end_offset` advances only
    past fully-read frames; an in-progress append at the end is ignored
    until the next call.
    """
    path = _spool_path(sid)
    chunks = []
    total = 0
    end_off = start
    try:
        with open(path, "rb") as f:
            f.seek(start)
            while total < max_bytes:
                h = f.read(4)
                if len(h) < 4:
                    break
                n = struct.unpack("<I", h)[0]
                p = f.read(n)
                if len(p) < n:
                    break
                end_off = f.tell()
                chunks.append(p)
                total += n
    except FileNotFoundError:
        return b"", start, 0
    return b"".join(chunks), end_off, len(chunks)


# ---------------------------------------------------------------------------
# ANSI / VT100 stripping
# ---------------------------------------------------------------------------
# Good-enough scrubber for boss-Claude to read worker output. Doesn't
# emulate a terminal (cursor moves, scroll regions, etc. just disappear)
# but yields readable text for the common cases (shell prompts, Claude's
# streamed output, compiler errors). For TUIs like vim, pipe through a real
# emulator (pyte); see Phase 5 future-work.

_CSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ESC_RE = re.compile(rb"\x1b[@-Z\\-_]")
_CTRL_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _ansi_strip(b):
    b = _CSI_RE.sub(b"", b)
    b = _OSC_RE.sub(b"", b)
    b = _ESC_RE.sub(b"", b)
    b = _CTRL_RE.sub(b"", b)
    return b


# ---------------------------------------------------------------------------
# Screen rendering (pyte-backed)
# ---------------------------------------------------------------------------
# Byte-stream reads of TUI workers (Claude, vim, htop, ...) are very noisy
# because the worker redraws lines in place via cursor moves. Feeding the
# spool through pyte (a VT100 emulator) collapses all that to the actual
# screen state, which is what a human or boss-Claude needs to see.
#
# Trade-off: pyte runs the FULL spool history every call (the screen state
# at time T depends on all bytes 0..T, so we can't resume mid-stream).
# That's O(spool_size) per render. On a multi-MB spool a snapshot takes
# ~1-3 s; the CLI's `tp tail -f` keeps its own incremental pyte instance
# in memory to avoid the cost on each tick.

DEFAULT_SCREEN_COLS = 200
DEFAULT_SCREEN_ROWS = 20


def _build_screen(cols, rows):
    """Local import: pyte is required but we keep the import lazy so
    stream-mode-only callers don't pay the import cost on stdlib paths."""
    import pyte
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    return screen, stream


def _feed_spool(stream, path):
    """Walk every frame of the spool and feed it to the pyte stream.

    Returns (bytes_fed, frames_fed, end_offset). Quietly stops at a
    truncated final frame (in-progress append).
    """
    bytes_fed = 0
    frames_fed = 0
    end_off = 0
    try:
        with open(path, "rb") as f:
            while True:
                h = f.read(4)
                if len(h) < 4:
                    break
                n = struct.unpack("<I", h)[0]
                p = f.read(n)
                if len(p) < n:
                    break
                stream.feed(p)
                bytes_fed += n
                frames_fed += 1
                end_off = f.tell()
    except FileNotFoundError:
        pass
    return bytes_fed, frames_fed, end_off


# pyte stores colours as named strings ('red', 'brightblue', 'default')
# or 6-hex strings ('ff7733'). Map names to SGR digits; hex falls through
# to the truecolour 38;2;R;G;B / 48;2;R;G;B encoding.
_PYTE_FG_CODES = {
    "black": 30, "red": 31, "green": 32, "brown": 33, "yellow": 33,
    "blue": 34, "magenta": 35, "cyan": 36, "white": 37, "default": 39,
    "brightblack": 90, "brightred": 91, "brightgreen": 92,
    "brightbrown": 93, "brightyellow": 93,
    "brightblue": 94, "brightmagenta": 95, "brightcyan": 96, "brightwhite": 97,
}
_PYTE_BG_CODES = {k: v + 10 for k, v in _PYTE_FG_CODES.items()}
_HEX6_RE = re.compile(r"^[0-9a-fA-F]{6}$")


def _color_to_sgr(color, *, is_fg):
    if not color or color == "default":
        return None
    codes = _PYTE_FG_CODES if is_fg else _PYTE_BG_CODES
    if color in codes:
        return str(codes[color])
    if _HEX6_RE.match(color):
        r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
        return f"{'38' if is_fg else '48'};2;{r};{g};{b}"
    return None


def _sgr_for_char(char):
    """SGR sequence (including the \\x1b[ prefix and m suffix) that paints
    a single char's style from a clean state. Returns '' for the default
    cell so plain text has no inline codes."""
    parts = []
    fg = _color_to_sgr(getattr(char, "fg", None), is_fg=True)
    bg = _color_to_sgr(getattr(char, "bg", None), is_fg=False)
    if fg:
        parts.append(fg)
    if bg:
        parts.append(bg)
    if getattr(char, "bold", False):
        parts.append("1")
    if getattr(char, "italics", False):
        parts.append("3")
    if getattr(char, "underscore", False):
        parts.append("4")
    if getattr(char, "reverse", False):
        parts.append("7")
    if getattr(char, "strikethrough", False):
        parts.append("9")
    if not parts:
        return ""
    return f"\x1b[{';'.join(parts)}m"


def _row_to_ansi(screen, y, cols):
    """Render one screen row to a string with inline ANSI codes."""
    out = []
    cur_sgr = ""
    for x in range(cols):
        ch = screen.buffer[y][x]
        sgr = _sgr_for_char(ch)
        if sgr != cur_sgr:
            if cur_sgr:
                out.append("\x1b[0m")
            if sgr:
                out.append(sgr)
            cur_sgr = sgr
        out.append(ch.data or " ")
    if cur_sgr:
        out.append("\x1b[0m")
    # rstrip drops trailing whitespace including styled spaces, which
    # loses background-colour info at line ends. Acceptable trade-off
    # for keeping output clean; preserving styled trailing space would
    # require tracking the rightmost styled cell explicitly.
    return "".join(out).rstrip()


def render_screen(sid, cols=DEFAULT_SCREEN_COLS, rows=DEFAULT_SCREEN_ROWS,
                  keep_color=False, since_spool_end=None, wait_secs=0.0):
    """Render the worker's current terminal screen via pyte.

    Cheap-poll semantics: if `since_spool_end` is provided and the spool
    hasn't grown past that offset, returns a tiny `{"unchanged": True,
    "spool_end": ...}` response without running pyte at all. Combined
    with `wait_secs > 0`, the call blocks server-side until either the
    spool grows or the deadline elapses, so boss-Claude can poll cheaply
    without burning context on identical screens.

    Returns dict with:
      unchanged    present only when since_spool_end was set and the
                   spool hasn't grown. Caller can short-circuit; no
                   `lines` are emitted in this case (token-cheap).
      lines        list of `rows` strings, plain text (no ANSI codes).
      lines_ansi   present only when keep_color=True: same rows with
                   inline SGR sequences so you can pipe to a terminal
                   that supports colour, or grep for `\\x1b[3?m` codes
                   to disambiguate visually-distinct text (autosuggest
                   ghost vs typed input, Claude UI colour-coding, ...).
      cursor       {"x": col, "y": row} - where the worker's caret is.
      size         {"cols": ..., "rows": ...}.
      bytes_fed    total payload bytes consumed.
      frames_fed   number of frames consumed.
      spool_end    spool offset after the last complete frame.
    """
    path = _spool_path(sid)
    deadline = time.monotonic() + max(0.0, wait_secs)
    while True:
        current_size = _safe_getsize(path) or 0
        # `<=` so external truncation (file shrank below since_spool_end)
        # also short-circuits as "no new content" rather than tripping
        # the render path on a smaller-than-expected spool.
        if since_spool_end is not None and current_size <= since_spool_end:
            if time.monotonic() >= deadline:
                return {
                    "unchanged": True,
                    "spool_end": current_size,
                    "size": {"cols": cols, "rows": rows},
                }
            time.sleep(0.25)
            continue
        break

    screen, stream = _build_screen(cols, rows)
    bytes_fed, frames_fed, end_off = _feed_spool(stream, path)
    lines = [line.rstrip() for line in screen.display]
    result = {
        "lines": lines,
        "cursor": {"x": screen.cursor.x, "y": screen.cursor.y},
        "size": {"cols": cols, "rows": rows},
        "bytes_fed": bytes_fed,
        "frames_fed": frames_fed,
        "spool_end": end_off,
    }
    if keep_color:
        result["lines_ansi"] = [_row_to_ansi(screen, y, cols) for y in range(rows)]
    return result


def screen_as_text(rendered):
    """Format a render_screen() result as a single newline-joined string
    suitable for printing to a terminal or piping to a Claude tool result.
    Uses lines_ansi when present (keep_color=True), else plain lines.
    Returns "" for an `unchanged` short-circuit response (nothing to print)."""
    if rendered.get("unchanged"):
        return ""
    if "lines_ansi" in rendered:
        return "\n".join(rendered["lines_ansi"])
    return "\n".join(rendered["lines"])


# ---------------------------------------------------------------------------
# render_history -- full scrollback via pyte.HistoryScreen
# ---------------------------------------------------------------------------
# read_screen only returns the currently-visible `rows` lines (the worker's
# active terminal). For "give me the full conversation since session start"
# we need pyte's HistoryScreen, which keeps a deque of scrolled-off lines
# in screen.history.top. That captures the rendered transcript - free of
# the noisy in-place redraws that read_output(strip_ansi=true) would still
# produce.
#
# Performance: pyte.HistoryScreen feeds at ~1 MB/s on a modern CPU. A
# 10 MB spool (~hours of dense Claude conversation) takes ~10s. That's
# slow for interactive use but acceptable for one-shot audits.

DEFAULT_HISTORY_SCROLLBACK = 5000


def _history_line_to_text(line, cols):
    """Convert one HistoryScreen history-line to a string.

    pyte 0.8 stores history lines as a StaticDefaultDict-like mapping
    of column index -> Char with a `.get(x)` method. If a future pyte
    changes the structure (e.g. to a list), update both this and
    `_history_line_to_ansi` accordingly.
    """
    out = []
    for x in range(cols):
        ch = line.get(x)
        out.append(ch.data if ch and ch.data else " ")
    return "".join(out).rstrip()


def _history_line_to_ansi(line, cols):
    """Same as `_history_line_to_text`, with inline SGR escapes preserved.
    Pyte-version contract: see `_history_line_to_text`."""
    out = []
    cur_sgr = ""
    for x in range(cols):
        ch = line.get(x)
        if ch is None:
            out.append(" ")
            continue
        sgr = _sgr_for_char(ch)
        if sgr != cur_sgr:
            if cur_sgr:
                out.append("\x1b[0m")
            if sgr:
                out.append(sgr)
            cur_sgr = sgr
        out.append(ch.data or " ")
    if cur_sgr:
        out.append("\x1b[0m")
    return "".join(out).rstrip()


def render_history(sid, cols=DEFAULT_SCREEN_COLS, rows=DEFAULT_SCREEN_ROWS,
                   scrollback=DEFAULT_HISTORY_SCROLLBACK, keep_color=False):
    """Render the worker's full session through pyte.HistoryScreen.

    Returns the scrolled-off history (oldest first) plus the currently-
    visible screen, giving boss-Claude the full transcript needed to
    audit a worker session against rules / instructions / past decisions.

    For sessions older than the scrollback can hold, the oldest lines
    fall off the top (FIFO). Default 5000 lines captures roughly an
    hour of dense Claude conversation; bump for longer audits.

    Returns dict with:
      history_lines  scrolled-off lines, oldest first, plain text.
      lines          currently-visible rows, plain text.
      history_ansi   present only when keep_color=True: scrolled-off
                     lines with inline SGR codes preserving colour.
      lines_ansi     present only when keep_color=True: visible rows
                     with inline SGR codes.
      cursor         {"x": col, "y": row} where the caret is now.
      size           {"cols": ..., "rows": ..., "scrollback": ...}.
      truncated      true if the history deque is at capacity. This is
                     an upper bound on "lines were dropped" - at the
                     exact moment the deque first fills, no drops have
                     happened yet, so the flag may be set for one render
                     before drops actually begin. False = boss has the
                     full history; True = boss should bump scrollback
                     if they want to be sure nothing was dropped.
      bytes_fed      total payload bytes consumed.
      frames_fed     number of frames consumed.
      spool_end      spool offset after the last complete frame.
    """
    import pyte
    screen = pyte.HistoryScreen(cols, rows, history=scrollback)
    stream = pyte.ByteStream(screen)
    path = _spool_path(sid)
    bytes_fed, frames_fed, end_off = _feed_spool(stream, path)

    history_lines = [
        _history_line_to_text(line, cols) for line in screen.history.top
    ]
    lines = [line.rstrip() for line in screen.display]
    truncated = len(screen.history.top) >= scrollback

    result = {
        "history_lines": history_lines,
        "lines": lines,
        "cursor": {"x": screen.cursor.x, "y": screen.cursor.y},
        "size": {"cols": cols, "rows": rows, "scrollback": scrollback},
        "truncated": truncated,
        "bytes_fed": bytes_fed,
        "frames_fed": frames_fed,
        "spool_end": end_off,
    }
    if keep_color:
        result["history_ansi"] = [
            _history_line_to_ansi(line, cols) for line in screen.history.top
        ]
        result["lines_ansi"] = [
            _row_to_ansi(screen, y, cols) for y in range(rows)
        ]
    return result


def history_as_text(rendered):
    """Format a render_history() result as a single newline-joined transcript:
    scrolled-off history first, then currently-visible screen. Uses
    *_ansi when present (keep_color=True), else plain text."""
    if "history_ansi" in rendered:
        return "\n".join(rendered["history_ansi"] + rendered["lines_ansi"])
    return "\n".join(rendered["history_lines"] + rendered["lines"])


# ---------------------------------------------------------------------------
# read_output
# ---------------------------------------------------------------------------

def read_output(sid, since=None, max_bytes=32768, wait_secs=0.0, strip_ansi=True):
    """Return new output from a session's spool.

    First call: pass `since=None` to receive the most recent `max_bytes`
    of payload (a tail). Save the returned `new_offset` and pass it as
    `since` on subsequent calls to get strictly-new bytes.

    If `wait_secs > 0` and no new frames are available, polls every
    ~100 ms until either new frames arrive or the deadline elapses; the
    final read is returned (possibly empty).
    """
    path = _spool_path(sid)
    deadline = time.monotonic() + max(0.0, wait_secs)
    data = b""
    new_off = since if since is not None else 0
    n = 0
    while True:
        if since is None:
            data, new_off, n = _scan_tail(path, max_bytes)
        else:
            data, new_off, n = _read_spool_range(sid, since, max_bytes)
        if n > 0 or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    payload = _ansi_strip(data) if strip_ansi else data
    return {
        "bytes": payload.decode("utf-8", errors="replace"),
        "new_offset": new_off,
        "frames_read": n,
        "size_at_read": _safe_getsize(path),
    }


# ---------------------------------------------------------------------------
# send_input: local-only, no relay, no crypto
# ---------------------------------------------------------------------------

def send_input(sid, text, newline=True):
    """Type plain bytes into a worker session via the local input file.

    Appends `text` (UTF-8) plus a trailing `\\r` (Enter) to
    `~/.cache/termpilot/sid/<sid>/in.local`. The wrapper's
    `local_input_poller` thread picks up the bytes and writes them to the
    PTY. Same-machine only: no relay round-trip, phone view stays clean.
    """
    entry = _entry_for_sid(sid)
    if entry is None:
        raise RuntimeError(f"no termpilot session found for sid {sid!r}")
    if not entry.get("alive"):
        raise RuntimeError(f"wrapper for sid {sid!r} is not running")
    path = _in_local_path(sid)
    if not os.path.exists(path):
        raise RuntimeError(
            f"sid {sid!r} has no in.local file. The worker's wrapper does "
            "not have local-input support. Upgrade termpilot on the worker "
            "side (or it may have been started with --no-local-input)."
        )
    data = text.encode("utf-8")
    if newline and not data.endswith(b"\r"):
        data += b"\r"
    with open(path, "ab") as f:
        f.write(data)
    return {"bytes_written": len(data), "newline_appended": newline}


# ---------------------------------------------------------------------------
# Named-key map: kept in lockstep with relay/lib/keyboard.js GROUPS so the
# CLI, the MCP tool, and the webapp's virtual keyboard all send identical
# bytes. Keys are stored normalized (lowercase, hyphenated) so lookups are
# case-insensitive.

NAMED_KEYS = {
    # Navigation
    "up":        "\x1b[A",  "down":  "\x1b[B",
    "left":      "\x1b[D",  "right": "\x1b[C",
    "enter":     "\r",
    "esc":       "\x1b",    "escape":"\x1b",
    "tab":       "\t",
    "shifttab":  "\x1b[Z",  "shift-tab":"\x1b[Z",  "backtab":"\x1b[Z",
    "backspace": "\x7f",    "bs":    "\x7f",
    "home":      "\x1b[H",
    "end":       "\x1b[F",
    "pgup":      "\x1b[5~", "pageup":   "\x1b[5~",
    "pgdn":      "\x1b[6~", "pagedown": "\x1b[6~",
    "del":       "\x1b[3~", "delete":   "\x1b[3~",
    "ins":       "\x1b[2~", "insert":   "\x1b[2~",
    "space":     " ",
    # Function keys
    "f1":  "\x1bOP",   "f2":  "\x1bOQ",   "f3":  "\x1bOR",   "f4":  "\x1bOS",
    "f5":  "\x1b[15~", "f6":  "\x1b[17~", "f7":  "\x1b[18~",
    "f8":  "\x1b[19~", "f9":  "\x1b[20~", "f10": "\x1b[21~",
    "f11": "\x1b[23~", "f12": "\x1b[24~",
}
for _c in range(ord("A"), ord("Z") + 1):
    NAMED_KEYS[f"ctrl-{chr(_c).lower()}"] = chr(_c - 0x40)
NAMED_KEYS["ctrl-space"] = "\x00"
NAMED_KEYS["ctrl-["]     = "\x1b"
NAMED_KEYS["ctrl-\\"]    = "\x1c"
NAMED_KEYS["ctrl-]"]     = "\x1d"
NAMED_KEYS["ctrl-^"]     = "\x1e"
NAMED_KEYS["ctrl-_"]     = "\x1f"


def _normalize_key(name):
    """Case-insensitive lookup helper. Accepts `ShiftTab`, `shift tab`,
    `Ctrl_C`, etc. and normalises to the dict's key form."""
    return name.strip().lower().replace(" ", "-").replace("_", "-")


# Modifier names accepted in combo syntax like `Ctrl-Left`, `Alt-a`,
# `Ctrl-Shift-Up`. Order in the input doesn't matter; modifiers are
# collected into a set and applied in xterm's canonical order
# (Shift → Ctrl → Alt prefix).
_MODIFIER_NAMES = {"ctrl", "alt", "shift"}


def _apply_mods(base, mods):
    """Apply a set of modifier names ({'ctrl','alt','shift'}) to a base
    byte sequence using xterm conventions.

    - Arrow / Home / End / PgUp / PgDn / Del / Ins / Fn: when a modifier
      is set, rewrites the CSI sequence to include xterm's modifier-code
      parameter (e.g. Ctrl+Left = \\x1b[1;5D, Shift+PgUp = \\x1b[5;2~).
    - Letters with Shift: uppercase. With Ctrl: control byte (A→0x01).
    - Alt: prefix the result with ESC (xterm meta convention).
    """
    has_shift = "shift" in mods
    has_ctrl  = "ctrl"  in mods
    has_alt   = "alt"   in mods

    # xterm modifier code: 1 + Shift + 2*Alt + 4*Ctrl
    mod_code = 1 + (1 if has_shift else 0) + (2 if has_alt else 0) + (4 if has_ctrl else 0)

    # CSI single-letter finals (arrows + Home/End) → `\x1b[1;<mod><final>`
    if mod_code > 1 and len(base) == 3 and base.startswith("\x1b[") and base[-1] in "ABCDHF":
        return f"\x1b[1;{mod_code}{base[-1]}"

    # SS3 final (F1–F4 send \x1bO[PQRS]) → upgrade to CSI form on modifier
    if mod_code > 1 and len(base) == 3 and base.startswith("\x1bO") and base[-1] in "PQRS":
        return f"\x1b[1;{mod_code}{base[-1]}"

    # CSI-tilde finals (PgUp/PgDn/Ins/Del/F5–F12) → `\x1b[<n>;<mod>~`
    if mod_code > 1 and base.startswith("\x1b[") and base.endswith("~"):
        middle = base[2:-1]
        return f"\x1b[{middle};{mod_code}~"

    # Single-byte base: apply Shift then Ctrl, then Alt prefix.
    bytes_ = base
    if has_shift and len(bytes_) == 1 and "a" <= bytes_ <= "z":
        bytes_ = bytes_.upper()
    if has_ctrl and len(bytes_) == 1:
        c = ord(bytes_)
        if 0x41 <= c <= 0x5A:    bytes_ = chr(c - 0x40)         # Ctrl-A..Z
        elif 0x61 <= c <= 0x7A:  bytes_ = chr(c - 0x60)         # Ctrl-a..z
        elif bytes_ == " ":      bytes_ = "\x00"
        elif bytes_ == "[":      bytes_ = "\x1b"
        elif bytes_ == "\\":     bytes_ = "\x1c"
        elif bytes_ == "]":      bytes_ = "\x1d"
        elif bytes_ == "^":      bytes_ = "\x1e"
        elif bytes_ == "_":      bytes_ = "\x1f"
        elif bytes_ == "?":      bytes_ = "\x7f"
    if has_alt:
        bytes_ = "\x1b" + bytes_
    return bytes_


def resolve_key(name):
    """Parse a key name (possibly with modifiers) into the byte sequence.

    Returns the bytes to send, or None if the name is unrecognised.
    Lookup order:
      1. Direct hit in NAMED_KEYS (so `Ctrl-A`, `Ctrl-Space`, `Esc`,
         `ShiftTab`, `F5` etc. all resolve without parsing).
      2. Modifier-prefixed form: split on `-`, leading segments must be
         modifier names (ctrl / alt / shift, case-insensitive). The rest
         names the base key (either another NAMED_KEYS entry, or a
         single literal character). Modifiers are then composed via
         `_apply_mods`.

    Examples (input → output bytes):
      Ctrl-Left       → \\x1b[1;5D
      Alt-a           → \\x1ba
      Ctrl-Shift-Up   → \\x1b[1;6A
      Alt-F5          → \\x1b[15;3~
      Shift-PgUp      → \\x1b[5;2~
      Ctrl-Alt-X      → \\x1b\\x18      (Alt prefix + Ctrl-X)
    """
    norm = _normalize_key(name)
    direct = NAMED_KEYS.get(norm)
    if direct is not None:
        return direct
    parts = [p for p in norm.split("-") if p]
    if len(parts) < 2:
        return None
    mods = set()
    base_start = len(parts)
    for i, p in enumerate(parts):
        if p in _MODIFIER_NAMES:
            mods.add(p)
        else:
            base_start = i
            break
    if not mods:
        return None
    base_name = "-".join(parts[base_start:])
    base_bytes = NAMED_KEYS.get(base_name)
    if base_bytes is None and len(base_name) == 1:
        base_bytes = base_name
    if base_bytes is None:
        return None
    return _apply_mods(base_bytes, mods)


def send_key(sid, key_name):
    """Send a single named key (with optional modifier prefix) by name.

    Always newline=False so control sequences don't pick up a trailing
    Enter. Accepts forms like `Esc`, `Ctrl-C`, `Ctrl-Left`, `Alt-a`,
    `Ctrl-Shift-Up`, `Alt-F5`. See `resolve_key` for the full grammar.

    Raises ValueError if the name doesn't resolve. Raises RuntimeError
    on the usual missing-session / no-in.local conditions.
    """
    seq = resolve_key(key_name)
    if seq is None:
        raise ValueError(
            f"unknown key {key_name!r}. Try a name from NAMED_KEYS "
            "(Esc, Tab, ShiftTab, Up, Backspace, Ctrl-C, F5, ...) "
            "or a modifier combo like Ctrl-Left / Alt-a / Ctrl-Shift-Up."
        )
    return send_input(sid, seq, newline=False)


# ---------------------------------------------------------------------------
# send_signal: Ctrl-keys via in.local; SIGTERM/SIGHUP via os.kill
# ---------------------------------------------------------------------------
# Ctrl-C / Ctrl-\ / Ctrl-Z go through the PTY line discipline (write the
# control byte; the kernel converts to a signal for the foreground
# process group). SIGTERM / SIGHUP target the wrapper PID directly: the
# wrapper exits, the PTY is closed, the child gets SIGHUP. That's the
# right pair for "interrupt the current operation" vs "shut the session
# down."

_CTRL_CHAR_SIGNALS = {
    "SIGINT": b"\x03",
    "SIGQUIT": b"\x1c",
    "SIGTSTP": b"\x1a",
}
_OS_SIGNALS = {
    "SIGTERM": _signal.SIGTERM,
    "SIGHUP": _signal.SIGHUP,
}


def send_signal(sid, signal_name="SIGINT"):
    """Send a signal to the worker session.

    Control-char signals (SIGINT, SIGQUIT, SIGTSTP) go via the local input
    file (same path as `send_input`) so the line discipline converts them
    to OS signals for the worker's foreground process group. SIGTERM and
    SIGHUP are delivered to the wrapper PID directly via `os.kill`.

    Anything outside this allowlist raises `ValueError`. SIGKILL is
    intentionally not supported: the wrapper has crash-recovery logic
    that depends on a clean exit; if you really need to nuke a session
    use `kill -9 <pid>` from a shell.
    """
    name = signal_name.upper()
    entry = _entry_for_sid(sid)
    if entry is None or not entry.get("alive"):
        raise RuntimeError(f"no live wrapper for sid {sid!r}")
    if name in _CTRL_CHAR_SIGNALS:
        path = _in_local_path(sid)
        if not os.path.exists(path):
            raise RuntimeError(
                f"sid {sid!r} has no in.local; cannot send {name} via the "
                "line discipline."
            )
        byte = _CTRL_CHAR_SIGNALS[name]
        with open(path, "ab") as f:
            f.write(byte)
        return {"method": "ctrl_char", "bytes": len(byte)}
    if name in _OS_SIGNALS:
        pid = entry.get("pid")
        if not pid:
            raise RuntimeError(f"no pid for sid {sid!r}")
        os.kill(pid, _OS_SIGNALS[name])
        return {"method": "os_signal", "pid": pid, "signal": name}
    allowed = sorted(set(_CTRL_CHAR_SIGNALS) | set(_OS_SIGNALS))
    raise ValueError(
        f"signal {signal_name!r} not in allowlist {allowed}"
    )


# ---------------------------------------------------------------------------
# wait_for_idle
# ---------------------------------------------------------------------------
# Two idle-detection modes:
#
# - "bytes": spool stops growing for quiet_secs. Wrong for animated TUI
#   workers (Claude's spinner redraws every 100 ms, so the spool never
#   stops growing even when the worker is "thinking"). Right when the
#   worker is a script that periodically prints log lines.
#
# - "screen" (default): the *rendered* screen stops changing for
#   quiet_secs. Strips spinner glyphs, braille animations, elapsed
#   timers, and token counters before comparing so the cosmetic ticking
#   doesn't count as "active." This is what boss-Claude almost always
#   wants when orchestrating another Claude.

# Common spinner / animation glyphs across cli-spinners, ora, Claude
# TUI, etc. Each is replaced with a space before comparing screens, so
# a spinner cell cycling through these counts as "no change."
# The braille range U+2800-U+28FF already covers ▖▘▝▗ etc., so we don't
# enumerate quadrant blocks separately.
_SPINNER_GLYPHS = (
    "⠀-⣿"   # Braille Patterns (cli-spinners "dots" + many others)
    "▖-▟"   # Block element fractions used by some progress bars
    "✪-✯"   # Various star asterisks
    "◐-◗"   # Circle halves / quadrants (clock-style spinners)
    "◴-◷"   # Pie slice spinners
    "✳✴"    # Eight-pointed asterisks (Claude uses ✸ family)
    "✶✷"    # Six-pointed black star, six-pointed pinwheel
    "✸✹✺✻✼✽"  # Stars in Claude's spinner set
    "·•․‧∘∙"  # Middle dot, bullet, dot operator
    "*✱❂"   # ASCII *, heavy asterisk, eight-spoked asterisk
)
_SPINNER_RE = re.compile(f"[{_SPINNER_GLYPHS}]")
# Bar-spinner chars (|/-\) only when they appear alone (preceded by
# start-of-string or whitespace AND followed by whitespace or end). The
# earlier `(?<![A-Za-z0-9])...(?![A-Za-z0-9])` form would also eat the
# `-` in "hello - world" or the `|` in "foo | bar", which loses real
# content when comparing screens.
_BAR_SPINNER_RE = re.compile(r"(?:^|(?<=\s))[|/\\\-](?=\s|$)")
# Elapsed timer + counter patterns: "1m 26s", "3.4s", "↓ 5.1k tokens",
# "50%". The negative-lookahead `(?![A-Za-z])` prevents partial matches
# inside words ("5second" must not match the "sec" suffix).
_TIMER_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*[kKmMgG]?\s*"
    r"(?:ms|s|m|h|d|min|sec|tokens?|KB|MB|GB|chars?|bytes?|%)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)


def _normalize_for_idle(text):
    """Strip animated/cosmetic content so two screens that differ only
    in spinner frame / timer tick / token counter compare equal."""
    text = _SPINNER_RE.sub(" ", text)
    text = _BAR_SPINNER_RE.sub(" ", text)
    text = _TIMER_RE.sub(" ", text)
    return text


def _screen_signature(screen):
    """Normalized signature of a pre-rendered pyte screen. Two snapshots
    of the same screen-state-modulo-animation produce equal signatures."""
    return "\n".join(_normalize_for_idle(line.rstrip())
                     for line in screen.display)


def wait_for_idle(sid, quiet_secs=15.0, timeout=600.0, since=None,
                  idle_mode="screen", cols=DEFAULT_SCREEN_COLS,
                  rows=DEFAULT_SCREEN_ROWS):
    """Block until the worker has been idle for `quiet_secs`, then return.

    `idle_mode="screen"` (default) compares pyte-rendered screens with
    spinner / timer / counter animation stripped - the right answer for
    TUI workers (another Claude, vim, htop, ...) whose spool grows
    constantly from redraws even when nothing real is happening.

    `idle_mode="bytes"` compares spool size only - the original
    behaviour. Use when the worker is a non-TUI script that periodically
    prints log lines and you want strict byte-level silence.

    `since` is bytes-mode only and ignored in screen mode (screen mode
    tracks its own offset internally via the cached pyte stream).

    Returns dict with:
      mode         "screen" or "bytes" (what was used).
      elapsed      total seconds spent waiting.
      idle_for     seconds since the last detected change.
      spool_end    spool offset at idle time.
      lines        screen mode only: visible screen at idle time.
      cursor       screen mode only: cursor x/y at idle time.
      bytes        bytes mode only: text accumulated since `since`. Empty
                   if no explicit `since` was passed (default `since` is
                   the spool size at call time, so by definition nothing
                   has been written after it once we go idle).
      new_offset   bytes mode only: spool offset boss should pass next.

    Raises TimeoutError if no idle window of the requested length occurs
    within `timeout` seconds.
    """
    start = time.monotonic()
    deadline = start + timeout
    path = _spool_path(sid)

    if idle_mode == "bytes":
        last_size = _safe_getsize(path) or 0
        if since is None:
            since = last_size
        last_change_at = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(
                    f"sid {sid!r} did not go idle for {quiet_secs}s "
                    f"within {timeout}s (mode=bytes)"
                )
            cur_size = _safe_getsize(path)
            if cur_size is None:
                cur_size = last_size
            if cur_size > last_size:
                last_size = cur_size
                last_change_at = now
            if now - last_change_at >= quiet_secs:
                data, new_off, _ = _read_spool_range(
                    sid, since, max_bytes=64 * 1024,
                )
                return {
                    "mode": "bytes",
                    "bytes": _ansi_strip(data).decode("utf-8", errors="replace"),
                    "new_offset": new_off,
                    "spool_end": _safe_getsize(path) or 0,
                    "elapsed": now - start,
                    "idle_for": now - last_change_at,
                }
            time.sleep(0.25)

    # idle_mode == "screen"
    #
    # Performance: build one pyte screen, do the initial O(spool-size)
    # feed once, then incrementally feed only NEW bytes on each poll.
    # The earlier implementation re-fed the entire spool every 250 ms,
    # which on a multi-MB spool exceeded the poll interval and made the
    # function never converge on large sessions.
    screen, stream = _build_screen(cols, rows)
    last_offset = 0
    # Drain whatever was already in the spool when the call started. May
    # take several seconds on multi-MB sessions; that's the one-time cost.
    while True:
        data, new_off, n = _read_spool_range(
            sid, last_offset, max_bytes=16 * 1024 * 1024,
        )
        if n == 0:
            break
        stream.feed(data)
        last_offset = new_off

    last_sig = _screen_signature(screen)
    last_change_at = time.monotonic()
    while True:
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError(
                f"sid {sid!r} did not go idle for {quiet_secs}s "
                f"within {timeout}s (mode=screen)"
            )
        # Feed only the delta since the previous poll.
        while True:
            data, new_off, n = _read_spool_range(
                sid, last_offset, max_bytes=16 * 1024 * 1024,
            )
            if n == 0:
                break
            stream.feed(data)
            last_offset = new_off
        cur_sig = _screen_signature(screen)
        if cur_sig != last_sig:
            last_sig = cur_sig
            last_change_at = now
        if now - last_change_at >= quiet_secs:
            return {
                "mode": "screen",
                "lines": [line.rstrip() for line in screen.display],
                "cursor": {"x": screen.cursor.x, "y": screen.cursor.y},
                "spool_end": last_offset,
                "elapsed": now - start,
                "idle_for": now - last_change_at,
            }
        time.sleep(0.25)


# ---------------------------------------------------------------------------
# wait_for_output
# ---------------------------------------------------------------------------

def wait_for_output(sid, pattern, timeout=600.0, since=None, strip_ansi=True):
    """Block until new spool output matches `pattern` (regex).

    Scans a rolling 64 KB window of the most-recent stripped text; the
    match position is reported relative to that window. Raises
    `TimeoutError` if no match within `timeout`.
    """
    rx = re.compile(pattern)
    start = time.monotonic()
    deadline = start + timeout
    if since is None:
        since = _safe_getsize(_spool_path(sid)) or 0
    accumulated = b""
    window = 65536
    while True:
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError(
                f"sid {sid!r}: pattern {pattern!r} not matched within {timeout}s"
            )
        data, new_off, _ = _read_spool_range(sid, since, max_bytes=64 * 1024)
        if data:
            since = new_off
            accumulated = (accumulated + data)[-window:]
            text_bytes = _ansi_strip(accumulated) if strip_ansi else accumulated
            text = text_bytes.decode("utf-8", errors="replace")
            m = rx.search(text)
            if m:
                return {
                    "bytes": text,
                    "match": m.group(0),
                    "new_offset": new_off,
                    "elapsed": now - start,
                }
        time.sleep(0.25)


# ---------------------------------------------------------------------------
# tail_events
# ---------------------------------------------------------------------------

def tail_events(sid=None, since_ts=None, limit=50, cat=None):
    """Filter `~/.cache/termpilot/events.log` (JSONL).

    Args:
        sid: keep only entries whose `sid` matches; None = any.
        since_ts: ISO-8601 timestamp string; keep only entries with `ts >= since_ts`.
        cat: iterable of category names; None = all categories.
        limit: return at most this many most-recent matching entries.
    """
    cats = set(cat) if cat else None
    matches = []
    try:
        with open(EVENT_LOG, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if sid is not None and rec.get("sid") != sid:
                    continue
                if cats is not None and rec.get("cat") not in cats:
                    continue
                if since_ts is not None and rec.get("ts", "") < since_ts:
                    continue
                matches.append(rec)
    except OSError:
        return []
    return matches[-limit:]
