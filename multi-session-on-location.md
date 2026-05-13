# Multiple termpilot sessions in one cwd — design plan

## TL;DR

Yes, doable cleanly, and in the common case **without the user having to type anything new**. The collision surface is much smaller than it looks because of how the system is already designed. There is exactly **one** piece of state that ties resilience to a cwd today — `~/.cache/termpilot/cwd/<sha256(cwd)>/{wrapper.lock, active.json}` — and the clean fix is to namespace that one path by an **instance label**.

The instance label is resolved in this order:

1. `--instance NAME` flag (explicit override)
2. `TERMPILOT_INSTANCE` env var (for tmux/screen wrappers)
3. **Derived from the controlling TTY** (`os.ttyname(0)` → `pts-3` etc.) — the default
4. Literal `"default"` if stdin isn't a TTY (tests, scripts)

So in practice:

- Open two terminals, both `cd ~`, run `termpilot` in each → two different ptys → two slots → just works.
- Each tmux/screen pane has its own pts → one slot per pane automatically.
- SIGKILL + rerun in the **same** terminal window → same pts → crash recovery still works (its whole reason for existence).
- Tests with non-TTY stdin → fall back to `"default"` → sequential single-slot behaviour, identical to today.

`--instance` exists as an escape hatch for users who want a stable, human-chosen name (e.g. `--instance staging` vs `--instance prod`) or for non-interactive setups where the TTY-derived default isn't available.

Nothing else has to change: no protocol bump, no relay change, no browser change, no crypto change.

## Why most of the system is already safe

The relay was designed for many concurrent sessions; the wrapper was designed for one-per-cwd. Inventory of every place state lives:

| State | Keyed by | Concurrent-in-same-cwd safe today? |
|---|---|---|
| `~/.cache/termpilot/cwd/<hash>/wrapper.lock` | cwd | **No** — single lock per cwd |
| `~/.cache/termpilot/cwd/<hash>/active.json` | cwd | **No** — last writer wins, breaks crash recovery |
| `~/.cache/termpilot/sid/<sid>/out.{spool,cursor,next_seq}` | sid | Yes — sids are random 12-hex-char per start |
| `~/.cache/termpilot/events.log` | global | Mostly yes — `O_APPEND` line writes are atomic; rotation has a tiny pre-existing race that already affects any two concurrent wrappers regardless of cwd. Out of scope. |
| `~/.config/termpilot/{token,relay-url,relay-secret,update-pending.json}` | global | Yes — read at startup, no write contention |
| OS keyring entry | global | Yes — read at startup |
| Relay `data/<sid>/…` | sid | Yes — relay never indexes by cwd; `cwd` lives inside the encrypted meta blob |
| Relay `data/push/<token_hash>/…` | token_hash | Yes — orthogonal to session count |
| Browser `localStorage` / `sessionTerms` Map | sid | Yes — every session is listed independently |

Everything outside the first two rows is already either per-sid, per-token, or read-only at session start. **The only thing that genuinely collides is the per-cwd cache dir.**

The collision is not "ciphertext gets corrupted" or "sessions leak into each other" — it's narrower:

- Two wrappers in the same cwd race to write the same `active.json`. The second one's `wrapper_sid` + `marker_b64` overwrite the first's.
- If wrapper A (the loser of the race) then SIGKILLs, the *next* termpilot in that cwd finds B's sid in `active.json`, not A's, and won't replay A's spool. A's spool sits in `~/.cache/termpilot/sid/<A's sid>/` until `cleanup_stale_sid_dirs` removes it after 7 days. The bytes are lost.

That's the entire failure mode. It's a recoverability failure, not a confidentiality or integrity failure. Still — the user asked for rock-solid, so we fix it properly.

## The change

### 1. Cache layout

Replace:

```
~/.cache/termpilot/cwd/<sha256(cwd)>/
  wrapper.lock
  active.json
```

with:

```
~/.cache/termpilot/cwd/<sha256(cwd)>/<instance>/
  wrapper.lock
  active.json
```

### 2. Resolving `<instance>`

A new helper, `resolve_instance(args)`, returns a label using the first source that yields a valid result:

```python
def resolve_instance(args) -> str:
    # 1. Explicit CLI flag wins.
    if args.instance:
        return _validate_label(args.instance, source="--instance")
    # 2. Env var (set by tmux/screen wrappers, CI, etc.).
    env = os.environ.get("TERMPILOT_INSTANCE")
    if env:
        return _validate_label(env, source="TERMPILOT_INSTANCE")
    # 3. Controlling TTY of stdin → stable per-terminal, per-pane.
    try:
        tty = os.ttyname(0)         # e.g. "/dev/pts/3" or "/dev/ttys003"
    except OSError:
        tty = None
    if tty:
        # Strip "/dev/" prefix, translate remaining "/" to "-", validate.
        label = tty.removeprefix("/dev/").replace("/", "-")
        try:
            return _validate_label(label, source="tty")
        except ValueError:
            log_event("instance_tty_unsanitary", tty=tty)
    # 4. Fallback. Sequential single-slot behaviour, same as pre-change termpilot.
    return "default"


_LABEL_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")

def _validate_label(s: str, *, source: str) -> str:
    if not _LABEL_RE.fullmatch(s) or s in (".", ".."):
        raise ValueError(f"invalid instance label from {source}: {s!r}")
    return s
```

Why TTY rather than PID, PPID, or auto-numbering:

- **PID is unstable across restart.** A SIGKILLed wrapper has a different PID from the wrapper the user runs next, so crash recovery would never trigger. Defeats the point.
- **PPID is fragile.** A subshell or `&&` chain changes PPID. Same terminal, "different" identity — same recoverability bug.
- **Auto-numbering** (`default`, `default-2`, …) requires picking the "next free" slot, which means a slot vacated by a SIGKILLed wrapper gets claimed by an unrelated new one — recovery goes to the wrong wrapper, or we keep all slots forever and leak state.
- **TTY is the natural stable identity** for "this terminal window". It survives SIGKILL+rerun, distinguishes panes for free, and gives up cleanly to a `"default"` fallback when there's no TTY.

### 3. CLI

Add one flag to `cmd_run`:

```
--instance NAME    Independent resilience slot within a single cwd.
                   Default: derived from the controlling TTY
                   ($TERMPILOT_INSTANCE wins over TTY; --instance wins over both).
                   Charset: [a-zA-Z0-9_.-], length 1-64.
```

Behaviour:

- `termpilot` in two different terminal windows in the same cwd → each picks up its own pts label → two independent slots, no flag needed.
- `termpilot` twice in the **same** terminal pane (e.g. nested) → both resolve to the same TTY label → second one fails on the lock unless `--force` or a different `--instance`. This is the right answer: two interactive wrappers sharing one TTY would fight for input.
- `termpilot --instance work` → forces the slot to `"work"` regardless of TTY. Useful for "I want a stable name in the browser sidebar" or "I'm running from cron".

### 4. Title default (UX note)

When two sessions share the same `basename "$PWD"`, the browser shows them as two list items with identical titles, sorted newest-first within each token. That's correct — sids are different, encrypted state is different — just visually ambiguous. Three options:

- (a) Do nothing. Browser already sorts newest-first; users disambiguate by timestamp. Simplest.
- (b) Append the instance label to the title when it's not `"default"`. But TTY labels like `pts-3` are meaningless to a human looking at a phone — adding them to the title makes the UX *worse*, not better.
- (c) Encourage `--title` in docs for users who run multi-session.

Recommendation: **(a) + (c)**. Leave the title plumbing untouched and add a one-line README note: "Running multiple sessions in one cwd? Pass `--title "home (left)"` etc. to tell them apart in the browser."

### 5. Code touchpoints (small)

In `termpilot-wrap`:

- `cwd_cache_dir(cwd)` → `cwd_cache_dir(cwd, instance)` — append `instance` segment. All call sites take the instance (`acquire_wrapper_lock`, `read_active_json`, `write_active_json`, `clear_active_json`, plus the inline `cwd_cache_dir(cwd)` in `cmd_run`'s teardown at ~line 1618).
- `acquire_wrapper_lock(cwd)` → `acquire_wrapper_lock(cwd, instance)`. No semantic change beyond the path.
- New `resolve_instance(args)` + `_validate_label` helpers near the existing cache helpers.
- `parse_run_args` adds `--instance`. Default is `None` (so `resolve_instance` knows to look at env / TTY).
- `cmd_run` calls `instance = resolve_instance(args)` once, near the existing `cwd = os.getcwd()` line, and passes it through.
- `log_event("wrapper_start", …)` gains an `instance=` field so events.log diagnostics distinguish slots.

In `install.sh`:

- No changes required. The shell function does not need to forward `TERMPILOT_INSTANCE` explicitly; env vars propagate naturally. If a user wants per-pane instances and the wrapper's TTY-detection doesn't fit their setup, they set the env var themselves.

In `ARCHITECTURE.md`:

- Update the "Resilience" section to document the per-instance dir and the four-step resolution.
- Update the file-layout cache tree.

In `README.md`:

- Add a short "Running multiple sessions in the same directory" subsection: "It just works — open another terminal, run `termpilot`. Each terminal pane gets its own resilience slot automatically. Override with `--instance NAME` if you want a specific name."

### 6. Migration

The state we'd be moving (`cwd/<hash>/active.json`) is **at most 5 minutes old to be useful** (`CRASH_RECOVERY_SECS`). After 5 minutes it's stale and the wrapper picks a fresh sid anyway. Recommendation: **no migration**. Any in-flight crash-recovery state from before the upgrade is forfeit. The only affected case is a user who SIGKILLed a wrapper, upgraded termpilot, and re-ran inside 5 minutes — they lose at most the bytes their SIGKILL had already endangered. The complexity of a migration step isn't worth it.

The old `cwd/<hash>/active.json` (if any) would just sit orphaned alongside the new `cwd/<hash>/<instance>/active.json`. Add it to the GC sweep in `cleanup_stale_sid_dirs` (which currently only touches `cwd/sid/`) so it gets cleaned up over time. One-liner.

### 7. What we explicitly do NOT change

- **Relay protocol.** Sessions are sid-keyed; the relay already supports N concurrent sessions per cwd because it never sees cwd as anything but ciphertext inside `meta.bin`. No endpoint changes, no AAD changes, no `v1` → `v2` bump.
- **Browser.** Already shows N sessions per token regardless of cwd.
- **Crypto.** Token, AAD strings, trigger secret — all untouched.
- **`active.json` schema.** Same fields. Same crash-recovery logic. Same 5-minute window. The only difference is *where* the file lives.
- **`--force`.** Stays as the escape hatch for "the lock is stuck and I know it".

## Test plan

Add `tests/test_multi_instance.py`:

1. **Two TTY-implicit slots coexist.** Spawn two wrappers attached to two different ptys (use `pty.openpty()` to fabricate stdin pts pairs) in the same cwd; both register, both produce independent sids, both exit cleanly. Verify two distinct `cwd/<hash>/<tty-label>/` directories exist with separate `active.json` files.
2. **Same TTY collides.** Spawn two wrappers in the same cwd sharing the same fabricated stdin pts → second exits 4. Same code path as today, just keyed on `<hash>/<pts-label>/` instead of `<hash>/`.
3. **Explicit `--instance` overrides TTY.** Start wrapper A with `--instance foo` and wrapper B without it, in the same cwd, on the same fabricated stdin pts. Different slots → both succeed.
4. **Crash recovery per slot.** Start A (TTY pts-X), SIGKILL it after some bytes are spooled, restart on the same pts-X within 5 min → sid is reused, spool is replayed. Meanwhile a long-running B on pts-Y is untouched.
5. **Non-TTY fallback.** Run wrapper with stdin redirected from `/dev/null` → instance resolves to `"default"`. Run a second one with the same setup → second exits 4 on the lock. (Same behaviour as today's tests, which is the point.)
6. **Instance validation.** `--instance ../escape`, `--instance ""`, `--instance "name with space"`, 65-char name → all rejected at argparse time, exit 2.
7. **Env var precedence.** `TERMPILOT_INSTANCE=foo termpilot` ⇒ creates the `foo` slot regardless of TTY; explicit `--instance bar` wins over env.

Extend `tests/test_resilience.py` to also cover the new cwd dir layout (rename the hash-dir helper, add an instance parameter — all existing assertions trivially pass once threaded through).

`tests/run-all.sh` already invokes everything in `tests/`; the new file gets picked up automatically.

## Estimated effort

- `termpilot-wrap`: ~50 lines changed (one signature, six call sites, one argparse entry, the `resolve_instance` helper + label validator). All in the resilience block that's already heavily commented.
- `install.sh`: no change.
- `tests/test_multi_instance.py`: ~200 lines (mostly setup/teardown lifted from `test_resilience.py`; the `pty.openpty()` plumbing for test 1 is the only genuinely new code).
- Docs: `ARCHITECTURE.md` (one paragraph + diagram cache-tree update), `README.md` (one short subsection), `CLAUDE.md` (one line in the "Where state lives" bullet).

Total: a couple of hours of focused work, including writing and running the tests.

## Conclusion

This is genuinely possible cleanly because the existing design already treats sessions as sid-keyed everywhere except the per-cwd resilience cache. The cache was cwd-keyed because the *intended use case* was one wrapper per project directory, not because of any deeper coupling.

Promoting that one path from `cwd/<hash>/…` to `cwd/<hash>/<instance>/…`, with `<instance>` defaulting to the controlling TTY's pts label, is a faithful generalisation:

- One terminal, one wrapper, no flag → identical behaviour to today.
- Two terminals, two wrappers, no flag → two independent slots, just works.
- Crash recovery still keyed on a stable identity (the terminal window's pts), so the 5-minute SIGKILL recovery contract is preserved.
- `--instance NAME` available for the cases where the user wants a deliberate human name (CI, cron, browser-sidebar disambiguation).

If we had to fight the relay or the crypto layer to make this work, the answer would be "no, not cleanly." We don't. So: **yes, cleanly, with the TTY as the implicit instance and `--instance NAME` as the optional override.**
