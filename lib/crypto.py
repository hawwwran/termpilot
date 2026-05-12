"""
Crypto primitives for termpilot.

Layer A:  password → token  (PBKDF2-HMAC-SHA-256, 600k iter, fixed salt)
Layer B:  token  → AES-256-GCM with random nonce per record + AAD

Wire format for an encrypted record (`Crypto.encrypt` output):
    nonce(12) || ciphertext || tag(16)
    base64-encoded for JSON transport.

The same KDF parameters and the same wire format are mirrored in JS via
WebCrypto in `php/lib/crypto.js`. Cross-language round-trip is
verified by `tests/test_crypto.py` (writes vectors) and
`tests/test_crypto.html` (reads vectors).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# Protocol-version-locked constants. Changing any of these requires a
# coordinated change in the JS port. The salt isn't a "secret" — it's
# a domain separator. A constant salt is fine because the password is
# the only secret.
SALT = b"termpilot:v1"
PBKDF2_ITERATIONS = 600_000
TOKEN_BYTES = 32  # AES-256 key length
NONCE_BYTES = 12
TAG_BYTES = 16

# Domain-separated info string for the trigger secret derivation. The
# trigger secret is a *separate* secret from the device token — leaking
# it lets the holder close sessions / fire pushes but does NOT let them
# decrypt any session content. See trigger_id_for / derive_trigger_secret.
TRIGGER_INFO = b"termpilot:trigger:v1"


def derive_token(password: str) -> bytes:
    """Derive a 32-byte token from a UTF-8 password.

    Deterministic — same password always yields the same token. Uses
    PBKDF2-HMAC-SHA-256 with 600,000 iterations.
    """
    if not isinstance(password, str) or password == "":
        raise ValueError("password must be a non-empty string")
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        SALT,
        PBKDF2_ITERATIONS,
        TOKEN_BYTES,
    )


def token_to_hex(token: bytes) -> str:
    if len(token) != TOKEN_BYTES:
        raise ValueError(f"token must be {TOKEN_BYTES} bytes, got {len(token)}")
    return token.hex()


def hex_to_token(hex_str: str) -> bytes:
    h = hex_str.strip().lower()
    if len(h) != TOKEN_BYTES * 2:
        raise ValueError(f"hex token must be {TOKEN_BYTES * 2} chars, got {len(h)}")
    try:
        b = bytes.fromhex(h)
    except ValueError as e:
        raise ValueError(f"invalid hex token: {e}")
    return b


class Crypto:
    """AES-256-GCM with associated data and base64 wire format."""

    def __init__(self, key: bytes):
        if len(key) != TOKEN_BYTES:
            raise ValueError(f"key must be {TOKEN_BYTES} bytes, got {len(key)}")
        self._aesgcm = AESGCM(key)

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        """Encrypt `plaintext` with a fresh random nonce.

        Returns: nonce(12) || ciphertext || tag(16)
        AESGCM in `cryptography` already appends the tag to the
        ciphertext for us, so the structure is:
            nonce + aesgcm.encrypt(...)
        """
        if not isinstance(plaintext, (bytes, bytearray)):
            raise TypeError("plaintext must be bytes")
        if not isinstance(aad, (bytes, bytearray)):
            raise TypeError("aad must be bytes")
        nonce = secrets.token_bytes(NONCE_BYTES)
        ct_with_tag = self._aesgcm.encrypt(nonce, bytes(plaintext), bytes(aad))
        return nonce + ct_with_tag

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        """Decrypt a record produced by `encrypt`."""
        if not isinstance(blob, (bytes, bytearray)):
            raise TypeError("blob must be bytes")
        if not isinstance(aad, (bytes, bytearray)):
            raise TypeError("aad must be bytes")
        if len(blob) < NONCE_BYTES + TAG_BYTES:
            raise ValueError("blob too short")
        nonce = bytes(blob[:NONCE_BYTES])
        ct_with_tag = bytes(blob[NONCE_BYTES:])
        return self._aesgcm.decrypt(nonce, ct_with_tag, bytes(aad))

    # Convenience: base64 over JSON.
    def encrypt_b64(self, plaintext: bytes, aad: bytes) -> str:
        return base64.b64encode(self.encrypt(plaintext, aad)).decode("ascii")

    def decrypt_b64(self, b64: str, aad: bytes) -> bytes:
        return self.decrypt(base64.b64decode(b64.encode("ascii")), aad)


# AAD constructors — keep them in one place so JS and Python stay in sync.

def aad_marker(sid: str) -> bytes:
    return f"marker:v1:{sid}".encode("ascii")


def aad_meta(sid: str) -> bytes:
    return f"meta:v1:{sid}".encode("ascii")


def aad_record(stream: str, sid: str, seq: int) -> bytes:
    """For chunked streams: out (PTY → browser) / in (browser → PTY)."""
    if stream not in ("out", "in"):
        raise ValueError(f"unknown stream: {stream}")
    return f"{stream}:v1:{sid}:{seq}".encode("ascii")


# ---- Trigger secret (authorises state-changing relay endpoints) ----------
#
# The trigger secret is HMAC-SHA256(token, TRIGGER_INFO). Every entity
# that has the device token (wrapper PC + each browser) can derive it
# independently; the relay never sees it derived from anything but
# pre-existing knowledge (a stored trigger_id from a prior register /
# subscribe). The relay stores SHA-256(trigger_secret) = trigger_id as
# a public verifier; on op_close / op_push_notify, the caller presents
# trigger_secret in the body and the relay re-hashes + compares.
#
# Threat model: an attacker holding RELAY_SECRET (the shared HTTP gate)
# but NOT the device token can read encrypted blobs (still useless —
# AES-GCM) but cannot derive trigger_secret, so close/push_notify
# require token possession, not just RELAY_SECRET.

def derive_trigger_secret(token: bytes) -> bytes:
    """32-byte secret bound to the device token. Same on wrapper + browser."""
    if len(token) != TOKEN_BYTES:
        raise ValueError(f"token must be {TOKEN_BYTES} bytes, got {len(token)}")
    return hmac.new(token, TRIGGER_INFO, hashlib.sha256).digest()


def trigger_id_for(token: bytes) -> bytes:
    """Public verifier (32 bytes). Stored on the relay during register /
    push_subscribe; compared against SHA-256(client-supplied secret) on
    close / push_notify."""
    return hashlib.sha256(derive_trigger_secret(token)).digest()
