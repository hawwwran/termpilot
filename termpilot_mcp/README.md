# termpilot_mcp

A local Model Context Protocol server (and matching CLI) that lets one Claude
Code session observe and orchestrate another Claude running inside a termpilot
terminal on the same machine.

Use case: start a *worker* Claude with `tp claude` and give it a long task. In
a separate terminal, start a *boss* Claude with the termpilot MCP enabled and
tell it to watch the worker, review when the worker finishes, interrupt if it
goes off the rails, then prompt it to commit and push. The boss sees the
worker's output by tailing termpilot's local plaintext output spool, and types
input back through the existing relay protocol.

For the engineering rationale and protocol details, see
[../ARCHITECTURE.md](../ARCHITECTURE.md). The CLI and MCP both call into
`termpilot_mcp/core.py`; the package layout and reusable utilities are
documented there.

## Install

The CLI (`tp ls / tail / send / wait`) ships with termpilot itself and is
stdlib-only; nothing extra to install if `tp` is on your PATH.

For the MCP server, run the one-shot command:

```sh
termpilot --activate-mcp
```

That creates a dedicated venv at `~/.local/share/termpilot/mcp-venv/`,
installs the `mcp` PyPI package into it (sidestepping PEP 668 on modern
Debian/Ubuntu/Zorin), and registers the server with Claude Code at user
scope so every Claude session on this machine picks it up automatically.

Reverse it any time:

```sh
termpilot --deactivate-mcp           # unregister, keep the venv
termpilot --deactivate-mcp --purge   # unregister and delete the venv
```

Open a new Claude Code session; the `termpilot` MCP tools are advertised
automatically. Ask the boss to "watch another Claude" and it should call
`list_sessions` unprompted.

### Doing it by hand

If you'd rather skip `--activate-mcp` and wire it up yourself:

```sh
python3 -m venv ~/.local/share/termpilot/mcp-venv
~/.local/share/termpilot/mcp-venv/bin/pip install \
    -r ~/.local/share/termpilot/termpilot_mcp/requirements.txt
claude mcp add termpilot -s user -- \
    ~/.local/share/termpilot/mcp-venv/bin/python \
    ~/.local/share/termpilot/termpilot-wrap \
    mcp-serve
```

The wrapper invocation (`termpilot-wrap mcp-serve`) is what
`--activate-mcp` registers too. The wrapper handles its own `sys.path`
probe, so `termpilot_mcp/` next to it (release-zip flat layout) or one
level up (dev-tree layout) is found without `PYTHONPATH`.

## CLI

The same primitives are exposed as `tp` subcommands for shell use and
debugging. Run `tp <command> --help` for full flag listings.

| Command | What it does |
| --- | --- |
| `tp ls` | List active termpilot wrappers on this machine (sid, cwd, instance, pid, alive) |
| `tp tail <sid>` | Print new output from a session's local spool. `--follow` long-polls; `--since N` starts from byte offset N; `--raw` skips ANSI stripping |
| `tp send <sid> "text"` | Type input into a worker session via the relay |
| `tp wait <sid>` | Block until the worker has been silent for `--idle SECS` or until `--pattern REGEX` matches new output |
| `tp mcp-serve` | Run the stdio MCP server (invoked by Claude Code, not by humans) |

## Security model

Boss has read access to the worker's plaintext output spool because it
runs as the same Linux user that owns `~/.cache/termpilot/` (mode 0700).
It can type into the worker because it can append to a 0600 file in the
same directory. There is no privilege escalation: a process running as
your user could already read your keyring token (32-byte AES key sitting
in GNOME Keyring or `~/.config/termpilot/token`), so letting it read the
spool is the same trust boundary, and letting it type into the worker is
no different from letting it use `xdotool` against any of your
terminals.

What this server does *not* do: bypass the relay's bearer auth, decrypt
other users' sessions, talk to any session it doesn't have OS-level
read/write access to, or write input visible in the phone view.
Same-machine only by design: when the boss writes to `in.local`, the
wrapper's local poller picks it up and writes to the PTY without any
relay round-trip, so the relay (and any phone tailing it) never sees
boss prompts.

Worker wrappers running with `--no-local-input` opt out of this channel
entirely; `tp send` and `send_input` will refuse with a clear error.

## Orchestrating: prompt patterns

The boss is just a Claude Code session with this MCP enabled. Direction
comes from how you prompt it. A few patterns that work:

### Watch and report

> Watch session abc123 with `wait_for_idle(quiet_secs=30)` in a loop.
> After each idle window, read the new output and summarize what the
> worker did since your last summary. If you see `error:` or
> `Traceback`, raise it to me immediately.

Boss loops `wait_for_idle → read_output → summarize → repeat`. One MCP
tool call per work cycle, not one per second.

### Wait, then review

> Worker session abc123 is implementing `foo()`. When it goes idle for
> 60 seconds, read what it produced. If `foo` is missing a docstring,
> `send_input` "please add a docstring explaining the return type" and
> wait again. Stop when the docstring is there.

Boss loops `wait_for_idle`, then grep, then maybe `send_input`, then
`wait_for_idle` again, until the stop condition fires.

### Watch for sentinel, then act

> Tail session abc123 with `wait_for_output(pattern="DONE")`. As soon as
> it matches, `send_input` "git add -A && git commit -m WIP && git push".

Boss blocks on the regex and acts the moment the worker emits the
marker. Cheap and precise.

### Interrupt and redirect

> Watch session abc123. If `wait_for_output` matches
> `rm -rf|force-push|DROP TABLE`, immediately `send_signal SIGINT` and
> then type a correction explaining why.

Boss uses regex to catch a dangerous action mid-stream, Ctrl-C's it
through the line discipline, and prompts a course-correction.

### Why blocking primitives matter

Every tool call is a context-window round trip. Naïve polling
(`read_output` every second) burns through boss-Claude's window with
empty results and ends up cost-prohibitive within minutes.
`wait_for_idle` and `wait_for_output` compress an entire work cycle into
one tool call: the server blocks server-side until something interesting
happens, then returns once. Reach for them first when designing
orchestration prompts.

## Status

Phases 0–4 are complete: core data plane, CLI subcommands, stdio MCP
server with descriptive tools, unit tests (34 passing), and this
documentation. Future work (Windows port, pyte-based screen snapshots,
pip-packageable distribution) is tracked in `ARCHITECTURE.md`'s
"Same-machine orchestration" section. Implementation history lives at
`~/.claude/plans/yes-that-sounds-great-distributed-stallman.md`.
