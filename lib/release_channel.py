"""
Release-channel awareness for TermPilot.

Implements the `--version` and `--update` subcommands. Lives in its own
module so the main wrapper stays focused on the PTY/relay bridge.

VERSION.json lives next to the wrapper script. Each release zip carries
its own VERSION.json (overwritten by CI). A dev checkout carries
whatever value happens to be committed, which is fine — `--update`
from a dev checkout still resolves and runs install-latest-version.sh
(the prod install), shadowing the dev tree until the user re-runs
the dev tree's install.sh.
"""
from __future__ import annotations

import json
import os
import shlex
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


GITHUB_REPO       = "hawwwran/termpilot"
GITHUB_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
INSTALLER_RAW     = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/install-latest-version.sh"

# Where the deferred update notice is parked between sessions. Written
# by the async post-start check; read by the sync pre-start check on
# the *next* invocation. Living in ~/.config/termpilot/ keeps the file
# per-user (no /tmp races) and adjacent to the other config files.
PENDING_NOTICE_FILE = os.path.expanduser("~/.config/termpilot/update-pending.json")

# Short timeout for the on-start checks — we never want the wrapper to
# stall a session because GitHub or DNS is flaky. The user-facing
# `--version` / `--update` paths keep the longer default.
ON_START_TIMEOUT_S = 3.0


def _read_local_version(script_dir: str):
    """Return (version, data_dict) or (None, None) on any failure."""
    p = os.path.join(script_dir, "VERSION.json")
    try:
        with open(p) as f:
            data = json.load(f)
        v = data.get("version")
        if isinstance(v, str) and v:
            return v, data
    except (OSError, ValueError):
        pass
    return None, None


def _semver_tuple(s: str | None):
    """Best-effort semver tuple. Strips leading 'v' and any -prerelease."""
    s = (s or "").lstrip("v").split("-", 1)[0]
    parts = s.split(".")[:3]
    out = []
    for p in parts:
        try: out.append(int(p))
        except ValueError: out.append(0)
    while len(out) < 3: out.append(0)
    return tuple(out)


def _fetch_latest_tag(timeout: float = 15.0) -> str | None:
    """Hit the GitHub API for the latest release. Returns tag str or
    None. `timeout` is per-request seconds; the on-start checks pass a
    short value so a wrapped session never stalls on network trouble."""
    try:
        req = urllib.request.Request(GITHUB_LATEST_API, headers={
            # GitHub requires a User-Agent on every request.
            "User-Agent": "termpilot-update-check",
            "Accept": "application/vnd.github+json",
        })
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = data.get("tag_name")
        if isinstance(tag, str) and tag:
            return tag
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return None


def _confirm(prompt: str) -> bool:
    """Read y/n from stdin (default yes). Returns True on yes."""
    sys.stdout.write(prompt + " [Y/n] ")
    sys.stdout.flush()
    try:
        reply = sys.stdin.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return reply in ("", "y", "yes")


def _is_dev_tree(script_dir: str) -> bool:
    """A `.git` directory next to the wrapper marks a dev checkout.
    `--version` / `--update` still report newer releases there, but
    skip the "install now?" prompt — the user is editing the code,
    not running it as a production install."""
    return os.path.isdir(os.path.join(script_dir, ".git"))


def _run_installer(script_dir: str) -> int:
    """Invoke install-latest-version.sh. Prefer the local copy next to
    the wrapper (it's part of the same release zip); fall back to
    streaming from GitHub if it's missing (e.g., partial dev tree)."""
    local = os.path.join(script_dir, "install-latest-version.sh")
    if os.path.isfile(local):
        return subprocess.call(["bash", local])
    sys.stdout.write(
        "install-latest-version.sh not present next to this wrapper; "
        "fetching from GitHub.\n"
    )
    return subprocess.call([
        "bash", "-c",
        f"curl -fsSL {shlex.quote(INSTALLER_RAW)} | bash",
    ])


def cmd_version(args, script_dir: str) -> int:
    v, data = _read_local_version(script_dir)
    if v:
        tag  = (data.get("tag")      if data else None) or f"v{v}"
        date = (data.get("released") if data else None) or "?"
        sys.stdout.write(f"termpilot {v} ({tag}, released {date})\n")
    else:
        sys.stdout.write("termpilot (version unknown — VERSION.json missing or unreadable)\n")
    sys.stdout.write("Checking for newer release on GitHub...\n")
    latest = _fetch_latest_tag()
    if latest is None:
        sys.stdout.write("  Could not reach GitHub. You may still be on the latest version.\n")
        return 0
    if v and _semver_tuple(latest) <= _semver_tuple(v):
        sys.stdout.write(f"  You're on the latest version (remote: {latest}).\n")
        return 0
    sys.stdout.write(f"  Newer version available: {latest}\n")
    if _is_dev_tree(script_dir):
        sys.stdout.write("  DEV environment detected. Skipping install prompt.\n")
        return 0
    if _confirm("Install it now?"):
        return _run_installer(script_dir)
    return 0


def cmd_update(args, script_dir: str) -> int:
    sys.stdout.write("Checking for newer release on GitHub...\n")
    latest = _fetch_latest_tag()
    if latest is None:
        sys.stderr.write("termpilot: could not fetch latest release from GitHub.\n")
        return 1
    v, _ = _read_local_version(script_dir)
    if v and _semver_tuple(latest) <= _semver_tuple(v):
        sys.stdout.write(f"You're on the latest version ({v}).\n")
        return 0
    sys.stdout.write(f"Newer version available: {latest} "
                     + (f"(installed: {v})\n" if v else "(installed: unknown)\n"))
    if _is_dev_tree(script_dir):
        sys.stdout.write("DEV environment detected. Skipping install prompt.\n")
        return 0
    if _confirm("Install it now?"):
        return _run_installer(script_dir)
    return 0


# ============================================================================
#  On-start update notice (deferred — never blocks the wrapped session)
# ============================================================================
#
# Each `termpilot run` invocation does two things, both quick:
#
#  1. SYNC pre-flight (3s max): if a previous run parked a pending
#     notice in PENDING_NOTICE_FILE, re-check GitHub right now. If the
#     release is still newer than installed, print a coloured notice
#     to stderr. If we've caught up (or GitHub is reachable but says
#     no newer), clear the pending file. If GitHub is *not* reachable,
#     trust the stored notice and show it anyway.
#
#  2. ASYNC post-flight (background thread, 3s timeout): query GitHub
#     and write/clear PENDING_NOTICE_FILE for the *next* invocation.
#     Runs after the wrapped session has taken the TTY, so it can
#     never write into the child's output and never slows startup.

def _save_pending(tag: str) -> None:
    d = os.path.dirname(PENDING_NOTICE_FILE)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
    except OSError:
        return
    tmp = PENDING_NOTICE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"tag": tag, "checked_at": int(time.time())}, f)
        os.replace(tmp, PENDING_NOTICE_FILE)
        try: os.chmod(PENDING_NOTICE_FILE, 0o600)
        except OSError: pass
    except OSError:
        try: os.unlink(tmp)
        except OSError: pass


def _clear_pending() -> None:
    try: os.unlink(PENDING_NOTICE_FILE)
    except OSError: pass


def _read_pending_tag() -> str | None:
    try:
        with open(PENDING_NOTICE_FILE) as f:
            data = json.load(f)
        t = data.get("tag")
        if isinstance(t, str) and t:
            return t
    except (OSError, ValueError):
        pass
    return None


def _show_update_notice(tag: str, current_v: str | None) -> None:
    """Compact, coloured notice to stderr. Written above any session
    output the wrapper will produce; the wrapped child takes the TTY
    a few lines later so this stays as a header."""
    CYAN   = "\033[0;36m"
    YELLOW = "\033[1;33m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RESET  = "\033[0m"
    rule = "═" * 57
    installed = f"  {DIM}(installed: {current_v}){RESET}" if current_v else ""
    sys.stderr.write(
        f"{CYAN}═══ TermPilot update ════════════════════════════════════{RESET}\n"
        f"  {YELLOW}{tag}{RESET} available{installed}\n"
        f"  Run {BOLD}termpilot --update{RESET} to install\n"
        f"{CYAN}{rule}{RESET}\n"
    )


def handle_pending_update_notice(script_dir: str) -> None:
    """Sync pre-flight: read the pending file, re-check GitHub (3s),
    show or clear the notice accordingly. Skips in dev checkouts.
    Never raises — any error is swallowed so a session start can't
    be blocked by the update channel."""
    try:
        if _is_dev_tree(script_dir):
            return
        if not os.path.exists(PENDING_NOTICE_FILE):
            return
        v, _ = _read_local_version(script_dir)
        latest = _fetch_latest_tag(timeout=ON_START_TIMEOUT_S)
        if latest is None:
            # GitHub unreachable; trust the parked tag as a still-valid
            # notice. Don't touch the file — next time we'll re-check.
            pending = _read_pending_tag()
            if pending:
                _show_update_notice(pending, v)
            else:
                _clear_pending()
            return
        if v and _semver_tuple(latest) <= _semver_tuple(v):
            _clear_pending()
            return
        _show_update_notice(latest, v)
        _save_pending(latest)
    except Exception:
        pass


def spawn_async_update_check(script_dir: str) -> None:
    """Async post-flight: fire-and-forget background thread. Writes
    PENDING_NOTICE_FILE if a newer release exists, removes it if
    caught up, no-ops if GitHub is unreachable. Skips in dev
    checkouts. Daemon thread → dies with the wrapper."""
    if _is_dev_tree(script_dir):
        return
    def _go():
        try:
            latest = _fetch_latest_tag(timeout=ON_START_TIMEOUT_S)
            if latest is None:
                return
            v, _ = _read_local_version(script_dir)
            if v and _semver_tuple(latest) <= _semver_tuple(v):
                _clear_pending()
                return
            _save_pending(latest)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()
