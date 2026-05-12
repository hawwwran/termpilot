/**
 * Crypto primitives for termpilot — JavaScript port of crypto.py.
 *
 * Same wire format as the Python side:
 *   nonce(12) || ciphertext || tag(16)
 * KDF: PBKDF2-HMAC-SHA-256, 600,000 iterations, salt = "termpilot:v1"
 *
 * All bytes are Uint8Array. Helpers for hex/base64 conversion included.
 *
 * The exported `AM` object is the public surface used by index.html.
 */
(function () {
  const SALT_STR = "termpilot:v1";
  const PBKDF2_ITERATIONS = 600000;
  const TOKEN_BYTES = 32;
  const NONCE_BYTES = 12;
  const TAG_BYTES = 16;
  // Domain separator for the trigger-secret derivation. See triggerIdFor
  // / deriveTriggerSecret below for the protocol. Must match the Python
  // side's TRIGGER_INFO byte-for-byte.
  const TRIGGER_INFO = "termpilot:trigger:v1";

  const enc = new TextEncoder();
  const dec = new TextDecoder();

  // ---- byte helpers ------------------------------------------------------

  function hexToBytes(hex) {
    if (typeof hex !== "string") throw new Error("hex must be string");
    const h = hex.trim().toLowerCase();
    if (h.length !== TOKEN_BYTES * 2) throw new Error("hex length must be " + (TOKEN_BYTES * 2));
    if (!/^[0-9a-f]+$/.test(h)) throw new Error("hex contains non-hex chars");
    const out = new Uint8Array(h.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(h.slice(i * 2, i * 2 + 2), 16);
    return out;
  }
  function bytesToHex(b) {
    return Array.from(b).map(x => x.toString(16).padStart(2, "0")).join("");
  }
  function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  function bytesToB64(b) {
    let bin = "";
    for (let i = 0; i < b.length; i++) bin += String.fromCharCode(b[i]);
    return btoa(bin);
  }
  function concat(a, b) {
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }
  function bytesEqual(a, b) {
    if (a.length !== b.length) return false;
    let acc = 0;
    for (let i = 0; i < a.length; i++) acc |= a[i] ^ b[i];
    return acc === 0;
  }

  // ---- KDF ---------------------------------------------------------------

  /**
   * Derive a 32-byte token from a UTF-8 password. Deterministic.
   *
   * NOT USED BY PRODUCTION FLOWS — the wrapper generates a random
   * 32-byte token via os.urandom and the browser stores it as hex.
   * This function exists only so tests/test_crypto.html can validate
   * cross-language PBKDF2 vectors against the Python side. If you're
   * tempted to use it for a "password instead of a token" path, don't:
   * the SALT is a fixed string, so any two users picking the same
   * password share the same token across every install.
   */
  async function deriveToken(password) {
    if (typeof password !== "string" || password.length === 0)
      throw new Error("password must be a non-empty string");
    const salt = enc.encode(SALT_STR);
    const baseKey = await crypto.subtle.importKey(
      "raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]
    );
    const bits = await crypto.subtle.deriveBits(
      { name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
      baseKey, TOKEN_BYTES * 8
    );
    return new Uint8Array(bits);
  }

  // ---- AES-GCM -----------------------------------------------------------

  /** Import a 32-byte token as an AES-GCM CryptoKey for encrypt+decrypt. */
  async function importAesKey(tokenBytes) {
    if (!(tokenBytes instanceof Uint8Array) || tokenBytes.length !== TOKEN_BYTES)
      throw new Error("tokenBytes must be Uint8Array of " + TOKEN_BYTES);
    return crypto.subtle.importKey(
      "raw", tokenBytes, "AES-GCM", false, ["encrypt", "decrypt"]
    );
  }

  /**
   * Encrypt plaintext with AAD. Returns Uint8Array: nonce || ciphertext || tag.
   * @param {CryptoKey} key
   * @param {Uint8Array} plaintext
   * @param {string} aadStr
   */
  async function encrypt(key, plaintext, aadStr) {
    if (!(plaintext instanceof Uint8Array)) throw new Error("plaintext must be Uint8Array");
    const nonce = crypto.getRandomValues(new Uint8Array(NONCE_BYTES));
    const ct = new Uint8Array(await crypto.subtle.encrypt(
      { name: "AES-GCM", iv: nonce, additionalData: enc.encode(aadStr) },
      key, plaintext
    ));
    return concat(nonce, ct);
  }

  /**
   * Decrypt a record produced by `encrypt`. Throws on tag mismatch.
   * @param {CryptoKey} key
   * @param {Uint8Array} blob   nonce || ciphertext || tag
   * @param {string} aadStr
   */
  async function decrypt(key, blob, aadStr) {
    if (!(blob instanceof Uint8Array)) throw new Error("blob must be Uint8Array");
    if (blob.length < NONCE_BYTES + TAG_BYTES) throw new Error("blob too short");
    const nonce = blob.subarray(0, NONCE_BYTES);
    const ct = blob.subarray(NONCE_BYTES);
    const pt = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: nonce, additionalData: enc.encode(aadStr) },
      key, ct
    );
    return new Uint8Array(pt);
  }

  // Convenience: base64 over JSON.
  async function encryptB64(key, plaintext, aadStr) {
    return bytesToB64(await encrypt(key, plaintext, aadStr));
  }
  async function decryptB64(key, b64, aadStr) {
    return decrypt(key, b64ToBytes(b64), aadStr);
  }

  // ---- AAD constructors --------------------------------------------------

  function aadMarker(sid) { return "marker:v1:" + sid; }
  function aadMeta(sid) { return "meta:v1:" + sid; }
  function aadRecord(stream, sid, seq) {
    if (stream !== "out" && stream !== "in")
      throw new Error("unknown stream: " + stream);
    return stream + ":v1:" + sid + ":" + seq;
  }

  // ---- Trigger secret (matches lib/crypto.py:derive_trigger_secret) ---
  //
  // HMAC-SHA256(token, "termpilot:trigger:v1") → 32-byte trigger_secret.
  // SHA-256(trigger_secret) → 32-byte trigger_id (public verifier).
  //
  // The browser computes trigger_id at push_subscribe time so the relay
  // can store a public verifier without ever learning the device token.
  // The wrapper computes trigger_secret at op_close / op_push_notify
  // time to prove token possession.

  async function deriveTriggerSecret(tokenBytes) {
    if (!(tokenBytes instanceof Uint8Array) || tokenBytes.length !== TOKEN_BYTES)
      throw new Error("tokenBytes must be Uint8Array of " + TOKEN_BYTES);
    const k = await crypto.subtle.importKey(
      "raw", tokenBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
    );
    const sig = await crypto.subtle.sign("HMAC", k, enc.encode(TRIGGER_INFO));
    return new Uint8Array(sig);
  }

  async function triggerIdFor(tokenBytes) {
    const sec = await deriveTriggerSecret(tokenBytes);
    const h = await crypto.subtle.digest("SHA-256", sec);
    return new Uint8Array(h);
  }

  // ---- Public surface ----------------------------------------------------

  window.TP = {
    SALT_STR, PBKDF2_ITERATIONS, TOKEN_BYTES, NONCE_BYTES, TAG_BYTES,
    TRIGGER_INFO,
    hexToBytes, bytesToHex, b64ToBytes, bytesToB64, bytesEqual,
    deriveToken, importAesKey,
    encrypt, decrypt, encryptB64, decryptB64,
    aadMarker, aadMeta, aadRecord,
    deriveTriggerSecret, triggerIdFor,
  };
})();
