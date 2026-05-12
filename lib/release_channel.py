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
import urllib.error
import urllib.request


GITHUB_REPO       = "hawwwran/termpilot"
GITHUB_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
INSTALLER_RAW     = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/install-latest-version.sh"


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


def _fetch_latest_tag() -> str | None:
    """Hit the GitHub API for the latest release. Returns tag str or None."""
    try:
        req = urllib.request.Request(GITHUB_LATEST_API, headers={
            # GitHub requires a User-Agent on every request.
            "User-Agent": "termpilot-update-check",
            "Accept": "application/vnd.github+json",
        })
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
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
    if _confirm("Install it now?"):
        return _run_installer(script_dir)
    return 0
