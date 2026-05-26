"""Command-line surface for termpilot orchestration.

Subcommands dispatched from `linux/termpilot-wrap` (so users type `tp ls`,
`tp tail`, etc.) and from `python -m termpilot_mcp.cli`. Each handler
parses its own argv, calls into `termpilot_mcp.core`, and returns an exit
code. Stdlib-only; the `mcp` SDK is only needed by `mcp-serve`.
"""

import argparse
import json
import os
import re
import signal as _signal
import sys
import time

from termpilot_mcp import core


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n):
    if n is None:
        return "-"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f}KB"
    return f"{n/(1024*1024):.1f}MB"


def _short_cwd(cwd):
    if not cwd:
        return "-"
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    if len(cwd) > 40:
        cwd = "..." + cwd[-37:]
    return cwd


def _print_table(rows, columns, out=sys.stdout):
    """Aligned-column table. `columns` is the ordered list of dict keys; the
    header is the upper-cased key name."""
    if not rows:
        return
    headers = {c: c.upper().replace("_", "-") for c in columns}
    widths = {
        c: max(len(headers[c]), *(len(str(r.get(c, ""))) for r in rows))
        for c in columns
    }
    fmt = "  ".join(f"{{:<{widths[c]}}}" for c in columns)
    out.write(fmt.format(*(headers[c] for c in columns)) + "\n")
    for r in rows:
        out.write(fmt.format(*(str(r.get(c, "")) for c in columns)) + "\n")


def _install_sigpipe_default():
    """When piping `tp tail | head`, restore SIGPIPE so we exit silently
    instead of dumping a BrokenPipeError traceback."""
    try:
        _signal.signal(_signal.SIGPIPE, _signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# tp ls
# ---------------------------------------------------------------------------

def handle_ls(argv):
    p = argparse.ArgumentParser(
        prog="tp ls",
        description="List live termpilot wrappers on this machine. Stale "
                    "entries (wrapper process exited but the cache dir is "
                    "still present) are hidden by default; pass --all to "
                    "include them.",
    )
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of a table (for scripting / MCP).")
    p.add_argument("--all", action="store_true",
                   help="show all known sessions including stale entries "
                        "whose wrapper process has exited. Default: live only.")
    args = p.parse_args(argv)
    all_sessions = core.list_sessions()
    if args.all:
        sessions = all_sessions
        stale_hidden = 0
    else:
        sessions = [s for s in all_sessions if s.get("alive")]
        stale_hidden = len(all_sessions) - len(sessions)
    if args.json:
        print(json.dumps(sessions, indent=2, default=str))
        return 0
    if not sessions:
        if stale_hidden:
            print(f"No live termpilot sessions on this machine "
                  f"({stale_hidden} stale hidden; pass --all to show).")
        else:
            print("No termpilot sessions on this machine.")
        return 0
    rows = []
    for s in sessions:
        rows.append({
            "instance": (s.get("instance") or "-")[:12],
            "sid": s.get("sid") or "-",
            "pid": str(s.get("pid")) if s.get("pid") else "-",
            "alive": "yes" if s.get("alive") else "no",
            "spool": _fmt_bytes(s.get("spool_size")),
            "local_in": "yes" if s.get("in_local_size") is not None else "no",
            "cwd": _short_cwd(s.get("cwd")),
        })
    cols = ["instance", "sid", "pid", "spool", "local_in", "cwd"]
    if args.all:
        cols.insert(3, "alive")  # only useful when stale entries are visible
    _print_table(rows, columns=cols)
    if stale_hidden:
        print(f"\n({stale_hidden} stale entries hidden; pass --all to show)")
    return 0


# ---------------------------------------------------------------------------
# tp tail
# ---------------------------------------------------------------------------

def handle_tail(argv):
    p = argparse.ArgumentParser(
        prog="tp tail",
        description="Show output from a session. Two modes: `screen` "
                    "(default) renders the worker's current terminal via "
                    "pyte - the right answer for orchestrating TUI "
                    "workers like another Claude, vim, htop, lazygit, "
                    "etc. `stream` prints raw bytes-since-offset (the "
                    "transcript view) and is what you want for pipelines "
                    "like `tp tail <sid> --mode stream | grep error`. In "
                    "stream mode, pass --raw when colour carries meaning "
                    "(zsh autosuggest, Claude's UI colour-coding) - ANSI "
                    "stripping flattens visually-distinct content to "
                    "identical text.",
        epilog="Examples:\n"
               "  tp tail abc123                  one-shot pyte render of the worker screen\n"
               "  tp tail abc123 -f               watch the worker live (screen, redraws ~2 Hz)\n"
               "  tp tail abc123 --mode stream    byte-history transcript instead of screen\n"
               "  tp tail abc123 --mode stream --raw | grep 'error'\n"
               "                                  keep colours so red errors stand out\n"
               "  tp tail abc123 -f --rows 30 --cols 160\n"
               "                                  custom virtual terminal size\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("sid", help="session id (12 hex chars; see `tp ls`).")
    p.add_argument("--mode", choices=("screen", "stream"), default=None,
                   help="`screen` = pyte-rendered terminal view (default; "
                        "best for orchestrating TUI workers like another "
                        "Claude). `stream` = byte history with ANSI "
                        "stripped (use for pipelines: "
                        "`tp tail abc --mode stream | grep error`).")
    p.add_argument("-f", "--follow", action="store_true",
                   help="keep watching; in screen mode redraws the screen "
                        "every ~0.5 s; in stream mode long-polls for new "
                        "bytes. Exit cleanly on Ctrl-C.")
    p.add_argument("--cols", type=int, default=core.DEFAULT_SCREEN_COLS,
                   help=f"screen mode: virtual terminal width (default {core.DEFAULT_SCREEN_COLS}).")
    p.add_argument("--rows", type=int, default=core.DEFAULT_SCREEN_ROWS,
                   help=f"screen mode: virtual terminal height (default {core.DEFAULT_SCREEN_ROWS}).")
    p.add_argument("--interval", type=float, default=0.5, metavar="SECS",
                   help="screen mode: redraw cadence in -f mode (default 0.5 s).")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                   help="screen mode: emit ANSI colour codes. `auto` "
                        "(default) = colour when stdout is a TTY; "
                        "`always` = colour even when piped; `never` = "
                        "plain text. Stream mode ignores this; use --raw "
                        "there.")
    # Stream-mode-only flags
    p.add_argument("--since", type=int, default=None, metavar="OFFSET",
                   help="stream mode: resume from a prior `new_offset`. "
                        "Default: start with the last --max-bytes of payload.")
    p.add_argument("--wait", type=float, default=0.0, metavar="SECS",
                   help="stream mode: long-poll for this many seconds if no "
                        "new bytes are available.")
    p.add_argument("--max-bytes", type=int, default=32768, metavar="N",
                   help="stream mode: cap on payload bytes per read "
                        "(default: 32768).")
    p.add_argument("--raw", action="store_true",
                   help="stream mode: keep ANSI escapes inline. PREFER "
                        "this when colour carries meaning the consumer "
                        "needs (zsh/fish autosuggest ghosts, Claude's UI "
                        "colour-coding, compiler severity, syntax "
                        "highlighting). Default strips for readability "
                        "but flattens visually-distinct text to identical "
                        "strings.")
    args = p.parse_args(argv)
    _install_sigpipe_default()
    mode = args.mode or "screen"
    if mode == "screen":
        return _tail_screen(args)
    return _tail_stream(args)


def _tail_screen(args):
    try:
        import pyte  # noqa: F401  validates the dep early with a clean message
    except ImportError:
        print(
            "tp tail: screen mode requires the `pyte` package "
            "(run `termpilot --activate-mcp` to install it into the venv, "
            "or use `--mode stream` for the byte-history view).",
            file=sys.stderr,
        )
        return 2
    keep_color = (
        args.color == "always"
        or (args.color == "auto" and sys.stdout.isatty())
    )
    if not args.follow:
        r = core.render_screen(args.sid, cols=args.cols, rows=args.rows,
                               keep_color=keep_color)
        sys.stdout.write(core.screen_as_text(r) + "\n")
        return 0
    # Follow mode: incremental pyte feed + redraw loop.
    import time as _time
    import pyte
    screen = pyte.Screen(args.cols, args.rows)
    stream = pyte.ByteStream(screen)
    last_off = 0
    is_tty = sys.stdout.isatty()
    try:
        while True:
            data, new_off, n = core._read_spool_range(
                args.sid, last_off, max_bytes=1024 * 1024,
            )
            if n > 0:
                stream.feed(data)
                last_off = new_off
            if is_tty:
                sys.stdout.write("\x1b[2J\x1b[H")  # clear + home
            else:
                sys.stdout.write("\n--- screen @ {:.1f}s ---\n".format(_time.monotonic()))
            if keep_color:
                for y in range(args.rows):
                    sys.stdout.write(core._row_to_ansi(screen, y, args.cols) + "\n")
            else:
                for line in screen.display:
                    sys.stdout.write(line.rstrip() + "\n")
            sys.stdout.flush()
            _time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


def _tail_stream(args):
    since = args.since
    wait_secs = args.wait if not args.follow else max(args.wait, 30.0)
    try:
        while True:
            r = core.read_output(
                args.sid, since=since, max_bytes=args.max_bytes,
                wait_secs=wait_secs, strip_ansi=not args.raw,
            )
            if r["bytes"]:
                sys.stdout.write(r["bytes"])
                sys.stdout.flush()
            since = r["new_offset"]
            if not args.follow:
                return 0
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


# ---------------------------------------------------------------------------
# tp screenshot
# ---------------------------------------------------------------------------

def handle_screenshot(argv):
    p = argparse.ArgumentParser(
        prog="tp screenshot",
        description="One-shot pyte-rendered snapshot of a session's "
                    "current terminal screen. Identical to "
                    "`tp tail <sid> --mode screen` without -f, exposed as "
                    "a top-level verb for discoverability. Colour is "
                    "preserved when stdout is a TTY (auto) so the snapshot "
                    "looks the same as the worker's screen - including "
                    "the zsh/fish autosuggest ghost colour and Claude's UI "
                    "colour-coding that distinguishes user input from "
                    "suggestions.",
    )
    p.add_argument("sid", help="session id (12 hex chars; see `tp ls`).")
    p.add_argument("--cols", type=int, default=core.DEFAULT_SCREEN_COLS)
    p.add_argument("--rows", type=int, default=core.DEFAULT_SCREEN_ROWS)
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                   help="emit ANSI colour codes. `auto` = colour when "
                        "stdout is a TTY (default); `always` = colour even "
                        "when piped (useful for less -R); `never` = plain "
                        "text only.")
    p.add_argument("--with-cursor", action="store_true",
                   help="also print cursor position (col,row) to stderr.")
    args = p.parse_args(argv)
    _install_sigpipe_default()
    try:
        import pyte  # noqa: F401
    except ImportError:
        print(
            "tp screenshot: requires the `pyte` package "
            "(run `termpilot --activate-mcp` to install it into the venv).",
            file=sys.stderr,
        )
        return 2
    keep_color = (
        args.color == "always"
        or (args.color == "auto" and sys.stdout.isatty())
    )
    try:
        r = core.render_screen(args.sid, cols=args.cols, rows=args.rows,
                               keep_color=keep_color)
    except RuntimeError as e:
        print(f"tp screenshot: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(core.screen_as_text(r) + "\n")
    if args.with_cursor:
        sys.stderr.write(f"[cursor x={r['cursor']['x']} y={r['cursor']['y']}]\n")
    return 0


# ---------------------------------------------------------------------------
# tp send
# ---------------------------------------------------------------------------
# Named-key map lives in core.py so cli, server, and unit tests share one
# source of truth (kept in lockstep with relay/lib/keyboard.js).


def _format_bytes_for_display(s):
    """Render a byte string with visible escapes for the --list-keys table."""
    out = []
    for b in s.encode("utf-8"):
        if b == 0x1b:
            out.append("\\e")
        elif b == 0x0d:
            out.append("\\r")
        elif b == 0x0a:
            out.append("\\n")
        elif b == 0x09:
            out.append("\\t")
        elif b == 0x7f:
            out.append("\\x7f")
        elif b < 0x20:
            out.append(f"\\x{b:02x}")
        else:
            out.append(chr(b))
    return "".join(out)


def _print_key_list(out=sys.stdout):
    """Emit a grouped reference matching the webapp's virtual keyboard."""
    groups = [
        ("Navigation",
         ["Up", "Down", "Left", "Right",
          "Enter", "Esc", "Tab", "ShiftTab", "Backspace", "Space",
          "Home", "End", "PgUp", "PgDn", "Del", "Ins"]),
        ("Function keys",
         [f"F{i}" for i in range(1, 13)]),
        ("Control characters",
         [f"Ctrl-{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
         + ["Ctrl-Space", "Ctrl-[", "Ctrl-\\", "Ctrl-]", "Ctrl-^", "Ctrl-_"]),
    ]
    out.write("Named keys (case-insensitive; pass via --key NAME):\n\n")
    for title, names in groups:
        out.write(f"  {title}\n")
        for name in names:
            seq = core.NAMED_KEYS.get(core._normalize_key(name))
            if seq is None:
                continue
            out.write(f"    {name:<14}  bytes:  {_format_bytes_for_display(seq):<16}\n")
        out.write("\n")
    out.write(
        "Aliases: Esc/Escape, Tab/ShiftTab/Shift-Tab/BackTab, Bs/Backspace,\n"
        "         Pgup/PageUp, Pgdn/PageDown, Del/Delete, Ins/Insert.\n"
        "\n"
        "Modifier combos (xterm encoding; what GNOME Terminal sends):\n"
        "  Ctrl-Left       \\e[1;5D      jump word back\n"
        "  Ctrl-Right      \\e[1;5C      jump word forward\n"
        "  Ctrl-Up         \\e[1;5A      scroll up\n"
        "  Ctrl-Down       \\e[1;5B      scroll down\n"
        "  Alt-a           \\ea          alt + letter (xterm meta prefix)\n"
        "  Alt-Left        \\e\\e[D       alt + arrow\n"
        "  Shift-Up        \\e[1;2A      shift-select up\n"
        "  Ctrl-Shift-Up   \\e[1;6A      ctrl + shift modifiers stack\n"
        "  Alt-F5          \\e[15;3~     modifier + function key\n"
        "  Ctrl-Space      \\x00         (already in NAMED_KEYS)\n"
        "\n"
        "Modifier syntax: prefix with `Ctrl-` / `Alt-` / `Shift-` (any order,\n"
        "case-insensitive). Multiple modifiers stack: `Ctrl-Shift-Left`,\n"
        "`Alt-Ctrl-Up`, etc. Modifier order in the input doesn't matter;\n"
        "xterm's canonical encoding is applied automatically.\n"
        "Ctrl-C, Ctrl-D, Ctrl-Z send the matching control byte\n"
        "(same as pressing them at the keyboard; the line discipline turns\n"
        "Ctrl-C into SIGINT for the foreground process).\n"
    )


_SEND_EPILOG = """\
Examples:
  tp send abc123 "yes"                type "yes" and press Enter (default).
  tp send abc123 "y" --no-newline     type "y" without Enter (cursor stays).
  tp send abc123 --key Enter          just press Enter.
  tp send abc123 --key Esc            press Escape (cancel a prompt).
  tp send abc123 --key Backspace      delete the character to the left.
  tp send abc123 --key ShiftTab       reverse-tab (navigate fields backward).
  tp send abc123 --key Ctrl-C         interrupt (same as `send_signal SIGINT`).
  tp send abc123 --key Up             arrow up (shell history, menu nav).
  tp send abc123 --key Ctrl-Left      word jump back (in shells / editors).
  tp send abc123 --key Alt-a          alt + letter (xterm meta prefix).
  tp send abc123 --key Ctrl-Shift-Up  modifiers stack (any order, any case).
  printf 'hello\\t' | tp send abc123 -    type "hello" then Tab via stdin.

The trailing Enter trap: by default `tp send <sid> "text"` appends \\r so
the worker receives "text" followed by Enter. If you're sending control
bytes via stdin or a quoted escape (`$'\\x7f'`), pass --no-newline or use
--key NAME (which implies --no-newline). Otherwise the worker sees your
backspace, then Enter, and the half-edited line gets submitted.

Run `tp send --list-keys` for the full set of named keys with their
bytes, matching the webapp's virtual keyboard one-to-one.
"""


def handle_send(argv):
    p = argparse.ArgumentParser(
        prog="tp send",
        description="Type text or a single named key into a worker session via "
                    "the local input file. Same-machine only: no relay "
                    "round-trip, no visibility to the phone view.",
        epilog=_SEND_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--list-keys", action="store_true",
                   help="print the named-key reference and exit "
                        "(no other args needed).")
    p.add_argument("sid", nargs="?", help="session id (12 hex chars; see `tp ls`).")
    p.add_argument("text", nargs="?",
                   help="text to send. Use - to read raw bytes from stdin. "
                        "Omit when --key is given.")
    p.add_argument("--key", metavar="NAME",
                   help="send a single named key (Esc, Tab, ShiftTab, Up, "
                        "Backspace, Ctrl-C, F1, etc.). Implies --no-newline. "
                        "Mutually exclusive with `text`.")
    p.add_argument("-N", "--no-newline", action="store_true",
                   help="don't append a trailing \\r (Enter) after the text. "
                        "Use when sending control bytes, partial input, or "
                        "anything where Enter would submit half-typed content.")
    args = p.parse_args(argv)

    if args.list_keys:
        _print_key_list()
        return 0

    if not args.sid:
        p.error("sid is required (run `tp ls` to see active sessions)")

    try:
        if args.key:
            if args.text is not None:
                p.error("--key and positional `text` are mutually exclusive; "
                        "send the literal key bytes, or pass text without --key.")
            r = core.send_key(args.sid, args.key)
            label = args.key
        else:
            if args.text is None:
                p.error("either `text` or --key is required "
                        "(use - for stdin, run --help for examples)")
            text = args.text
            if text == "-":
                text = sys.stdin.read()
            r = core.send_input(args.sid, text, newline=not args.no_newline)
            label = f"{r['bytes_written']} bytes"
    except ValueError as e:
        sys.stderr.write(
            f"tp send: {e}\n"
            "Run `tp send --list-keys` for the full set of named keys.\n"
        )
        return 2
    except RuntimeError as e:
        print(f"tp send: {e}", file=sys.stderr)
        return 1
    print(f"sent {label} to {args.sid}")
    return 0


# ---------------------------------------------------------------------------
# tp wait
# ---------------------------------------------------------------------------

def handle_wait(argv):
    p = argparse.ArgumentParser(
        prog="tp wait",
        description="Block until a worker session goes idle or a pattern "
                    "matches its output. Idle defaults to screen-mode "
                    "(pyte-rendered screen stops changing modulo spinner / "
                    "timer animation) - the right answer for TUI workers "
                    "like another Claude whose spool grows constantly from "
                    "redraws.",
    )
    p.add_argument("sid", help="session id.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--idle", type=float, metavar="SECS",
                   help="wait until the rendered screen has been unchanged "
                        "for N seconds (animation-aware).")
    g.add_argument("--pattern", metavar="REGEX",
                   help="wait until new (stripped) output matches this regex.")
    p.add_argument("--timeout", type=float, default=600.0, metavar="SECS",
                   help="give up after this many seconds (default: 600).")
    p.add_argument("--idle-mode", choices=("screen", "bytes"), default="screen",
                   help="how to detect idle. `screen` (default) = rendered "
                        "view stops changing modulo animation. `bytes` = "
                        "strict spool-size silence (use for non-TUI workers).")
    p.add_argument("--cols", type=int, default=core.DEFAULT_SCREEN_COLS,
                   help="virtual terminal width for screen-mode idle.")
    p.add_argument("--rows", type=int, default=core.DEFAULT_SCREEN_ROWS,
                   help="virtual terminal height for screen-mode idle.")
    p.add_argument("--raw", action="store_true",
                   help="don't strip ANSI escapes when pattern-matching.")
    args = p.parse_args(argv)
    _install_sigpipe_default()
    try:
        if args.idle is not None:
            r = core.wait_for_idle(
                args.sid, quiet_secs=args.idle, timeout=args.timeout,
                idle_mode=args.idle_mode, cols=args.cols, rows=args.rows,
            )
        else:
            r = core.wait_for_output(
                args.sid, pattern=args.pattern, timeout=args.timeout,
                strip_ansi=not args.raw,
            )
    except TimeoutError as e:
        print(f"tp wait: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if r.get("mode") == "screen":
        sys.stdout.write("\n".join(r["lines"]) + "\n")
    else:
        sys.stdout.write(r.get("bytes", ""))
        if r.get("bytes") and not r["bytes"].endswith("\n"):
            sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------
# tp mcp-serve  (stub until Phase 3)
# ---------------------------------------------------------------------------

def handle_mcp_serve(argv):
    p = argparse.ArgumentParser(
        prog="tp mcp-serve",
        description="Run the termpilot stdio MCP server. Invoked by Claude Code "
                    "via its MCP config; you normally don't run this by hand.",
    )
    p.parse_args(argv)  # handles --help; no positional args
    try:
        from termpilot_mcp import server  # noqa: F401
    except ImportError:
        print(
            "tp mcp-serve: termpilot_mcp.server is not implemented yet "
            "(Phase 3). Install termpilot_mcp/requirements.txt and rerun "
            "once server.py lands.",
            file=sys.stderr,
        )
        return 2
    return server.main(argv)


# ---------------------------------------------------------------------------
# tp transcript
# ---------------------------------------------------------------------------

def handle_transcript(argv):
    p = argparse.ArgumentParser(
        prog="tp transcript",
        description="Dump the worker's full session transcript via pyte's "
                    "HistoryScreen: every line that's scrolled past the top "
                    "of the terminal plus the current visible screen, "
                    "rendered clean (no TUI redraw noise). Use this when "
                    "you need to audit what the worker has done across an "
                    "entire session, not just a snapshot. Slow on multi-MB "
                    "spools (pyte runs at ~1 MB/s); intended for one-shot "
                    "audit-style use, not live tailing.",
        epilog="Examples:\n"
               "  tp transcript abc123                         dump full history to stdout\n"
               "  tp transcript abc123 > worker.log            save to a file\n"
               "  tp transcript abc123 --scrollback 20000      retain more older lines\n"
               "  tp transcript abc123 --color always | less -R\n"
               "                                               keep colours, page in less\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("sid", help="session id (12 hex chars; see `tp ls`).")
    p.add_argument("--cols", type=int, default=core.DEFAULT_SCREEN_COLS)
    p.add_argument("--rows", type=int, default=core.DEFAULT_SCREEN_ROWS)
    p.add_argument("--scrollback", type=int, default=core.DEFAULT_HISTORY_SCROLLBACK,
                   help=f"max scrolled-off lines to retain (default "
                        f"{core.DEFAULT_HISTORY_SCROLLBACK}; older lines fall "
                        "off the top FIFO if the session is longer).")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                   help="emit ANSI colour codes; `auto` = colour on TTY.")
    args = p.parse_args(argv)
    _install_sigpipe_default()
    try:
        import pyte  # noqa: F401
    except ImportError:
        print(
            "tp transcript: requires the `pyte` package "
            "(run `termpilot --activate-mcp` to install it into the venv).",
            file=sys.stderr,
        )
        return 2
    keep_color = (
        args.color == "always"
        or (args.color == "auto" and sys.stdout.isatty())
    )
    try:
        r = core.render_history(
            args.sid, cols=args.cols, rows=args.rows,
            scrollback=args.scrollback, keep_color=keep_color,
        )
    except RuntimeError as e:
        print(f"tp transcript: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(core.history_as_text(r) + "\n")
    if r["truncated"]:
        sys.stderr.write(
            f"[note: session is longer than --scrollback ({args.scrollback}); "
            "oldest lines fell off the top. Pass a larger --scrollback to "
            "capture more.]\n"
        )
    return 0


# ---------------------------------------------------------------------------
# tp gc - garbage-collect forgotten wrappers
# ---------------------------------------------------------------------------
# Built because of an upstream Blackbox bug: closing a tab visually
# doesn't always release the underlying PTY, so the bash + termpilot
# inside keep running even though nothing's at the keyboard. tp gc gives
# the user a manual lever to clean these up by age. Dry-run by default;
# --kill required to actually terminate anything.

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_DURATION_MULT = {"s": 1, "": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(s):
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(
            f"invalid duration {s!r}; expected forms: 90s, 30m, 2h, 1d"
        )
    return float(m.group(1)) * _DURATION_MULT[m.group(2)]


def _fmt_duration(secs):
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        h, rem = divmod(int(secs), 3600)
        return f"{h}h{rem // 60}m"
    d, rem = divmod(int(secs), 86400)
    return f"{d}d{rem // 3600}h"


def _process_age_seconds(pid):
    """Age in seconds since the process started, from /proc.

    /proc/<pid>/stat field 22 (starttime) is clock-ticks since boot;
    /proc/uptime is seconds since boot. Comm field is parenthesised and
    can contain spaces/parens itself, so we anchor on the last `)`.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        end = data.rindex(b")")
        after = data[end + 2:].split()
        starttime_ticks = int(after[19])
        clock_ticks = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        return uptime - (starttime_ticks / clock_ticks)
    except (OSError, ValueError, IndexError):
        return None


def _current_tty_instance():
    """Return the instance label the wrapper would use for the current TTY
    (e.g. 'pts-7'), or None if stdin isn't a TTY."""
    try:
        tty = os.ttyname(0)
    except OSError:
        return None
    return tty.removeprefix("/dev/").replace("/", "-")


def _last_out_seconds(sid):
    """Seconds since the worker's output spool was last written. None if
    no spool exists yet (brand-new wrapper)."""
    if not sid:
        return None
    path = os.path.join(core.CACHE_BASE, "sid", sid, "out.spool")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    delta = time.time() - mtime
    return delta if delta >= 0 else 0.0


def handle_gc(argv):
    p = argparse.ArgumentParser(
        prog="tp gc",
        description="Inspect and (optionally) garbage-collect live "
                    "termpilot wrappers. With no filter, lists every live "
                    "wrapper with age and last-output time so you can spot "
                    "the forgotten ones. Add --older-than DURATION to "
                    "filter; add --kill to terminate the filtered set. The "
                    "current terminal is excluded by default so `tp gc "
                    "--kill` from a terminal won't self-immolate.",
        epilog="Examples:\n"
               "  tp gc\n"
               "    list all live wrappers with age + last-output columns\n"
               "  tp gc --older-than 2h\n"
               "    filter to wrappers older than 2 hours (dry-run)\n"
               "  tp gc --older-than 24h --kill\n"
               "    terminate anything older than a day\n"
               "  tp gc --older-than 0s --kill --include-current\n"
               "    nuke EVERY live wrapper, this terminal included\n"
               "  tp gc --signal KILL --older-than 1h --kill\n"
               "    use SIGKILL (default SIGTERM); only if TERM didn't take\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--older-than", metavar="DURATION", default=None,
                   help="age threshold (forms: 90s, 30m, 2h, 1d). Required "
                        "when --kill is given (safety: refuse to terminate "
                        "the unfiltered set by accident). Pass "
                        "--older-than 0s to mean 'all ages'.")
    p.add_argument("--kill", action="store_true",
                   help="actually terminate matching wrappers (requires "
                        "--older-than). Default is dry-run / list-only.")
    p.add_argument("--signal", default="TERM", metavar="NAME",
                   help="signal to send (without SIG prefix). Default: TERM. "
                        "Use KILL only if a polite TERM didn't take.")
    p.add_argument("--include-current", action="store_true",
                   help="also consider the wrapper attached to this terminal "
                        "(default: excluded to avoid self-immolation).")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of a table.")
    args = p.parse_args(argv)

    if args.kill and args.older_than is None:
        sys.stderr.write(
            "tp gc: --kill requires --older-than to avoid accidentally "
            "terminating every wrapper.\n"
            "       Pass --older-than 0s if you really want 'all ages'.\n"
        )
        return 2

    threshold = None
    if args.older_than is not None:
        try:
            threshold = _parse_duration(args.older_than)
        except ValueError as e:
            sys.stderr.write(f"tp gc: {e}\n")
            return 2

    try:
        sig = getattr(_signal, "SIG" + args.signal.upper())
    except AttributeError:
        sys.stderr.write(
            f"tp gc: unknown signal name {args.signal!r}. "
            "Try TERM, INT, HUP, KILL.\n"
        )
        return 2

    current_instance = None if args.include_current else _current_tty_instance()

    all_sessions = core.list_sessions()
    candidates = []
    excluded_current = 0
    for s in all_sessions:
        if not s.get("alive"):
            continue
        pid = s.get("pid")
        if not pid:
            continue
        if current_instance and s.get("instance") == current_instance:
            excluded_current += 1
            continue
        age = _process_age_seconds(pid)
        if age is None:
            continue
        if threshold is not None and age < threshold:
            continue
        s = dict(s)
        s["age_secs"] = age
        s["last_out_secs"] = _last_out_seconds(s.get("sid"))
        candidates.append(s)

    candidates.sort(key=lambda s: s["age_secs"], reverse=True)

    if not candidates:
        if threshold is not None:
            excl = (f" (excluding current terminal {current_instance})"
                    if current_instance else "")
            print(f"No candidates: no live wrappers older than "
                  f"{args.older_than}{excl}.")
        else:
            note = (f" (excluded current terminal {current_instance})"
                    if excluded_current else "")
            print(f"No live wrappers on this machine{note}.")
        return 0

    if args.json:
        print(json.dumps(candidates, indent=2, default=str))
    else:
        rows = []
        for s in candidates:
            last_out = s["last_out_secs"]
            rows.append({
                "age": _fmt_duration(s["age_secs"]),
                "last_out": _fmt_duration(last_out) if last_out is not None else "-",
                "pid": str(s["pid"]),
                "instance": (s.get("instance") or "-")[:12],
                "sid": s.get("sid") or "-",
                "cwd": _short_cwd(s.get("cwd")),
            })
        _print_table(rows,
                     columns=["age", "last_out", "pid", "instance", "sid", "cwd"])
        if excluded_current and not args.include_current:
            print(f"\n(excluded current terminal: {current_instance}; "
                  "pass --include-current to include it)")

    if not args.kill:
        if threshold is None:
            print(f"\nListing only. Pass --older-than DURATION (and --kill) "
                  "to terminate a subset.")
        else:
            print(f"\nDry-run: {len(candidates)} wrapper(s) would be killed "
                  f"with SIG{args.signal.upper()}. Pass --kill to act.")
        return 0

    killed = 0
    failed = []
    for s in candidates:
        try:
            os.kill(s["pid"], sig)
            killed += 1
        except OSError as e:
            failed.append((s["pid"], str(e)))
    print(f"\nSent SIG{args.signal.upper()} to {killed}/{len(candidates)} wrapper(s).")
    for pid, err in failed:
        sys.stderr.write(f"  pid {pid}: {err}\n")
    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# Top-level dispatcher (for `python -m termpilot_mcp.cli ...`)
# ---------------------------------------------------------------------------

_HANDLERS = {
    "ls": handle_ls,
    "tail": handle_tail,
    "screenshot": handle_screenshot,
    "transcript": handle_transcript,
    "gc": handle_gc,
    "send": handle_send,
    "wait": handle_wait,
    "mcp-serve": handle_mcp_serve,
}


def _print_usage(out=sys.stdout):
    out.write(
        "usage: tp <command> [args]\n"
        "\n"
        "Termpilot orchestration commands (same-machine):\n"
        "  ls                 List active termpilot sessions on this machine.\n"
        "  tail <sid>         Watch a session (screen mode default).\n"
        "  screenshot <sid>   One-shot pyte-rendered snapshot of the worker's screen.\n"
        "  transcript <sid>   Full session history via pyte scrollback (audit view).\n"
        "  gc                 Garbage-collect forgotten wrappers by age.\n"
        "  send <sid> <text>  Type text into a session (local; no relay).\n"
        "  wait <sid>         Block until session is idle or a pattern matches.\n"
        "  mcp-serve          Run the stdio MCP server (called by Claude Code).\n"
        "\n"
        "Run `tp <command> --help` for command-specific options.\n"
    )


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        _print_usage(sys.stdout if argv else sys.stderr)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd not in _HANDLERS:
        print(f"tp: unknown subcommand {cmd!r}", file=sys.stderr)
        _print_usage(sys.stderr)
        return 2
    return _HANDLERS[cmd](rest)


if __name__ == "__main__":
    sys.exit(main())
