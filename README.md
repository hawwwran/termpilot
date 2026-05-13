# TermPilot

Remotely view and control **any terminal session** through a PHP relay
on shared hosting. Run `bash`, `tmux`, `htop`, `cmd`, PowerShell or
anything else interactive on a PC, and drive it from a browser on your
phone or another machine. **End-to-end encrypted**: the relay sees only
ciphertext blobs; only the PC running the wrapper and the browser hold
the keys.

<p align="center">
  <img src="webapp.jpg" alt="TermPilot on a phone, streaming a Claude Code session running on the host PC — any interactive terminal program works the same way." width="280" />
</p>

## Three independently-shipped pieces

| Component | Path | Ships in release as |
|-----------|------|---------------------|
| Linux / macOS wrapper | [`linux/`](linux/) | `termpilot-linux-macos.zip` |
| Windows wrapper | [`windows/`](windows/) | `termpilot-windows.zip` |
| Relay + browser PWA | [`relay/`](relay/) | uploaded to your PHP host via `tools/deploy.sh` |

The wrappers share one wire protocol (AES-256-GCM with AAD bound to
`(stream, sid, seq)`, base64 over JSON) so any combination drives the
same relay and any browser drives any wrapper. The shared crypto and
release-channel modules live under [`shared/`](shared/) and are
included verbatim in both zips.

## Quick install

**Linux / macOS** — one-liner:

```sh
curl -fsSL https://raw.githubusercontent.com/hawwwran/termpilot/main/linux/install-latest-version.sh | bash
```

See [`linux/README.md`](linux/README.md) for full notes.

**Windows** — open PowerShell and run:

```powershell
iwr -useb https://raw.githubusercontent.com/hawwwran/termpilot/main/windows/install-latest-version.ps1 | iex
```

The script downloads the latest `termpilot-windows.zip`, extracts it to
`%LOCALAPPDATA%\Programs\termpilot\`, and runs the bundled `install.bat`
(which offers to install Python 3.12 via winget if it isn't on PATH,
then `pip install --user`s the runtime deps and adds shims to USER PATH).
If you'd rather not pipe to `iex`, download the zip manually from the
[latest release](https://github.com/hawwwran/termpilot/releases/latest)
and double-click `install.bat`. See [`windows/README.md`](windows/README.md)
for the full notes (Credential Manager, ConPTY caveats, log mirroring).

**Relay** — upload the contents of [`relay/`](relay/) to a PHP-capable
host. The Linux/macOS quickstart covers `RELAY_SECRET`, `.htaccess`,
and the cron `?op=gc` cleanup.

## Architecture, threat model, ops notes

See [`ARCHITECTURE.md`](ARCHITECTURE.md). That's the canonical
engineering reference — wire format, resilience design, release flow,
operational footguns.

## Repo layout

```
termpilot/
├── linux/                       Linux/macOS wrapper, installers, tests
├── windows/                     Windows wrapper, installers
├── shared/                      crypto.py + release_channel.py (both wrappers)
├── relay/                       PHP backend + browser PWA (uploaded to your host)
├── tools/
│   ├── deploy.sh                upload relay/ over FTPS
│   ├── fetch-logs.sh            pull relay.log over FTP
│   ├── vendor-fetch.sh          refresh pinned browser deps under relay/lib/vendor/
│   └── build-release.sh         build per-platform release zips locally
├── .github/workflows/release.yml CI: on v* tag, build + attach both zips
├── VERSION.json                 dev value; CI rewrites per release
├── ARCHITECTURE.md              architecture, threat model, ops notes
└── README.md                    you are here
```

## Build releases locally

```sh
tools/build-release.sh linux       # → dist/termpilot-linux-macos.zip
tools/build-release.sh windows     # → dist/termpilot-windows.zip
```

The zips are flat (no `linux/` or `windows/` subdir leaks in), suitable
for direct extraction by end users.

## Tests

Linux/macOS test suite (covers the Linux wrapper + the relay, exercises
shared crypto):

```sh
linux/tests/run-all.sh
```

There is no automated Windows test suite yet; the wrapper is exercised
by hand on a Windows box.
