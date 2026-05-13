"""
Resilience layer for the Windows wrapper.

Direct counterpart to the lock + spool code in the Linux wrapper
(termpilot-wrap lines ~70–500). Same on-disk layout, same semantics,
adjusted for Windows quirks:

  - %LOCALAPPDATA%\\termpilot\\cache\\        (instead of ~/.cache/termpilot/)
      cwd/<encoded-cwd>/<instance>/
        wrapper.lock           msvcrt.locking LK_NBLCK (advisory)
        active.json            {wrapper_sid, ts, pid, marker_b64}
      sid/<sid>/
        out.spool / out.cursor / out.next_seq

  - msvcrt.locking instead of fcntl.flock. Different unit (locks a byte
    range starting at the current file position), still advisory.
    Releasing requires LK_UNLCK on the same byte range.

  - chmod calls are no-ops on Windows; ACL hardening would need icacls
    which is best-effort.

  - Instance label: TTY-name doesn't work on Windows. We default to
    "default" and let the user pass --instance NAME / $TERMPILOT_INSTANCE.

Public surface mirrors Linux: log_event, resolve_instance, cwd_cache_dir,
sid_cache_dir, acquire_wrapper_lock, release_wrapper_lock,
read_active_json, write_active_json, OutputSpool, cleanup_stale_sid_dirs,
CRASH_RECOVERY_SECS, EVENT_LOG.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import sys
import time
from pathlib import Path
from typing import Optional


# %LOCALAPPDATA% is the right home for non-roaming cache state on
# Windows; the spool can grow large and we don't want it on the user's
# AD-roamed profile.
def _cache_base() -> str:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return os.path.join(base, "termpilot", "cache")
    return os.path.join(str(Path.home()), "AppData", "Local", "termpilot", "cache")


CACHE_BASE = _cache_base()
EVENT_LOG = os.path.join(CACHE_BASE, "events.log")
EVENT_LOG_MAX_BYTES = 256 * 1024
CRASH_RECOVERY_SECS = 5 * 60

# If install.bat captured an install-source path (typically the share
# the user installed from), the wrapper mirrors event-log writes there
# so the developer / operator can read logs without copying files off
# the machine.
def _install_source_log_path() -> Optional[str]:
    base = os.environ.get("APPDATA")
    if not base:
        return None
    p = Path(base) / "termpilot" / "install_source.txt"
    try:
        src = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not src:
        return None
    try:
        logs_dir = Path(src) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    host = (os.environ.get("COMPUTERNAME")
            or os.environ.get("HOSTNAME")
            or "win")
    return str(logs_dir / f"{host}-{os.getpid()}.log")


_share_log_path_cache: Optional[str] = None
_share_log_path_resolved: bool = False


def _share_log_path() -> Optional[str]:
    global _share_log_path_cache, _share_log_path_resolved
    if not _share_log_path_resolved:
        _share_log_path_cache = _install_source_log_path()
        _share_log_path_resolved = True
    return _share_log_path_cache


def log_event(cat: str, **fields) -> None:
    """Append a JSONL event. Best-effort; never raises. Mirrors to the
    install-source logs folder if one was recorded at install time."""
    try:
        os.makedirs(CACHE_BASE, exist_ok=True)
    except OSError:
        return
    record = {"ts": time.time(), "cat": cat, **fields}
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    # Rotate primary log
    try:
        if os.path.exists(EVENT_LOG) and os.path.getsize(EVENT_LOG) > EVENT_LOG_MAX_BYTES:
            try:
                bak = EVENT_LOG + ".1"
                if os.path.exists(bak):
                    os.remove(bak)
                os.replace(EVENT_LOG, bak)
            except OSError:
                pass
    except OSError:
        pass
    try:
        with open(EVENT_LOG, "ab") as f:
            f.write(line)
    except OSError:
        pass
    if os.environ.get("TERMPILOT_DEBUG"):
        try:
            sys.stderr.write(f"[event] {cat} {fields}\n")
        except Exception:
            pass
    share = _share_log_path()
    if share:
        try:
            with open(share, "ab") as f:
                f.write(line)
        except OSError:
            pass


def _encode_cwd(cwd: str) -> str:
    return hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16]


_INSTANCE_LABEL_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


def _validate_instance_label(s: str, *, source: str) -> str:
    if s in (".", "..") or not _INSTANCE_LABEL_RE.fullmatch(s):
        raise ValueError(f"invalid instance label from {source}: {s!r}")
    return s


def resolve_instance(arg_value: Optional[str]) -> str:
    """Pick the per-cwd resilience-slot label.

    Windows has no /dev/pts equivalent. We use a stable derivative of
    the parent console window when available, else fall back to
    "default". The user can always pass --instance NAME explicitly.
    """
    if arg_value:
        return _validate_instance_label(arg_value, source="--instance")
    env = os.environ.get("TERMPILOT_INSTANCE")
    if env:
        return _validate_instance_label(env, source="TERMPILOT_INSTANCE")
    # Try to derive an instance from the console window handle (HWND)
    # so two terminals in the same cwd get distinct slots automatically.
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            label = f"con{hwnd & 0xFFFFFFFF:x}"
            try:
                return _validate_instance_label(label, source="console")
            except ValueError:
                pass
    except Exception:
        pass
    return "default"


def cwd_cache_dir(cwd: str, instance: str) -> str:
    return os.path.join(CACHE_BASE, "cwd", _encode_cwd(cwd), instance)


def sid_cache_dir(sid: str) -> str:
    return os.path.join(CACHE_BASE, "sid", sid)


# ---------------------------------------------------------------------------
# Locking — msvcrt-based, byte-range advisory on a fixed offset.
# ---------------------------------------------------------------------------
#
# msvcrt.locking() locks a byte range starting at the current file
# position. We lock 1 byte at offset 0, with non-blocking semantics.
# To release, seek back to 0 and LK_UNLCK the same byte.

def acquire_wrapper_lock(cwd: str, instance: str) -> Optional[int]:
    """Try to take an exclusive lock for (cwd, instance). Returns an
    open file descriptor (kept alive for the lifetime of the wrapper)
    or None if another process holds it."""
    import msvcrt
    d = cwd_cache_dir(cwd, instance)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"termpilot: cannot create cache dir {d}: {e}\n")
        return None
    lock_path = os.path.join(d, "wrapper.lock")
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        sys.stderr.write(f"termpilot: cannot open lock file {lock_path}: {e}\n")
        return None
    try:
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return fd


def release_wrapper_lock(fd: Optional[int]) -> None:
    if fd is None:
        return
    import msvcrt
    try:
        os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# active.json
# ---------------------------------------------------------------------------

def read_active_json(cwd: str, instance: str) -> dict:
    p = os.path.join(cwd_cache_dir(cwd, instance), "active.json")
    try:
        with open(p, "r") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_active_json(cwd: str, instance: str, **fields) -> None:
    d = cwd_cache_dir(cwd, instance)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return
    p = os.path.join(d, "active.json")
    obj = read_active_json(cwd, instance)
    obj.update(fields)
    obj["ts"] = int(time.time())
    obj["pid"] = os.getpid()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
    except OSError:
        pass


def clear_active_json(cwd: str, instance: str) -> None:
    p = os.path.join(cwd_cache_dir(cwd, instance), "active.json")
    try:
        os.unlink(p)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Cleanup of stale spool dirs (best-effort housekeeping)
# ---------------------------------------------------------------------------

def cleanup_stale_sid_dirs(keep_sid: str, max_age_secs: int = 7 * 24 * 3600) -> None:
    base = os.path.join(CACHE_BASE, "sid")
    if not os.path.isdir(base):
        return
    # Defense-in-depth: if a junction / symlink ever appeared under sid/,
    # rmtree would otherwise traverse it. Realpath-canonicalise and skip
    # anything whose target escapes the cache base. Mirrors the relay's
    # op_gc sanity check.
    try:
        real_base = os.path.realpath(base)
    except OSError:
        return
    now = time.time()
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        if name == keep_sid:
            continue
        d = os.path.join(base, name)
        try:
            real_d = os.path.realpath(d)
        except OSError:
            continue
        if real_d != real_base and not real_d.startswith(real_base + os.sep):
            continue
        try:
            if now - os.path.getmtime(d) <= max_age_secs:
                continue
        except OSError:
            continue
        try:
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# OutputSpool — plaintext journal of PTY bytes awaiting POST.
# Mirrors the Linux class verbatim except for chmod/fsync edge cases.
# ---------------------------------------------------------------------------

class OutputSpool:
    HDR = struct.Struct("<I")

    def __init__(self, sid: str, stream: str):
        d = sid_cache_dir(sid)
        os.makedirs(d, exist_ok=True)
        self.sid = sid
        self.stream = stream
        self._spool_path = os.path.join(d, f"{stream}.spool")
        self._cursor_path = os.path.join(d, f"{stream}.cursor")
        self._seq_path = os.path.join(d, f"{stream}.next_seq")
        if not os.path.exists(self._spool_path):
            with open(self._spool_path, "wb"):
                pass
        # Append-binary in shared mode so other processes don't get locked
        # out of stat'ing the file (Windows opens it exclusively by default).
        self._fh = open(self._spool_path, "ab", buffering=0)
        self._fh.seek(0, 2)
        self._size = self._fh.tell()

    def append(self, plain: bytes) -> int:
        self._fh.write(self.HDR.pack(len(plain)))
        self._fh.write(plain)
        try:
            self._fh.flush()
        except OSError:
            pass
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass
        self._size += 4 + len(plain)
        return self._size

    def cursor(self) -> int:
        try:
            with open(self._cursor_path, "rb") as f:
                d = f.read(8)
            return int.from_bytes(d, "little") if len(d) == 8 else 0
        except OSError:
            return 0

    def write_cursor(self, off: int) -> None:
        tmp = self._cursor_path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(off.to_bytes(8, "little"))
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, self._cursor_path)
        except OSError:
            pass

    def confirm_through(self, off: int) -> None:
        if off > self.cursor():
            self.write_cursor(off)

    def next_seq(self) -> int:
        try:
            with open(self._seq_path, "rb") as f:
                d = f.read(4)
            return int.from_bytes(d, "little") if len(d) == 4 else 0
        except OSError:
            return 0

    def write_next_seq(self, n: int) -> None:
        tmp = self._seq_path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(n.to_bytes(4, "little"))
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, self._seq_path)
        except OSError:
            pass

    def pending_chunks(self) -> list:
        out: list = []
        cur = self.cursor()
        try:
            with open(self._spool_path, "rb") as f:
                f.seek(cur)
                while True:
                    h = f.read(4)
                    if len(h) < 4:
                        break
                    n = self.HDR.unpack(h)[0]
                    p = f.read(n)
                    if len(p) < n:
                        break
                    end_off = f.tell()
                    out.append({"plain": p, "end_off": end_off})
        except OSError:
            pass
        return out

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass
