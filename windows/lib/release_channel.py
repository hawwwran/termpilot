"""
Release-channel awareness for the Windows wrapper.

Mirrors lib/release_channel.py from the Linux tree, but the installer
call uses cmd.exe + install-latest-version.bat. VERSION.json lives next
to the wrapper script as before.
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


GITHUB_REPO       = "hawwwran/termpilot"
GITHUB_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
INSTALLER_RAW     = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/windows/install-latest-version.bat"

ON_START_TIMEOUT_S = 3.0


def _config_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "termpilot"
    return Path.home() / "AppData" / "Roaming" / "termpilot"


PENDING_NOTICE_FILE = str(_config_dir() / "update-pending.json")


def _read_local_version(script_dir: str):
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


def _semver_tuple(s):
    s = (s or "").lstrip("v").split("-", 1)[0]
    parts = s.split(".")[:3]
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def _fetch_latest_tag(timeout: float = 15.0):
    try:
        req = urllib.request.Request(GITHUB_LATEST_API, headers={
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
    sys.stdout.write(prompt + " [Y/n] ")
    sys.stdout.flush()
    try:
        reply = sys.stdin.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return reply in ("", "y", "yes")


def _is_dev_tree(script_dir: str) -> bool:
    """A .git dir anywhere up the path tree marks a dev checkout."""
    p = Path(script_dir).resolve()
    for _ in range(8):
        if (p / ".git").is_dir():
            return True
        if p.parent == p:
            break
        p = p.parent
    return False


def _run_installer(script_dir: str) -> int:
    local = os.path.join(script_dir, "install-latest-version.bat")
    if os.path.isfile(local):
        return subprocess.call(["cmd.exe", "/c", local])
    sys.stdout.write(
        "install-latest-version.bat not next to this wrapper; "
        "please re-download from "
        f"{INSTALLER_RAW}\n"
    )
    return 1


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


# --- on-start notice (deferred) -------------------------------------------

def _save_pending(tag: str) -> None:
    d = os.path.dirname(PENDING_NOTICE_FILE)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return
    tmp = PENDING_NOTICE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"tag": tag, "checked_at": int(time.time())}, f)
        os.replace(tmp, PENDING_NOTICE_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _clear_pending() -> None:
    try:
        os.unlink(PENDING_NOTICE_FILE)
    except OSError:
        pass


def format_update_notice_lines(tag: str, current_v) -> list:
    """Return the notice as a list of ANSI-coloured lines (no trailing
    newlines). Used by callers that want to inject the notice into a
    shell-startup banner instead of writing it directly to stderr."""
    CYAN   = "\033[0;36m"
    YELLOW = "\033[1;33m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RESET  = "\033[0m"
    rule = "═" * 57
    installed = f"  {DIM}(installed: {current_v}){RESET}" if current_v else ""
    return [
        f"{CYAN}═══ TermPilot update ════════════════════════════════════{RESET}",
        f"  {YELLOW}{tag}{RESET} available{installed}",
        f"  Run {BOLD}termpilot --update{RESET} to install",
        f"{CYAN}{rule}{RESET}",
    ]


def _show_update_notice(tag: str, current_v) -> None:
    sys.stderr.write("\n".join(format_update_notice_lines(tag, current_v)) + "\n")


def peek_pending_update_notice(script_dir: str):
    """Like handle_pending_update_notice, but returns (tag, current_v)
    if a notice should be shown — without writing anything. Also saves
    the pending file (so the deferred-notice machinery still ticks).

    Returns None when nothing should be displayed (dev tree, no pending
    file, GitHub unreachable, or already caught up)."""
    try:
        if _is_dev_tree(script_dir):
            return None
        if not os.path.exists(PENDING_NOTICE_FILE):
            return None
        v, _ = _read_local_version(script_dir)
        latest = _fetch_latest_tag(timeout=ON_START_TIMEOUT_S)
        if latest is None:
            return None
        if v and _semver_tuple(latest) <= _semver_tuple(v):
            _clear_pending()
            return None
        _save_pending(latest)
        return (latest, v)
    except Exception:
        return None


def handle_pending_update_notice(script_dir: str) -> None:
    """Stderr-writing variant. Kept for callers that aren't injecting
    the banner into a shell startup."""
    notice = peek_pending_update_notice(script_dir)
    if notice is not None:
        _show_update_notice(*notice)


def spawn_async_update_check(script_dir: str) -> None:
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
