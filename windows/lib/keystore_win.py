"""
Windows token storage for termpilot.

Mirrors the public API of lib/keystore.py from the Linux tree, but uses
Windows-specific paths and ACL hardening for the file fallback.

Strategy:
- Prefer the `keyring` package (Windows Credential Manager on Win10+).
  Service "termpilot", username "default" — same names as Linux so a
  token generated on Linux can be re-pasted into a Windows install by
  hand if desired.
- Fall back to %APPDATA%\\termpilot\\token. We can't chmod 600 on
  Windows, but we lock the file's ACL to "owner only" via icacls when
  available.

Public API (same shape as Linux keystore):
    has_token() -> bool
    save_token(token_bytes) -> str        # "keyring" or "file"
    load_token() -> bytes | None
    delete_token() -> None
    backend_in_use() -> "keyring" | "file" | "none"
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import crypto

KEYRING_SERVICE = "termpilot"
KEYRING_USERNAME = "default"


def _config_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "termpilot"
    # Last-ditch fallback if APPDATA isn't set (rare on real Windows;
    # common when running under unusual test harnesses).
    return Path.home() / "AppData" / "Roaming" / "termpilot"


TOKEN_FILE = _config_dir() / "token"


def _try_keyring():
    try:
        import keyring  # type: ignore
        from keyring.backends import fail  # type: ignore
    except Exception:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    if isinstance(backend, fail.Keyring):
        return None
    # Probe write/read so a broken Credential Manager surface fails fast.
    try:
        keyring.set_password(KEYRING_SERVICE, "__probe__", "ok")
        if keyring.get_password(KEYRING_SERVICE, "__probe__") != "ok":
            return None
        keyring.delete_password(KEYRING_SERVICE, "__probe__")
    except Exception:
        return None
    return keyring


_keyring_cache: Optional[bool] = None


def _keyring():
    global _keyring_cache
    if _keyring_cache is None:
        kr = _try_keyring()
        _keyring_cache = kr if kr is not None else False
    return _keyring_cache or None


def keyring_available() -> bool:
    return _keyring() is not None


def backend_in_use() -> str:
    if _keyring() is not None and _keyring_get() is not None:
        return "keyring"
    if TOKEN_FILE.exists():
        return "file"
    return "none"


def has_token() -> bool:
    return load_token() is not None


def warn_once_no_keyring() -> None:
    if _keyring() is None:
        sys.stderr.write(
            "WARNING: Windows Credential Manager unavailable; token will "
            f"be stored in {TOKEN_FILE}. The file is ACL'd to your user "
            "but anyone who can read it can decrypt your sessions. "
            "Install the `keyring` Python package (pip install --user "
            "keyring) for Credential Manager storage.\n"
        )


# ---- keyring path ----------------------------------------------------------


def _keyring_get() -> Optional[bytes]:
    kr = _keyring()
    if kr is None:
        return None
    try:
        v = kr.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except Exception:
        return None
    if not v:
        return None
    try:
        return crypto.hex_to_token(v)
    except Exception:
        return None


def _keyring_set(token_bytes: bytes) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(
            KEYRING_SERVICE, KEYRING_USERNAME,
            crypto.token_to_hex(token_bytes),
        )
        return True
    except Exception:
        return False


def _keyring_delete() -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
        return True
    except Exception:
        return False


# ---- file path -------------------------------------------------------------


def _lock_acl_owner_only(path: Path) -> None:
    """Best-effort: remove inherited ACEs and grant only the current user
    full control. icacls is part of base Windows; if it's missing we
    silently leave the file at its default ACL (still under %APPDATA%,
    which is per-user). Anything more aggressive (DACL via win32security)
    would pull in pywin32.
    """
    try:
        user = os.environ.get("USERNAME") or ""
        if not user:
            return
        # Disable inheritance, drop existing ACEs, grant the user (F)ull.
        # /Q quiets, /C continues on partial errors.
        subprocess.run(
            ["icacls", str(path), "/inheritance:r"],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["icacls", str(path), "/grant", f"{user}:F"],
            check=False, capture_output=True,
        )
    except FileNotFoundError:
        # icacls not on PATH — give up silently.
        pass


def _file_get() -> Optional[bytes]:
    if not TOKEN_FILE.exists():
        return None
    try:
        hex_str = TOKEN_FILE.read_text().strip()
        return crypto.hex_to_token(hex_str)
    except Exception as e:
        sys.stderr.write(f"WARNING: couldn't load token from {TOKEN_FILE}: {e}\n")
        return None


def _file_set(token_bytes: bytes) -> None:
    parent = TOKEN_FILE.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass
    # On Windows there's no O_NOFOLLOW; the parent dir is %APPDATA% which
    # is per-user, so a foreign symlink at this path would require an
    # attacker who already controls the user's profile.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (crypto.token_to_hex(token_bytes) + "\n").encode("ascii"))
    finally:
        os.close(fd)
    os.replace(str(tmp), str(TOKEN_FILE))
    _lock_acl_owner_only(TOKEN_FILE)


def _file_delete() -> None:
    try:
        TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass


# ---- public API ------------------------------------------------------------


def save_token(token_bytes: bytes) -> str:
    if len(token_bytes) != crypto.TOKEN_BYTES:
        raise ValueError("token must be 32 bytes")
    if _keyring_set(token_bytes):
        if TOKEN_FILE.exists():
            _file_delete()
        return "keyring"
    warn_once_no_keyring()
    _file_set(token_bytes)
    return "file"


def load_token() -> Optional[bytes]:
    t = _keyring_get()
    if t is not None:
        return t
    return _file_get()


def delete_token() -> None:
    _keyring_delete()
    _file_delete()
