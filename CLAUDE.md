# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` is the umbrella overview; `linux/README.md` and `windows/README.md` are the platform-specific user-facing setup guides; `ARCHITECTURE.md` is the canonical engineering reference (threat model, wire format, resilience design, release flow, operational footguns). Read those first for anything non-trivial — they go deeper than this file by design.

## Four sides, one protocol

TermPilot is a single piece of work split across four trees that share a wire format. Changes to crypto, framing, or the AAD strings must land in all of them in lockstep:

- **Linux/macOS wrapper** (`linux/termpilot-wrap`, Python 3.9+ stdlib only) — spawns a child in a PTY via `pty.fork`, runs daemon threads (`output_uploader`, `input_poller`, `heartbeat`), encrypts every record before POST. Imports `from shared import crypto, release_channel` and `from lib import keystore` (i.e. `linux/lib/keystore.py`).
- **Windows wrapper** (`windows/termpilot-win-wrap.py`, Python 3.9+ requires `pywinpty`, `keyring`, `cryptography`, `qrcode`) — same protocol but uses pywinpty for ConPTY, msvcrt locking, Credential Manager via the `keyring` package, console-control handler for clean window-close. Imports `from shared import crypto, release_channel` and `from lib import keystore_win as keystore, pty_backend, resilience_win as resil`.
- **Relay** (`relay/relay.php`) — stateless PHP on shared hosting. Stores opaque ciphertext + minimal public metadata under `data/<sid>/`. Enforces per-(stream, sid) sequence positions server-side and returns HTTP 409 on conflict — clients resync and retry. The relay never sees keys or token bytes.
- **Browser** (`relay/index.html` + `relay/lib/{crypto,session,index,keyboard}.js`) — multi-token enumeration: tries every stored token against each session's marker; successful decrypt = session belongs to that token. Per-session offscreen xterm instances cached in `sessionTerms` so switching is instant. PWA installable; service worker at `relay/sw.js` handles precache, push, background sync.

The crypto is AES-256-GCM with **AAD bound to `(stream, sid, seq)`** and a literal `v1` version tag (`marker:v1:<sid>`, `out:v1:<sid>:<seq>`, etc.). There is no version negotiation — a v1 client decrypting v2 records fails with a tag mismatch and the wrapper stalls with a visible "input wedged" banner. Treat protocol bumps as flag-day events.

The wrappers share `shared/crypto.py` and `shared/release_channel.py` — one source of truth. Each wrapper adds `shared/`'s parent to `sys.path` at startup, probing both the flat release-zip layout (sibling to wrapper) and the nested dev-tree layout (one level up).

## Commands

```sh
linux/tests/run-all.sh                      # all Python tests (~100 s; test_wrapper_e2e.py spawns real bash)
python3 linux/tests/test_<name>.py          # run a single test file directly
python3 linux/tests/test_crypto.py --gen-vectors   # regenerate linux/tests/crypto_vectors.json
cd linux && ./install.sh                    # point the `termpilot` shell function at THIS checkout (dev mode)
~/.local/share/termpilot/install.sh         # point it back at the installed prod copy
tools/deploy.sh                             # FTPS upload of relay/ to live relay (uses ~/.netrc); stamps sw.js build ts
tools/fetch-logs.sh                         # pull relay.log via FTP
tools/vendor-fetch.sh                       # re-download pinned xterm.js + jsQR; checksums verified
tools/build-release.sh linux                # → dist/termpilot-linux-macos.zip (flat)
tools/build-release.sh windows              # → dist/termpilot-windows.zip (flat)
```

Browser-side cross-language vectors are run manually — `linux/tests/test_crypto.html` over a local `python3 -m http.server`; `run-all.sh` does NOT cover this path.

## Where state lives

- **Linux/macOS PC**: token in OS keyring (`service="termpilot"`, `username="default"`) or `~/.config/termpilot/token` (chmod 600) as fallback. Relay URL at `~/.config/termpilot/relay-url`, relay Bearer secret at `~/.config/termpilot/relay-secret` (chmod 600 — wrapper refuses looser perms). Per-(cwd, instance) lock + crash-recovery state at `~/.cache/termpilot/cwd/<encoded>/<instance>/`; `<instance>` defaults to the controlling TTY's pts label (e.g. `pts-3`), with `--instance NAME` / `$TERMPILOT_INSTANCE` overrides and `"default"` fallback for non-TTY stdin — see `resolve_instance` in `linux/termpilot-wrap`. Encrypted output spool at `~/.cache/termpilot/sid/<sid>/out.{spool,cursor,next_seq}` — survives wrapper SIGKILL. Diagnostic JSONL at `~/.cache/termpilot/events.log` (rotates at 256 KB; set `TERMPILOT_DEBUG=1` to mirror to stderr).
- **Windows PC**: token in Credential Manager via `keyring` (same service/username) or `%APPDATA%\termpilot\token` (ACL'd to user) as fallback. Relay URL at `%APPDATA%\termpilot\relay-url`, secret at `%APPDATA%\termpilot\relay-secret`. Cache + spool under `%LOCALAPPDATA%\termpilot\cache\`. Diagnostic log at `%LOCALAPPDATA%\termpilot\events.log` *and* mirrored to `<install_source>\logs\<HOST>-<PID>.log` when `%APPDATA%\termpilot\install_source.txt` records a writable share path (useful when installing from a network share for diagnosis). `<instance>` defaults to a stable derivative of the parent console window handle, with `--instance NAME` / `$TERMPILOT_INSTANCE` overrides — see `resolve_instance` in `windows/lib/resilience_win.py`.
- **Relay**: `data/<sid>/{meta.bin,out.records,out.idx,in.records,in.idx,meta.public.json}`; `data/push/<token_hash>/<sub_id>.json` and `.trigger_id`; `data/vapid.json` (chmod 600).
- **Browser**: tokens + push state in `localStorage` (`termpilot-tokens`, `termpilot-push`, `termpilot-pending-sends`). IndexedDB mirror at `termpilot-app-state` / store `kv` (`session:<sid>`, `queue:<sid>`) — only the service worker reads from IDB, pages write to both.

## Things that look fragile but are load-bearing

- **HTTP 409 / sequence-conflict retry on POST** is not optional. A timeout where the relay actually got the request leaves the next position taken; resyncing `next_seq` and retrying the same record is the only way to avoid duplicate-at-wrong-AAD-seq → cascade decrypt failure.
- **`php -S` is single-threaded.** Long-poll requests serialize, so local dev tests that hold a poll open while issuing another request will deadlock. Apache / php-fpm don't have this problem. Kill wrappers / close browser tabs before re-running tests.
- **Phone is view-only for size.** Browser resize must never drive the wrapper's PTY size — only the local terminal's SIGWINCH (Linux) or 1-Hz size poller (Windows) does.
- **`RELAY_SECRET` is a spam gate, not an authorization key.** `op_close` and `op_push_notify` are gated by `trigger_secret = HMAC-SHA256(token, "termpilot:trigger:v1")` (`trigger_id` = its SHA-256 is the public verifier). Both wrapper PC and each browser derive it independently; the relay only stores the hash. Don't add an endpoint that mutates a session without `trigger_secret` proof.
- **Dev checkout detection**: `shared/release_channel.py` walks up the path tree looking for `.git/`; finding one anywhere up to 8 levels above the wrapper marks a dev checkout and disables the update notice. `--update` from a dev checkout still works but it re-extracts to `~/.local/share/termpilot/` (Linux) / `%LOCALAPPDATA%\Programs\termpilot\` (Windows) and *repoints the shim there* — your dev tree is never touched, but `termpilot` stops calling it until you re-run the dev `install.sh` / `install.bat`.
- **Vendored browser deps** (`relay/lib/vendor/xterm.min.{css,js}`, `jsQR.js`) are checked in verbatim with SHA-256 pins in `relay/lib/vendor/NOTICE` and `tools/vendor-fetch.sh`. Bump both in lockstep. The CSP is locked to `'self'` because of this — no CDN at runtime.
- **`tools/deploy.sh` sed-replaces `__TERMPILOT_VERSION__` in `relay/sw.js`** before upload, which bumps `CACHE_NAME` and triggers the in-app update banner. The source tree keeps the literal placeholder, so do not commit a stamped `sw.js`.
- **VERSION.json in `main` is the dev value** (currently `0.0.0`); the release workflow at `.github/workflows/release.yml` rewrites it for each shipped zip on a `v*` tag push. Don't hand-edit it to a real version on `main`.
- **The Windows wrapper's `cmd.exe /Q /K "..."` startup chain** is load-bearing for the banner. The chain does `@chcp 65001 > nul & @cls & echo …termpilot session… & prompt $P$G` so (1) UTF-8 box-drawing chars in the optional update notice render correctly, (2) cmd's own banner is wiped, (3) the termpilot banner is emitted from *inside* the PTY screen buffer where ConPTY's initial cursor sync can't clobber it. For PowerShell, `-NoLogo -NoExit -Command "Clear-Host; Write-Host '…'"` plays the same role.
- **The Windows console-close handler must block until cleanup is done.** Windows gives a `SetConsoleCtrlHandler` callback ~5 s before force-terminating the process; the handler waits on `cleanup_done` (timeout 4.5 s) so the main thread's `finally` block has time to POST `?op=close` to the relay. Returning from the handler immediately leaves the session orphaned on the relay.
- **Wrapper Relay client reuses HTTP connections per-thread.** Each daemon thread (output_uploader, input_poller, heartbeat) keeps its own `http.client.HTTPSConnection` alive via `threading.local`; without keep-alive, each record POST pays ~2 RTT of TLS handshake (~280 ms on a 20 ms link), which caps streaming throughput at ~3.5 records/sec and shows up as seconds of lag during a Claude response. The class retries once on `RemoteDisconnected` / `BadStatusLine` to handle servers that close idle keep-alive sockets. Don't replace this with plain `urllib.request.urlopen`/`build_opener` — those silently regress to no-pooling.
- **`LONG_POLL_SECS = 5` on the relay** is a worker-occupancy knob, not a UX one. Each long-poll holds one PHP-FPM worker for its dwell time; on shared hosts with small `pm.max_children`, longer dwells starve the pool and queue contending POSTs at the FastCGI gateway (9.7 s p95 wall while server-time stayed at 0.2 ms — gateway queue). If lag returns, drop further before reaching for the protocol.
- **`op_close` runs auto-GC after `fastcgi_finish_request()`.** Response goes out first, then the worker keeps running to sweep stale session dirs (5-min cutoff for cleanly-closed, 1-hour for silently-dead, both capped at 50 removals per pass). With `php -S` (no `fastcgi_finish_request`) the cleanup runs synchronously before the response — tests rely on this so they can assert post-close state.
- **Slow-network detector ignores big batches.** The disc/banner in the browser only counts samples whose response body is ≤ 8 KB (`NET_TRUSTED_BYTES` in `relay/lib/index.js`). Big-batch downloads (e.g. Claude's final response chunk) take seconds on a slow link and would otherwise flip the disc severe even on a healthy network — that's bandwidth, not latency.

## Sudo workflow (CLAUDE personal rule)

Per `~/CLAUDE.md`, never run `sudo` directly. When a step needs root, write a numbered script under `~/temp-scripts/` (`000-…`, `001-…`), make it executable, `exec &> >(tee "$LOG")` for logging, ask the user to run it, then read the `.log` to continue. The Linux wrapper's own `--generate-token` / `--show-token` use `sudo -v` internally (anti-shoulder-surf, not cryptographic) — that's invoked by the wrapper itself, not by Claude. Windows has no `sudo` gate; the wrapper prompts for confirmation only on overwrite.
