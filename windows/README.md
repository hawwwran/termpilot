# TermPilot for Windows

Standalone Windows port of the TermPilot wrapper. Speaks the exact same
encrypted wire protocol as the Linux wrapper (`../linux/termpilot-wrap`) — the
relay (`../relay/`) and browser PWA don't change.

## Requirements

- Windows 10 1809+ (ConPTY support)
- Python 3.9 or newer on PATH (or installed via the official installer
  with "Add python.exe to PATH" ticked)
- Network access for the install step (downloads pip packages)

## Quick install

**Fresh-install one-liner (no manual download needed)** — open PowerShell and run:

```powershell
iwr -useb https://raw.githubusercontent.com/hawwwran/termpilot/main/windows/install-latest-version.ps1 | iex
```

That downloads the latest `termpilot-windows.zip` from GitHub, extracts it
to `%LOCALAPPDATA%\Programs\termpilot\`, and runs the bundled `install.bat`.

**From an already-extracted zip** — double-click `install.bat`, or run it
from cmd / PowerShell:

    install.bat

This installs `tp` and `termpilot` into `%LOCALAPPDATA%\Programs\termpilot\bin\`
and adds that directory to your USER PATH. It also `pip install --user`s
the runtime dependencies (`pywinpty`, `keyring`, `cryptography`, `qrcode`).

If Python 3.9+ isn't on PATH, `install.bat` offers to install Python 3.12
via winget (user scope, no admin needed) or, as a fallback, downloads the
official python.org installer and runs it silently. Both paths add Python
to your user PATH so the installer continues without a console restart.

**Re-fetch a newer release later** — `termpilot --update`, or re-run the
one-liner above. If you've already extracted a release, the bundled
`install-latest-version.bat` does the same thing without leaving the
extracted dir.

After install, open a new console window (PATH changes only apply to new
sessions) and set yourself up:

    termpilot --set-relay-url https://your.host/path
    termpilot --generate-token
    termpilot

`termpilot` with no arguments spawns PowerShell (or `cmd.exe` if no
PowerShell is on PATH) through the wrapper. Pass a command line to run
a different program:

    termpilot cmd
    termpilot pwsh -NoLogo
    termpilot -- pwsh -NoLogo -File script.ps1   (use -- when child flags clash)

## What gets installed where

- `%LOCALAPPDATA%\Programs\termpilot\bin\tp.cmd`         — shim
- `%LOCALAPPDATA%\Programs\termpilot\bin\termpilot.cmd`  — shim
- `%APPDATA%\termpilot\relay-url`                        — your relay URL
- `%APPDATA%\termpilot\relay-secret`                     — Bearer (optional, ACL-locked)
- `%APPDATA%\termpilot\token`                            — fallback token (only if Credential Manager is unavailable)
- `%APPDATA%\termpilot\install_source.txt`               — where install.bat was run from (used for log mirroring)
- `%LOCALAPPDATA%\termpilot\cache\`                      — per-session output spool + locks

Tokens are stored in **Windows Credential Manager** when the `keyring`
package is installed (it is, by default — `install.bat` pulls it via
pip). Service `termpilot`, user `default`. Same names as Linux, so the
same hex token works on either OS (paste it once into the web UI and
either machine can drive it).

## Logging

The wrapper writes structured JSONL events to:

- `%LOCALAPPDATA%\termpilot\events.log` (primary, rotates at 256 KB)
- `<install_source>\logs\<HOST>-<PID>.log` (mirror, only when
  `install_source.txt` exists and the path is writable — useful when
  installing from a network share so logs land back on the share)

Set `TERMPILOT_DEBUG=1` to also stream events to stderr in the running
console.

If a session feels laggy, run `termpilot --test-connection` from a fresh
console — it probes the relay's `debug.php` endpoint and reports
wall-clock RTT versus PHP-side time so you can tell whether the issue is
network, relay-side filesystem, or FastCGI worker queueing on the host.

## Multiple windows in the same project

Each running wrapper holds a per-(cwd, instance) lock. The instance
defaults to a stable derivative of the parent console window handle,
so opening two terminals in the same directory yields two separate
sessions automatically. Override with `--instance NAME` or
`$env:TERMPILOT_INSTANCE`.

## Differences from Linux

- No `sudo` gate around token operations on Windows (sudo doesn't
  exist there). A simple "overwrite existing token?" prompt is used
  for `--generate-token`. The token itself is still a 32-byte random
  secret in Credential Manager; the gate was only anti-shoulder-surf.
- No `qrencode`-style QR rendering. Token is shown as hex; paste it
  into the web UI's manage-tokens dialog the same way as on Linux.
- No `SIGWINCH`. Console resize is detected by a 1-second poller; the
  relay learns about new dimensions within ~1 second of the user
  resizing the window.
- The wrapper depends on `pywinpty` (pip package). The Linux wrapper
  is stdlib-only; this is one of the few unavoidable cross-platform
  differences.

## Uninstall

There is no automated uninstaller. Manually:

1. Remove `%LOCALAPPDATA%\Programs\termpilot\bin\` from your USER PATH
   (PowerShell: `[Environment]::SetEnvironmentVariable("Path", ..., "User")`)
2. Delete `%LOCALAPPDATA%\Programs\termpilot\`
3. Delete `%APPDATA%\termpilot\`
4. Delete `%LOCALAPPDATA%\termpilot\` (cache + spool)
5. In Credential Manager, remove the `termpilot` entry under
   **Windows Credentials → Generic Credentials**.
