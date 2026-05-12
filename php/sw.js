/**
 * termpilot service worker.
 *
 * Goals:
 *   1. Make the app installable (PWA) — that requires *any* registered
 *      service worker plus a manifest.
 *   2. Serve the static shell from cache so launching the installed app
 *      while offline still gives you the UI (which then handles its own
 *      "session offline" state via the connection-state badge).
 *
 * Crucially, we NEVER intercept relay.php traffic. Those endpoints are
 * long-poll request/response with strict ordering; a stale cached
 * response would corrupt the seq protocol. They go straight to the
 * network on every call.
 *
 * Cache strategy:
 *   - Shell assets (HTML, JS, CSS, icons, xterm CDN) → cache-first,
 *     update lazily after a successful network fetch.
 *   - Anything else (notably the relay API) → ignored by the SW; the
 *     browser handles it as if no SW were registered.
 */

// Version is injected at deploy time by tools/deploy.sh (sed-replaces the
// placeholder before upload). In a working tree the literal placeholder is
// fine — tests don't exercise the SW, and any local server still gets a
// stable cache name.
const VERSION = "__TERMPILOT_VERSION__";
const CACHE_NAME = "termpilot-shell-" + VERSION;

const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png",
  "./icon-maskable-192.png",
  "./icon-maskable-512.png",
  "./lib/crypto.js",
  "./lib/session.js",
  "./lib/index.css",
  "./lib/index.js",
  "./lib/vendor/xterm.min.css",
  "./lib/vendor/xterm.min.js",
  "./lib/vendor/jsQR.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    // Use addAll-with-fallback: a single 404 (e.g. asset rename) shouldn't
    // abort the whole install. Cache what we can; offline-launch will
    // miss whatever failed but the install still succeeds.
    //
    // Since everything in SHELL is now same-origin (vendored locally,
    // see php/lib/vendor/), we refuse to cache `type === "opaque"`
    // responses — those would only ever come from a cross-origin
    // redirect, which is a tamper indicator now that no shell asset
    // legitimately lives off-origin.
    await Promise.all(SHELL.map(async (url) => {
      try {
        const resp = await fetch(url, { cache: "reload" });
        if (resp.ok && resp.type === "basic") await cache.put(url, resp);
      } catch (e) {
        // Best-effort precache.
      }
    }));
    // Don't skipWaiting() unconditionally — we want updates to sit in the
    // "waiting" state so the page can show its UPDATE banner. The page
    // sends { type: "SKIP_WAITING" } when the user clicks UPDATE.
  })());
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

// Web Push: deliberately content-free. The relay sends an empty body, so
// every notification looks the same — the user opens the app to see what
// triggered it. This keeps push payloads outside the E2E-encrypted record
// stream. v1 of the wrapper does not fire any push triggers itself; the
// listener stays wired so future triggers (e.g. terminal bell, exit
// status) can drop in without a service-worker upgrade.
self.addEventListener("push", (event) => {
  event.waitUntil(self.registration.showNotification("termpilot", {
    body: "Your terminal needs attention.",
    icon: "./icon-192.png",
    badge: "./icon-192.png",
    tag: "termpilot-notify",
    renotify: true,
    requireInteraction: false,
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    // Match the manifest default: open the terminal view. If the user
    // already has the app open (any view), just focus it — don't yank
    // them between chat and terminal mid-session.
    const newUrl = new URL("./index.html", self.registration.scope).href;
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of all) {
      try {
        if (new URL(client.url).origin === self.location.origin) {
          return client.focus();
        }
      } catch (e) { /* skip malformed urls */ }
    }
    if (self.clients.openWindow) await self.clients.openWindow(newUrl);
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

// ============================================================================
//  Background Sync — drain offline-queued sends from a closed app.
// ============================================================================
// Chromium-only. Other browsers don't fire `sync` events and the page-side
// foreground drainer remains the only path. Tag format: `termpilot-drain-<sid>`,
// registered by the page when it attaches to a session.
//
// State lives in IndexedDB (see session.js for the writer side):
//   "session:<sid>" → { sid, token_hex, secret, relay_url, next_seq }
//   "queue:<sid>"   → [{ id, plain_b64, ts }, …]

const _IDB_DB = "termpilot-app-state";
const _IDB_STORE = "kv";

function _swOpenIdb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(_IDB_DB, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(_IDB_STORE)) {
        db.createObjectStore(_IDB_STORE, { keyPath: "key" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function _swIdbGet(key) {
  const db = await _swOpenIdb();
  try {
    return await new Promise((res, rej) => {
      const tx = db.transaction(_IDB_STORE, "readonly");
      const r = tx.objectStore(_IDB_STORE).get(key);
      r.onsuccess = () => res(r.result ? r.result.value : null);
      r.onerror = () => rej(r.error);
    });
  } finally { db.close(); }
}
async function _swIdbPut(key, value) {
  const db = await _swOpenIdb();
  try {
    await new Promise((res, rej) => {
      const tx = db.transaction(_IDB_STORE, "readwrite");
      tx.objectStore(_IDB_STORE).put({ key, value });
      tx.oncomplete = () => res();
      tx.onerror = () => rej(tx.error);
    });
  } finally { db.close(); }
}
async function _swIdbDelete(key) {
  const db = await _swOpenIdb();
  try {
    await new Promise((res, rej) => {
      const tx = db.transaction(_IDB_STORE, "readwrite");
      tx.objectStore(_IDB_STORE).delete(key);
      tx.oncomplete = () => res();
      tx.onerror = () => rej(tx.error);
    });
  } finally { db.close(); }
}

function _hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  return bytes;
}
function _b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < out.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
function _bytesToB64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

async function _swEncryptInputBlob(tokenHex, sid, seq, plain) {
  const key = await crypto.subtle.importKey(
    "raw", _hexToBytes(tokenHex), "AES-GCM", false, ["encrypt"]);
  const aad = new TextEncoder().encode("in:v1:" + sid + ":" + seq);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce, additionalData: aad }, key, plain);
  const wire = new Uint8Array(12 + ct.byteLength);
  wire.set(nonce, 0);
  wire.set(new Uint8Array(ct), 12);
  return _bytesToB64(wire);
}

async function _swDrainSession(sid) {
  const sess = await _swIdbGet("session:" + sid);
  let queue = (await _swIdbGet("queue:" + sid)) || [];
  if (!sess || !sess.token_hex || !sess.relay_url) return;
  if (queue.length === 0) return;

  let nextSeq = (typeof sess.next_seq === "number") ? sess.next_seq : 0;
  let drained = 0;

  // Drain in order. On any 4xx/5xx that isn't 409, abort — we'll be retried
  // on the next sync event. Background Sync will re-fire on connectivity.
  while (queue.length > 0) {
    const entry = queue[0];
    let plain;
    try { plain = _b64ToBytes(entry.plain_b64); }
    catch (e) { queue.shift(); continue; }  // malformed entry, drop

    let blob;
    try {
      blob = await _swEncryptInputBlob(sess.token_hex, sid, nextSeq, plain);
    } catch (e) { break; }  // crypto error → leave queue, give up

    const url = sess.relay_url + (sess.relay_url.includes("?") ? "&" : "?") + "op=input";
    const headers = { "Content-Type": "application/json" };
    if (sess.secret) headers["Authorization"] = "Bearer " + sess.secret;
    let resp;
    try {
      resp = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify({ session_id: sid, records: [{ seq: nextSeq, blob }] }),
      });
    } catch (e) { break; }  // network failure → leave queue for next sync

    if (resp.status === 200) {
      queue.shift();
      drained++;
      nextSeq++;
      await _swIdbPut("queue:" + sid, queue);
      await _swIdbPut("session:" + sid, Object.assign({}, sess, { next_seq: nextSeq }));
    } else if (resp.status === 409) {
      let body = null;
      try { body = await resp.json(); } catch (e) {}
      if (body && typeof body.expected_seq === "number" && body.expected_seq > nextSeq) {
        nextSeq = body.expected_seq;
        await _swIdbPut("session:" + sid, Object.assign({}, sess, { next_seq: nextSeq }));
        continue;  // retry the same entry with the new seq
      }
      break;
    } else {
      break;  // 4xx/5xx — leave for next sync
    }
  }

  if (queue.length === 0) {
    await _swIdbDelete("queue:" + sid);
  }
  // Quiet-good: no notification for successful drain. The user discovers the
  // bubbles are gone next time they open the app. (Could surface a banner
  // later if it turns out users want explicit feedback.)
  return drained;
}

self.addEventListener("sync", (event) => {
  if (!event.tag || !event.tag.startsWith("termpilot-drain-")) return;
  const sid = event.tag.slice("termpilot-drain-".length);
  event.waitUntil(_swDrainSession(sid).catch(() => { /* swallow */ }));
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;  // never touch POSTs to the relay
  const url = new URL(req.url);

  // Pass-through for the API surface — these MUST hit the network.
  // Long-poll responses are time-bound; a cached response would freeze
  // the UI. The seq protocol breaks if served stale.
  if (url.pathname.endsWith("/relay.php") ||
      url.pathname.endsWith("relay.php")) {
    return;  // default browser behaviour
  }

  // Cache-first for the shell. After a hit we still kick off a background
  // refresh so the cached copy doesn't go stale forever.
  //
  // Everything in the shell is now same-origin. We refuse to cache
  // `type === "opaque"` responses — those would only ever come from a
  // cross-origin redirect, which is a tamper indicator (and was the
  // mechanism that let a poisoned CDN response get pinned forever in
  // the prior version of this worker).
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(req);
    if (cached) {
      // Background refresh; ignore failure.
      event.waitUntil((async () => {
        try {
          const fresh = await fetch(req);
          if (fresh && fresh.ok && fresh.type === "basic") {
            await cache.put(req, fresh);
          }
        } catch (e) { /* offline; keep using cached */ }
      })());
      return cached;
    }
    // Not in cache — fetch and store (best-effort) for next time.
    try {
      const fresh = await fetch(req);
      if (fresh && fresh.ok && fresh.type === "basic") {
        cache.put(req, fresh.clone()).catch(() => {});
      }
      return fresh;
    } catch (e) {
      // True offline + nothing cached. Return a synthetic 504 so the
      // page can show its connection-offline state cleanly.
      return new Response(JSON.stringify({ error: "offline" }),
        { status: 504, headers: { "Content-Type": "application/json" } });
    }
  })());
});
