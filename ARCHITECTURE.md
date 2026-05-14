# TermPilot — architecture

Remote viewing + control of **any terminal session** through a PHP relay
on shared hosting. The wrapper spawns an arbitrary command in a PTY
(bash, tmux, htop, …) and bridges its bytes to the browser.
**End-to-end encrypted**: the relay sees only ciphertext blobs; only the
PC running `termpilot-wrap` and the browser hold the keys.

## Status (2026-05-10, v2)

The plaintext v1 was scrapped. The current implementation:

- AES-256-GCM per record. The token is a 32-byte random secret (no
  password derivation in the user-facing flow; PBKDF2 still lives in
  `crypto.derive_token` for cross-language test vectors only).
- Server stores opaque ciphertext + minimal public metadata (timestamps,
  cols/rows for layout, byte counts).
- Browser tries each stored token against each session's marker blob;
  successful decryption = session belongs to that token; sessions are
  grouped by the token's user-chosen device label.
- Token stored locally in OS keyring (GNOME Keyring on Linux, macOS
  Keychain on Darwin); falls back to `~/.config/termpilot/token`
  (chmod 600) with a printed warning.
- `termpilot --generate-token` / `--show-token` are gated by `sudo -v`
  (anti-shoulder-surf only — not cryptographic). No separate password.

### Resilience (improvements.md §3, landed 2026-05-10)

- **Disk-backed output spool** at `~/.cache/termpilot/sid/<sid>/`:
  PTY chunks are appended (plaintext, mode 0600) before encrypt+POST; the
  cursor file advances on confirmed delivery. If the wrapper SIGKILLs
  between read and POST, the bytes survive.
- **Per-(cwd, instance) single-instance lock** at
  `~/.cache/termpilot/cwd/<encoded-cwd>/<instance>/wrapper.lock` via
  `fcntl.LOCK_EX|LOCK_NB`. `<instance>` is resolved in this order
  (`resolve_instance` in `termpilot-wrap`):
    1. `--instance NAME` flag,
    2. `$TERMPILOT_INSTANCE`,
    3. the controlling TTY of stdin (e.g. `pts-3` on Linux, `ttys003` on macOS),
    4. literal `"default"` if none of the above yields a valid label.
  Charset `[a-zA-Z0-9_.-]`, length 1-64; anything else is rejected at
  startup. Two terminal windows in the same cwd get two distinct pts
  labels → two independent slots, no flag needed. SIGKILL + rerun in
  the **same** terminal pane reuses the same pts label and so still
  triggers crash-recovery within the 5-minute window. `--force` still
  overrides the lock for stuck-state recovery.
- **active.json** in the same per-instance dir tracks `{wrapper_sid, marker_b64, ts, pid}`.
  - On wrapper start: if `ts` is < 5 min old (CRASH_RECOVERY_SECS) and the
    spool dir still exists → reuse the sid and replay unsent chunks.
  - Clean exit drops `wrapper_sid` and `marker_b64` (so the next start
    gets a fresh sid).
- **Relay `?op=sessions`** returns ALL non-closed sessions with `alive`
  flag (TTL configurable via `ALIVE_TTL_SECS`, default 300 s) and
  `offline_secs`. The browser keeps stale sessions visible (greyed).
- **Auto-GC on close**: every `op_close` runs `auto_gc_pass()` after
  flushing its response (via `fastcgi_finish_request()` so the wrapper
  doesn't wait). Removes session dirs where `closed_at` > `AUTO_GC_CLOSED_SECS`
  (5 min default — explicit close, no client will reattach), `last_seen` >
  `AUTO_GC_STALE_SECS` (1 h default — wrapper went silent but might
  recover from a network blip), or orphans > 1 h old (no `meta.public.json`).
  Hard-capped at 50 removals per pass; backlogs drain across subsequent
  closes without making any one close slow.
- **Browser connection-state badge** (●/◐/○) driven from `TPSession.api()`
  timestamps. Sessions that disappear from `/sessions` no longer detach
  the terminal view — the bar shows "offline" until the wrapper returns.
- **Pending-sends queue** in `index.html`: failed/offline sends are
  persisted in `localStorage.termpilot-pending-sends` and drained on the next
  successful `/sessions` ping.

### Background sync for offline send queue (improvements.md §6, landed 2026-05-10)

- **IndexedDB mirror** at DB `termpilot-app-state`, store `kv` (`session:<sid>` and
  `queue:<sid>` records). Pages keep using `localStorage.termpilot-pending-sends`
  as the synchronous source of truth and write the same data to IDB on
  every `enqueuePendingSend`/`dropPendingSend`/`attach`/`detach`. The SW
  reads only from IDB.
- **Sync registration**: `registration.sync.register("termpilot-drain-<sid>")` is
  called by `index.html` on every queue add. The Background Sync API is
  Chromium-only; Firefox/Safari/iOS no-op cleanly and the existing
  foreground drainer remains the only path there.
- **SW handler**: `sync` event reads `session:<sid>` + `queue:<sid>` from
  IDB, encrypts each entry with WebCrypto AES-256-GCM (AAD bound to
  `in:v1:<sid>:<seq>` matching the page-side crypto.js), POSTs to
  `?op=input` with the saved Bearer secret. 200 → drop entry + advance
  next_seq. 409/expected_seq → resync next_seq, retry same entry.
  Network/4xx → leave queue, abort drain (next sync event will retry).
- **`sendInputBytes`** enqueues on offline / non-200 / network-error.
  Foreground drainer (`_drainTerminalQueue`) runs whenever `/sessions`
  confirms the wrapper is back.

### Session list ordering + stale marker (landed 2026-05-10)

`renderSessions()` in `index.html` sorts each token's session list:

1. Alive sessions first (where `alive !== false`), newest-first by
   `started_at`.
2. Stale (alive=false) sessions after that, also newest-first.

Stale entries get the `.stale` class on the `<li>` (CSS dims them to
~55% opacity, raises to ~85% when active) plus a small `<span class="stale-pill">offline</span>`
chip in warm orange. Visual cue without removing them — the user can
still attach to read history.

### Push-permission UX (landed 2026-05-10)

`renderPushRow()` in `index.html` distinguishes three states beyond
the simple enabled/disabled toggle:

- `granted + enabled` → "Disable notifications".
- `denied` → explains how to re-enable in browser site settings (lock
  icon → Site settings → Notifications: Allow), with a "Try again"
  button that retries the subscribe flow once permission is fixed.
- `default` (or `granted + not enabled`) → "Enable notifications".

The click handler maps `enablePush()` errors to friendlier messaging:
`/blocked|denied/` → guided alert; `/permission default|dismissed/` →
silently re-enables the button so the user can click again. Other
errors still fall through to a generic alert.

### Per-session view cache (landed 2026-05-10)

Switching sessions in the sidebar used to re-fetch history (`?op=info` +
`?op=before` for the last `HISTORY_LIMIT` records, plus N AES-GCM
decrypts) every time. Re-entering the same session now restores its
already-rendered view from in-memory cache, so the session list ↔
session detail bounce is instant.

- **`sessionTerms`** — `Map<sid, {term, host, logHtml, outNextSeq,
  inNextSeq, scrollTop}>`. Each session gets its own long-lived xterm
  instance + offscreen host so its parser state (cursor position,
  scrollback, ANSI mode flags) survives a session switch without bleeding
  into another. The global `term` reference is bound by `attach()` to
  whichever session is active. `pollLoop` resumes from the saved
  `outNextSeq` — no re-streaming all the bytes through xterm.
- **Always land at the bottom on open** — `attach()` calls
  `logEl.scrollTop = logEl.scrollHeight` in a `requestAnimationFrame`.
  The user-expectation is "open a session, see what's new"; preserving
  last-read position would lose that on every re-entry.
- **Cache eviction** — `clearAllSessionTerms()` fires when the user
  closes the manage-tokens modal or hits the Reload button (both signal
  "I want fresh data"). The cache is JS-memory only and dies on page
  reload.

### PWA / installable web app (landed 2026-05-10)

- `relay/manifest.webmanifest` — name, scope, `start_url=./index.html`,
  `display=standalone`, theme/background `#0d0f12`, plus a single
  Terminal app shortcut.
- `relay/sw.js` — minimal service worker: precaches the static shell on
  install (HTML, JS, icons, xterm CDN), cache-first with background
  refresh on the shell, and **never** intercepts `relay.php` (the seq
  protocol is real-time and would break under cached responses).
- `relay/icon-*.png` — app icons (192 and 512 px, regular and maskable
  variants). The maskable variant has the foreground shrunk to ~75% of
  the canvas with the dark theme background extending to the edges, so
  Android's mask doesn't crop the artwork. Swap the source raster and
  re-run the `convert` lines documented in the file header to refresh.
- `<link rel="manifest">`, `<link rel="apple-touch-icon">`, theme-color,
  and `mobile-web-app-capable` (modern + apple-* legacy) wired into
  `index.html`. Service worker registers on load.
- `.htaccess` adds the `application/manifest+json` MIME type and a
  no-cache directive on `sw.js` so updates propagate.
- `tools/deploy.sh` uploads all PWA assets alongside the existing files.

### Update banner (improvements.md §4, landed 2026-05-10)

- `tools/deploy.sh` sed-replaces a `__TERMPILOT_VERSION__` placeholder in
  `relay/sw.js` with a UTC build timestamp (`YYYYMMDDTHHMMSSZ`) before
  upload. The source tree keeps the literal placeholder, so dev servers
  and tests aren't affected.
- The version is the suffix of `CACHE_NAME` (`termpilot-shell-<ts>`); on
  activate, all other `termpilot-shell-*` caches are deleted.
- The SW no longer calls `self.skipWaiting()` from `install`. New
  workers sit in `waiting` until the page posts `{type:"SKIP_WAITING"}`.
  A `message` handler in `sw.js` answers that postMessage.
- `index.html` registers the SW, watches for `registration.waiting`
  and `updatefound→installed`, and shows an orange banner
  ("A new version is available." + UPDATE button). The
  banner is suppressed on first install (no controller at page load),
  and deferred while an INPUT/TEXTAREA/contenteditable is focused
  (re-checks every 30 s, max 5 min defer).
- Click → `waitingWorker.postMessage({type:"SKIP_WAITING"})` → SW
  activates → `controllerchange` fires → page reloads. Pages only
  reload when *they* requested the update, so the natural
  `clients.claim()` controllerchange on first install is ignored.
- `registration.update()` polled every 10 minutes and on
  visibilitychange/focus, forcing a fresh `sw.js` fetch instead of
  waiting for the browser's 24 h default.

### Push notifications (improvements.md §5, landed 2026-05-10)

Plaintext-on-relay model: subscriptions live as plaintext on the relay
(opaque FCM/Mozilla/Apple endpoint URLs — no PII), pushes are
deliberately **content-free** so no session content leaks through them.

- **VAPID keypair** auto-generated by relay.php on first
  `?op=push_pubkey` request, stored at `data/vapid.json` (chmod 600).
  Single keypair for the whole relay; reused for every push. ES256
  (P-256 ECDSA) keys are exported via PHP openssl, the public key
  serialised as the 65-byte uncompressed point (`0x04 || x || y`)
  base64url-encoded.
- **Token hash** = `SHA-256(token_bytes)` hex. Both the wrapper (Python)
  and the browser (WebCrypto) compute it; the relay only sees the hash,
  not the token itself, so it can group subscriptions per device-token
  without seeing the secret.
- **Subscription storage**: `data/push/<token_hash>/<sub_id>.json`,
  one file per browser. `sub_id` is server-issued (16-byte hex). Same
  endpoint subscribed twice → same id (idempotent dedupe).
- **Wrapper trigger**: v1 fires no push triggers itself. The relay
  endpoint and browser receiver stay wired so future triggers (terminal
  bell `\x07`, child process exit, write-burst quiet period) can drop in
  without touching the relay or service worker.
- **Relay sender**: for each subscription file under that token_hash,
  signs a fresh VAPID JWT (RFC 8292) per push-service origin with the
  ES256 key, POSTs an empty body to the endpoint with
  `Authorization: vapid t=<jwt>, k=<pub>`, `TTL: 60`. PHP openssl emits
  DER; the relay converts to RFC-7515 raw r||s (32+32 bytes). 404/410
  responses → subscription file deleted (per RFC 8030 dead-endpoint
  semantics).
- **Service worker**: a single `push` event handler shows a generic
  "Your terminal needs attention." notification (icon = `icon-192.png`,
  tag = `termpilot-notify` so repeats collapse). Clicking it focuses an existing
  app tab on the same origin, otherwise opens `index.html`.
- **Browser UI**: a "Push notifications" row inside the manage-tokens
  modal with one toggle button. Enable → notification permission prompt
  → `pushManager.subscribe({applicationServerKey: ...})` → POST
  `?op=push_subscribe` once per locally-stored token, persisted in
  `localStorage.termpilot-push`. Disable → POST `?op=push_unsubscribe` for each,
  then `subscription.unsubscribe()`. New tokens added later → on next
  modal open / cross-tab `termpilot-tokens` change → `syncPushTokens()`
  registers the existing subscription for the new tokens too.
- **Endpoints** (all behind `RELAY_SECRET` like the rest of the relay):
  `GET ?op=push_pubkey`, `POST ?op=push_subscribe`,
  `POST ?op=push_unsubscribe`, `POST ?op=push_notify`.

## Threat model

| actor                       | sees plaintext? |
|-----------------------------|-----------------|
| Network adversary           | No (HTTPS + AES-GCM)
| Hosting admin / FTP intruder | No (only ciphertext blobs on disk)
| Browser DevTools, anyone with the token | Yes (intentional)
| Lost token                  | All sessions encrypted with that token become unreadable. Acceptable.

`RELAY_SECRET` in `config.php` is **optional**. When set, it acts as
a HTTP Bearer-auth spam gate. When unset / empty / `CHANGE_ME`, the
relay runs OPEN — every request is treated as unauthenticated.

This is a spam/DoS gate, not content secrecy:
- Content remains AES-GCM-encrypted under per-device tokens regardless.
- `op_close` and `op_push_notify` are token-bound via `trigger_secret`
  (see below), so even an open relay can't be tricked into closing
  other people's sessions or spamming pushes.
- The loss when running open: anyone who finds the URL can register
  fake sessions, write encrypted noise to existing ones, and consume
  disk + CPU. Recommended for any non-throwaway deployment.

The browser hides the secret-input field automatically when the
relay reports `auth_required = false`.

## Architecture

```
        local PC                            shared PHP host                  any browser
   ┌──────────────────┐                ┌──────────────────────┐         ┌────────────────┐
   │  termpilot-wrap    │  outbound      │   relay.php          │         │  index.html    │
   │   - PTY proxy    │  HTTPS         │     output / input   │ ←─────  │  (terminal UI) │
   │   - encrypts     │ ─────────────► │     resize / sessions│         │  (decrypts on  │
   │     output + in  │  CIPHERTEXT    │     register / meta  │         │   the fly)     │
   │   - decrypts     │  ONLY          │     push_*           │         └────────────────┘
   │     input from   │                │                      │
   │     browser      │                │  data/<sid>/         │
   │                  │                │    meta.bin          │
   │  4 daemon        │                │    out.records,.idx  │
   │  threads:        │                │    in.records,.idx   │
   │   output_uploader│                │    meta.public.json  │
   │   input_poller   │                │  data/push/          │
   │   heartbeat      │                │    <token_hash>/     │
   │   transcript_tail│                │      <sub_id>.json   │
   │  (push trigger   │                │  data/vapid.json     │
   │   only — no      │                └──────────────────────┘
   │   transcript on  │
   │   the relay)     │
   └──────────────────┘
```

### Crypto layer

- **Key**: 32 random bytes generated once by `--generate-token`. Stored
  on PC in OS keyring or `~/.config/termpilot/token` (chmod 600). PBKDF2 still lives
  in `crypto.derive_token` and is exercised only by deterministic
  cross-language test vectors.
- **Cipher**: AES-256-GCM with random 12-byte nonce per record + 16-byte tag.
- **Wire format**: `nonce(12) ‖ ciphertext ‖ tag(16)`, base64 over JSON.
- **AAD bound to (stream, sid, seq)** — prevents cross-stream replay,
  cross-session swaps, and reordering.
- **Sequence enforcement on the server** — POSTs with seq != next-position
  return HTTP 409; clients advance and continue. Eliminates double-store
  on retries after timeouts.

### Trigger secret (op_close + op_push_notify auth)

Anyone holding `RELAY_SECRET` would otherwise be able to close other
sessions or trigger pushes for any token_hash. RELAY_SECRET is a
shared HTTP gate, not an authorisation key — so these endpoints are
gated on **proof of token possession**:

```
trigger_secret = HMAC-SHA256(token, b"termpilot:trigger:v1")   # 32B, secret
trigger_id     = SHA-256(trigger_secret)                         # 32B, public
```

- Every entity holding the device token (wrapper PC + each browser)
  derives both independently. The relay never sees the token.
- The relay stores `trigger_id_hex` at register time
  (`meta.public.json`) and at first push_subscribe per token_hash
  (`data/push/<hash>/.trigger_id`, chmod 0600).
- On `op_close` / `op_push_notify`, the caller presents
  `trigger_secret_hex` in the body. The relay re-hashes and
  `hash_equals`-compares against the stored verifier; mismatch → 401.
- Both functions live in `shared/crypto.py` and `relay/lib/crypto.js`
  as `derive_trigger_secret` / `trigger_id_for` (Python) and
  `deriveTriggerSecret` / `triggerIdFor` (JS). Cross-language vectors
  in `linux/tests/crypto_vectors.json`.

### Protocol versioning ("v1" in AAD strings)

The AAD strings (`marker:v1:<sid>`, `meta:v1:<sid>`, `out:v1:<sid>:<seq>`,
`in:v1:<sid>:<seq>`) and the trigger info string
(`termpilot:trigger:v1`) carry a literal `v1`. Bumping to v2 requires
a coordinated change on all three sides (wrapper, relay, browser).

There is currently **no automatic version negotiation**: a v1 client
decrypting a v2 record fails with tag mismatch. The wrapper input
poller stalls at that seq and surfaces a visible "input wedged"
banner on stderr + an `input_wedge` event (refuses to silently skip
because the same code path defends against a hostile relay injecting
records to censor real input). The browser surfaces "decrypt failed"
and aborts attach after 5 consecutive failures. A future v2 upgrade
should add a `protocol_version` field to `meta.public.json` and have
the browser surface "this session uses a newer protocol — please
reload" rather than abort. Until then, treat protocol changes as
flag-day events.

### Single view, single endpoint

The browser hits `relay.php?op=output` (encrypted byte records), decrypts
each, feeds the bytes through a per-session offscreen xterm, then renders
the buffer as a coloured `<pre>` log. Input goes back via
`relay.php?op=input` with the same per-(stream, sid, seq) AAD.

(A chat view consuming a separate transcript.php existed in earlier
versions and was removed once the terminal view covered both use cases —
see git history if you need the design.)

### Multi-token enumeration (browser side)

`/sessions` returns ALL sessions on the relay with their `marker` blobs.
The browser tries each stored token against each marker:

```js
for (session of allSessions)
  for (token of storedTokens)
    if (failedAttempts.has(session.id, token.id)) skip
    try {
      decrypt(token, session.marker, "marker:v1:" + session.id)
      // success → session belongs to token; fetch & decrypt full meta
    } catch (e) {
      failedAttempts.add(session.id, token.id)
    }
```

Sessions whose marker doesn't decrypt with any of your tokens are
**hidden** — your view of the session list is exactly the union of what
your tokens unlock. Failed pairs are cached in memory until reload.

## CLI

```
termpilot --help                       show usage
termpilot --set-relay-url URL          persist the relay URL
termpilot --get-relay-url              print the configured relay URL
termpilot --set-relay-secret SECRET    persist the relay Bearer token (config.php's RELAY_SECRET)
termpilot --clear-relay-secret         remove the stored Bearer token
termpilot --generate-token             sudo-gated; mint a random 32-byte token, save (keyring/file)
termpilot --show-token                 sudo-gated; reveal stored token
termpilot --version                    print installed version + check for newer release
termpilot --update                     check for newer release; install if user accepts
                                       (skips the install prompt in dev checkouts)
termpilot                              spawn $SHELL in a PTY
termpilot bash                         spawn bash explicitly
termpilot tmux new -A -s main          persistent tmux session
termpilot htop                         any interactive program
tp claude                              any command works directly — the `--` separator
                                       is only needed when the child takes a flag that
                                       collides with a wrapper flag (--force, --insecure,
                                       --no-local, --title, --relay, --auth)
tp                                     short alias for `termpilot` (set by install.sh
                                       only if nothing else on the system provides `tp`)
```

The `termpilot` shell function is installed by `install.sh`; it
forwards to the right subcommand of the underlying `termpilot-wrap`
binary. Token-mutating and config-writing subcommands above are
recognised by the function and routed directly to the wrapper — they
work even before the relay URL is set.

`TERMPILOT_TOKEN_HEX` env var bypasses keyring/file lookup and is only
intended for tests.

## File layout

```
termpilot/
├── README.md                  umbrella overview (points to linux/, windows/, relay/)
├── ARCHITECTURE.md            this file
├── .gitignore
├── VERSION.json               dev value; CI rewrites per release
│
├── shared/                    cross-platform Python modules used by both wrappers
│   ├── crypto.py              AES-256-GCM + PBKDF2 + AAD constructors
│   └── release_channel.py     --version / --update + deferred update notice
│
├── linux/                     Linux/macOS wrapper, installers, tests, README
│   ├── README.md              Linux/macOS quickstart (ships in the zip)
│   ├── termpilot-wrap         the wrapper binary
│   ├── install.sh             dev installer (registers `termpilot` shell function)
│   ├── install-latest-version.sh   end-user bootstrap (downloads release zip)
│   ├── lib/
│   │   └── keystore.py        Linux/macOS keyring → file fallback
│   └── tests/
│       ├── run-all.sh         run every Python test (~100 s)
│       ├── test_crypto.py     crypto primitives + cross-language vectors
│       ├── test_keystore.py   keyring/file token storage
│       ├── test_e2e.py        relay protocol + security
│       ├── test_resilience.py spool, lock, active.json, alive flag, GC
│       ├── test_push.py       VAPID gen, subscribe/unsubscribe, notify dispatch
│       ├── test_wrapper_e2e.py wrapper round-trip with bash
│       ├── test_multi_instance.py per-cwd resilience-slot resolution
│       ├── test_config_gate.py RELAY_SECRET gate behaviour
│       ├── test_crypto.html   cross-language vectors (manual: browser)
│       └── crypto_vectors.json Python-generated vectors consumed by JS test
│
├── windows/                   Windows wrapper, installers, lib, README
│   ├── README.md              Windows quickstart (ships in the zip)
│   ├── termpilot-win-wrap.py  the wrapper (pywinpty-backed)
│   ├── install.bat / install.ps1
│   ├── install-latest-version.bat / .ps1
│   ├── requirements.txt       pywinpty, keyring, cryptography, qrcode
│   └── lib/
│       ├── keystore_win.py    Credential Manager + ACL'd file fallback
│       ├── pty_backend.py     pywinpty wrapper + console-mode helpers
│       └── resilience_win.py  msvcrt locking + OutputSpool + log-mirror
│
├── relay/                     what gets uploaded to the PHP host
│   ├── relay.php              encrypted byte/input/push relay
│   ├── index.html             terminal view (login + manage tokens + decrypt)
│   ├── manifest.webmanifest   PWA manifest
│   ├── sw.js                  service worker
│   ├── icon-{192,512}.png     app icons (regular + maskable variants)
│   ├── lib/
│   │   ├── crypto.js          JS port of shared/crypto.py (WebCrypto AES-GCM)
│   │   ├── session.js         multi-token enumeration + per-session key cache
│   │   ├── index.js           page-level UI logic
│   │   ├── index.css          styles
│   │   └── vendor/            xterm.js + jsQR (verbatim, pinned)
│   ├── config.example.php     copy → config.php; optional RELAY_SECRET + auto-GC tunables
│   └── .htaccess              deny data/, logs/, config.php; PWA MIME types
│
├── tools/
│   ├── lib-ftp-host.sh        shared: resolves FTP host (env / file / prompt)
│   ├── deploy.sh              upload relay/ over FTPS
│   ├── fetch-logs.sh          pull relay logs via FTP
│   ├── vendor-fetch.sh        refresh pinned browser deps under relay/lib/vendor/
│   └── build-release.sh       build per-platform release zips locally
│
└── .github/workflows/release.yml CI: on v* tag, build + attach both zips
```

The release workflow produces two flat zips:
`termpilot-linux-macos.zip` and `termpilot-windows.zip`. Each contains
the relevant platform tree's files plus a copy of `shared/`, flattened
to the zip's top level so end-users extract straight into their install
root with no nested `linux/` or `windows/` subdir.

## Setup (PC side)

Supported platforms: **Linux**, **macOS**, and **Windows**. Linux/macOS
runs `linux/termpilot-wrap` (pure-stdlib Python plus POSIX PTY/termios
calls). Windows runs `windows/termpilot-win-wrap.py`, which depends on
`pywinpty`, `keyring`, `cryptography`, and `qrcode`. Both speak the
same wire protocol through `shared/crypto.py`.

### End-user install via the bootstrap (Linux/macOS)

```sh
curl -fsSL https://raw.githubusercontent.com/hawwwran/termpilot/main/linux/install-latest-version.sh | bash
```

`linux/install-latest-version.sh` resolves the latest GitHub release tag,
downloads `termpilot-linux-macos.zip`, extracts to
`~/.local/share/termpilot/`, runs the bundled `install.sh`, then asks
whether to mint a device token and whether to deploy the relay.
Subsequent updates via `termpilot --update` re-extract over the same
path and re-run `install.sh` so the shell function repoints cleanly.

### End-user install (Windows)

Download `termpilot-windows.zip` from the latest release, extract, and
run `install.bat`. The installer offers to install Python 3.12 via
winget (user scope, no admin) if Python isn't found, `pip install --user`s
the runtime deps, creates `tp.cmd` + `termpilot.cmd` shims under
`%LOCALAPPDATA%\Programs\termpilot\bin\`, and adds that dir to USER
PATH. See `windows/README.md` for the full notes (Credential Manager
storage, ConPTY caveats, log mirroring back to the install source).

### Develop / contribute

1. `cd ~/git/termpilot/linux && ./install.sh` — repoints the shell function
   at the dev checkout. On Linux this patches `~/.bashrc`; on macOS
   `~/.zshrc` (and `~/.bash_profile` if you also use bash). It also
   defines a short `tp` alias when no other `tp` command exists.
2. `termpilot --set-relay-url https://your.host/path`
3. `termpilot --generate-token` — sudo-gated; mints a random 32-byte
   token, saves it (keyring/file), prints hex.
4. (Only if your relay sets `RELAY_SECRET` in `config.php`)
   `termpilot --set-relay-secret 'YOUR_RELAY_SECRET'`
5. `termpilot` — start a session.

To switch back to the installed prod copy after dev work:
`~/.local/share/termpilot/install.sh`.

### Release flow

- `VERSION.json` in repo root holds the version metadata (read by
  `termpilot --version` and `--update`). The committed value reflects
  the in-development version; CI rewrites it at release time so each
  shipped zip carries its own.
- `.github/workflows/release.yml` triggers on `v*` tags. It verifies
  the tag is on `main`, stamps `VERSION.json`, generates a
  `VERSION.md` changelog from `git log <prev>..<this>`, then runs
  `tools/build-release.sh linux` and `tools/build-release.sh windows`
  to produce two flat zips (`termpilot-linux-macos.zip` and
  `termpilot-windows.zip`, each containing the relevant platform tree
  plus `shared/`, `VERSION.json`, and a platform README), and attaches
  both to the GitHub Release.
- The local launcher
  `~/SynologyDrive/Development/linux/hwntools-custom-packages/releases/release-termpilot.sh`
  is the convenience wrapper that picks the next version (patch bump
  by default), pushes the tag, and polls until the release asset is
  attached. It's sudo-gated (anti-shoulder-surf) and uses `gh` for
  the release API checks.

### On-start update notice (deferred, two-phase)

Each `termpilot run` invocation does two cheap things to surface a
new release without ever stalling or interleaving with the wrapped
child's output. Both live in `shared/release_channel.py`.

**Phase 1 (sync, 3-second budget).** Before any relay or PTY work,
the wrapper checks for a parked notice at
`~/.config/termpilot/update-pending.json`. If one exists, it re-queries
GitHub. Three outcomes:

| GitHub reachable? | Newer than installed? | Action |
|---|---|---|
| no | — | silent return; parked file untouched |
| yes | no (caught up) | clear parked file; no banner |
| yes | yes | show coloured banner on stderr; refresh parked tag |

The banner is a short coloured stripe printed BEFORE the wrapper's
own connect-banner and session start, so it always renders above the
wrapped child's TTY output:

```
═══ TermPilot update ════════════════════════════════════
  v0.1.1 available  (installed: 0.0.0)
  Run termpilot --update to install
═════════════════════════════════════════════════════════
```

**Phase 2 (async, daemon thread, 3-second timeout).** After Phase 1,
a background thread queries GitHub and writes the parked file (or
clears it if we've caught up) for the *next* invocation. Runs while
the PTY child has the screen, so a slow GitHub never delays the
session and stale info is never written into the child's output.

The deferred display is the point: a sync check on every session
start would either add up to 3s of latency (acceptable but
noticeable), or risk splattering text into the wrapped child if the
result arrived after the PTY took over. Parking the *result* for
next start sidesteps both.

Dev checkouts (`.git` next to the wrapper) skip both phases — the
developer doesn't need to be nudged toward `termpilot --update`,
which is a no-op in dev anyway.

## Setup (browser side)

1. Visit the relay URL.
2. Login modal: paste a first device token + name (e.g. "Desktop"). The
   relay-secret field appears only when the relay has `RELAY_SECRET`
   configured.
3. "Manage tokens" button later for additional devices or rotation.

## Testing

`linux/tests/run-all.sh` runs all Python tests (~100 s, dominated by
`test_wrapper_e2e.py` which spawns a real bash through the wrapper).

For browser-side cross-language vectors:
```sh
python3 linux/tests/test_crypto.py --gen-vectors
python3 -m http.server 7755 --bind 127.0.0.1 &
xdg-open http://127.0.0.1:7755/linux/tests/test_crypto.html
```

## Operational notes (future-me)

- Phone is **strictly view-only** for size — never let phone size affect
  the wrapper's PTY. Only the local terminal's SIGWINCH drives it.
- Keyring on Linux requires GNOME Keyring (or compatible Secret Service);
  macOS uses the system Keychain via the same `keyring` Python package.
  Without one, `termpilot-wrap` falls back to `~/.config/termpilot/token`
  and prints a warning. The fallback is fine for personal use.
- `install.sh` patches every shell rc file that exists or matches
  `$SHELL` (`~/.bashrc`, `~/.bash_profile`, `~/.zshrc`). On a fresh macOS
  account it creates `~/.zshrc`; on a fresh Linux account it creates
  `~/.bashrc`.
- `php -S` is single-threaded — long-poll requests serialize, so testing
  with the dev server requires care (kill the wrapper or close browser
  tabs before running tests that POST). Apache/php-fpm doesn't have this
  problem.
- Sequence-conflict (HTTP 409) handling on POST is critical — without
  it, a timeout where the server actually got the request causes a
  duplicate at the next position with the wrong AAD seq → all subsequent
  decryptions fail. Don't remove that path.
- Each `termpilot` invocation creates its own session_id (random hex).
  Sessions are scoped to a single wrapper-process lifetime — for
  longer-lived sessions, wrap the command in `tmux` or `screen`.
- **Diagnostic event log:** every wrapper run appends JSONL events to
  `~/.cache/termpilot/events.log` (rotates at 256 KB →
  `events.log.1`). Categories: `wrapper_start`, `recovery_no_marker`,
  plus any `tx_*` / `push_*` events from future trigger code. Set
  `TERMPILOT_DEBUG=1` to also mirror to stderr.
- Tokens in localStorage live indefinitely. To purge from the browser
  side, use the Manage Tokens modal or just clear localStorage.
