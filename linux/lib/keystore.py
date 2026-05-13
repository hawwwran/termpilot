"""
Token storage for termpilot.

Strategy:
- Try the OS keyring first (libsecret / GNOME Keyring on Linux,
  Keychain on macOS, Credential Store on Windows). Token is stored
  with service="termpilot", username="default".
- Fall back to ~/.config/termpilot/token (chmod 600) if keyring is unavailable, with
  a clear warning printed to stderr the first time we use the file.

The token itself is the 32-byte AES key (stored as 64-char hex). In v2
it is generated from os.urandom(32) — there is no password derivation
in the user-facing flow. crypto.derive_token still exists but is
exercised only by deterministic cross-language test vectors.

Public API:
    has_token() -> bool
    save_token(token_bytes) -> str        # returns "keyring" or "file"
    load_token() -> bytes | None
    delete_token() -> None
    backend_in_use() -> "keyring" | "file" | "none"
"""
from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path
from typing import Optional

from shared import crypto

KEYRING_SERVICE = "termpilot"
KEYRING_USERNAME = "default"
TOKEN_FILE = Path.home() / ".config/termpilot/token"


def _try_keyring():
    """Return the keyring module if it's importable AND has a working
    backend that isn't a fail-safe null backend. Returns None otherwise.
    """
    try:
        import keyring  # type: ignore
        from keyring.backends import fail  # type: ignore
    except Exception:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    # ChainerBackend with no usable backends behaves like null; probe it.
    if isinstance(backend, fail.Keyring):
        return None
    # Attempt a write/read cycle on a probe key to ensure it actually works.
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
    """Memoised lookup; importing keyring is slow."""
    global _keyring_cache
    if _keyring_cache is None:
        kr = _try_keyring()
        _keyring_cache = kr if kr is not None else False
    return _keyring_cache or None


def backend_in_use() -> str:
    """Return one of "keyring", "file", or "none"."""
    if _keyring() is not None and _keyring_get() is not None:
        return "keyring"
    if TOKEN_FILE.exists():
        return "file"
    return "none"


def keyring_available() -> bool:
    return _keyring() is not None


def has_token() -> bool:
    return load_token() is not None


def warn_once_no_keyring() -> None:
    """Print a one-time warning to stderr if we're using the file
    fallback because no keyring is available."""
    if _keyring() is None:
        if sys.platform == "darwin":
            hint = (
                "On macOS this normally means the `keyring` Python package "
                "isn't installed; `pipx install keyring` or `pip install --user "
                "keyring` will activate the macOS Keychain backend."
            )
        elif sys.platform.startswith("linux"):
            hint = (
                "Install gnome-keyring (or another Secret Service provider) "
                "and the `keyring` Python package for better security."
            )
        else:
            hint = (
                "Install the `keyring` Python package and a system credential "
                "store for better security."
            )
        sys.stderr.write(
            "WARNING: no OS keyring is available; token will be stored in "
            f"{TOKEN_FILE} (chmod 600). Anyone with read access to this "
            f"file can decrypt your sessions. {hint}\n"
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


def _file_get() -> Optional[bytes]:
    # O_NOFOLLOW + fstat: refuse to read a symlink at this path, and
    # check perms on the actual inode we have open (no TOCTOU between
    # the perms check and the read). Without the flag, Path.stat() and
    # read_text() would happily traverse a link planted by a local
    # attacker into something readable but not theirs.
    try:
        fd = os.open(str(TOKEN_FILE), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:
            sys.stderr.write(
                f"WARNING: {TOKEN_FILE} is a symlink; refusing to load. "
                f"Remove and re-run --generate-token.\n"
            )
        return None
    try:
        st = os.fstat(fd)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            sys.stderr.write(
                f"WARNING: {TOKEN_FILE} has loose permissions "
                f"({oct(stat.S_IMODE(st.st_mode))}); refusing to load. "
                f"Run: chmod 600 {TOKEN_FILE}\n"
            )
            return None
        hex_str = os.read(fd, 65536).decode("ascii").strip()
        return crypto.hex_to_token(hex_str)
    except Exception as e:
        sys.stderr.write(f"WARNING: couldn't load token from {TOKEN_FILE}: {e}\n")
        return None
    finally:
        try: os.close(fd)
        except OSError: pass


def _file_set(token_bytes: bytes) -> None:
    # Ensure the parent dir exists with mode 0700 and tighten on every
    # call so a stale 0755 dir from an earlier tool can't expose the
    # token through directory traversal.
    parent = TOKEN_FILE.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(str(parent), 0o700)
    except OSError:
        pass
    # Write atomically with mode 0600 from creation. O_NOFOLLOW + O_EXCL
    # so a pre-existing symlink at the .tmp path can't redirect the
    # write — a local attacker who can race the writer would otherwise
    # capture the hex token at the moment os.open creates-and-follows.
    tmp = TOKEN_FILE.with_suffix(".tmp")
    # Remove any stale .tmp from a prior interrupted run — O_EXCL would
    # otherwise refuse to open.
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
    except OSError:
        pass
    fd = os.open(
        str(tmp),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(fd, (crypto.token_to_hex(token_bytes) + "\n").encode("ascii"))
    finally:
        os.close(fd)
    os.replace(str(tmp), str(TOKEN_FILE))
    os.chmod(str(TOKEN_FILE), 0o600)


def _file_delete() -> None:
    try:
        TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass


# ---- public API ------------------------------------------------------------


def save_token(token_bytes: bytes) -> str:
    """Persist the token. Returns the backend used: "keyring" or "file"."""
    if len(token_bytes) != crypto.TOKEN_BYTES:
        raise ValueError("token must be 32 bytes")
    if _keyring_set(token_bytes):
        # If a fallback file existed, remove it to avoid drift.
        if TOKEN_FILE.exists():
            _file_delete()
        return "keyring"
    warn_once_no_keyring()
    _file_set(token_bytes)
    return "file"


def load_token() -> Optional[bytes]:
    """Return the stored token, preferring keyring over file."""
    t = _keyring_get()
    if t is not None:
        return t
    return _file_get()


def delete_token() -> None:
    """Remove the token from BOTH backends (defensive)."""
    _keyring_delete()
    _file_delete()
