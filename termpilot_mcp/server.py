"""termpilot stdio MCP server.

Exposes the data plane in `termpilot_mcp/core.py` as MCP tools so a Claude
Code session can discover, observe, and orchestrate another Claude
running in a termpilot terminal on the same machine.

The tool descriptions are the load-bearing prose here; they ride in
every Claude session that has this server enabled and tell the model when
to reach for orchestration. Keep them tight, concrete, and honest about
each tool's purpose.

Run via stdio (`tp mcp-serve` or `python -m termpilot_mcp.server`); the
MCP client launches the process and pipes JSON-RPC over stdin/stdout.
"""

import asyncio
import json
import sys
from pathlib import Path

# Ensure sibling-module imports work when invoked from arbitrary cwd.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from termpilot_mcp import core  # noqa: E402

from mcp.server import Server, NotificationOptions  # noqa: E402
from mcp.server.models import InitializationOptions  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
import mcp.types as types  # noqa: E402


SERVER_NAME = "termpilot"
SERVER_VERSION = "0.1.0"

# The server's top-level instructions, visible to the boss-Claude as part
# of the MCP advertisement at session start. This is the discovery hook:
# the user says "watch another Claude," boss reads this and reaches for
# the tools without needing per-project coaching.
SERVER_INSTRUCTIONS = (
    "Lets one Claude observe and orchestrate another Claude running in a "
    "termpilot terminal session on this machine. Use these tools when the "
    "user wants one Claude session to supervise, monitor, review, "
    "interrupt, or coordinate the work of another Claude in a separate "
    "terminal (typically started with `tp claude`). Identify the other "
    "session by its session id (sid) from `list_sessions`. Read the "
    "worker's output with `read_output`, block on milestones with "
    "`wait_for_idle` or `wait_for_output`, interrupt with `send_signal`, "
    "type into it with `send_input`."
)


server = Server(SERVER_NAME, version=SERVER_VERSION, instructions=SERVER_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------

_SID_SCHEMA = {
    "type": "string",
    "description": (
        "Session id (12 hex chars) from `list_sessions`. Identifies which "
        "termpilot session the call targets."
    ),
    "pattern": "^[0-9a-f]{12}$",
}


TOOLS = [
    types.Tool(
        name="list_sessions",
        description=(
            "List termpilot terminal sessions on this machine. Returns each "
            "session's sid (12-char id used by every other tool), cwd, pid, "
            "alive flag, and local_in flag. Call this first when the user "
            "wants you to watch or orchestrate another Claude; the sid is "
            "the handle for every other tool here. local_in='no' means that "
            "session is on an older termpilot build that can't accept "
            "`send_input` until it restarts."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="read_output",
        description=(
            "Read new byte-history output from a session (the transcript "
            "of what was printed). For TUI workers - another Claude, vim, "
            "htop, etc. - prefer `read_screen`, which returns the "
            "current rendered screen; read_output on a TUI returns the "
            "raw redraws (e.g. the spinner character repeated 12 times).\n"
            "\n"
            "On the first call pass since=null to receive a tail of the "
            "most recent ~32 KB; save the returned new_offset and pass it "
            "as `since` on subsequent calls to get only strictly-new "
            "bytes. If wait_secs > 0 and no new data is available, blocks "
            "up to that many seconds. Returns JSON with `bytes` (the "
            "text), `new_offset` (for the next call), and `frames_read`.\n"
            "\n"
            "*** strip_ansi: prefer false when colour carries meaning ***\n"
            "Default true gives clean readable text but flattens visually-"
            "distinct content to identical strings. Pass strip_ansi=false "
            "when colour is semantically load-bearing. Common cases: "
            "(1) shell autosuggest (zsh-autosuggestions, fish) where the "
            "ghost completion is rendered in a dim grey - after stripping, "
            "typed text and ghost suggestion look identical and the model "
            "can't tell what the user actually pressed; (2) Claude's own "
            "UI, which colour-codes user input vs ghost suggestions vs "
            "tool output vs system messages; (3) compiler/linter/test "
            "severity (red errors, yellow warnings); (4) syntax-"
            "highlighted code. With strip_ansi=false the raw ANSI codes "
            "stay inline in `bytes` - harder for a human to read but "
            "preserves the semantics. The model can scan for `\\x1b[3?m` "
            "colour codes to disambiguate."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sid": _SID_SCHEMA,
                "since": {
                    "type": ["integer", "null"],
                    "description": (
                        "Byte offset from a previous `new_offset`. null = start "
                        "with a 32 KB tail of the most recent output."
                    ),
                    "default": None,
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Cap on bytes returned per call. Default 32768.",
                    "default": 32768,
                },
                "wait_secs": {
                    "type": "number",
                    "description": (
                        "If no new output is available, block up to this many "
                        "seconds before returning. Default 0 (no wait)."
                    ),
                    "default": 0.0,
                },
                "strip_ansi": {
                    "type": "boolean",
                    "description": (
                        "Strip ANSI escape sequences and control bytes "
                        "from the returned text. Default true. PASS FALSE "
                        "when colour carries meaning (zsh/fish autosuggest "
                        "ghost text, Claude's UI colour-coding of user "
                        "input vs suggestions, compiler severity, syntax "
                        "highlighting). See the tool description for the "
                        "full rationale."
                    ),
                    "default": True,
                },
            },
            "required": ["sid"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="read_screen",
        description=(
            "Render the worker's current terminal screen via pyte (a VT100 "
            "emulator) and return it as text. This is the right tool when "
            "you want to know what the worker is showing RIGHT NOW (a "
            "snapshot of their terminal), not a transcript of every byte.\n"
            "\n"
            "Always prefer this over `read_output` for TUI workers - "
            "another Claude, vim, htop, lazygit, etc. - because those "
            "redraw in place. read_output on a TUI returns noisy "
            "concatenated redraws (e.g. the spinner character 12 times); "
            "read_screen collapses everything to the current state.\n"
            "\n"
            "Use `read_output` instead when you need: a byte-history "
            "transcript, regex-matching across recent output, or output "
            "from non-TUI commands where in-place redraws aren't a "
            "factor.\n"
            "\n"
            "Returns: lines (array of plain strings, one per row); "
            "lines_ansi (same rows with inline ANSI colour codes, only "
            "present when keep_color=true); cursor (x,y); size "
            "(cols,rows); and bytes_fed/frames_fed/spool_end for "
            "diagnostics. Default size is 200 cols x 20 rows - wider than "
            "most terminals, fits Claude's UI cleanly. For high-volume "
            "scrolling content (code diffs, test output, git log), bump "
            "rows (e.g. rows=80) so older lines stay visible before "
            "they scroll past the top of the virtual screen.\n"
            "\n"
            "*** cheap polling: since_spool_end + wait_secs ***\n"
            "Pass since_spool_end (the spool_end you got from a previous "
            "call) to short-circuit when nothing's changed. If the spool "
            "hasn't grown, the response is just "
            "{unchanged:true, spool_end:X} - no `lines` re-rendered, no "
            "tokens wasted on identical content. Combine with wait_secs>0 "
            "to block server-side until the worker produces something or "
            "the timeout fires. This is the cheap polling primitive: "
            "boss-Claude can call read_screen(sid, since_spool_end=last, "
            "wait_secs=30) in a loop and only burn tokens when there's "
            "actually new content to read.\n"
            "\n"
            "*** keep_color: pass true when colour carries meaning ***\n"
            "Default false (plain text, cheap on tokens). Pass true when "
            "the worker uses colour to distinguish content the model needs "
            "to read apart: zsh/fish autosuggest ghost completions (dim "
            "grey vs typed bright), Claude's user-vs-suggestion colour-"
            "coding, compiler severity, syntax highlighting. With "
            "keep_color=true the returned `lines_ansi` has SGR escapes "
            "inline (`\\x1b[3?m` foreground, `\\x1b[1m` bold, etc.) so the "
            "model can grep for colour codes to disambiguate."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sid": _SID_SCHEMA,
                "cols": {
                    "type": "integer",
                    "description": "Virtual terminal width. Default 200.",
                    "default": 200,
                },
                "rows": {
                    "type": "integer",
                    "description": "Virtual terminal height. Default 20.",
                    "default": 20,
                },
                "keep_color": {
                    "type": "boolean",
                    "description": (
                        "When true, also returns `lines_ansi` with inline "
                        "ANSI/SGR codes that preserve colour, bold, etc. "
                        "Default false. PASS TRUE when colour carries "
                        "meaning (autosuggest ghosts, Claude UI colour-"
                        "coding, severity highlighting)."
                    ),
                    "default": False,
                },
                "since_spool_end": {
                    "type": ["integer", "null"],
                    "description": (
                        "The `spool_end` value from a previous read_screen "
                        "call. Server returns {unchanged:true} if the "
                        "spool hasn't grown past this offset - avoids "
                        "re-rendering identical screens. Default null."
                    ),
                    "default": None,
                },
                "wait_secs": {
                    "type": "number",
                    "description": (
                        "If since_spool_end is set and the spool is still "
                        "at that offset, block server-side for up to this "
                        "many seconds waiting for new content. Default 0 "
                        "(don't block). Pair with since_spool_end for "
                        "cheap long-poll loops."
                    ),
                    "default": 0.0,
                },
            },
            "required": ["sid"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="send_input",
        description=(
            "Type free-form text into a session (same effect as if the user "
            "pressed those keys at the keyboard). Default appends Enter "
            "(\\r); set newline=false to send raw bytes only. Same-machine "
            "only: writes to a local file the wrapper polls, no relay "
            "round-trip, no visibility in the phone view.\n"
            "\n"
            "Trailing-Enter trap: when sending control bytes embedded in "
            "text, always pass newline=false. Otherwise the worker receives "
            "your bytes THEN Enter, submitting any half-edited line.\n"
            "\n"
            "For a single named key (Esc, Tab, ShiftTab, Backspace, arrows, "
            "Ctrl-C, etc.) use `send_key` instead - it's friendlier and "
            "implicitly skips the trailing Enter. Use `send_input` for "
            "plain text, multi-key sequences (e.g. \"hello\\t\" for type-"
            "then-Tab), or sending a JSON-escaped raw byte you need to "
            "inline. The byte map is the same as `send_key` accepts (see "
            "`tp send --list-keys` for the canonical reference)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sid": _SID_SCHEMA,
                "text": {
                    "type": "string",
                    "description": "Text to type into the session.",
                },
                "newline": {
                    "type": "boolean",
                    "description": (
                        "Append a trailing carriage return (Enter). Default true."
                    ),
                    "default": True,
                },
            },
            "required": ["sid", "text"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="send_key",
        description=(
            "Send a single named key (optionally with modifiers) to a "
            "session. Case-insensitive, automatically uses newline=false.\n"
            "\n"
            "Base names: Esc, Tab, ShiftTab, Up, Down, Left, Right, Enter, "
            "Backspace, Home, End, PgUp, PgDn, Del, Ins, Space, F1..F12, "
            "Ctrl-A..Ctrl-Z, Ctrl-Space, and friends.\n"
            "\n"
            "Modifier combos: prefix with Ctrl-, Alt-, and/or Shift- (any "
            "order, any case). Examples: Ctrl-Left (word jump back), "
            "Ctrl-Right (word jump forward), Alt-a (xterm meta prefix), "
            "Ctrl-Shift-Up (modifiers stack), Alt-F5, Shift-PgUp. Uses "
            "xterm/CSI encoding (e.g. Ctrl-Left = \\u001b[1;5D), which is "
            "what GNOME Terminal / iTerm / Claude / readline-based shells "
            "all expect.\n"
            "\n"
            "Prefer this over `send_input` for single keys: friendlier and "
            "no trailing-Enter trap. Use `send_input` for plain text or "
            "multi-key sequences. `send_key Ctrl-C` writes the same byte "
            "(0x03) as `send_signal SIGINT` -- pick whichever reads "
            "clearer for what you're doing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sid": _SID_SCHEMA,
                "key": {
                    "type": "string",
                    "description": (
                        "Named key. See send_input description for the full "
                        "byte map. Common: Esc, Tab, ShiftTab, Backspace, "
                        "Up, Down, Left, Right, Enter, Ctrl-C."
                    ),
                },
            },
            "required": ["sid", "key"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="send_signal",
        description=(
            "Send a signal to a session. SIGINT/SIGQUIT/SIGTSTP go through the "
            "terminal's line discipline (the same way Ctrl-C / Ctrl-\\ / "
            "Ctrl-Z would work if typed by the user); they reach whatever "
            "program is running inside the session, including Claude. SIGTERM "
            "and SIGHUP go directly to the wrapper process, cleanly ending the "
            "session. SIGKILL is intentionally not supported. Most common use: "
            "SIGINT to interrupt the worker if it's stuck or off-track."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sid": _SID_SCHEMA,
                "signal": {
                    "type": "string",
                    "description": (
                        "Signal name. SIGINT (interrupt), SIGQUIT (quit + core), "
                        "SIGTSTP (suspend) go via Ctrl-key; SIGTERM, SIGHUP end "
                        "the session."
                    ),
                    "enum": ["SIGINT", "SIGQUIT", "SIGTSTP", "SIGTERM", "SIGHUP"],
                    "default": "SIGINT",
                },
            },
            "required": ["sid"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="wait_for_idle",
        description=(
            "Block until the session has produced no new output for quiet_secs "
            "consecutive seconds, then return the trailing output. Use this as "
            "a long-poll primitive when waiting for the worker to finish a step "
            "before reviewing or commenting. One tool call per work cycle, "
            "much cheaper than tight polling with read_output."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sid": _SID_SCHEMA,
                "quiet_secs": {
                    "type": "number",
                    "description": (
                        "How many consecutive seconds of silence count as 'idle.' "
                        "Default 15."
                    ),
                    "default": 15.0,
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        "Max seconds to wait for an idle window before giving up. "
                        "Default 600."
                    ),
                    "default": 600.0,
                },
            },
            "required": ["sid"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="wait_for_output",
        description=(
            "Block until new output matches a regex, then return the matched "
            "window. Use this when the worker emits a known sentinel "
            "('All checks passed', 'Done.', a Claude prompt glyph) and you "
            "want to act exactly when it appears. The match runs against "
            "ANSI-stripped text unless strip_ansi=false."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sid": _SID_SCHEMA,
                "pattern": {
                    "type": "string",
                    "description": (
                        "Python regex (re.search semantics) matched against new "
                        "stripped output."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": "Max seconds to wait for a match. Default 600.",
                    "default": 600.0,
                },
                "strip_ansi": {
                    "type": "boolean",
                    "description": "Strip ANSI before matching. Default true.",
                    "default": True,
                },
            },
            "required": ["sid", "pattern"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="tail_events",
        description=(
            "Read entries from termpilot's diagnostic event log. Categories "
            "include wrapper_start, local_input, input_wedge, "
            "preserve_spool_on_exit. Useful for debugging when a session is "
            "misbehaving. Most orchestration tasks don't need this."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sid": {
                    "type": ["string", "null"],
                    "description": (
                        "Filter to events with this sid. null = all sessions."
                    ),
                    "default": None,
                },
                "limit": {
                    "type": "integer",
                    "description": "Most-recent N matching entries. Default 50.",
                    "default": 50,
                },
                "cat": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": (
                        "Filter to these category names. null = all categories."
                    ),
                    "default": None,
                },
            },
            "additionalProperties": False,
        },
    ),
]


@server.list_tools()
async def _list_tools():
    return TOOLS


def _json_text(obj):
    return [types.TextContent(
        type="text",
        text=json.dumps(obj, default=str, indent=2),
    )]


def _err(msg):
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"error: {msg}")],
        isError=True,
    )


@server.call_tool()
async def _call_tool(name, arguments):
    args = arguments or {}
    try:
        if name == "list_sessions":
            return _json_text(core.list_sessions())
        if name == "read_output":
            r = core.read_output(
                args["sid"],
                since=args.get("since"),
                max_bytes=args.get("max_bytes", 32768),
                wait_secs=args.get("wait_secs", 0.0),
                strip_ansi=args.get("strip_ansi", True),
            )
            return _json_text(r)
        if name == "read_screen":
            r = core.render_screen(
                args["sid"],
                cols=args.get("cols", 200),
                rows=args.get("rows", 20),
                keep_color=args.get("keep_color", False),
                since_spool_end=args.get("since_spool_end"),
                wait_secs=args.get("wait_secs", 0.0),
            )
            return _json_text(r)
        if name == "send_input":
            r = core.send_input(
                args["sid"], args["text"],
                newline=args.get("newline", True),
            )
            return _json_text(r)
        if name == "send_key":
            r = core.send_key(args["sid"], args["key"])
            return _json_text({**r, "key": args["key"]})
        if name == "send_signal":
            r = core.send_signal(args["sid"], args.get("signal", "SIGINT"))
            return _json_text(r)
        if name == "wait_for_idle":
            r = core.wait_for_idle(
                args["sid"],
                quiet_secs=args.get("quiet_secs", 15.0),
                timeout=args.get("timeout", 600.0),
            )
            return _json_text(r)
        if name == "wait_for_output":
            r = core.wait_for_output(
                args["sid"], args["pattern"],
                timeout=args.get("timeout", 600.0),
                strip_ansi=args.get("strip_ansi", True),
            )
            return _json_text(r)
        if name == "tail_events":
            r = core.tail_events(
                sid=args.get("sid"),
                limit=args.get("limit", 50),
                cat=args.get("cat"),
            )
            return _json_text(r)
        return _err(f"unknown tool: {name}")
    except TimeoutError as e:
        return _err(f"timeout: {e}")
    except (RuntimeError, ValueError, KeyError) as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

async def _run_stdio():
    async with stdio_server() as (read_stream, write_stream):
        init_opts = InitializationOptions(
            server_name=SERVER_NAME,
            server_version=SERVER_VERSION,
            instructions=SERVER_INSTRUCTIONS,
            capabilities=server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )
        await server.run(read_stream, write_stream, init_opts)


def main(argv=None):
    """Sync entry point for `tp mcp-serve` and `python -m termpilot_mcp.server`."""
    try:
        asyncio.run(_run_stdio())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
