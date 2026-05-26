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
        description="List termpilot wrappers registered on this machine.",
    )
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of a table (for scripting / MCP).")
    p.add_argument("--alive", action="store_true",
                   help="show only sessions whose wrapper process is currently alive.")
    args = p.parse_args(argv)
    sessions = core.list_sessions()
    if args.alive:
        sessions = [s for s in sessions if s.get("alive")]
    if args.json:
        print(json.dumps(sessions, indent=2, default=str))
        return 0
    if not sessions:
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
    _print_table(rows, columns=["instance", "sid", "pid", "alive", "spool", "local_in", "cwd"])
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
        description="Block until a worker session goes idle or a pattern matches its output.",
    )
    p.add_argument("sid", help="session id.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--idle", type=float, metavar="SECS",
                   help="wait until N seconds have passed without spool growth.")
    g.add_argument("--pattern", metavar="REGEX",
                   help="wait until new (stripped) output matches this regex.")
    p.add_argument("--timeout", type=float, default=600.0, metavar="SECS",
                   help="give up after this many seconds (default: 600).")
    p.add_argument("--raw", action="store_true",
                   help="don't strip ANSI escapes when pattern-matching.")
    args = p.parse_args(argv)
    _install_sigpipe_default()
    try:
        if args.idle is not None:
            r = core.wait_for_idle(args.sid, quiet_secs=args.idle, timeout=args.timeout)
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
    sys.stdout.write(r["bytes"])
    if not r["bytes"].endswith("\n"):
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
# Top-level dispatcher (for `python -m termpilot_mcp.cli ...`)
# ---------------------------------------------------------------------------

_HANDLERS = {
    "ls": handle_ls,
    "tail": handle_tail,
    "screenshot": handle_screenshot,
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
        "  tail <sid>         Watch a session (screen mode in -f, stream mode for pipes).\n"
        "  screenshot <sid>   One-shot pyte-rendered snapshot of the worker's screen.\n"
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
