#!/usr/bin/env python3
"""
termpilot-win-wrap — TermPilot wrapper for Windows.

Functionally the same as the Linux `termpilot-wrap`, but uses pywinpty
for the PTY layer, msvcrt for file locking, the Windows Credential
Manager (via the `keyring` package) for token storage, and Windows
console APIs (SetConsoleMode, GetConsoleScreenBufferInfo) instead of
termios/ioctl.

Wire format is byte-identical to the Linux side — AAD bound to
(stream, sid, seq), AES-256-GCM, base64 over JSON. The relay never
distinguishes Linux from Windows wrappers.

Subcommands:
  help, --help, -h
  generate-token, --generate-token
  show-token, --show-token
  set-relay-url, --set-relay-url URL
  get-relay-url, --get-relay-url
  set-relay-secret, --set-relay-secret SECRET
  clear-relay-secret, --clear-relay-secret
  version, --version, -V
  update, --update
  run [args]  (default if no subcommand)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import shutil
import http.client
import json
import os
import queue
import secrets
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)
# `shared/` ships flat alongside the wrapper in the release zip, or one
# level up in the dev tree. Probe both layouts.
for _shared_parent in (SCRIPT_DIR, os.path.join(SCRIPT_DIR, "..")):
    if os.path.isdir(os.path.join(_shared_parent, "shared")):
        _shared_parent = os.path.realpath(_shared_parent)
        if _shared_parent not in sys.path:
            sys.path.insert(0, _shared_parent)
        break

from shared import crypto  # noqa: E402
from lib import keystore_win as keystore, pty_backend, resilience_win as resil  # noqa: E402


# ---------------------------------------------------------------------------
# Config paths
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "termpilot"
    return Path.home() / "AppData" / "Roaming" / "termpilot"


CONFIG_DIR = _config_dir()
RELAY_URL_FILE = str(CONFIG_DIR / "relay-url")
SECRET_FILE = str(CONFIG_DIR / "relay-secret")


USAGE = """\
termpilot (Windows) — view and control any terminal over an encrypted relay.

Setup (run once)
  termpilot --set-relay-url URL          persist your relay's URL
  termpilot --get-relay-url              print the configured relay URL
  termpilot --set-relay-secret SECRET    persist the relay's Bearer token
  termpilot --clear-relay-secret         forget the relay's Bearer token
  termpilot --generate-token             create / replace your encryption token
  termpilot --show-token                 print the stored token
  termpilot --version                    print installed version + check for newer
  termpilot --update                     check for newer release and install if any
  termpilot --help                       this message

Run
  termpilot                              spawn the default shell (powershell.exe)
                                         through the wrapper
  termpilot cmd                          spawn cmd.exe
  termpilot powershell                   spawn powershell explicitly
  termpilot -- powershell -NoProfile     use -- when the child takes a flag
                                         that collides with a wrapper flag

  --instance NAME  resilience-slot label within this cwd. Defaults to a
                   stable derivative of the parent console window, so
                   two windows in one cwd get separate slots
                   automatically. $TERMPILOT_INSTANCE wins over the
                   console default; --instance wins over both. Charset
                   [a-zA-Z0-9_.-], length 1-64.

Configuration files (under %APPDATA%\\termpilot\\)
  relay-url        base URL of the relay deployment
  relay-secret     optional Bearer secret for the relay
  token            encryption token if Credential Manager is unavailable

Token storage:
  Tokens are stored in the Windows Credential Manager when available
  (via the `keyring` package). If that fails, the wrapper falls back
  to %APPDATA%\\termpilot\\token, ACL'd to your user.
"""


def cmd_help(args):
    sys.stdout.write(USAGE)
    return 0


def _confirm(prompt: str, default_no: bool = True) -> bool:
    suffix = "[y/N]" if default_no else "[Y/n]"
    while True:
        sys.stderr.write(f"{prompt} {suffix} ")
        sys.stderr.flush()
        line = sys.stdin.readline().strip().lower()
        if not line:
            return not default_no
        if line in ("y", "yes"):
            return True
        if line in ("n", "no"):
            return False


# ---------------------------------------------------------------------------
# Token QR rendering
# ---------------------------------------------------------------------------
#
# Mirrors the Linux _render_qr_truecolor logic: pull a boolean matrix
# (here from the pure-Python `qrcode` package) and emit it with explicit
# 24-bit foreground/background colours so the QR renders pure
# black-on-white regardless of the active console theme. jsQR (the
# browser-side scanner) is strict about quiet-zone contrast — themed
# colours can defeat it.

def _print_token_qr(hex_str: str) -> None:
    if not sys.stdout.isatty():
        return
    try:
        import qrcode  # type: ignore
        from qrcode.constants import ERROR_CORRECT_H  # type: ignore
    except ImportError:
        sys.stdout.write(
            "(install `qrcode` for a scannable QR: "
            "pip install --user qrcode)\n\n"
        )
        return

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, border=4)
    qr.add_data(hex_str)
    try:
        qr.make(fit=True)
    except Exception:
        return

    try:
        matrix = qr.get_matrix()
    except Exception:
        return
    if not matrix:
        return

    rows = [[bool(c) for c in row] for row in matrix]
    width = len(rows[0])
    if len(rows) % 2 != 0:
        rows.append([False] * width)

    BLACK = "0;0;0"
    WHITE = "255;255;255"

    out_lines = []
    for i in range(0, len(rows), 2):
        top, bot = rows[i], rows[i + 1]
        parts = []
        last_pair = None
        for c in range(width):
            fg = BLACK if top[c] else WHITE
            bg = BLACK if bot[c] else WHITE
            pair = (fg, bg)
            if pair != last_pair:
                parts.append(f"\033[38;2;{fg};48;2;{bg}m")
                last_pair = pair
            parts.append("▀")
        parts.append("\033[0m")
        out_lines.append("".join(parts))
    sys.stdout.write("\n".join(out_lines) + "\n\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def cmd_generate_token(args):
    if keystore.has_token():
        sys.stderr.write("A token already exists.\n")
        if not _confirm("Overwrite it? Existing sessions will be unreadable."):
            sys.stderr.write("aborted; existing token preserved.\n")
            return 1
    token = secrets.token_bytes(crypto.TOKEN_BYTES)
    backend = keystore.save_token(token)
    hex_str = crypto.token_to_hex(token)
    # Enable VT processing so the truecolor escapes render; not needed
    # for the hex line itself but cheap to do here.
    pty_backend.enable_vt_output()
    sys.stdout.write("\n")
    sys.stdout.write(f"Token (saved in {backend}):\n")
    sys.stdout.write(f"  {hex_str}\n\n")
    _print_token_qr(hex_str)
    sys.stdout.write("Copy this into the web UI's 'manage tokens' panel.\n")
    sys.stdout.write("Show it again later with:\n")
    sys.stdout.write("  termpilot --show-token\n")
    return 0


def cmd_show_token(args):
    stored = keystore.load_token()
    if stored is None:
        sys.stderr.write(
            "No token stored. Run `termpilot --generate-token` to create one.\n"
        )
        return 1
    hex_str = crypto.token_to_hex(stored)
    pty_backend.enable_vt_output()
    sys.stdout.write("\n")
    sys.stdout.write(f"Token (from {keystore.backend_in_use()}):\n")
    sys.stdout.write(f"  {hex_str}\n\n")
    _print_token_qr(hex_str)
    return 0


# ---------------------------------------------------------------------------
# Relay URL / secret
# ---------------------------------------------------------------------------

def _validate_relay_url(url: str):
    s = (url or "").strip()
    if not s:
        return "URL cannot be empty"
    if not (s.startswith("http://") or s.startswith("https://")):
        return "URL must start with http:// or https://"
    if " " in s or "\n" in s:
        return "URL must not contain whitespace"
    return None


def _normalize_relay_base(url: str) -> str:
    s = (url or "").strip().rstrip("/")
    if s.endswith("/relay.php"):
        s = s[: -len("/relay.php")]
    return s


def _relay_endpoints(base: str):
    base = base.rstrip("/")
    return base + "/relay.php", base + "/relay.php"


def cmd_set_relay_url(args):
    if not args:
        sys.stderr.write("Usage: termpilot --set-relay-url <https://your.host/path>\n")
        return 2
    url = _normalize_relay_base(args[0])
    err = _validate_relay_url(url)
    if err:
        sys.stderr.write(f"termpilot: bad URL — {err}\n")
        return 2
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"termpilot: cannot create {CONFIG_DIR}: {e}\n")
        return 1
    tmp = RELAY_URL_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(url + "\n")
        os.replace(tmp, RELAY_URL_FILE)
    except OSError as e:
        sys.stderr.write(f"termpilot: cannot write {RELAY_URL_FILE}: {e}\n")
        return 1
    sys.stdout.write(f"Saved {RELAY_URL_FILE}: {url}\n")
    return 0


def cmd_get_relay_url(args):
    try:
        with open(RELAY_URL_FILE, "r") as f:
            url = f.read().strip()
    except OSError:
        sys.stderr.write(
            "No relay URL configured.\n"
            "Set one with:\n"
            "  termpilot --set-relay-url https://your.host/path\n"
        )
        return 1
    if not url:
        sys.stderr.write("Relay URL file exists but is empty.\n")
        return 1
    sys.stdout.write(url + "\n")
    return 0


def cmd_set_relay_secret(args):
    if not args or not args[0]:
        sys.stderr.write(
            "Usage: termpilot --set-relay-secret <SECRET>\n"
            "  The value must match RELAY_SECRET in your config.php.\n"
        )
        return 2
    secret = args[0].strip()
    if not secret:
        sys.stderr.write("termpilot: secret cannot be empty\n")
        return 2
    if any(c.isspace() for c in secret):
        sys.stderr.write("termpilot: secret must not contain whitespace\n")
        return 2
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"termpilot: cannot create {CONFIG_DIR}: {e}\n")
        return 1
    tmp = SECRET_FILE + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, secret.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, SECRET_FILE)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        sys.stderr.write(f"termpilot: cannot write {SECRET_FILE}: {e}\n")
        return 1
    # Best-effort ACL tighten — same call shape as the keystore.
    try:
        user = os.environ.get("USERNAME") or ""
        if user:
            subprocess.run(["icacls", SECRET_FILE, "/inheritance:r"],
                           check=False, capture_output=True)
            subprocess.run(["icacls", SECRET_FILE, "/grant", f"{user}:F"],
                           check=False, capture_output=True)
    except FileNotFoundError:
        pass
    sys.stdout.write(f"Saved {SECRET_FILE}.\n")
    return 0


def cmd_clear_relay_secret(args):
    try:
        os.unlink(SECRET_FILE)
        sys.stdout.write(f"Removed {SECRET_FILE}.\n")
        return 0
    except FileNotFoundError:
        sys.stdout.write(f"No secret file at {SECRET_FILE} — nothing to do.\n")
        return 0
    except OSError as e:
        sys.stderr.write(f"termpilot: cannot remove {SECRET_FILE}: {e}\n")
        return 1


def _resolve_auth_secret(arg_value):
    if arg_value:
        return arg_value
    env = os.environ.get("TERMPILOT_SECRET")
    if env:
        return env
    if not os.path.exists(SECRET_FILE):
        return None
    try:
        return open(SECRET_FILE).read().strip()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Relay HTTP client (no happy-eyeballs needed on a single-relay deployment;
# Windows Python's getaddrinfo handles dual-stack reasonably already)
# ---------------------------------------------------------------------------

class Backoff:
    def __init__(self, base: float = 0.5, cap: float = 60.0):
        self.base = base
        self.cap = cap
        self._d = base

    def sleep(self):
        d = self._d
        time.sleep(d)
        self._d = min(self.cap, d * 2.0)

    def reset(self):
        self._d = self.base


class Relay:
    MAX_RESPONSE_BYTES = 16 * 1024 * 1024

    def __init__(self, base: str, secret: str, insecure: bool = False):
        self.base = base.rstrip("?")
        self.secret = secret
        self.ctx = (
            ssl._create_unverified_context() if insecure
            else ssl.create_default_context()
        )
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self.ctx)
        )

    def _url(self, op, **params):
        from urllib.parse import urlencode
        q = {"op": op, **{k: str(v) for k, v in params.items() if v is not None}}
        sep = "&" if "?" in self.base else "?"
        return f"{self.base}{sep}{urlencode(q)}"

    def request(self, method: str, op: str, *, params=None, body=None, timeout=30.0):
        url = self._url(op, **(params or {}))
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if self.secret:
            req.add_header("Authorization", f"Bearer {self.secret}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with self._opener.open(req, timeout=timeout) as r:
            raw = r.read(self.MAX_RESPONSE_BYTES + 1)
            if len(raw) > self.MAX_RESPONSE_BYTES:
                raise RuntimeError(f"relay response > {self.MAX_RESPONSE_BYTES} bytes")
            return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# run subcommand
# ---------------------------------------------------------------------------

def parse_run_args(argv):
    p = argparse.ArgumentParser(prog="termpilot", add_help=False, allow_abbrev=False)
    p.add_argument("--relay", default=os.environ.get("TERMPILOT_RELAY"))
    p.add_argument("--auth", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--no-local", action="store_true")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--instance", default=None)
    p.add_argument("-h", "--help", action="store_true")
    args, rest = p.parse_known_args(argv)
    if rest and rest[0] == "--":
        rest = rest[1:]
    return args, rest


def _walk_parent_chain(max_depth: int = 10) -> list:
    """Return the chain of ancestor process exe-names, nearest first.
    Uses ToolHelp32 snapshots via ctypes. Empty list on any failure."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value:
        return []
    try:
        index = {}
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if not kernel32.Process32First(snap, ctypes.byref(pe)):
            return []
        while True:
            index[pe.th32ProcessID] = (
                pe.th32ParentProcessID,
                pe.szExeFile.decode("mbcs", errors="replace"),
            )
            if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                break
    finally:
        kernel32.CloseHandle(snap)

    chain = []
    cur = os.getpid()
    for _ in range(max_depth):
        entry = index.get(cur)
        if not entry:
            break
        ppid, _name = entry
        parent = index.get(ppid)
        if not parent:
            break
        chain.append(parent[1])
        if ppid == cur:
            break
        cur = ppid
    return chain


def _detect_caller_shell() -> str:
    """Look at the parent process chain to figure out where the user
    actually typed `termpilot`. The `tp.cmd` shim always introduces one
    cmd.exe layer, so we skip a single leading cmd.exe and look at
    what's above it.

    Returns "cmd" or "powershell". Defaults to "cmd" on any uncertainty —
    PowerShell-on-cmd is a frequent setup; cmd-as-default is the safer
    surprise."""
    chain = [name.lower() for name in _walk_parent_chain()]
    # Skip the .cmd shim's cmd.exe layer (at most one).
    if chain and chain[0] == "cmd.exe":
        chain = chain[1:]
    for name in chain:
        if name in ("powershell.exe", "pwsh.exe"):
            return "powershell"
        if name == "cmd.exe":
            return "cmd"
        # Window hosts and explorer don't tell us anything about the
        # shell — keep walking.
        if name in ("windowsterminal.exe", "wt.exe", "openconsole.exe",
                    "conhost.exe", "explorer.exe"):
            continue
        # An unknown ancestor (IDE terminal, ssh session, etc.) — stop;
        # we'll fall through to the cmd default.
        break
    return "cmd"


def _default_shell():
    """Pick the shell that matches the user's caller. Override with
    $TERMPILOT_DEFAULT_SHELL=cmd|powershell|pwsh if the auto-detect
    guesses wrong (e.g., from a non-standard terminal host)."""
    override = (os.environ.get("TERMPILOT_DEFAULT_SHELL") or "").strip().lower()
    if override in ("cmd", "cmd.exe"):
        return [os.environ.get("ComSpec", "cmd.exe")]
    if override in ("pwsh", "pwsh.exe"):
        if shutil.which("pwsh.exe"):
            return ["pwsh.exe"]
        if shutil.which("powershell.exe"):
            return ["powershell.exe"]

    caller = _detect_caller_shell()
    if caller == "powershell":
        # Prefer pwsh (PowerShell 7+) when present; fall back to the
        # built-in Windows PowerShell 5.x.
        for candidate in ("pwsh.exe", "powershell.exe"):
            if shutil.which(candidate):
                return [candidate]
    return [os.environ.get("ComSpec", "cmd.exe")]


def _print_startup_banner(sid: str, spool_count: int, spool_bytes: int) -> None:
    DIM = "\033[2m"
    RESET = "\033[0m"
    TITLE = "\033[1;36m"
    pad = " " * len("termpilot ")
    lines = [f"{TITLE}termpilot{RESET} {DIM}session {RESET}{sid}"]
    if spool_count:
        lines.append(
            f"{pad}{DIM}spool   {RESET}replaying {spool_count} chunk(s) "
            f"({spool_bytes} bytes)"
        )
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()


def _wrap_shell_for_banner(cmd: list, sid: str,
                           spool_count: int, spool_bytes: int,
                           extra_lines: list = None) -> tuple:
    """If cmd is a bare cmd.exe / powershell / pwsh invocation, rewrite
    it to hide the shell's own startup banner AND emit the termpilot
    session banner *from inside* the PTY screen buffer. That places the
    banner where ConPTY's initial cursor sync can't clobber it.

    `extra_lines` are ANSI-formatted lines emitted before the main
    termpilot banner — used e.g. for the update-available notice that
    would otherwise be wiped by cls / Clear-Host.

    Returns (new_cmd_list, injected_bool). When injected_bool is True
    the caller must skip the stderr banner — we've put the banner into
    the child shell's first line of output instead.
    """
    if not cmd:
        return cmd, False
    base = os.path.basename(cmd[0]).lower()
    extras = cmd[1:]
    ESC = "\x1b"
    extra_lines = list(extra_lines or [])
    banner = f"{ESC}[1;36mtermpilot{ESC}[0m {ESC}[2msession{ESC}[0m {sid}"
    spool_line = ""
    if spool_count:
        spool_line = (
            f"{' ' * len('termpilot ')}{ESC}[2mspool{ESC}[0m   "
            f"replaying {spool_count} chunk(s) ({spool_bytes} bytes)"
        )

    if base in ("cmd", "cmd.exe") and not extras:
        # @chcp 65001 switches cmd to UTF-8 so the box-drawing chars in
        #   the update notice render correctly. ConPTY operates in UTF-8
        #   already, so this just keeps cmd's view of bytes consistent.
        # @cls clears the cmd startup banner from the PTY screen.
        # echo … emits each line; & chains commands regardless of exit.
        # prompt $P$G restores the standard prompt.
        pieces = ["@chcp 65001 > nul", "@cls"]
        for ln in extra_lines:
            pieces.append(f"echo {ln}")
        pieces.append(f"echo {banner}")
        if spool_line:
            pieces.append(f"echo {spool_line}")
        pieces.append("prompt $P$G")
        startup = "&".join(pieces)
        return [cmd[0], "/Q", "/K", startup], True

    if base in ("powershell", "powershell.exe", "pwsh", "pwsh.exe") and not extras:
        # -NoLogo suppresses PS banner. -NoExit keeps the shell open
        # after our -Command runs. Single quotes in PS strings get
        # doubled to escape.
        def _ps_quote(s: str) -> str:
            return "'" + s.replace("'", "''") + "'"
        ps_cmds = []
        for ln in extra_lines:
            ps_cmds.append(f"Write-Host {_ps_quote(ln)}")
        ps_cmds.append(f"Write-Host {_ps_quote(banner)}")
        if spool_line:
            ps_cmds.append(f"Write-Host {_ps_quote(spool_line)}")
        startup = "Clear-Host; " + "; ".join(ps_cmds)
        return [cmd[0], "-NoLogo", "-NoExit", "-Command", startup], True

    return cmd, False


def _safely(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def cmd_run(argv):
    args, cmd = parse_run_args(argv)
    pending_update = None
    try:
        from shared import release_channel
        pending_update = release_channel.peek_pending_update_notice(SCRIPT_DIR)
        release_channel.spawn_async_update_check(SCRIPT_DIR)
    except Exception:
        pass

    if not args.relay:
        try:
            with open(RELAY_URL_FILE, "r") as f:
                stored = f.read().strip()
            if stored:
                args.relay = stored
        except OSError:
            pass
    if args.help or not args.relay:
        sys.stderr.write(USAGE)
        if not args.relay:
            sys.stderr.write(
                "\nerror: relay URL not configured.\n"
                "  Set one with:  termpilot --set-relay-url https://your.host/path\n"
            )
        return 2

    if not cmd:
        cmd = _default_shell()

    relay_base = _normalize_relay_base(args.relay)
    relay_url, _t = _relay_endpoints(relay_base)
    auth_secret = _resolve_auth_secret(args.auth) or ""

    env_hex = os.environ.get("TERMPILOT_TOKEN_HEX")
    if env_hex:
        try:
            token = crypto.hex_to_token(env_hex)
        except Exception as e:
            sys.stderr.write(f"TERMPILOT_TOKEN_HEX invalid: {e}\n")
            return 2
    else:
        token = keystore.load_token()
    if token is None:
        sys.stderr.write(
            "No token. Run `termpilot --generate-token` to create one,\n"
            "then `termpilot --show-token` to copy it to the web UI.\n"
        )
        return 2

    crypto_obj = crypto.Crypto(token)
    token_hash_hex = hashlib.sha256(token).hexdigest()
    trigger_secret_hex = crypto.derive_trigger_secret(token).hex()
    trigger_id_hex = crypto.trigger_id_for(token).hex()

    cwd = os.getcwd()
    try:
        instance = resil.resolve_instance(args.instance)
    except ValueError as e:
        sys.stderr.write(f"termpilot: {e}\n")
        return 2

    lock_fd = resil.acquire_wrapper_lock(cwd, instance)
    if lock_fd is None and not args.force:
        sys.stderr.write(
            f"termpilot: another wrapper appears to be running in {cwd} "
            f"(instance '{instance}').\n"
            "  Close it first, pass --force to override, or pick a different "
            "--instance NAME.\n"
        )
        return 4
    if lock_fd is None:
        sys.stderr.write("termpilot: --force given; proceeding without an exclusive lock\n")

    active = resil.read_active_json(cwd, instance)
    now_ts = int(time.time())
    prev_sid = active.get("wrapper_sid") or None
    prev_marker_b64 = active.get("marker_b64") or None
    prev_age = now_ts - int(active.get("ts") or 0) if active.get("ts") else None

    sid = None
    if (prev_sid and prev_age is not None and 0 <= prev_age <= resil.CRASH_RECOVERY_SECS
            and os.path.isdir(resil.sid_cache_dir(prev_sid))):
        sid = prev_sid
    if sid is not None and not prev_marker_b64:
        resil.log_event("recovery_no_marker", prev_sid=prev_sid)
        sid = None
    if sid is None:
        sid = os.urandom(6).hex()
        prev_marker_b64 = None

    resil.cleanup_stale_sid_dirs(keep_sid=sid)
    resil.log_event("wrapper_start", sid=sid, prev_sid=prev_sid,
                    prev_age_s=prev_age, cwd_short=os.path.basename(cwd),
                    instance=instance, host=os.environ.get("COMPUTERNAME"))

    out_spool = resil.OutputSpool(sid, "out")
    spool_pending = out_spool.pending_chunks()
    spool_pending_bytes = sum(len(p["plain"]) for p in spool_pending)

    title = args.title or (cmd[0] if cmd else "session")
    relay = Relay(relay_url, auth_secret, insecure=args.insecure)

    cmd_str = shlex.join(cmd) if hasattr(shlex, "join") else " ".join(cmd)
    init_cols, init_rows = pty_backend.get_console_size()

    meta_plain = json.dumps({
        "title": title, "cwd": cwd, "cmd": cmd_str,
        "cols": init_cols, "rows": init_rows,
    }).encode("utf-8")

    enc_meta_b64 = crypto_obj.encrypt_b64(meta_plain, crypto.aad_meta(sid))
    if prev_marker_b64:
        enc_marker_b64 = prev_marker_b64
    else:
        enc_marker_b64 = crypto_obj.encrypt_b64(b"termpilot:v1", crypto.aad_marker(sid))

    try:
        reg = relay.request("POST", "register", body={
            "session_id": sid,
            "encrypted_meta": enc_meta_b64,
            "encrypted_marker": enc_marker_b64,
            "trigger_id_hex": trigger_id_hex,
            "cols": init_cols, "rows": init_rows,
        }, timeout=15)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        sys.stderr.write(f"termpilot: register failed: {e}\n")
        resil.release_wrapper_lock(lock_fd)
        return 1
    if reg.get("session_id") != sid:
        sys.stderr.write(f"termpilot: register returned unexpected sid {reg}\n")
        resil.release_wrapper_lock(lock_fd)
        return 1
    # Try to fold the wrapper banner into the shell's own startup so it
    # lands inside the PTY screen buffer (where ConPTY's initial cursor
    # sync can't clobber it) AND so we can pass -NoLogo / /Q to suppress
    # the shell's own version banner. The pending-update notice (if any)
    # rides along in the same injection so cls / Clear-Host doesn't wipe
    # it. Falls back to a pair of stderr writes if the cmd isn't a bare
    # shell.
    update_lines = []
    if pending_update is not None:
        try:
            from shared import release_channel
            update_lines = release_channel.format_update_notice_lines(*pending_update)
        except Exception:
            update_lines = []
    cmd, banner_injected = _wrap_shell_for_banner(
        cmd, sid, len(spool_pending), spool_pending_bytes,
        extra_lines=update_lines,
    )
    if not banner_injected:
        if pending_update is not None:
            try:
                from shared import release_channel
                sys.stderr.write(
                    "\n".join(release_channel.format_update_notice_lines(*pending_update))
                    + "\n"
                )
            except Exception:
                pass
        _print_startup_banner(sid, len(spool_pending), spool_pending_bytes)
    resil.write_active_json(cwd, instance, wrapper_sid=sid, marker_b64=enc_marker_b64)

    # Enable VT input/output on the local console so colours and arrow
    # keys work the way the user expects.
    prev_stdin_mode = pty_backend.enable_vt_input()
    prev_stdout_mode = pty_backend.enable_vt_output()
    have_console_stdin = pty_backend.is_stdin_console() and not args.no_local

    # Spawn the child via pywinpty.
    child_env = os.environ.copy()
    for v in ("TERMPILOT_TOKEN_HEX", "TERMPILOT_SECRET", "TERMPILOT_RELAY"):
        child_env.pop(v, None)
    try:
        child = pty_backend.PtyChild(
            cmd=cmd, cwd=cwd, env=child_env,
            cols=init_cols, rows=init_rows,
        )
    except RuntimeError as e:
        sys.stderr.write(f"termpilot: {e}\n")
        resil.release_wrapper_lock(lock_fd)
        return 1
    except Exception as e:
        sys.stderr.write(f"termpilot: failed to spawn child: {e}\n")
        resil.release_wrapper_lock(lock_fd)
        return 1

    stop = threading.Event()
    shutdown_requested = threading.Event()
    cleanup_done = threading.Event()

    # Ctrl-Close handler.
    #
    # When the user closes the console window, Windows sends
    # CTRL_CLOSE_EVENT to every process attached to that console and
    # then force-terminates them after about 5 seconds. Returning from
    # the handler immediately yields control back to the OS, which
    # kills us before the main thread's finally-block has time to POST
    # ?op=close — that's why on Windows previously the relay saw the
    # session "stall" instead of receiving a clean close. The fix is to
    # *block* inside the handler until cleanup_done is set: the OS
    # extends our lifetime as long as the handler runs (up to its
    # ~5-second budget). We wait a hair under that so we always return
    # cleanly rather than being killed mid-handler.
    try:
        import ctypes
        from ctypes import wintypes

        HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _ctrl_handler(ctrl_type):
            # 0=CTRL_C, 1=CTRL_BREAK, 2=CTRL_CLOSE, 5=LOGOFF, 6=SHUTDOWN
            if ctrl_type in (2, 5, 6):
                shutdown_requested.set()
                # Wait for the main-thread finally block to finish
                # (POST close, drain spool, release lock). The OS's
                # CTRL_CLOSE budget is ~5s; we wait 4.5 so we always
                # return cleanly rather than being mid-call when killed.
                cleanup_done.wait(timeout=4.5)
                return True
            return False

        _ctrl_handler_ref = HandlerRoutine(_ctrl_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl_handler_ref, True)
    except Exception:
        _ctrl_handler_ref = None  # noqa: F841

    # ---- I/O queues -------------------------------------------------------
    out_buf = bytearray()
    out_lock = threading.Lock()
    out_event = threading.Event()

    pending = list(spool_pending)
    next_seq_out = out_spool.next_seq()
    if pending and pending[0]["end_off"] <= out_spool.cursor():
        pending = [p for p in pending if p["end_off"] > out_spool.cursor()]

    # PTY reader thread
    def pty_reader():
        while not stop.is_set():
            try:
                data = child.read(65536)
            except Exception:
                break
            if not data:
                break
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except OSError:
                pass
            with out_lock:
                out_buf.extend(data)
            out_event.set()
        shutdown_requested.set()

    # Stdin reader thread (PC keyboard → child)
    def stdin_reader():
        while not stop.is_set():
            try:
                data = pty_backend.read_stdin_block(4096)
            except Exception:
                return
            if not data:
                return
            try:
                child.write(data)
            except Exception:
                return

    # Output uploader (same shape as Linux)
    def output_uploader():
        nonlocal next_seq_out
        backoff = Backoff(base=0.5, cap=60.0)
        while not stop.is_set():
            if not pending:
                out_event.wait(timeout=0.1)
                out_event.clear()
                with out_lock:
                    if not out_buf:
                        continue
                    chunk = bytes(out_buf)
                    out_buf.clear()
                end_off = out_spool.append(chunk)
                pending.append({"plain": chunk, "end_off": end_off})

            first = pending[0]
            seq = next_seq_out
            try:
                blob_b64 = crypto_obj.encrypt_b64(
                    first["plain"], crypto.aad_record("out", sid, seq),
                )
                if os.environ.get("TERMPILOT_DEBUG"):
                    sys.stderr.write(f"[upload] seq={seq} bytes={len(first['plain'])}\n")
                relay.request("POST", "output", body={
                    "session_id": sid,
                    "records": [{"seq": seq, "blob": blob_b64}],
                }, timeout=30)
                pending.pop(0)
                out_spool.confirm_through(first["end_off"])
                next_seq_out = seq + 1
                out_spool.write_next_seq(next_seq_out)
                backoff.reset()
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    try:
                        info = json.loads(e.read().decode("utf-8"))
                        expected = int(info.get("expected_seq", seq + 1))
                    except Exception:
                        expected = seq + 1
                    drop = max(0, expected - seq)
                    for _ in range(drop):
                        if not pending:
                            break
                        gone = pending.pop(0)
                        out_spool.confirm_through(gone["end_off"])
                    next_seq_out = expected
                    out_spool.write_next_seq(next_seq_out)
                    backoff.reset()
                else:
                    backoff.sleep()
            except Exception:
                backoff.sleep()
        # Final flush
        with out_lock:
            tail = bytes(out_buf)
            out_buf.clear()
        if tail:
            end_off = out_spool.append(tail)
            pending.append({"plain": tail, "end_off": end_off})
        for first in list(pending):
            seq = next_seq_out
            try:
                blob_b64 = crypto_obj.encrypt_b64(
                    first["plain"], crypto.aad_record("out", sid, seq),
                )
                relay.request("POST", "output", body={
                    "session_id": sid,
                    "records": [{"seq": seq, "blob": blob_b64}],
                }, timeout=5)
                pending.pop(0)
                out_spool.confirm_through(first["end_off"])
                next_seq_out = seq + 1
                out_spool.write_next_seq(next_seq_out)
            except Exception:
                break

    # Input poller (browser → wrapper)
    def input_poller():
        next_seq_in = 0
        backoff = Backoff(base=1.0, cap=60.0)
        wedged_seq = -1
        while not stop.is_set():
            try:
                j = relay.request("GET", "input", params={
                    "session": sid, "since_seq": next_seq_in,
                }, timeout=40)
                backoff.reset()
            except Exception:
                if stop.is_set():
                    return
                backoff.sleep()
                continue
            for rec in j.get("records") or []:
                seq = int(rec.get("seq", -1))
                blob_b64 = rec.get("blob") or ""
                if seq < 0 or not blob_b64:
                    continue
                if seq != next_seq_in:
                    continue
                try:
                    plaintext = crypto_obj.decrypt_b64(
                        blob_b64, crypto.aad_record("in", sid, seq),
                    )
                except Exception:
                    if seq != wedged_seq:
                        wedged_seq = seq
                        resil.log_event("input_wedge", sid=sid, seq=seq)
                        sys.stderr.write(
                            f"termpilot: input wedged at seq={seq} "
                            "(decrypt failed; refusing to skip — restart wrapper "
                            "or check the browser is using the right token)\n"
                        )
                    break
                wedged_seq = -1
                try:
                    child.write(plaintext)
                except Exception:
                    return
                next_seq_in = seq + 1

    # Heartbeat
    def heartbeat():
        while not stop.is_set():
            time.sleep(15)
            if stop.is_set():
                return
            _safely(relay.request, "POST", "heartbeat",
                    body={"session_id": sid}, timeout=10)

    # Resize watcher — Windows has no SIGWINCH. We poll the console size
    # every second and resize the PTY + relay if it changed.
    def resize_watcher():
        last = (init_cols, init_rows)
        while not stop.is_set():
            time.sleep(1.0)
            cur = pty_backend.get_console_size()
            if cur != last:
                cols, rows = cur
                child.resize(cols, rows)
                threading.Thread(target=lambda: _safely(
                    relay.request, "POST", "resize",
                    body={"session_id": sid, "cols": cols, "rows": rows},
                    timeout=5,
                ), daemon=True).start()
                last = cur

    threading.Thread(target=pty_reader, daemon=True).start()
    if have_console_stdin:
        threading.Thread(target=stdin_reader, daemon=True).start()
    threading.Thread(target=output_uploader, daemon=True).start()
    threading.Thread(target=input_poller, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=resize_watcher, daemon=True).start()

    # ---- Main loop: wait for child to exit or external close --------------
    # shutdown_requested.wait(0.1) wakes within 100 ms of a window-close
    # event; we don't have the 0.25-second slack the old poll loop did,
    # because CTRL_CLOSE_EVENT gives us only ~5 s to finish cleanup.
    try:
        while not shutdown_requested.is_set():
            if not child.alive():
                break
            if shutdown_requested.wait(timeout=0.1):
                break
    finally:
        stop.set()
        out_event.set()
        pty_backend.restore_console_mode(prev_stdin_mode)
        if prev_stdout_mode is not None:
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                STD_OUTPUT_HANDLE = -11
                h = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
                if h:
                    kernel32.SetConsoleMode(h, prev_stdout_mode)
            except Exception:
                pass

        # POST close FIRST so the relay learns the session ended even if
        # the OS truncates us on a window-close. Short timeout — the
        # whole finally-block runs inside the CTRL_CLOSE 5-second budget.
        _safely(relay.request, "POST", "close",
                body={"session_id": sid,
                      "trigger_secret_hex": trigger_secret_hex},
                timeout=2)

        if shutdown_requested.is_set():
            try:
                child.kill()
            except Exception:
                pass
        try:
            child.wait(timeout=1)
        except Exception:
            pass
        try:
            child.close()
        except Exception:
            pass

        try:
            leftover = out_spool.pending_chunks()
        except Exception:
            leftover = []
        try:
            out_spool.close()
        except Exception:
            pass

        try:
            cur = resil.read_active_json(cwd, instance)
            if leftover:
                cur["ts"] = int(time.time())
                cur.pop("pid", None)
            else:
                cur.pop("wrapper_sid", None)
                cur.pop("marker_b64", None)
                cur.pop("pid", None)
                cur["ts"] = int(time.time())
            d = resil.cwd_cache_dir(cwd, instance)
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass
            try:
                p = os.path.join(d, "active.json")
                tmp = p + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(cur, f)
                    f.flush()
                os.replace(tmp, p)
            except OSError:
                pass
        except Exception:
            pass

        if leftover:
            resil.log_event("preserve_spool_on_exit", sid=sid, pending=len(leftover))
        else:
            try:
                import shutil as _sh
                _sh.rmtree(resil.sid_cache_dir(sid), ignore_errors=True)
            except OSError:
                pass
        resil.release_wrapper_lock(lock_fd)
        # Tell the console-control handler it can return; the OS was
        # waiting on us before terminating. If the wrapper is exiting
        # normally (child exited, no CTRL_CLOSE), this is a no-op.
        cleanup_done.set()
    return 0


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------

def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] in ("help", "--help", "-h"):
        return cmd_help(argv[1:])
    if argv and argv[0] in ("generate-token", "--generate-token"):
        return cmd_generate_token(argv[1:])
    if argv and argv[0] in ("show-token", "--show-token"):
        return cmd_show_token(argv[1:])
    if argv and argv[0] in ("set-relay-url", "--set-relay-url"):
        return cmd_set_relay_url(argv[1:])
    if argv and argv[0] in ("get-relay-url", "--get-relay-url"):
        return cmd_get_relay_url(argv[1:])
    if argv and argv[0] in ("set-relay-secret", "--set-relay-secret"):
        return cmd_set_relay_secret(argv[1:])
    if argv and argv[0] in ("clear-relay-secret", "--clear-relay-secret"):
        return cmd_clear_relay_secret(argv[1:])
    if argv and argv[0] in ("version", "--version", "-V"):
        from shared import release_channel
        return release_channel.cmd_version(argv[1:], SCRIPT_DIR)
    if argv and argv[0] in ("update", "--update"):
        from shared import release_channel
        return release_channel.cmd_update(argv[1:], SCRIPT_DIR)
    if argv and argv[0] == "run":
        return cmd_run(argv[1:])
    return cmd_run(argv)


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc or 0)
