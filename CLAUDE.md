# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` is the user-facing setup guide; `ARCHITECTURE.md` is the canonical engineering reference (threat model, wire format, resilience design, release flow, operational footguns). Read those first for anything non-trivial — they go deeper than this file by design.

## Three sides, one protocol

TermPilot is a single piece of work split across three independent runtime surfaces that share a wire format. Changes to crypto, framing, or the AAD strings must land in all three in lockstep:

- **Wrapper** (`termpilot-wrap`, Python 3.9+, stdlib only) — spawns a child in a PTY, runs four daemon threads (`output_uploader`, `input_poller`, `heartbeat`, transcript-tail/push trigger), encrypts every record before POST. Imports from `lib/` (`crypto.py`, `keystore.py`, `release_channel.py`).
- **Relay** (`php/relay.php`) — stateless PHP on shared hosting. Stores opaque ciphertext + minimal public metadata under `data/<sid>/`. Enforces per-(stream, sid) sequence positions server-side and returns HTTP 409 on conflict — clients resync and retry. The relay never sees keys or token bytes.
- **Browser** (`php/index.html` + `php/lib/{crypto,session,index,keyboard}.js`) — multi-token enumeration: tries every stored token against each session's marker; successful decrypt = session belongs to that token. Per-session offscreen xterm instances cached in `sessionTerms` so switching is instant. PWA installable; service worker at `php/sw.js` handles precache, push, background sync.

The crypto is AES-256-GCM with **AAD bound to `(stream, sid, seq)`** and a literal `v1` version tag (`marker:v1:<sid>`, `out:v1:<sid>:<seq>`, etc.). There is no version negotiation — a v1 client decrypting v2 records fails with a tag mismatch and the wrapper stalls with a visible "input wedged" banner. Treat protocol bumps as flag-day events.

## Commands

```sh
tests/run-all.sh                          # all Python tests (~100 s; test_wrapper_e2e.py spawns real bash)
python3 tests/test_<name>.py              # run a single test file directly
python3 tests/test_crypto.py --gen-vectors    # regenerate tests/crypto_vectors.json for the browser test
./install.sh                              # point the `termpilot` shell function at THIS checkout (dev mode)
~/.local/share/termpilot/install.sh       # point it back at the installed prod copy
tools/deploy.sh                           # FTPS upload of php/ to live relay (uses ~/.netrc); stamps sw.js build ts
tools/fetch-logs.sh                       # pull relay.log via FTP
tools/vendor-fetch.sh                     # re-download pinned xterm.js + jsQR; checksums verified
```

Browser-side cross-language vectors are run manually — `tests/test_crypto.html` over a local `python3 -m http.server`; `run-all.sh` does NOT cover this path.

## Where state lives

- **PC**: token in OS keyring (`service="termpilot"`, `username="default"`) or `~/.config/termpilot/token` (chmod 600) as fallback. Relay URL at `~/.config/termpilot/relay-url`, relay Bearer secret at `~/.config/termpilot/relay-secret` (chmod 600 — wrapper refuses looser perms). Per-(cwd, instance) lock + crash-recovery state at `~/.cache/termpilot/cwd/<encoded>/<instance>/`; `<instance>` defaults to the controlling TTY's pts label (e.g. `pts-3`), with `--instance NAME` / `$TERMPILOT_INSTANCE` overrides and `"default"` fallback for non-TTY stdin — see `resolve_instance` in `termpilot-wrap`. Encrypted output spool at `~/.cache/termpilot/sid/<sid>/out.{spool,cursor,next_seq}` — survives wrapper SIGKILL. Diagnostic JSONL at `~/.cache/termpilot/events.log` (rotates at 256 KB; set `TERMPILOT_DEBUG=1` to mirror to stderr).
- **Relay**: `data/<sid>/{meta.bin,out.records,out.idx,in.records,in.idx,meta.public.json}`; `data/push/<token_hash>/<sub_id>.json` and `.trigger_id`; `data/vapid.json` (chmod 600).
- **Browser**: tokens + push state in `localStorage` (`termpilot-tokens`, `termpilot-push`, `termpilot-pending-sends`). IndexedDB mirror at `termpilot-app-state` / store `kv` (`session:<sid>`, `queue:<sid>`) — only the service worker reads from IDB, pages write to both.

## Things that look fragile but are load-bearing

- **HTTP 409 / sequence-conflict retry on POST** is not optional. A timeout where the relay actually got the request leaves the next position taken; resyncing `next_seq` and retrying the same record is the only way to avoid duplicate-at-wrong-AAD-seq → cascade decrypt failure.
- **`php -S` is single-threaded.** Long-poll requests serialize, so local dev tests that hold a poll open while issuing another request will deadlock. Apache / php-fpm don't have this problem. Kill wrappers / close browser tabs before re-running tests.
- **Phone is view-only for size.** Browser resize must never drive the wrapper's PTY size — only the local terminal's SIGWINCH does.
- **`RELAY_SECRET` is a spam gate, not an authorization key.** `op_close` and `op_push_notify` are gated by `trigger_secret = HMAC-SHA256(token, "termpilot:trigger:v1")` (`trigger_id` = its SHA-256 is the public verifier). Both wrapper PC and each browser derive it independently; the relay only stores the hash. Don't add an endpoint that mutates a session without `trigger_secret` proof.
- **Dev checkout detection**: `release_channel` skips both phases of the update notice when `.git` exists next to the wrapper. `--update` from a dev checkout still works but it re-extracts to `~/.local/share/termpilot/` and *repoints the shell function there* — your dev tree is never touched, but `termpilot` stops calling it until you re-run the dev `install.sh`.
- **Vendored browser deps** (`php/lib/vendor/xterm.min.{css,js}`, `jsQR.js`) are checked in verbatim with SHA-256 pins in `php/lib/vendor/NOTICE` and `tools/vendor-fetch.sh`. Bump both in lockstep. The CSP is locked to `'self'` because of this — no CDN at runtime.
- **`tools/deploy.sh` sed-replaces `__TERMPILOT_VERSION__` in `php/sw.js`** before upload, which bumps `CACHE_NAME` and triggers the in-app update banner. The source tree keeps the literal placeholder, so do not commit a stamped `sw.js`.
- **VERSION.json in `main` is the dev value** (currently `0.0.0`); the release workflow at `.github/workflows/release.yml` rewrites it for each shipped zip on a `v*` tag push. Don't hand-edit it to a real version on `main`.

## Sudo workflow (CLAUDE personal rule)

Per `~/CLAUDE.md`, never run `sudo` directly. When a step needs root, write a numbered script under `~/temp-scripts/` (`000-…`, `001-…`), make it executable, `exec &> >(tee "$LOG")` for logging, ask the user to run it, then read the `.log` to continue. The wrapper's own `--generate-token` / `--show-token` use `sudo -v` internally (anti-shoulder-surf, not cryptographic) — that's invoked by the wrapper itself, not by Claude.
