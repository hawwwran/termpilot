/**
 * Multi-token session enumeration + decryption helpers used by index.html
 * (the terminal view).
 *
 * Storage:
 *   localStorage.termpilot-secret    : Bearer token for the relay (HTTP gating)
 *   localStorage.termpilot-tokens    : JSON [{id, name, token_hex}, ...]
 *
 * Public API:
 *   TPSession.loadSecret() / saveSecret(s) / clearSecret()
 *   TPSession.loadTokens() / saveTokens(arr)
 *   TPSession.addToken(name, hex) / renameToken(id, name) / removeToken(id)
 *   TPSession.api(base, op, opts)   — fetch wrapper with Bearer auth
 *   TPSession.refreshSessionList(relayBase) → { groups, byTokenId }
 *   TPSession.attachSession(sid, tokenId) → cached AES key for that session
 *
 * Failed (sid, token) pairs are cached in-memory to avoid repeating
 * decryption attempts that already failed during this page lifetime.
 */
(function () {
  const SECRET_KEY = "termpilot-secret";
  const TOKENS_KEY = "termpilot-tokens";

  // --- secret (HTTP auth) ---
  function loadSecret() { return localStorage.getItem(SECRET_KEY) || ""; }
  function saveSecret(s) { if (s) localStorage.setItem(SECRET_KEY, s); else clearSecret(); }
  function clearSecret() { localStorage.removeItem(SECRET_KEY); }

  // --- tokens (encryption keys) ---
  function uuid() {
    // crypto.randomUUID is available on every browser this app supports
    // (Chromium 92+, Firefox 95+, Safari 15.4+). Refuse Math.random as a
    // fallback — a collision on a queue id silently drops the wrong entry.
    if (typeof crypto.randomUUID !== "function") {
      throw new Error("crypto.randomUUID required");
    }
    return crypto.randomUUID();
  }
  function loadTokens() {
    try {
      const v = JSON.parse(localStorage.getItem(TOKENS_KEY) || "[]");
      if (!Array.isArray(v)) return [];
      // Strict hex validation: a malformed token-hex would fail later
      // inside TP.importAesKey with an opaque "wrong length" message;
      // dropping it here keeps the failure mode obvious.
      return v.filter(t => t && typeof t.token_hex === "string"
                          && /^[0-9a-f]{64}$/i.test(t.token_hex));
    } catch (e) { return []; }
  }
  function saveTokens(arr) {
    localStorage.setItem(TOKENS_KEY, JSON.stringify(arr));
  }
  function addToken(name, tokenHex) {
    const arr = loadTokens();
    const id = uuid();
    arr.push({ id, name: String(name || ""), token_hex: tokenHex });
    saveTokens(arr);
    return id;
  }
  function renameToken(id, name) {
    const arr = loadTokens();
    for (const t of arr) if (t.id === id) t.name = String(name || "");
    saveTokens(arr);
  }
  function removeToken(id) {
    const arr = loadTokens().filter(t => t.id !== id);
    saveTokens(arr);
  }

  // --- Connection state -----------------------------------------------
  // Every api() call updates a pair of timestamps. getConnState() derives
  // online/reconnecting/offline from the gap between (lastSuccess, lastFail).
  // Listeners are notified after each api() and via a 2s tick.
  let lastSuccessTs = Date.now();
  let lastFailureTs = 0;
  const stateListeners = [];
  function getConnState() {
    const now = Date.now();
    // Offline if we haven't heard a success in 30s.
    if (now - lastSuccessTs > 30000) return "offline";
    // Reconnecting if a failure is more recent than success and within 30s.
    if (lastFailureTs > lastSuccessTs && now - lastFailureTs < 30000) return "reconnecting";
    return "online";
  }
  function _emitState() {
    const s = getConnState();
    for (const fn of stateListeners) { try { fn(s); } catch (e) {} }
  }
  function onConnState(fn) {
    stateListeners.push(fn);
    try { fn(getConnState()); } catch (e) {}
  }
  // Pause state ticks when the page is hidden — a backgrounded PWA has
  // no UI to refresh, and the tick was otherwise a 0.5 Hz battery drain.
  let _stateTimer = setInterval(_emitState, 2000);
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        if (_stateTimer != null) { clearInterval(_stateTimer); _stateTimer = null; }
      } else if (_stateTimer == null) {
        _stateTimer = setInterval(_emitState, 2000);
        _emitState();
      }
    });
  }

  // --- API ---
  async function api(base, op, opts = {}) {
    const url = new URL(base, location.href);
    url.searchParams.set("op", op);
    for (const [k, v] of Object.entries(opts.params || {})) url.searchParams.set(k, v);
    const init = {
      method: opts.method || "GET",
      headers: {},
      signal: opts.signal,
    };
    const sec = loadSecret();
    if (sec) init.headers["Authorization"] = "Bearer " + sec;
    if (opts.body) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    let r;
    try {
      r = await fetch(url, init);
    } catch (e) {
      // Network/abort errors. AbortError shouldn't move connection state
      // (it's a self-induced cancel during reattach/teardown); other errors
      // mean we genuinely failed to talk to the server.
      if (!opts.signal || !opts.signal.aborted) {
        lastFailureTs = Date.now();
        _emitState();
      }
      throw e;
    }
    // Any HTTP response (even 4xx/5xx) means the relay is reachable.
    lastSuccessTs = Date.now();
    _emitState();
    const text = await r.text();
    let parsed;
    try { parsed = JSON.parse(text); } catch (e) { parsed = { raw: text }; }
    return { status: r.status, body: parsed };
  }

  // Probe whether the relay requires a Bearer secret.
  // Cached for the page lifetime; the modal calls this before rendering.
  let _authReqCache = null;
  async function checkAuthRequired(base) {
    if (_authReqCache !== null) return _authReqCache;
    try {
      const { status, body } = await api(base, "auth_required");
      _authReqCache = (status === 200 && body && body.required === true);
    } catch (e) {
      _authReqCache = true; // fail-closed: assume required if we can't tell
    }
    return _authReqCache;
  }

  // --- Per-session key cache ---
  const sessionToKey = new Map();   // sid → CryptoKey
  const sessionToTokenId = new Map();  // sid → token id (for label lookup)
  const sessionToMeta = new Map();   // sid → decrypted meta object
  // LRU-capped "sid|tokenId" pairs we've already proven don't match.
  // Without a cap, a long-lived PWA against a busy relay accumulates
  // thousands of pairs (every historical sid × every token). Map gives
  // insertion-order iteration so we evict the oldest on overflow.
  const failed = new Map();
  const FAILED_CAP = 5000;
  function failedHas(k) { return failed.has(k); }
  function failedAdd(k) {
    if (failed.has(k)) { failed.delete(k); failed.set(k, 1); return; }
    failed.set(k, 1);
    if (failed.size > FAILED_CAP) {
      // Drop oldest 10% in one pass to amortise the cost.
      const drop = Math.floor(FAILED_CAP / 10);
      let i = 0;
      for (const key of failed.keys()) {
        if (i++ >= drop) break;
        failed.delete(key);
      }
    }
  }

  function attachSession(sid) {
    return sessionToKey.get(sid) || null;
  }
  function tokenIdForSession(sid) {
    return sessionToTokenId.get(sid) || null;
  }
  function metaForSession(sid) {
    return sessionToMeta.get(sid) || null;
  }
  function clearMatches() {
    sessionToKey.clear();
    sessionToTokenId.clear();
    sessionToMeta.clear();
    failed.clear();
  }

  /**
   * Try (sid, marker_b64) against every known token. On match, fetch+decrypt
   * meta and remember the binding. Returns the token id that matched, or null.
   */
  async function tryMatchSession(relayBase, sid, markerB64) {
    if (sessionToKey.has(sid)) return sessionToTokenId.get(sid);
    const tokens = loadTokens();
    for (const t of tokens) {
      const k = sid + "|" + t.id;
      if (failedHas(k)) continue;
      try {
        const tokenBytes = TP.hexToBytes(t.token_hex);
        const aesKey = await TP.importAesKey(tokenBytes);
        await TP.decryptB64(aesKey, markerB64, TP.aadMarker(sid));
        // Marker matched → fetch full meta and decrypt
        const metaResp = await api(relayBase, "meta", { params: { session: sid } });
        if (metaResp.status !== 200 || !metaResp.body.encrypted_meta) {
          failedAdd(k);
          continue;
        }
        const metaBytes = await TP.decryptB64(aesKey, metaResp.body.encrypted_meta, TP.aadMeta(sid));
        let meta = {};
        try { meta = JSON.parse(new TextDecoder().decode(metaBytes)); } catch (e) {}
        sessionToKey.set(sid, aesKey);
        sessionToTokenId.set(sid, t.id);
        sessionToMeta.set(sid, meta);
        return t.id;
      } catch (e) {
        failedAdd(k);
      }
    }
    return null;
  }

  /**
   * Refresh the global session list and group by matched token.
   * Returns:
   *   { groups: [{ token: {id,name}, sessions: [...] }, ...],
   *     orphans: [...]  // sessions that didn't match any token
   *   }
   */
  async function refreshSessionList(relayBase) {
    const tokens = loadTokens();
    const groups = tokens.map(t => ({ token: t, sessions: [] }));
    const orphans = [];
    const resp = await api(relayBase, "sessions");
    if (resp.status !== 200 || !resp.body.sessions) {
      return { groups, orphans, error: resp.body.error || ("status " + resp.status) };
    }
    for (const s of resp.body.sessions) {
      const tid = await tryMatchSession(relayBase, s.id, s.marker || "");
      const enriched = { ...s, meta: sessionToMeta.get(s.id) || {} };
      if (tid) {
        const g = groups.find(g => g.token.id === tid);
        if (g) g.sessions.push(enriched);
      } else {
        orphans.push(enriched);
      }
    }
    return { groups, orphans };
  }

  // --- Pending-sends queue --------------------------------------------
  // Keyed by sid. Each entry: { id, plain_b64, ts, aad: "in"|"input" }.
  // Persisted to localStorage so a page reload doesn't lose queued input.
  // The browser drains the queue once api() succeeds again.
  const PENDING_KEY = "termpilot-pending-sends";
  function _loadAllPending() {
    try {
      const v = JSON.parse(localStorage.getItem(PENDING_KEY) || "{}");
      return (v && typeof v === "object") ? v : {};
    } catch (e) { return {}; }
  }
  function _saveAllPending(map) {
    try { localStorage.setItem(PENDING_KEY, JSON.stringify(map)); }
    catch (e) {}
  }
  function pendingSendsFor(sid) {
    const all = _loadAllPending();
    return Array.isArray(all[sid]) ? all[sid] : [];
  }
  function enqueuePendingSend(sid, plain_b64) {
    const all = _loadAllPending();
    if (!Array.isArray(all[sid])) all[sid] = [];
    if (typeof crypto.randomUUID !== "function") {
      throw new Error("crypto.randomUUID required for queue id");
    }
    const id = crypto.randomUUID();
    all[sid].push({ id, plain_b64, ts: Date.now() });
    _saveAllPending(all);
    // Mirror to IDB and arm the sync tag so the browser drains us when
    // online (even if the page is closed). Best-effort: foreground drain
    // remains the primary path on browsers without SyncManager.
    idbPut("queue:" + sid, all[sid]);
    registerDrainSync(sid);
    return id;
  }
  function dropPendingSend(sid, id) {
    const all = _loadAllPending();
    if (!Array.isArray(all[sid])) return;
    all[sid] = all[sid].filter(e => e.id !== id);
    const remaining = all[sid];
    if (remaining.length === 0) delete all[sid];
    _saveAllPending(all);
    if (remaining.length === 0) idbDelete("queue:" + sid);
    else idbPut("queue:" + sid, remaining);
  }
  function clearPendingSends(sid) {
    const all = _loadAllPending();
    delete all[sid];
    _saveAllPending(all);
    idbDelete("queue:" + sid);
  }

  // --- IndexedDB key-value (so the SW can read state) -----------------
  // The SW can't see localStorage. We mirror the bits it needs (token,
  // relay secret/url, per-session queue) into IDB. Pages keep using
  // localStorage as the synchronous source of truth; IDB is the SW's
  // window into the same data.
  //
  // Schema: one DB `termpilot-app-state`, one store `kv` keyed by string.
  //   "tokens"        → [{id, name, token_hex}, …]
  //   "secret"        → string (relay Bearer)
  //   "relay_url"     → string (absolute URL of relay.php)
  //   "session:<sid>" → { sid, token_hex, secret, relay_url, next_seq }
  //   "queue:<sid>"   → [{ id, plain_b64, ts }, …]
  //
  // All operations are best-effort: IDB unavailable → silently no-op so
  // every caller can wrap with try/catch only at the outermost level.
  const IDB_DB = "termpilot-app-state";
  const IDB_STORE = "kv";
  const _idbSupported = typeof indexedDB !== "undefined";
  function _openIdb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(IDB_DB, 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(IDB_STORE)) {
          db.createObjectStore(IDB_STORE, { keyPath: "key" });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }
  async function idbPut(key, value) {
    if (!_idbSupported) return;
    try {
      const db = await _openIdb();
      await new Promise((res, rej) => {
        const tx = db.transaction(IDB_STORE, "readwrite");
        tx.objectStore(IDB_STORE).put({ key, value });
        tx.oncomplete = () => res();
        tx.onerror = () => rej(tx.error);
      });
      db.close();
    } catch (e) { /* swallow */ }
  }
  async function idbGet(key) {
    if (!_idbSupported) return null;
    try {
      const db = await _openIdb();
      const v = await new Promise((res, rej) => {
        const tx = db.transaction(IDB_STORE, "readonly");
        const r = tx.objectStore(IDB_STORE).get(key);
        r.onsuccess = () => res(r.result ? r.result.value : null);
        r.onerror = () => rej(r.error);
      });
      db.close();
      return v;
    } catch (e) { return null; }
  }
  async function idbDelete(key) {
    if (!_idbSupported) return;
    try {
      const db = await _openIdb();
      await new Promise((res, rej) => {
        const tx = db.transaction(IDB_STORE, "readwrite");
        tx.objectStore(IDB_STORE).delete(key);
        tx.oncomplete = () => res();
        tx.onerror = () => rej(tx.error);
      });
      db.close();
    } catch (e) {}
  }

  // --- Background-sync registration -----------------------------------
  // SyncManager is Chromium-only. On other browsers we no-op cleanly —
  // the existing foreground drainer handles everything.
  function bgSyncSupported() {
    return ("serviceWorker" in navigator) &&
           (typeof window !== "undefined") &&
           ("SyncManager" in window);
  }
  async function registerDrainSync(sid) {
    if (!bgSyncSupported()) return false;
    try {
      const reg = await navigator.serviceWorker.ready;
      if (!reg || !reg.sync) return false;
      await reg.sync.register("termpilot-drain-" + sid);
      return true;
    } catch (e) { return false; }
  }

  // --- Session attachment hooks ---------------------------------------
  // Called from the page when it attaches to / detaches from a session.
  // Persists the bits the SW needs to drain offline-queued sends:
  // token bytes (for AES-GCM), relay base URL, and bearer secret.
  async function attachSessionForSync(sid, opts) {
    if (!sid || !opts) return;
    const relayBase = opts.relayBase ||
      (typeof location !== "undefined" ? new URL("./relay.php", location.href).href : "");
    const session = {
      sid,
      token_hex: opts.tokenHex || null,
      secret: opts.secret != null ? opts.secret : loadSecret(),
      relay_url: relayBase,
      next_seq: typeof opts.nextSeq === "number" ? opts.nextSeq : 0,
    };
    await idbPut("session:" + sid, session);
    // Mirror the current pending queue so the SW sees what's already there.
    await idbPut("queue:" + sid, pendingSendsFor(sid));
    // Register the sync tag so the browser fires it next time online.
    registerDrainSync(sid);
  }
  async function detachSessionForSync(sid) {
    if (!sid) return;
    await idbDelete("session:" + sid);
    // Leave the queue intact in case it has un-drained entries — the
    // sync tag will still fire when online and the SW handler reads
    // the queue. Once the queue is empty the SW removes that key too.
  }
  async function updateSessionNextSeq(sid, nextSeq) {
    if (!sid) return;
    const cur = await idbGet("session:" + sid);
    if (cur) {
      cur.next_seq = nextSeq;
      await idbPut("session:" + sid, cur);
    }
  }

  // --- Push notifications ---------------------------------------------
  // The browser holds at most ONE PushSubscription per origin/SW. When push
  // is enabled, that single subscription is registered with the relay under
  // every locally-known token's SHA-256 hash; when any wrapper triggers
  // push_notify for one of those token hashes, the relay forwards a content-
  // free push to this browser's SW.
  const PUSH_KEY = "termpilot-push";
  function _loadPush() {
    try {
      const v = JSON.parse(localStorage.getItem(PUSH_KEY) || "null");
      return (v && typeof v === "object") ? v : null;
    } catch (e) { return null; }
  }
  function _savePush(v) {
    if (v) localStorage.setItem(PUSH_KEY, JSON.stringify(v));
    else localStorage.removeItem(PUSH_KEY);
  }
  function pushIsSupported() {
    return ("serviceWorker" in navigator) &&
           ("PushManager" in window) &&
           ("Notification" in window);
  }
  function pushPermission() {
    return ("Notification" in window) ? Notification.permission : "default";
  }
  async function tokenHashHex(tokenHex) {
    const bytes = TP.hexToBytes(tokenHex);
    const buf = await crypto.subtle.digest("SHA-256", bytes);
    return TP.bytesToHex(new Uint8Array(buf));
  }
  // Derive the public trigger_id (hex) for this token. Matches the
  // Python crypto.trigger_id_for; the relay stores it as the
  // per-token-hash verifier so op_push_notify can require proof of
  // token possession from the wrapper.
  async function triggerIdHex(tokenHex) {
    const bytes = TP.hexToBytes(tokenHex);
    const tid = await TP.triggerIdFor(bytes);
    return TP.bytesToHex(tid);
  }
  // Derive the matching secret (hex). Used by deleteOfflineSession in
  // index.html to authenticate op_close — only callers that hold the
  // device token can produce a valid secret.
  async function triggerSecretHex(tokenHex) {
    const bytes = TP.hexToBytes(tokenHex);
    const sec = await TP.deriveTriggerSecret(bytes);
    return TP.bytesToHex(sec);
  }
  function _b64urlFromArrayBuffer(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function _b64urlToBytes(b64u) {
    const pad = (4 - (b64u.length % 4)) % 4;
    const b64 = (b64u + "=".repeat(pad)).replace(/-/g, "+").replace(/_/g, "/");
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  function _subscriptionPayload(sub, tokenHashHex, triggerIdHexStr) {
    const j = sub.toJSON();
    return {
      token_hash: tokenHashHex,
      trigger_id_hex: triggerIdHexStr,
      endpoint: j.endpoint,
      keys: { p256dh: j.keys.p256dh, auth: j.keys.auth },
    };
  }
  async function _ensurePushSubscription(relayBase) {
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (sub) return sub;
    const r = await api(relayBase, "push_pubkey");
    if (r.status !== 200 || !r.body.public_b64u) {
      throw new Error("relay push_pubkey unavailable");
    }
    const appKey = _b64urlToBytes(r.body.public_b64u);
    // Sanity-check the relay's VAPID public key before handing it to
    // pushManager.subscribe. P-256 uncompressed points are exactly
    // 65 bytes with leading 0x04; anything else is the relay either
    // serving a malformed key or being MITM'd into one we wouldn't
    // sign verifiably.
    if (appKey.length !== 65 || appKey[0] !== 0x04) {
      throw new Error("relay returned malformed VAPID public key");
    }
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: appKey,
    });
    return sub;
  }
  /**
   * Turn push on for this browser. Asks for notification permission, creates
   * (or reuses) a push subscription, and registers it with the relay under
   * every locally-stored token's hash.
   */
  async function enablePush(relayBase) {
    if (!pushIsSupported()) throw new Error("push not supported in this browser");
    if (Notification.permission === "denied") throw new Error("notifications blocked");
    if (Notification.permission !== "granted") {
      const p = await Notification.requestPermission();
      if (p !== "granted") throw new Error("permission " + p);
    }
    const sub = await _ensurePushSubscription(relayBase);
    const tokens = loadTokens();
    const registered = [];
    for (const t of tokens) {
      const th = await tokenHashHex(t.token_hex);
      const tid = await triggerIdHex(t.token_hex);
      const payload = _subscriptionPayload(sub, th, tid);
      const r = await api(relayBase, "push_subscribe", { method: "POST", body: payload });
      if (r.status === 200 && r.body.id) {
        registered.push({ token_id: t.id, token_hash: th, sub_id: r.body.id });
      }
    }
    _savePush({ enabled: true, registered, ts: Date.now() });
    return registered.length;
  }
  /** Re-register the existing subscription for any tokens added since the
   *  last enable. Quietly no-ops if push is disabled or unsupported. */
  async function syncPushTokens(relayBase) {
    const state = _loadPush();
    if (!state || !state.enabled) return;
    if (!pushIsSupported()) return;
    if (Notification.permission !== "granted") return;
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) { _savePush(null); return; }
    const tokens = loadTokens();
    const have = new Set((state.registered || []).map(e => e.token_id));
    const out = (state.registered || []).filter(e => tokens.find(t => t.id === e.token_id));
    for (const t of tokens) {
      if (have.has(t.id)) continue;
      const th = await tokenHashHex(t.token_hex);
      const tid = await triggerIdHex(t.token_hex);
      const payload = _subscriptionPayload(sub, th, tid);
      const r = await api(relayBase, "push_subscribe", { method: "POST", body: payload });
      if (r.status === 200 && r.body.id) {
        out.push({ token_id: t.id, token_hash: th, sub_id: r.body.id });
      }
    }
    _savePush({ enabled: true, registered: out, ts: Date.now() });
  }
  /** Turn push off: unregister from the relay for every token, then drop
   *  the local subscription so the browser stops receiving pushes.
   *  trigger_secret_hex (HMAC of the device token) proves token possession
   *  — without it the relay refuses since op_push_unsubscribe is gated to
   *  prevent a RELAY_SECRET-holder from kicking other users off push. */
  async function disablePush(relayBase) {
    const state = _loadPush();
    if (state && Array.isArray(state.registered)) {
      const tokens = loadTokens();
      for (const e of state.registered) {
        try {
          const body = { token_hash: e.token_hash, id: e.sub_id };
          const tok = tokens.find(t => t.id === e.token_id);
          if (tok) {
            // If the matching token is still local, prove possession.
            // If it was deleted, omit — the relay rejects with 401 but
            // we still wipe local state below (best-effort).
            body.trigger_secret_hex = await triggerSecretHex(tok.token_hex);
          }
          await api(relayBase, "push_unsubscribe", { method: "POST", body });
        } catch (err) { /* best-effort */ }
      }
    }
    _savePush(null);
    if (!pushIsSupported()) return;
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) await sub.unsubscribe();
    } catch (e) { /* ignore */ }
  }
  function pushState() { return _loadPush(); }

  // --- Per-session UI state (lazy-load cursor etc) --------------------
  // Kept tiny; we only persist what's expensive to recompute on reload.
  const SESSION_STATE_KEY = "termpilot-session-state";
  function _loadAllSessionState() {
    try {
      const v = JSON.parse(localStorage.getItem(SESSION_STATE_KEY) || "{}");
      return (v && typeof v === "object") ? v : {};
    } catch (e) { return {}; }
  }
  function _saveAllSessionState(map) {
    try { localStorage.setItem(SESSION_STATE_KEY, JSON.stringify(map)); }
    catch (e) {}
  }
  function loadSessionState(sid) {
    const all = _loadAllSessionState();
    return (all[sid] && typeof all[sid] === "object") ? all[sid] : {};
  }
  function saveSessionState(sid, patch) {
    const all = _loadAllSessionState();
    const cur = (all[sid] && typeof all[sid] === "object") ? all[sid] : {};
    all[sid] = Object.assign(cur, patch);
    _saveAllSessionState(all);
  }

  window.TPSession = {
    loadSecret, saveSecret, clearSecret,
    loadTokens, saveTokens, addToken, renameToken, removeToken,
    api, checkAuthRequired,
    attachSession, tokenIdForSession, metaForSession, clearMatches,
    tryMatchSession, refreshSessionList,
    onConnState, getConnState,
    pendingSendsFor, enqueuePendingSend, dropPendingSend, clearPendingSends,
    loadSessionState, saveSessionState,
    pushIsSupported, pushPermission, pushState,
    enablePush, disablePush, syncPushTokens, tokenHashHex,
    triggerIdHex, triggerSecretHex,
    bgSyncSupported, registerDrainSync,
    attachSessionForSync, detachSessionForSync, updateSessionNextSeq,
  };
})();
