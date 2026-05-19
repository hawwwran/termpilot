const RELAY = "./relay.php";
let authRequired = true;  // pessimistic until first probe completes

/* ===== Layout state machine =========================================== */
const COLLAPSE_KEY = "termpilot-aside-collapsed";
const VIEW_KEY = "termpilot-mobile-view";
const mobileMQ = window.matchMedia("(max-width: 768px), (pointer: coarse)");
const isMobile = () => mobileMQ.matches;
const mainEl = document.querySelector("main");
const toggleAsideBtn = document.getElementById("toggle-aside");
const backBtn = document.getElementById("back-to-list");

function applyLayout() {
  if (isMobile()) {
    const view = localStorage.getItem(VIEW_KEY) || "list";
    mainEl.classList.toggle("view-list", view === "list");
    mainEl.classList.toggle("view-session", view === "session");
    mainEl.classList.remove("collapsed");
  } else {
    const collapsed = localStorage.getItem(COLLAPSE_KEY) !== "0";
    mainEl.classList.toggle("collapsed", collapsed);
    mainEl.classList.remove("view-list", "view-session");
  }
}
toggleAsideBtn.addEventListener("click", () => {
  const willCollapse = !mainEl.classList.contains("collapsed");
  localStorage.setItem(COLLAPSE_KEY, willCollapse ? "1" : "0");
  applyLayout();
});
backBtn.addEventListener("click", () => {
  // If the active session is gone from the relay (wrapper closed), going
  // "back" should detach so the empty-state hero takes over when there's
  // nothing else to pick. Otherwise the user lands on the unstyled sidebar
  // empty-mini. updateEmptyHero auto-routes mobile back to the hero view.
  if (sessionMissing) {
    detach("session ended");
    refreshSessions();
    return;
  }
  localStorage.setItem(VIEW_KEY, "list");
  applyLayout();
});
mobileMQ.addEventListener("change", applyLayout);
applyLayout();

/* ===== Theme ===========================================================
 * The relay app renders the terminal as a <pre id="log"> with synthetic
 * <span style="…"> cells driven by the ANSI16 array (declared further
 * down). Theming therefore needs to do three things on switch:
 *
 *   1) Rewrite ANSI16's contents in place — cellStyle() reads each
 *      colour by index per-render, so a future scheduleLogRender() will
 *      pick up the new palette automatically.
 *   2) Write per-theme CSS custom properties on :root so the rest of
 *      the chrome (defined in index.css using var(--tp-…)) re-tints.
 *   3) Update document.documentElement.style.colorScheme so the browser
 *      renders scrollbars / native form controls in matching mode.
 *
 * Defaults: with no stored preference the picker follows the OS
 * prefers-color-scheme. Manual selections win and persist. Theme data
 * (window.TPThemes) is loaded by lib/themes.js. If that file is
 * missing for any reason, this module degrades to a no-op and the
 * CSS-var defaults declared in index.css render the app as before.
 */
const THEME_KEY = "termpilot-theme";

function _hexToRgb(s) {
  const h = (s || "").replace("#", "");
  const n = parseInt(h.length === 3
    ? h.split("").map(c => c + c).join("")
    : h, 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}
function _rgbToHex(r, g, b) {
  const c = v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0");
  return "#" + c(r) + c(g) + c(b);
}
// Linear sRGB interpolation. `ratio` is the share of `b`; 0 returns
// `a`, 1 returns `b`. Used to derive chrome surfaces and borders from
// the theme's bg/fg/palette without depending on color-mix() (which
// only ships in Safari ≥ 16.4).
function _mix(a, b, ratio) {
  const A = _hexToRgb(a), B = _hexToRgb(b);
  return _rgbToHex(
    A.r + (B.r - A.r) * ratio,
    A.g + (B.g - A.g) * ratio,
    A.b + (B.b - A.b) * ratio,
  );
}
function _palOr(theme, i, fallback) {
  return (theme.palette && theme.palette[i]) || fallback;
}

function loadThemePref() {
  try { return localStorage.getItem(THEME_KEY) || "system"; }
  catch (e) { return "system"; }
}
function saveThemePref(v) {
  try {
    if (!v || v === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, v);
  } catch (e) {}
}
function findTheme(id) {
  const T = (window.TPThemes && window.TPThemes.THEMES) || [];
  return T.find(t => t.id === id) || null;
}
function resolveTheme(pref) {
  const TP = window.TPThemes;
  if (!TP) return null;
  if (pref === "system" || !pref) {
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    return findTheme(dark ? TP.DEFAULT_DARK_ID : TP.DEFAULT_LIGHT_ID)
        || (TP.THEMES && TP.THEMES[0]) || null;
  }
  return findTheme(pref) || findTheme(TP.DEFAULT_DARK_ID)
      || (TP.THEMES && TP.THEMES[0]) || null;
}

function applyTheme(theme) {
  if (!theme) return;  // no themes bundle, fall back to CSS defaults
  const root = document.documentElement;
  const bg = theme.bg, fg = theme.fg, p = theme.palette || [];

  // Bright ANSI variants (palette 8–15) for semantic accents, with the
  // normal variants (1–6) as fallback if a theme has unusual palette
  // shape. ANSI: 1=red 2=green 3=yellow 4=blue 9=br.red 10=br.green
  //              11=br.yellow 12=br.blue.
  const accent  = _palOr(theme, 12, _palOr(theme, 4, "#a8c0ff"));
  const accentS = _palOr(theme, 4,  _palOr(theme, 12, "#3d5afe"));
  const success = _palOr(theme, 10, _palOr(theme, 2, "#3ed27a"));
  const warn    = _palOr(theme, 11, _palOr(theme, 3, "#f5a524"));
  const error   = _palOr(theme, 9,  _palOr(theme, 1, "#e5484d"));

  const set = (k, v) => root.style.setProperty(k, v);

  // Surfaces
  set("--tp-bg", bg);
  set("--tp-bg-elev",         _mix(bg, fg, 0.06));
  set("--tp-bg-elev2",        _mix(bg, fg, 0.03));
  set("--tp-bg-sunken",       _mix(bg, fg, 0.10));
  set("--tp-bg-selected",     _mix(bg, accent, 0.18));
  set("--tp-bg-selected-strong", _mix(bg, accent, 0.32));
  set("--tp-bg-hover",        _mix(bg, fg, 0.13));
  set("--tp-bg-hover2",       _mix(bg, fg, 0.08));
  set("--tp-bg-key",          _mix(bg, fg, 0.05));

  // Text tiers
  set("--tp-fg", fg);
  set("--tp-fg-muted",   _mix(fg, bg, 0.30));
  set("--tp-fg-meta",    _mix(fg, bg, 0.40));
  set("--tp-fg-dim",     _mix(accent, bg, 0.30));
  set("--tp-fg-faint",   _mix(fg, bg, 0.55));
  set("--tp-fg-empty",   _mix(fg, bg, 0.50));
  set("--tp-fg-aside-sub", _mix(fg, bg, 0.50));

  // Borders
  set("--tp-border",        _mix(bg, fg, 0.10));
  set("--tp-border-strong", _mix(bg, fg, 0.20));
  set("--tp-border-soft",   _mix(bg, fg, 0.06));

  // Accent family
  set("--tp-accent", accent);
  set("--tp-accent-strong", accentS);
  set("--tp-accent-fg", _mix(fg, accent, 0.25));
  set("--tp-accent-meta", _mix(accent, fg, 0.30));

  // Semantic. The warn pills/badges (.kbd-latched-pill, .sb-offline-tag,
  // .stale-pill) render warn text on a warn-bg background, so warn-bg
  // must contrast with BOTH the page bg AND the warn text. On dark
  // themes mixing bg toward warn gives a dark brown — high contrast. On
  // light themes the same formula produces a near-white wash (e.g.
  // github + yellow → pale yellow on white) with poor contrast against
  // both the page and the warn text. For light themes mix warn toward
  // fg instead, yielding a darker amber that reads against both.
  // warn-bg-soft stays as a faint warn wash either way (used as a
  // large-area background where saturation, not contrast, is the goal).
  set("--tp-success", success);
  set("--tp-warn", warn);
  if (theme.isLight) {
    set("--tp-warn-bg",      _mix(warn, fg, 0.30));
    set("--tp-warn-bg-soft", _mix(bg, warn, 0.18));
    set("--tp-warn-border",  _mix(warn, fg, 0.20));
    set("--tp-warn-hover",   _mix(warn, fg, 0.40));
  } else {
    set("--tp-warn-bg",      _mix(bg, warn, 0.22));
    set("--tp-warn-bg-soft", _mix(bg, warn, 0.08));
    set("--tp-warn-border",  _mix(bg, warn, 0.45));
    set("--tp-warn-hover",   _mix(bg, warn, 0.30));
  }
  set("--tp-error", error);

  set("--tp-netq-slow", warn);
  set("--tp-netq-severe", _mix(warn, error, 0.50));

  set("--tp-update-bg",        _mix(bg, warn, 0.65));
  set("--tp-update-fg",        bg);
  set("--tp-update-btn-hover", _mix(bg, fg, 0.10));

  root.style.colorScheme = theme.isLight ? "light" : "dark";

  // Mutate the ANSI16 array contents in place. The const at line ~534
  // binds the *array reference* — replacing its contents is legal, and
  // cellStyle() reads ANSI16[n] per call so the next render picks up
  // the new colours automatically. The try/catch is because applyTheme
  // also runs at module load, before ANSI16 / scheduleLogRender are
  // declared (Temporal Dead Zone); the post-declaration callsite below
  // back-fills the first boot.
  try {
    if (p.length >= 16) {
      ANSI16.length = 0;
      for (let i = 0; i < 16; i++) ANSI16.push(p[i]);
    }
    scheduleLogRender();
  } catch (e) {}
}

// Boot: resolve and apply before the placeholder term is created
// (further down in this file), so first paint shows the right palette.
applyTheme(resolveTheme(loadThemePref()));

// Re-apply when the OS pref flips, but only if the user is in
// "system" mode — a manual selection wins permanently.
try {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener(
    "change",
    () => { if (loadThemePref() === "system") applyTheme(resolveTheme("system")); },
  );
} catch (e) {
  // Older Safari uses .addListener; not worth polyfilling for a nice-to-have.
}

/* ===== Token UI ======================================================= */
function escapeHtml(s) { return String(s).replace(/[&<>"'/]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;","/":"&#x2F;"})[c]); }

/* ===== QR scanner =====================================================
 * Lazy-loads jsQR (precached by the service worker so it works offline
 * after first install) and decodes 64-char hex tokens from the rear
 * camera. The scan button next to the hex input is only revealed when
 * the page is on a secure origin and the MediaDevices API exists.
 */
function qrScannerAvailable() {
  return !!(window.isSecureContext
    && navigator.mediaDevices
    && typeof navigator.mediaDevices.getUserMedia === "function");
}

let _jsqrPromise = null;
function _loadJsQR() {
  if (typeof window.jsQR === "function") return Promise.resolve(window.jsQR);
  if (_jsqrPromise) return _jsqrPromise;
  _jsqrPromise = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "./lib/vendor/jsQR.js";
    s.async = true;
    s.onload = () => {
      if (typeof window.jsQR === "function") resolve(window.jsQR);
      else reject(new Error("jsQR did not register"));
    };
    s.onerror = () => { _jsqrPromise = null; reject(new Error("failed to load jsQR")); };
    document.head.appendChild(s);
  });
  return _jsqrPromise;
}

function openQrScanner({ onResult }) {
  if (document.querySelector(".qr-scanner-backdrop")) return;
  const back = document.createElement("div");
  back.className = "modal-backdrop qr-scanner-backdrop";
  back.innerHTML = `
    <div class="modal qr-modal" role="dialog" aria-modal="true" aria-label="QR token scanner">
      <h2>Scan token</h2>
      <p>Point your camera at the QR printed by <code>termpilot --show-token</code>.</p>
      <div class="qr-stage">
        <video id="qr-video" playsinline muted autoplay></video>
        <div class="qr-frame" aria-hidden="true"></div>
      </div>
      <p class="qr-hint" id="qr-hint">Starting camera…</p>
      <div class="row actions">
        <button type="button" id="qr-cancel">cancel</button>
      </div>
    </div>`;
  document.body.appendChild(back);

  const video = back.querySelector("#qr-video");
  const hint = back.querySelector("#qr-hint");
  const cancelBtn = back.querySelector("#qr-cancel");
  let stream = null;
  let stopped = false;
  let rafId = null;
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });

  function cleanup() {
    stopped = true;
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    if (back.parentNode) back.remove();
  }
  function fail(msg) {
    hint.textContent = msg;
    hint.classList.add("error");
  }
  cancelBtn.addEventListener("click", cleanup);
  back.addEventListener("click", (e) => { if (e.target === back) cleanup(); });

  (async () => {
    let jsQR;
    try {
      jsQR = await _loadJsQR();
    } catch (e) {
      fail("Couldn't load the QR decoder. Check your connection and try again.");
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      });
    } catch (e) {
      const denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
      fail(denied
        ? "Camera access was blocked. Enable it in your browser's site settings, then try again."
        : "Couldn't open the camera: " + (e && e.message ? e.message : e));
      return;
    }
    if (stopped) { stream.getTracks().forEach(t => t.stop()); return; }
    video.srcObject = stream;
    try { await video.play(); } catch (e) { /* autoplay quirks, frames still flow */ }
    hint.textContent = "Looking for a QR code…";

    function tick() {
      if (stopped) return;
      if (video.readyState >= 2 && video.videoWidth > 0) {
        // Use the camera's native resolution. jsQR scales linearly with
        // pixel count, but on a phone 1280x720 → ~3 ms/frame and the QR
        // often fills only a fraction of the frame; we can't afford the
        // resolution loss from aggressive downsampling.
        const w = video.videoWidth;
        const h = video.videoHeight;
        if (canvas.width !== w) canvas.width = w;
        if (canvas.height !== h) canvas.height = h;
        ctx.drawImage(video, 0, 0, w, h);
        const img = ctx.getImageData(0, 0, w, h);
        const code = jsQR(img.data, w, h, { inversionAttempts: "attemptBoth" });
        if (code && code.data) {
          const hex = code.data.trim().toLowerCase();
          if (/^[0-9a-f]{64}$/.test(hex)) {
            try { onResult && onResult(hex); } finally { cleanup(); }
            return;
          }
          hint.textContent = "Decoded a QR, but it isn't a 64-char hex token. Keep scanning…";
        }
      }
      rafId = requestAnimationFrame(tick);
    }
    rafId = requestAnimationFrame(tick);
  })();
}

function showLoginIfNeeded() {
  const hasSecret = !!TPSession.loadSecret();
  const tokens = TPSession.loadTokens();
  const secretMissing = authRequired && !hasSecret;
  if (secretMissing || tokens.length === 0) {
    openTokenModal({ initial: secretMissing || tokens.length === 0 });
    return true;
  }
  return false;
}

function openTokenModal(opts = {}) {
  if (document.querySelector(".modal-backdrop")) return;
  const initial = !!opts.initial;
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  const tokens = TPSession.loadTokens();
  const tokensHtml = tokens.length === 0
    ? '<p style="color:#777;font-style:italic">No tokens yet.</p>'
    : tokens.map(t => `
        <div class="tok" data-id="${escapeHtml(t.id)}">
          <div>
            <div class="name-line">${escapeHtml(t.name) || "(unnamed)"}</div>
            <div class="hex-line" data-hex="${escapeHtml(t.token_hex)}">••••••••••••••••</div>
          </div>
          <div class="actions">
            <button data-act="reveal">show</button>
            <button data-act="rename">rename</button>
            <button data-act="delete">✕</button>
          </div>
        </div>`).join("");

  const secretRow = authRequired ? `
      <div class="row">
        <label>Relay secret (Bearer auth)</label>
        <input type="password" id="modal-secret" value="${escapeHtml(TPSession.loadSecret())}" placeholder="paste RELAY_SECRET" />
      </div>` : "";
  back.innerHTML = `
    <div class="modal" role="dialog" aria-modal="true">
      <h2>${initial ? "Set up TermPilot" : "Settings"}</h2>
      <p>${initial
        ? (authRequired
            ? "Enter your relay secret (from <code>config.php</code> on the host) and at least one device token (from <code>termpilot --show-token</code> on each PC)."
            : "Add at least one device token (from <code>termpilot --show-token</code> on each PC). This relay has no Bearer secret configured.")
        : "Add, rename, or remove device tokens. Multiple tokens let you see sessions from several PCs in one chat, grouped by device name."}</p>
      ${secretRow}
      <h2 style="margin-top:18px">Device tokens</h2>
      <div class="token-list" id="modal-tokens">${tokensHtml}</div>
      <div class="row">
        <label>Add a device</label>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <input type="text" id="modal-name" placeholder="device name (e.g. Desktop)" style="flex:1;min-width:140px" />
          <div class="hex-input-wrap">
            <input type="text" id="modal-hex" placeholder="64-char hex token" />
            <button type="button" id="modal-scan" class="qr-scan-btn" title="Scan QR token from termpilot --show-token" aria-label="Scan QR token" hidden>
              <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="3" y="3" width="7" height="7" rx="1"/>
                <rect x="14" y="3" width="7" height="7" rx="1"/>
                <rect x="3" y="14" width="7" height="7" rx="1"/>
                <path d="M14 14h3v3h-3z M17 17h4 M14 20h3 M20 14v3"/>
              </svg>
            </button>
          </div>
          <button id="modal-add">add</button>
        </div>
      </div>
      <h2 style="margin-top:18px">Push notifications</h2>
      <div class="row" id="modal-push-row"></div>
      <h2 style="margin-top:18px">Diagnostics</h2>
      <div class="row">
        <button type="button" id="modal-test-conn">Test connection</button>
        <pre id="modal-test-result" class="diag-out" hidden></pre>
      </div>
      <h2 style="margin-top:18px">Theme</h2>
      <div class="row theme-row">
        <span class="theme-current-name" id="modal-theme-current"></span>
        <button type="button" id="modal-theme-pick">Change theme…</button>
      </div>
      <div class="row actions">
        <button id="modal-close">${initial ? "save and continue" : "close"}</button>
      </div>
    </div>`;
  document.body.appendChild(back);

  const $ = sel => back.querySelector(sel);
  renderPushRow($("#modal-push-row"));
  if (qrScannerAvailable()) {
    const scanBtn = $("#modal-scan");
    scanBtn.hidden = false;
    scanBtn.addEventListener("click", () => {
      openQrScanner({
        onResult: (hex) => {
          $("#modal-hex").value = hex;
          $("#modal-hex").focus();
        },
      });
    });
  }
  $("#modal-add").addEventListener("click", () => {
    const name = $("#modal-name").value.trim();
    const hex = $("#modal-hex").value.trim();
    if (!hex || hex.length !== 64 || !/^[0-9a-fA-F]{64}$/.test(hex)) {
      alert("token must be 64 hex chars"); return;
    }
    TPSession.addToken(name || "device", hex.toLowerCase());
    back.remove(); openTokenModal({ initial });
  });
  // Theme row — show current resolved theme name and open the picker
  // on top of the settings modal (separate .theme-modal-backdrop so the
  // existing backdrop guard does not block it).
  (function _wireThemeRow() {
    const nameEl = $("#modal-theme-current");
    const btn = $("#modal-theme-pick");
    function refresh() {
      const pref = loadThemePref();
      const resolved = resolveTheme(pref);
      if (!resolved) {
        nameEl.textContent = "(themes unavailable)";
        btn.disabled = true;
        return;
      }
      nameEl.textContent = (pref === "system")
        ? `System default (${resolved.name})`
        : resolved.name;
    }
    refresh();
    btn.addEventListener("click", () => openThemePickerModal({ onChange: refresh }));
  })();
  $("#modal-test-conn").addEventListener("click", async () => {
    // Probe the relay's debug.php so the user can tell whether perceived
    // slowness is transport (high wall, low server) or the relay itself.
    // The secret used is whatever's in the modal field right now (so the
    // user can test a freshly-typed secret before saving).
    const out = $("#modal-test-result");
    const btn = $("#modal-test-conn");
    const secret = $("#modal-secret") ? $("#modal-secret").value.trim()
                                      : TPSession.loadSecret();
    btn.disabled = true;
    out.hidden = false;
    out.textContent = "Running probes…";
    try {
      out.textContent = await runConnectionDiagnostic(secret);
    } catch (e) {
      out.textContent = "Failed: " + (e && e.message || e);
    } finally {
      btn.disabled = false;
    }
  });
  $("#modal-close").addEventListener("click", () => {
    const secInput = $("#modal-secret");
    if (secInput) {
      const sec = secInput.value.trim();
      if (sec) TPSession.saveSecret(sec);
      else TPSession.clearSecret();
    }
    if (authRequired && !TPSession.loadSecret()) { alert("relay secret required"); return; }
    back.remove();
    TPSession.clearMatches();
    clearAllSessionTerms();
    refreshSessions();
  });
  back.querySelectorAll(".tok").forEach(card => {
    const id = card.dataset.id;
    card.querySelector('[data-act="reveal"]').addEventListener("click", () => {
      const hex = card.querySelector(".hex-line").dataset.hex;
      const el = card.querySelector(".hex-line");
      const showing = el.textContent === hex;
      el.textContent = showing ? "••••••••••••••••" : hex;
    });
    card.querySelector('[data-act="rename"]').addEventListener("click", () => {
      const newName = prompt("New name for this device:");
      if (newName !== null) {
        TPSession.renameToken(id, newName.trim());
        back.remove(); openTokenModal({ initial });
      }
    });
    card.querySelector('[data-act="delete"]').addEventListener("click", () => {
      if (confirm("Delete this token? Sessions encrypted with it will become unreadable.")) {
        TPSession.removeToken(id);
        back.remove(); openTokenModal({ initial });
      }
    });
  });
}

/* ===== Theme picker modal ============================================
 * Layered on top of the settings modal (z-index 101 vs 100). Uses a
 * distinct .theme-modal-backdrop class so the settings modal's
 * "only one .modal-backdrop at a time" guard does not block it.
 *
 * Layout: "Current" card at the top, separator, then a responsive
 * grid containing the "System default" tile followed by every bundled
 * theme. Cards are <button>s for free keyboard navigation. Clicking
 * applies + persists immediately; no save affordance.
 */
function openThemePickerModal(opts = {}) {
  if (document.querySelector(".theme-modal-backdrop")) return;
  const onChange = opts.onChange || (() => {});
  const TP = window.TPThemes;
  if (!TP || !TP.THEMES || TP.THEMES.length === 0) {
    alert("Theme bundle (lib/themes.js) is not loaded.");
    return;
  }

  // 8 swatches sampled from the 16-colour palette: normal 1–6 plus
  // bright 9 and 12. Gives a quick read of what code highlighting will
  // look like without rendering all 16 dots per tile.
  const DOT_INDICES = [1, 2, 3, 4, 5, 6, 9, 12];

  function paletteDotsHtml(palette) {
    return DOT_INDICES.map(i => {
      const c = (palette && palette[i]) || "#000";
      return `<span style="background:${escapeHtml(c)}"></span>`;
    }).join("");
  }
  function cardHtml(theme, isActive) {
    return `
      <button class="theme-card" data-theme-id="${escapeHtml(theme.id)}" aria-pressed="${isActive ? "true" : "false"}" title="${escapeHtml(theme.name)}">
        <div class="theme-preview" style="background:${escapeHtml(theme.bg)}; color:${escapeHtml(theme.fg)}">
          <span class="theme-sample">Aa</span>
          <div class="theme-palette">${paletteDotsHtml(theme.palette)}</div>
        </div>
        <div class="theme-name">${escapeHtml(theme.name)}</div>
      </button>`;
  }
  function systemTileHtml(isActive) {
    const darkT = findTheme(TP.DEFAULT_DARK_ID);
    const lightT = findTheme(TP.DEFAULT_LIGHT_ID);
    const lBg = (lightT && lightT.bg) || "#f4f4f4";
    const lFg = (lightT && lightT.fg) || "#222";
    const dBg = (darkT && darkT.bg) || "#282c34";
    const dFg = (darkT && darkT.fg) || "#abb2bf";
    return `
      <button class="theme-card theme-card-system" data-theme-id="__system__" aria-pressed="${isActive ? "true" : "false"}" title="Follow system preference">
        <div class="theme-preview theme-preview-system">
          <div class="theme-half" style="background:${lBg}; color:${lFg}"><span class="theme-sample">Aa</span></div>
          <div class="theme-half" style="background:${dBg}; color:${dFg}"><span class="theme-sample">Aa</span></div>
        </div>
        <div class="theme-name">System default</div>
      </button>`;
  }

  const pref = loadThemePref();
  const resolved = resolveTheme(pref);

  const back = document.createElement("div");
  back.className = "theme-modal-backdrop";
  const currentHtml = (pref === "system")
    ? systemTileHtml(true)
    : (resolved ? cardHtml(resolved, true) : "");
  // Show "currently <name>" only in system mode — for manual picks the
  // card itself already shows the name and a second label is redundant.
  const currentMetaHtml = (pref === "system" && resolved)
    ? `<div class="theme-current-meta">System default — currently ${escapeHtml(resolved.name)}</div>`
    : "";

  back.innerHTML = `
    <div class="modal theme-modal" role="dialog" aria-modal="true" aria-label="Theme picker">
      <h2>Theme</h2>
      <p>Pick a colour theme. The terminal log uses the selected palette;
         the rest of the app re-tints to match.</p>

      <label>Current</label>
      <div class="theme-current">
        ${currentHtml}
        ${currentMetaHtml}
      </div>

      <div class="theme-sep"></div>

      <label>All themes</label>
      <div class="theme-grid">
        ${systemTileHtml(pref === "system")}
        ${TP.THEMES.map(t => cardHtml(t, pref !== "system" && t.id === (resolved && resolved.id))).join("")}
      </div>

      <div class="row actions">
        <button id="theme-close">close</button>
      </div>
    </div>`;
  document.body.appendChild(back);

  // Restore grid scroll position if we were re-rendered after a click,
  // so the user doesn't lose their place when picking deep in the list.
  const grid = back.querySelector(".theme-grid");
  if (grid && typeof opts.gridScrollTop === "number") {
    grid.scrollTop = opts.gridScrollTop;
  }

  back.addEventListener("click", (e) => {
    if (e.target === back) { back.remove(); }
  });
  back.querySelector("#theme-close").addEventListener("click", () => back.remove());

  back.querySelectorAll(".theme-card").forEach(card => {
    card.addEventListener("click", () => {
      const id = card.dataset.themeId;
      if (id === "__system__") {
        saveThemePref("system");
        applyTheme(resolveTheme("system"));
      } else {
        const t = findTheme(id);
        if (!t) return;
        saveThemePref(id);
        applyTheme(t);
      }
      onChange();
      // Re-render the picker so aria-pressed + the Current section
      // reflect the new selection. Stash scrollTop so the user does not
      // lose their place when clicking a tile near the bottom.
      const gridScrollTop = grid ? grid.scrollTop : 0;
      back.remove();
      openThemePickerModal({ onChange, gridScrollTop });
    });
  });
}

// Hit debug.php a few times and return a small text report. Same shape
// as `termpilot --test-connection` so the two surfaces stay comparable.
// Reads the secret directly (not via TPSession.loadSecret) so the user
// can test a value they just typed but haven't saved yet.
async function runConnectionDiagnostic(secret) {
  const DEBUG = RELAY.replace(/relay\.php(\?.*)?$/, "debug.php");
  async function probe(op) {
    const t0 = performance.now();
    const headers = secret ? { "Authorization": "Bearer " + secret } : {};
    const r = await fetch(`${DEBUG}?op=${op}`, { headers, cache: "no-store" });
    const wall = performance.now() - t0;
    if (!r.ok) {
      const txt = await r.text();
      throw new Error(`HTTP ${r.status}: ${txt.slice(0, 120)}`);
    }
    const body = await r.json();
    return { wall, server: Number(body.server_ms || 0), body };
  }
  const stats = arr => {
    if (!arr.length) return "(n=0)";
    const s = [...arr].sort((a, b) => a - b);
    const p50 = s[s.length >> 1];
    const p95 = s[Math.max(0, Math.round(s.length * 0.95) - 1)];
    return `min ${s[0].toFixed(0)}  p50 ${p50.toFixed(0)}  p95 ${p95.toFixed(0)}  max ${s[s.length-1].toFixed(0)} ms  (n=${s.length})`;
  };
  // Warm-up to avoid counting cold-cache TLS handshake — drops the first
  // request from the report.
  try { await probe("ping"); } catch (e) { throw e; }
  const ping = [];
  for (let i = 0; i < 8; i++) {
    try { ping.push(await probe("ping")); } catch (e) { /* skip individual flake */ }
  }
  const fs = [];
  for (let i = 0; i < 3; i++) {
    try { fs.push(await probe("fs")); } catch (e) { /* skip */ }
  }
  const net = ping.map(p => p.wall - p.server);
  const lines = [];
  lines.push("Ping (no server work):");
  lines.push("  wall RTT     " + stats(ping.map(p => p.wall)));
  lines.push("  server time  " + stats(ping.map(p => p.server)));
  lines.push("  network      " + stats(net));
  lines.push("");
  lines.push("FS probe (write+read+append+cleanup):");
  lines.push("  wall RTT     " + stats(fs.map(p => p.wall)));
  lines.push("  server time  " + stats(fs.map(p => p.server)));
  if (fs.length) {
    const subs = {};
    for (const r of fs) {
      for (const [k, v] of Object.entries(r.body.timings_ms || {})) {
        (subs[k] = subs[k] || []).push(Number(v));
      }
    }
    for (const k of Object.keys(subs).sort()) {
      lines.push(`    ${k.padEnd(16)} ${stats(subs[k])}`);
    }
  }
  lines.push("");
  const pct = (arr, p) => {
    if (!arr.length) return 0;
    const s = [...arr].sort((a, b) => a - b);
    return s[Math.max(0, Math.round(s.length * p) - 1)];
  };
  const p50net = pct(net, 0.5);
  const p50srv = pct(ping.map(p => p.server), 0.5);
  const p50wall = pct(ping.map(p => p.wall), 0.5);
  const p95wall = pct(ping.map(p => p.wall), 0.95);
  // Queueing signature — server time stays low but wall p95 spikes:
  // request sat in the FastCGI gateway waiting for a worker to free up.
  if (p95wall > 1000 && p95wall > p50wall * 5 && p50srv < 50) {
    lines.push(`Verdict: RELAY QUEUEING (p50 ${p50wall.toFixed(0)} ms, p95 ${p95wall.toFixed(0)} ms wall;`);
    lines.push(`  server-time stays at ${p50srv.toFixed(1)} ms). PHP-FPM workers look exhausted —`);
    lines.push("  most likely the long-polls from active sessions are tying up the pool.");
  } else if (p50net > 500) {
    lines.push(`Verdict: NETWORK looks slow (p50 RTT-overhead ${p50net.toFixed(0)} ms).`);
  } else if (p50srv > 200) {
    lines.push(`Verdict: RELAY looks slow (p50 server-time ${p50srv.toFixed(0)} ms).`);
  } else {
    lines.push("Verdict: connection looks healthy.");
  }
  return lines.join("\n");
}

function renderPushRow(host) {
  if (!host) return;
  if (!TPSession.pushIsSupported()) {
    host.innerHTML = '<p style="color:#777;font-style:italic;margin:0">Not supported in this browser.</p>';
    return;
  }
  const perm = TPSession.pushPermission();
  const state = TPSession.pushState();
  const enabled = !!(state && state.enabled);
  const count = enabled ? (state.registered ? state.registered.length : 0) : 0;
  if (enabled) {
    host.innerHTML = `<p style="color:#9aa;margin:0 0 8px">Enabled on this device — ${count} token(s) registered. The relay can send a generic alert when a session needs attention; no session content is in the push. (No wrapper-side triggers are wired in v1.)</p>
       <button id="push-toggle">Disable notifications</button>`;
  } else if (perm === "denied") {
    host.innerHTML = `<p style="color:#f5a524;margin:0 0 8px">Notifications are blocked for this site.</p>
       <p style="color:#9aa;margin:0 0 8px;font-size:11px;line-height:1.5">To re-enable: tap the lock / info icon in the address bar → Site settings → set Notifications to "Allow", then click below to subscribe.</p>
       <button id="push-toggle">Try again</button>`;
  } else {
    host.innerHTML = `<p style="color:#9aa;margin:0 0 8px">Subscribe so the relay can wake your device when a session needs attention. Content-free alerts only.</p>
       <button id="push-toggle">Enable notifications</button>`;
  }
  const btn = host.querySelector("#push-toggle");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const origText = btn.textContent;
    btn.textContent = "Working…";
    try {
      if (enabled) await TPSession.disablePush(RELAY);
      else         await TPSession.enablePush(RELAY);
    } catch (e) {
      const msg = (e && e.message) || String(e);
      if (/blocked|denied/i.test(msg)) {
        alert("Notifications are blocked in your browser. Open this site's settings (lock icon in the address bar), set Notifications to Allow, then click Try again.");
      } else if (/permission default|dismissed/i.test(msg)) {
        btn.disabled = false; btn.textContent = origText;
        return;
      } else {
        alert("Push setup failed: " + msg);
      }
    }
    renderPushRow(host);
  });
}

// Event delegation: any element with data-action="settings" opens the
// settings modal (tokens + push notifications). Lets us re-render the
// sidebar / hero / etc. without rebinding each render.
document.addEventListener("click", (e) => {
  if (e.target.closest('[data-action="settings"]')) openTokenModal();
});

// When tokens are added without re-toggling push, retry registering them.
window.addEventListener("storage", (e) => {
  if (e.key === "termpilot-tokens") { try { TPSession.syncPushTokens(RELAY); } catch (_) {} }
});

/* ===== Headless xterm + log renderer (per-session) ===================== */
// One xterm instance per session id, kept alive while the user navigates
// between sessions. Re-attaching to a session restores its term + the
// rendered log innerHTML; pollLoop just resumes from the saved
// outNextSeq, fetching only bytes that arrived while we were away.
//
// `term` is mutable: `attach()` swaps it to the active session's term so
// renderLogHTML / pollLoop / control buttons all act on the right one.
// In-memory cache only — page reload clears it.
const sessionTerms = new Map();  // sid → { term, host, logHtml, outNextSeq, inNextSeq, scrollTop }
const TERM_HOST_CONTAINER = document.getElementById("term-host");
let term = null;  // bound by attach() to the active session's xterm

function _newSessionTerm() {
  // Each session needs its own offscreen host element since xterm pins
  // itself to one DOM node at open() time.
  const host = document.createElement("div");
  host.style.cssText = "width:0;height:0;overflow:hidden;visibility:hidden";
  TERM_HOST_CONTAINER.appendChild(host);
  const t = new Terminal({
    cursorBlink: false, convertEol: false, scrollback: 5000,
    cols: 80, rows: 24, allowProposedApi: true,
  });
  t.open(host);
  return { term: t, host, logHtml: "", outNextSeq: 0, inNextSeq: 0, scrollTop: 0 };
}
function _ensureSessionTerm(sid) {
  let entry = sessionTerms.get(sid);
  if (!entry) { entry = _newSessionTerm(); sessionTerms.set(sid, entry); }
  return entry;
}
function _disposeSessionTerm(sid) {
  const entry = sessionTerms.get(sid);
  if (!entry) return;
  try { entry.term.dispose(); } catch (e) {}
  try { entry.host.remove(); } catch (e) {}
  sessionTerms.delete(sid);
}
function clearAllSessionTerms() {
  for (const sid of Array.from(sessionTerms.keys())) _disposeSessionTerm(sid);
}

// Boot a placeholder term so renderLogHTML / syncTermSize don't NPE before
// the first attach. It gets disposed (with the rest) on token rotation.
const _bootEntry = _newSessionTerm();
sessionTerms.set("_boot", _bootEntry);
term = _bootEntry.term;
const logEl = document.getElementById("log");
const logLoaderEl = document.getElementById("log-loading");
// Loader state: a deferred-show timer lets fast loads skip the bar entirely
// (avoids a flash), and a monotonic progress clamp keeps the bar from
// regressing when `total` grows mid-catchup (new records arrive while we
// drain history).
const _loaderState = { showTimer: null, progressMax: 0, fillEl: null, barEl: null };
function _loaderEls() {
  if (!_loaderState.fillEl && logLoaderEl) {
    _loaderState.fillEl = logLoaderEl.querySelector(".loader-progress-fill");
    _loaderState.barEl  = logLoaderEl.querySelector(".loader-progress");
  }
}
function showLogLoader(label, opts) {
  if (!logLoaderEl) return;
  _loaderEls();
  const { delayMs = 0, determinate = false } = opts || {};
  if (label) {
    const lbl = logLoaderEl.querySelector(".loader-label");
    if (lbl) lbl.textContent = label;
  }
  if (_loaderState.barEl) _loaderState.barEl.classList.toggle("indeterminate", !determinate);
  if (_loaderState.fillEl && determinate) _loaderState.fillEl.style.width = "0%";
  _loaderState.progressMax = 0;
  if (_loaderState.showTimer) { clearTimeout(_loaderState.showTimer); _loaderState.showTimer = null; }
  if (delayMs > 0) {
    _loaderState.showTimer = setTimeout(() => {
      _loaderState.showTimer = null;
      logLoaderEl.hidden = false;
    }, delayMs);
  } else {
    logLoaderEl.hidden = false;
  }
}
function hideLogLoader() {
  if (!logLoaderEl) return;
  _loaderEls();
  if (_loaderState.showTimer) { clearTimeout(_loaderState.showTimer); _loaderState.showTimer = null; }
  logLoaderEl.hidden = true;
  _loaderState.progressMax = 0;
  if (_loaderState.fillEl) _loaderState.fillEl.style.width = "0%";
  if (_loaderState.barEl)  _loaderState.barEl.classList.add("indeterminate");
}
function updateLogLoaderProgress(current, total) {
  if (!logLoaderEl || !total || total <= 0) return;
  _loaderEls();
  let pct = Math.round(100 * Math.min(current, total) / total);
  if (pct < _loaderState.progressMax) pct = _loaderState.progressMax;
  _loaderState.progressMax = pct;
  if (_loaderState.barEl) _loaderState.barEl.classList.remove("indeterminate");
  if (_loaderState.fillEl) _loaderState.fillEl.style.width = pct + "%";
}

const ANSI16 = ["#000000","#cd3131","#0dbc79","#e5e510","#2472c8","#bc3fbc","#11a8cd","#e5e5e5","#666666","#f14c4c","#23d18b","#f5f543","#3b8eea","#d670d6","#29b8db","#ffffff"];
// Now that ANSI16 exists (it was in the TDZ for the boot applyTheme()
// call near the top of the file), re-apply the resolved theme so its
// palette lands in ANSI16 before any session renders.
(function _bootSyncAnsi16(){
  try {
    const t = resolveTheme(loadThemePref());
    if (t && t.palette && t.palette.length >= 16) {
      ANSI16.length = 0;
      for (let i = 0; i < 16; i++) ANSI16.push(t.palette[i]);
    }
  } catch (e) {}
})();
function ansi256(n){if(n<16)return ANSI16[n];if(n<232){n-=16;const r=Math.floor(n/36),g=Math.floor((n%36)/6),b=n%6;const cv=v=>v===0?0:55+v*40;return`rgb(${cv(r)},${cv(g)},${cv(b)})`;}const gr=8+(n-232)*10;return`rgb(${gr},${gr},${gr})`;}
function cellStyle(c){let s="";if(c.isFgRGB&&c.isFgRGB()){const fg=c.getFgColor();s+="color:#"+("000000"+fg.toString(16)).slice(-6)+";";}else if(c.isFgPalette&&c.isFgPalette())s+="color:"+ansi256(c.getFgColor())+";";if(c.isBgRGB&&c.isBgRGB()){const bg=c.getBgColor();s+="background:#"+("000000"+bg.toString(16)).slice(-6)+";";}else if(c.isBgPalette&&c.isBgPalette())s+="background:"+ansi256(c.getBgColor())+";";if(c.isBold&&c.isBold())s+="font-weight:bold;";if(c.isItalic&&c.isItalic())s+="font-style:italic;";if(c.isUnderline&&c.isUnderline())s+="text-decoration:underline;";if(c.isInverse&&c.isInverse())s+="filter:invert(1);";if(c.isDim&&c.isDim())s+="opacity:0.7;";return s;}
function renderLogHTML(){
  const buf=term.buffer.active;
  // Cursor position. xterm's IBuffer.cursorY is viewport-relative;
  // adding viewportY gives the row index into the same coordinate
  // space we iterate below.
  const cursorRow = buf.viewportY + buf.cursorY;
  const cursorX   = buf.cursorX;
  const out=[];
  for(let i=0;i<buf.length;i++){
    const line=buf.getLine(i);
    if(!line){out.push("");continue;}
    const isCursorRow = (i === cursorRow);
    let cur="",pend="",row="";
    const flush=()=>{
      if(!pend)return;
      if(cur)row+=`<span style="${cur}">${escapeHtml(pend)}</span>`;
      else row+=escapeHtml(pend);
      pend="";
    };
    for(let x=0;x<line.length;x++){
      if(isCursorRow && x===cursorX){
        flush();
        const c=line.getCell(x);
        const ch=(c && c.getChars()) || " ";
        row+=`<span class="cursor">${escapeHtml(ch)}</span>`;
        cur="";
        continue;
      }
      const c=line.getCell(x);
      if(!c)continue;
      const ch=c.getChars()||" ";
      const st=cellStyle(c);
      if(st!==cur){flush();cur=st;}
      pend+=ch;
    }
    flush();
    // Cursor at column == line width (one past the right margin) —
    // append it as a trailing cell so the user still sees where they are.
    if(isCursorRow && cursorX>=line.length){
      row+=`<span class="cursor">${escapeHtml(" ")}</span>`;
    }
    out.push(row||" ");
  }
  return out.join("\n");
}
let renderPending = false;
function scheduleLogRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => {
    renderPending = false;
    const wasAtBottom = (logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight) < 5;
    logEl.innerHTML = renderLogHTML();
    if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;
  });
}
function syncTermSize(s){if(!s)return;const c=Number(s.cols)||term.cols,r=Number(s.rows)||term.rows;if(c>0&&r>0&&(c!==term.cols||r!==term.rows)){try{term.resize(c,r);}catch(e){}}}

/* ===== Connection state badge ========================================= */
const connBadge = document.getElementById("connbadge");
function setBadge(state) {
  if (!connBadge) return;
  connBadge.classList.remove("online", "reconnecting", "offline");
  connBadge.classList.add(state);
  connBadge.title = state;
}
TPSession.onConnState(setBadge);
setBadge(TPSession.getConnState());

/* ===== Session list rendering ========================================= */
let currentSid = null;
let currentTokenId = null;
let currentKey = null;
let pollAbort = null;
let outNextSeq = 0;
let inNextSeq = 0;
let sessionAlive = true;     // server says wrapper has heartbeated recently
let sessionMissing = false;  // session id not currently in the /sessions list
const setStatus = t => document.getElementById("connstatus").textContent = t;

/* ===== Slow-network detection ========================================= */
// Long-poll means "fetch duration" alone is ambiguous: a quiet session and
// a slow link both produce long durations. Two signals disambiguate:
//   1) For polls that returned records, the relay had data ready, so the
//      duration ≈ network RTT + download. A rolling mean above a threshold
//      = slow link.
//   2) `total - next_seq` after we drain a batch is the queue the relay
//      still holds for us. If that stays high across consecutive polls,
//      we're falling behind the producer regardless of latency.
// Catchup samples are excluded — the first attach pulls a big history.
const NET_WINDOW = 6;
const NET_SLOW_DURATION_MS = 1500;
const NET_SEVERE_DURATION_MS = 4000;
const NET_BACKLOG_THRESH = 20;
const NET_BACKLOG_CONSEC = 3;
// Warm re-attach with a backlog larger than this slides the loading overlay
// over the cached HTML for the duration of the silent catch-up. Below the
// threshold the cached state stays visible — a small catch-up renders in
// one shot fast enough that hiding the cached content would feel jarrier
// than the brief stale-then-fresh transition.
const WARM_OVERLAY_BACKLOG = 2000;
const _netSamples = [];
let _netBacklogStreak = 0;
let _netState = "ok";
let _netForcedSevere = false;

// `bytes` is the response/request body size we trust as small-payload.
// Big batches (Claude finishing a long response) are bandwidth-limited
// not latency-limited — their slowness is "data arrived, just took a
// while to download," which is normal on a slow link and shouldn't
// trip a "your network is slow" banner. Samples over NET_TRUSTED_BYTES
// are kept for backlog accounting but excluded from duration triggers.
const NET_TRUSTED_BYTES = 8 * 1024;
function _netRecord(duration, hadRecords, backlog, bytes) {
  _netSamples.push({
    duration, hadRecords, backlog,
    bytes: typeof bytes === "number" ? bytes : 0,
  });
  if (_netSamples.length > NET_WINDOW) _netSamples.shift();
  _netBacklogStreak = backlog >= NET_BACKLOG_THRESH ? _netBacklogStreak + 1 : 0;
  _netForcedSevere = false;
  _netRefreshBanner();
}

function _netReset() {
  _netSamples.length = 0;
  _netBacklogStreak = 0;
  _netForcedSevere = false;
  _setNetBanner("ok");
}

function _netMarkFetchError() {
  // fetch() rejected (DNS, TLS, hard offline). Treat as severe until the
  // next successful poll clears it.
  _netForcedSevere = true;
  _netRefreshBanner();
}

// Inflight-input tracker: surfaces the slow-banner while an input POST is
// hanging, rather than waiting for it to return. Counter (not boolean) so
// overlapping sends don't clear the flag prematurely.
let _netInflightCount = 0;
let _netInflightTimer = null;
let _netInflightSlow = false;
function _netInflightStart() {
  _netInflightCount++;
  if (_netInflightTimer) return;
  _netInflightTimer = setTimeout(() => {
    _netInflightTimer = null;
    _netInflightSlow = true;
    _netRefreshBanner();
  }, NET_SLOW_DURATION_MS);
}
function _netInflightEnd() {
  _netInflightCount = Math.max(0, _netInflightCount - 1);
  if (_netInflightCount > 0) return;
  if (_netInflightTimer) { clearTimeout(_netInflightTimer); _netInflightTimer = null; }
  if (_netInflightSlow) {
    _netInflightSlow = false;
    _netRefreshBanner();
  }
}

function _netEvaluate() {
  if (!navigator.onLine || _netForcedSevere) return "severe";
  if (_netInflightSlow) return "slow";
  // Trusted = small-payload samples whose duration reflects round-trip
  // time, not download bandwidth.
  const trusted = _netSamples.filter(s =>
    s.hadRecords && s.bytes <= NET_TRUSTED_BYTES);
  const last = trusted[trusted.length - 1];
  if (last) {
    if (last.duration >= NET_SEVERE_DURATION_MS) return "severe";
    if (last.duration >= NET_SLOW_DURATION_MS) return "slow";
  }
  if (trusted.length >= 3) {
    const mean = trusted.reduce((a, s) => a + s.duration, 0) / trusted.length;
    if (mean >= NET_SEVERE_DURATION_MS) return "severe";
    if (mean >= NET_SLOW_DURATION_MS) return "slow";
  }
  if (_netBacklogStreak >= NET_BACKLOG_CONSEC) return "slow";
  return "ok";
}

function _netRefreshBanner() {
  _setNetBanner(_netEvaluate());
}

// Drive the top-right colored disc. Replaces the older banner; the
// state-machine is the same (ok / slow / severe), the surface is just
// less intrusive. Title + aria-label carry the human-readable reason so
// hover / screen-reader users still get it.
function _setNetBanner(state) {
  if (state === _netState) return;
  _netState = state;
  const el = document.getElementById("termpilot-net-disc");
  if (!el) return;
  el.dataset.state = state;
  let label;
  if (state === "severe") {
    label = navigator.onLine
      ? "Connection very slow — click for details"
      : "Offline — waiting for connection";
  } else if (state === "slow") {
    label = "Slow network — click for details";
  } else {
    label = "Connection OK — click for details";
  }
  el.title = label;
  el.setAttribute("aria-label", label);
}

window.addEventListener("online", _netRefreshBanner);
window.addEventListener("offline", _netRefreshBanner);

document.addEventListener("click", (e) => {
  if (e.target.closest("#termpilot-net-disc")) openNetqModal();
});

function _fmtMs(v) {
  if (!isFinite(v) || v <= 0) return "—";
  return v >= 100 ? `${v.toFixed(0)} ms` : `${v.toFixed(1)} ms`;
}

function _summarizeSamples() {
  // Single pass over the ring buffer for the modal stats. Returns
  // categorized counts/percentiles so the modal layout stays trivial.
  const all = _netSamples;
  const trusted = all.filter(s => s.hadRecords && s.bytes <= NET_TRUSTED_BYTES);
  const big = all.filter(s => s.hadRecords && s.bytes > NET_TRUSTED_BYTES);
  const empty = all.filter(s => !s.hadRecords);
  const pct = (arr, p) => {
    if (!arr.length) return NaN;
    const s = [...arr].sort((a, b) => a - b);
    return s[Math.max(0, Math.round(s.length * p) - 1)];
  };
  const durs = trusted.map(s => s.duration);
  return {
    state: _netState,
    online: navigator.onLine,
    inflightSlow: _netInflightSlow,
    backlogStreak: _netBacklogStreak,
    samples: all.length,
    trustedCount: trusted.length,
    bigCount: big.length,
    emptyCount: empty.length,
    p50: pct(durs, 0.5),
    p95: pct(durs, 0.95),
    max: durs.length ? Math.max(...durs) : NaN,
    lastBacklog: all.length ? all[all.length - 1].backlog : 0,
  };
}

function _netqStatsView() {
  const s = _summarizeSamples();
  const stateLabel = s.state === "severe"
    ? (s.online ? "Very slow" : "Offline")
    : s.state === "slow" ? "Slow" : "OK";
  return `
    <div class="modal netq-modal" role="dialog" aria-modal="true">
      <div class="netq-head">
        <h2>Connection quality</h2>
        <button type="button" class="netq-info" id="netq-info"
                title="What do these values mean?" aria-label="What do these values mean?">
          <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"/>
            <line x1="12" y1="16" x2="12" y2="12"/>
            <line x1="12" y1="8" x2="12.01" y2="8"/>
          </svg>
        </button>
      </div>
      <p class="netq-state-line">
        <span class="netq-state-dot ${escapeHtml(s.state)}"></span>
        <strong>${escapeHtml(stateLabel)}</strong>
      </p>
      <dl class="netq-stats">
        <dt>online</dt><dd>${s.online ? "yes" : "no"}</dd>
        <dt>recent polls</dt><dd>${s.samples}</dd>
        <dt>small-payload</dt><dd>${s.trustedCount}</dd>
        <dt>big batches</dt><dd>${s.bigCount}</dd>
        <dt>empty (idle)</dt><dd>${s.emptyCount}</dd>
        <dt>p50 latency</dt><dd>${escapeHtml(_fmtMs(s.p50))}</dd>
        <dt>p95 latency</dt><dd>${escapeHtml(_fmtMs(s.p95))}</dd>
        <dt>max latency</dt><dd>${escapeHtml(_fmtMs(s.max))}</dd>
        <dt>backlog (last)</dt><dd>${s.lastBacklog} records</dd>
        <dt>backlog streak</dt><dd>${s.backlogStreak}</dd>
        <dt>POST inflight</dt><dd>${s.inflightSlow ? "slow" : "no"}</dd>
      </dl>
      <div class="row actions">
        <button id="netq-test">Run full test</button>
        <button id="netq-close">close</button>
      </div>
      <pre id="netq-result" class="diag-out" hidden></pre>
    </div>`;
}

function _netqHelpView() {
  return `
    <div class="modal netq-modal" role="dialog" aria-modal="true">
      <div class="netq-head">
        <h2>What these values mean</h2>
      </div>
      <div class="netq-help">
        <h3>state</h3>
        <p>The current judgment: <code>OK</code>, <code>Slow</code>, or
        <code>Very Slow / Offline</code>. Derived from the metrics below
        over a rolling window of the last 6 samples.</p>

        <h3>online</h3>
        <p>What the device's network stack reports — whether the OS thinks
        there's any connection at all.</p>

        <h3>recent polls</h3>
        <p>Total samples in the window. One sample per output poll from
        the relay, whether or not the poll returned data.</p>

        <h3>small-payload / big batches / empty</h3>
        <p>How those samples break down. <strong>Small-payload</strong>
        responses (≤ ${NET_TRUSTED_BYTES / 1024} KB) finish so quickly that
        their wall-time really is round-trip time. <strong>Big batches</strong>
        carry a lot of data — Claude's final response chunk, say — and are
        bandwidth-limited, not latency-limited; including them would flag
        every long answer as "slow connection." <strong>Empty</strong> polls
        happened when the relay had nothing new (idle wait), so they tell
        us nothing about quality.</p>

        <h3>p50 / p95 / max latency</h3>
        <p>Percentiles over the small-payload samples only.
        <strong>p50</strong> is the median — half the requests were faster,
        half slower. <strong>p95</strong> is the 95th percentile — 95% of
        requests beat this, 5% were worse; it's the "tail" that shows
        what occasional bad samples look like. <strong>max</strong> is the
        single worst sample in the window.</p>

        <h3>backlog (last) / backlog streak</h3>
        <p>After the most recent poll, how many records the relay still
        had queued for us. &gt; 0 means we're downloading slower than the
        wrapper is producing. <strong>Streak</strong> is how many polls in
        a row have shown backlog. A streak of 3 trips the disc to slow
        even if latency looks fine — that's the producer outrunning us.</p>

        <h3>POST inflight</h3>
        <p>Shows <code>slow</code> while the most recent input POST (a
        keystroke you sent) has been waiting longer than 1.5 s. Clears as
        soon as the POST returns. This is what lets the disc go yellow
        <em>while</em> a slow request is happening, instead of only after
        it finishes.</p>
      </div>
      <div class="row actions">
        <button id="netq-back">← back</button>
        <button id="netq-close">close</button>
      </div>
    </div>`;
}

function openNetqModal() {
  if (document.querySelector(".modal-backdrop")) return;
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  document.body.appendChild(back);

  const renderView = (which) => {
    back.innerHTML = which === "help" ? _netqHelpView() : _netqStatsView();
    wireHandlers();
  };

  const wireHandlers = () => {
    const $ = sel => back.querySelector(sel);
    const close = $("#netq-close");
    if (close) close.addEventListener("click", () => back.remove());
    const info = $("#netq-info");
    if (info) info.addEventListener("click", () => renderView("help"));
    const backBtn = $("#netq-back");
    if (backBtn) backBtn.addEventListener("click", () => renderView("stats"));
    const test = $("#netq-test");
    if (test) test.addEventListener("click", async () => {
      const out = $("#netq-result");
      test.disabled = true;
      out.hidden = false;
      out.textContent = "Running probes…";
      try {
        out.textContent = await runConnectionDiagnostic(TPSession.loadSecret());
      } catch (e) {
        out.textContent = "Failed: " + (e && e.message || e);
      } finally {
        test.disabled = false;
      }
    });
  };

  back.addEventListener("click", (e) => {
    if (e.target === back) back.remove();  // backdrop click dismisses
  });
  renderView("stats");
}

function renderSessions(groups, orphans) {
  const host = document.getElementById("sessions-host");
  if (groups.every(g => g.sessions.length === 0)) {
    // No sidebar text — the main-area hero (empty-hero) carries the
    // empty-state messaging and reload button. A duplicate plain-text
    // version in the sidebar competes visually without adding info.
    host.innerHTML = "";
    return;
  }
  // Sort: alive newest-first, then stale newest-first.
  const cmp = (a, b) => {
    const aliveA = a.alive !== false ? 1 : 0;
    const aliveB = b.alive !== false ? 1 : 0;
    if (aliveA !== aliveB) return aliveB - aliveA;
    return (b.started_at || 0) - (a.started_at || 0);
  };
  let html = "";
  for (const g of groups) {
    if (g.sessions.length === 0) continue;
    const sorted = g.sessions.slice().sort(cmp);
    html += `<div class="group"><div class="group-head">${escapeHtml(g.token.name) || "(unnamed)"}</div><ul>`;
    for (const s of sorted) {
      const m = s.meta || {};
      const t = new Date((s.started_at || 0) * 1000).toLocaleTimeString();
      const isStale = s.alive === false;
      const liClasses = ((s.id === currentSid) ? "active " : "") + (isStale ? "stale" : "");
      const staleBadge = isStale ? '<span class="stale-pill">offline</span>' : "";
      const lastSeen = (s.last_seen || 0);
      const delBtn = isStale
        ? `<button type="button" class="del-btn" title="Delete offline session" aria-label="Delete offline session" data-last-seen="${lastSeen}"><svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M6.5 1.5h3a1 1 0 0 1 1 1V3h3a.5.5 0 0 1 0 1h-.586l-.85 9.36A1.5 1.5 0 0 1 10.572 14.5H5.428a1.5 1.5 0 0 1-1.492-1.14L3.086 4H2.5a.5.5 0 0 1 0-1h3v-.5a1 1 0 0 1 1-1zm.5 1.5V3h2v-.5h-2zM4.092 4l.84 9.23a.5.5 0 0 0 .497.38h5.142a.5.5 0 0 0 .498-.38L11.908 4H4.092zM6.5 5.5a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm3 0a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5z"/></svg></button>`
        : "";
      html += `<li class="${liClasses.trim()}" data-sid="${escapeHtml(s.id)}" data-tid="${escapeHtml(g.token.id)}">${escapeHtml(m.title || "(untitled)")} <span class="pill">${escapeHtml(s.id.slice(0,6))}</span>${staleBadge}<small>${escapeHtml(m.cwd || "")}<br/>started ${t}</small>${delBtn}</li>`;
    }
    html += "</ul></div>";
  }
  host.innerHTML = html;
  host.querySelectorAll("li[data-sid]").forEach(li => {
    li.addEventListener("click", () => attach(li.dataset.sid, li.dataset.tid));
    const del = li.querySelector(".del-btn");
    if (del) del.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteOfflineSession(li.dataset.sid, li.dataset.tid, parseInt(del.dataset.lastSeen || "0", 10));
    });
  });
}

async function deleteOfflineSession(sid, tid, lastSeen) {
  if (!confirm(`Delete this offline session?\n\nID: ${sid.slice(0,8)}\n\nIt will be hidden from the list. The wrapper makes a new session id on next start, so this won't reappear unless the same offline wrapper checks in again with new data.`)) return;
  // Safety: re-check the session is still offline. If new data has arrived
  // since the user opened the menu, abort — the wrapper just came back.
  try {
    const resp = await TPSession.api(RELAY, "sessions");
    if (resp.status === 200 && Array.isArray(resp.body && resp.body.sessions)) {
      const cur = resp.body.sessions.find(s => s.id === sid);
      if (cur && (cur.alive !== false || (cur.last_seen || 0) > lastSeen)) {
        alert("New data arrived from this session — delete cancelled.");
        await refreshSessions();
        return;
      }
    }
  } catch (e) { /* network blip — fall through and try the close */ }
  // op_close on the relay requires proof of token possession via
  // trigger_secret_hex (HMAC-derived from the device token). The
  // browser holds the token; the relay does not.
  const tok = TPSession.loadTokens().find(t => t.id === tid);
  if (!tok) {
    alert("Failed to delete: no token bound to this session in this browser");
    return;
  }
  let secretHex;
  try {
    secretHex = await TPSession.triggerSecretHex(tok.token_hex);
  } catch (e) {
    alert("Failed to derive trigger secret: " + e.message);
    return;
  }
  let r;
  try {
    r = await TPSession.api(RELAY, "close", {
      method: "POST",
      body: { session_id: sid, trigger_secret_hex: secretHex },
    });
  } catch (e) {
    alert("Failed to delete: " + e.message);
    return;
  }
  if (r.status !== 200) {
    const msg = (r.body && r.body.error) ? r.body.error : ("HTTP " + r.status);
    alert("Failed to delete: " + msg);
    return;
  }
  if (currentSid === sid) detach("session deleted");
  if (sessionTerms.has(sid)) _disposeSessionTerm(sid);
  TPSession.clearPendingSends(sid);
  await refreshSessions();
}

async function refreshSessions() {
  if (showLoginIfNeeded()) return;
  try {
    const { groups, orphans, error } = await TPSession.refreshSessionList(RELAY);
    if (error) { setStatus("relay: " + error); updateEmptyHero([], error); return; }
    renderSessions(groups, orphans);
    updateEmptyHero(groups, null);
    // Evict cached xterm instances for sessions that no longer exist on
    // the relay. Without this, every visited session keeps a full
    // xterm + scrollback + DOM node in memory forever — a long-lived
    // PWA accumulates tens of MB over time. The currently-attached
    // session is always kept regardless.
    const live = new Set();
    for (const g of groups) for (const s of g.sessions) live.add(s.id);
    for (const o of orphans || []) live.add(o.id);
    for (const cachedSid of Array.from(sessionTerms.keys())) {
      if (cachedSid === "_boot") continue;
      if (cachedSid === currentSid) continue;
      if (!live.has(cachedSid)) _disposeSessionTerm(cachedSid);
    }
    if (!currentSid) {
      // Auto-attach: desktop attaches to first session of any group
      for (const g of groups) {
        if (g.sessions.length) {
          if (!isMobile() || localStorage.getItem(VIEW_KEY) === "session") {
            attach(g.sessions[0].id, g.token.id);
          }
          break;
        }
      }
    }
    if (currentSid) {
      // Update session-bar if metadata refreshed. Crucially, do NOT detach
      // on a transient miss — the wrapper might be briefly offline. Show
      // "session offline" instead and re-attach when the session returns.
      let allSessions = [];
      for (const g of groups) allSessions = allSessions.concat(g.sessions);
      const cur = allSessions.find(s => s.id === currentSid);
      if (!cur) {
        sessionMissing = true;
        sessionAlive = false;
        applySessionAliveUI();
        setStatus("session offline (waiting for wrapper)");
      } else {
        const wasMissing = sessionMissing;
        sessionMissing = false;
        sessionAlive = (cur.alive !== false);  // alive defaults to true if unset
        updateSessionBar(cur);
        applySessionAliveUI();
        if (wasMissing && currentSid) {
          // Reconnected: we were detached-without-detach; nothing else
          // needs to do — pollLoop is still running on the same sid.
          setStatus("reconnected: " + currentSid.slice(0,6));
        }
        // Drain anything queued while we were offline. No-op if the queue
        // is empty or we're still considered offline.
        if (sessionAlive) _drainTerminalQueue();
      }
    }
  } catch (e) {
    setStatus("error: " + e.message);
  }
}

/* ===== Empty-state hero ============================================== */
function updateEmptyHero(groups, error) {
  const hasAny = groups.some(g => g.sessions.length > 0);
  const sectionEl = document.querySelector("main > section");
  const hero = document.getElementById("empty-hero");
  const hint = document.getElementById("empty-hint");
  const show = !hasAny && !currentSid;
  if (show) {
    sectionEl.classList.add("show-empty-hero");
    hero.hidden = false;
    if (hint) {
      if (error) {
        hint.textContent = "Relay error: " + error;
        hint.classList.add("error");
      } else {
        hint.textContent = "Last checked " + new Date().toLocaleTimeString();
        hint.classList.remove("error");
      }
    }
    // On mobile, the section is hidden by default in list view. Force the
    // session view so the hero is what the user lands on when there's
    // nothing in the sidebar to pick anyway.
    if (isMobile() && localStorage.getItem(VIEW_KEY) !== "session") {
      localStorage.setItem(VIEW_KEY, "session");
      applyLayout();
    }
  } else {
    sectionEl.classList.remove("show-empty-hero");
    hero.hidden = true;
  }
}

let reloadInflight = false;
async function triggerReload(buttonEl) {
  if (reloadInflight) return;
  reloadInflight = true;
  const allBtns = document.querySelectorAll("#empty-reload, #sidebar-reload");
  allBtns.forEach(b => { b.disabled = true; b.classList.add("loading"); });
  // Bust the per-session match cache so newly-decryptable sessions show up
  // immediately if a token was just added on another device.
  TPSession.clearMatches();
  try {
    await refreshSessions();
  } finally {
    reloadInflight = false;
    allBtns.forEach(b => { b.disabled = false; b.classList.remove("loading"); });
  }
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("#empty-reload");
  if (t) triggerReload(t);
});

function applySessionAliveUI() {
  const bar = document.getElementById("session-bar");
  const sendInput = document.querySelector("#inputform input[type=text]");
  const sendBtn = document.querySelector("#inputform button[type=submit]");
  const offline = sessionMissing || !sessionAlive;
  if (bar) bar.classList.toggle("session-offline", offline);
  if (sendInput) sendInput.disabled = offline;
  if (sendBtn) sendBtn.disabled = offline;
  // Show an "offline" pill in the session bar
  const titleEl = document.getElementById("sb-title");
  if (titleEl) {
    titleEl.querySelectorAll(".sb-offline-tag").forEach(n => n.remove());
    if (offline && !bar.classList.contains("empty-state")) {
      const tag = document.createElement("span");
      tag.className = "sb-offline-tag";
      tag.textContent = sessionMissing ? "offline" : "stale";
      titleEl.appendChild(tag);
    }
  }
}

function updateSessionBar(s) {
  const bar = document.getElementById("session-bar");
  const t = document.getElementById("sb-title"), id = document.getElementById("sb-id");
  const cw = document.getElementById("sb-cwd"), st = document.getElementById("sb-started");
  if (!s) { bar.classList.add("empty-state"); t.textContent = "No session selected"; id.textContent=""; cw.textContent=""; st.textContent=""; return; }
  bar.classList.remove("empty-state");
  const m = s.meta || {};
  t.textContent = m.title || "(untitled)";
  id.textContent = s.id;
  cw.textContent = m.cwd || "";
  st.textContent = "started " + new Date((s.started_at || 0) * 1000).toLocaleString();
  syncTermSize({ cols: s.cols, rows: s.rows });
}

function _captureTerminalState() {
  if (!currentSid) return;
  const entry = sessionTerms.get(currentSid);
  if (!entry) return;
  entry.logHtml = logEl.innerHTML;
  entry.outNextSeq = outNextSeq;
  entry.inNextSeq = inNextSeq;
  entry.scrollTop = logEl.scrollTop;
}

function detach(reason) {
  // Stash the active session's term state before tearing down. The term
  // instance itself stays in sessionTerms — re-attaching restores it.
  _captureTerminalState();
  if (pollAbort) { pollAbort.abort(); pollAbort = null; }
  if (currentSid) TPSession.detachSessionForSync(currentSid);
  currentSid = null; currentTokenId = null; currentKey = null;
  outNextSeq = 0; inNextSeq = 0;
  _netReset();
  // Don't reset/clear the previous term — it lives in the cache.
  logEl.innerHTML = "";
  hideLogLoader();
  updateSessionBar(null);
  if (reason) setStatus(reason);
  if (typeof TPKeyboard !== "undefined") TPKeyboard.onSessionHidden();
  if (isMobile() && reason === "session ended") {
    localStorage.setItem(VIEW_KEY, "list"); applyLayout();
  }
}

async function attach(sid, tokenId) {
  if (currentSid === sid) {
    if (isMobile()) { localStorage.setItem(VIEW_KEY, "session"); applyLayout(); }
    return;
  }
  detach();
  currentSid = sid; currentTokenId = tokenId;
  // Resolve the encryption key for this session
  currentKey = TPSession.attachSession(sid);
  if (!currentKey) {
    setStatus("no key for session " + sid.slice(0,6));
    return;
  }
  // Get-or-create this session's xterm + bind it as the global `term`.
  const entry = _ensureSessionTerm(sid);
  term = entry.term;
  const isWarm = !!entry.logHtml;
  outNextSeq = entry.outNextSeq;
  inNextSeq = entry.inNextSeq;
  if (isWarm) {
    logEl.innerHTML = entry.logHtml;
    // Don't hide the loader here. detach() already hid it; pollLoop's first
    // response decides whether to bring the overlay back (for backlogs
    // > 2000) so the user sees a determinate bar rather than stale cached
    // HTML during a long catch-up.
    setStatus("re-attached: " + sid.slice(0,6));
  } else {
    logEl.innerHTML = "";
    showLogLoader("Loading session…", { delayMs: 200 });
    setStatus("attached: " + sid.slice(0,6));
  }
  // Always land at the bottom on open — newest output is what you want
  // when re-entering a session.
  requestAnimationFrame(() => { logEl.scrollTop = logEl.scrollHeight; });
  if (isMobile()) { localStorage.setItem(VIEW_KEY, "session"); applyLayout(); }
  // Background-sync: persist what the SW needs to drain offline-queued
  // sends for this session. Token-hex comes from the matched token.
  const tokens = TPSession.loadTokens();
  const tok = tokens.find(t => t.id === tokenId);
  if (tok) {
    TPSession.attachSessionForSync(sid, {
      tokenHex: tok.token_hex,
      relayBase: new URL(RELAY, location.href).href,
    });
  }
  pollLoop(sid, isWarm);
}

function _concatBytes(chunks) {
  if (chunks.length === 1) return chunks[0];
  let total = 0;
  for (const c of chunks) total += c.length;
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) { out.set(c, off); off += c.length; }
  return out;
}

async function pollLoop(sid, isWarm) {
  pollAbort = new AbortController();
  _netReset();
  // Treat the first batch(es) as catchup: pull bigger pages and skip
  // intermediate renders so a long history doesn't visibly play back.
  let catchingUp = true;
  // One-shot guard for the warm-overlay decision below: we need to know the
  // backlog size (only available in the first /output response) before we
  // can decide whether to slide the loading overlay over the cached HTML.
  let firstResponse = true;
  // Consecutive decrypt failures across batches. Past a small threshold
  // we surface a loud error and stop polling — silently skipping every
  // record while the cursor walks forward would let a hostile relay (or
  // a genuine token/sid mismatch) drop the whole session without the
  // user noticing.
  let decryptFails = 0;
  const DECRYPT_FAIL_ABORT = 5;
  while (currentSid === sid && !pollAbort.signal.aborted) {
    const _netT0 = performance.now();
    const _netCatchupSample = catchingUp;
    try {
      const params = { session: sid, since_seq: outNextSeq };
      if (catchingUp) params.limit = 500;
      const { status, body } = await TPSession.api(RELAY, "output", {
        params, signal: pollAbort.signal,
      });
      const _netDuration = performance.now() - _netT0;
      if (status === 401 || status === 403) {
        // Auth wedged. Tight-loop retrying every second burns the
        // user's data plan and the relay's quota; surface the problem
        // and stop until the user fixes credentials.
        setStatus("relay rejected our credentials (" + status + "). Reload after fixing.");
        return;
      }
      if (status !== 200) { setStatus("poll error: " + (body.error || status)); await sleep(1000); continue; }
      const records = body.records || [];
      const next = Number(body.next_seq || outNextSeq);
      const total = Number(body.total || next);
      if (firstResponse) {
        firstResponse = false;
        // Warm re-attach + long backlog: slide the loading overlay over the
        // stale cached HTML. Otherwise the DOM would sit on old content for
        // seconds during silent catch-up, then jump to fresh state — feels
        // like a delayed step-by-step update. The cached HTML stays in
        // place behind the opaque overlay, so a mid-catchup detach still
        // captures a valid logHtml.
        if (isWarm && catchingUp && (total - outNextSeq) > WARM_OVERLAY_BACKLOG) {
          showLogLoader("Loading session…", { determinate: true });
          updateLogLoaderProgress(outNextSeq, total);
        }
      }
      const chunks = [];
      let batchFails = 0;
      for (const rec of records) {
        const seq = Number(rec.seq);
        try {
          chunks.push(await TP.decryptB64(currentKey, rec.blob, TP.aadRecord("out", sid, seq)));
        } catch (e) {
          // Tag mismatch on a record — skip it but advance cursor.
          // OperationError is the WebCrypto-specific AEAD failure;
          // anything else is a decode/format problem worth distinguishing
          // in the badge below.
          batchFails++;
          decryptFails++;
          console.warn("decrypt failed for output seq", seq, e && e.name);
        }
      }
      if (records.length && chunks.length === 0) {
        // Every record in this batch failed. Show a visible warning.
        setStatus("⚠ decrypt failed for " + batchFails + " record(s) — token / session mismatch?");
      } else if (chunks.length) {
        decryptFails = 0;  // a successful decrypt clears the streak
      }
      if (decryptFails >= DECRYPT_FAIL_ABORT) {
        setStatus("⚠ aborted: " + decryptFails + " consecutive decrypt failures. "
                  + "Reload or re-attach with a different token.");
        return;
      }
      const caughtUp = next >= total;
      if (chunks.length) {
        const combined = _concatBytes(chunks);
        if (catchingUp && !caughtUp) {
          term.write(combined);  // suppress render: more batches coming
        } else {
          const wasCatchingUp = catchingUp;
          term.write(combined, () => {
            scheduleLogRender();
            if (wasCatchingUp) {
              // Hide the loader on the next frame so the freshly-rendered
              // log paints first and there's no flash of empty content.
              requestAnimationFrame(() => requestAnimationFrame(hideLogLoader));
            }
          });
          if (catchingUp) catchingUp = false;
        }
      } else if (catchingUp && caughtUp) {
        scheduleLogRender();
        requestAnimationFrame(() => requestAnimationFrame(hideLogLoader));
        catchingUp = false;
      }
      outNextSeq = next;
      // Drive the progress bar regardless of cold/warm path; no-op when
      // total is unknown or the loader is hidden. Monotonic clamp inside
      // updateLogLoaderProgress prevents stutter if total grows during
      // catch-up (live records arriving while we drain history).
      updateLogLoaderProgress(next, total);
      if (!_netCatchupSample) {
        // Sum of decrypted plaintext is a close-enough proxy for response
        // body size — the actual wire payload is plaintext + ~28 bytes of
        // AES-GCM nonce+tag per record + base64 overhead, all dominated
        // by plaintext for any record big enough to matter here.
        let _netBytes = 0;
        for (const c of chunks) _netBytes += c.length;
        _netRecord(_netDuration, records.length > 0, Math.max(0, total - next), _netBytes);
      }
    } catch (e) {
      if (pollAbort.signal.aborted) return;
      _netMarkFetchError();
      setStatus("poll error: " + e.message);
      await sleep(1000);
    }
  }
}

function sleep(ms){return new Promise(r=>setTimeout(r,ms));}

/* ===== Send input (encrypted) + offline queue ========================== */
function _bytesToB64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}
async function _postInputOnce(plain) {
  // Snapshot the session identity at the top. If the user switches to
  // a different session mid-await, the encrypted blob below was built
  // for the original sid/key/AAD; posting it under the new sid would
  // either land in the wrong stream or be rejected by the wrapper's
  // AEAD check after a successful relay accept. Bail rather than send
  // crossed input.
  const sid = currentSid;
  const key = currentKey;
  if (!sid || !key) return false;
  for (let attempt = 0; attempt < 2; attempt++) {
    if (currentSid !== sid || currentKey !== key) return false;
    const seq = inNextSeq;
    const blob = await TP.encryptB64(key, plain, TP.aadRecord("in", sid, seq));
    if (currentSid !== sid || currentKey !== key) return false;
    const _netT0 = performance.now();
    _netInflightStart();
    let status, body;
    try {
      ({ status, body } = await TPSession.api(RELAY, "input", {
        method: "POST",
        body: { session_id: sid, records: [{ seq, blob }] },
      }));
    } finally {
      _netInflightEnd();
    }
    const _netDuration = performance.now() - _netT0;
    if (currentSid !== sid) return false;
    if (status === 200) {
      // Input POST is short-poll, so duration is a pure RTT probe — feed
      // it to the slow-network detector so the banner can light up on
      // the user's first slow send instead of waiting for output to
      // round-trip back through the wrapper.
      _netRecord(_netDuration, true, 0);
      inNextSeq = Number(body.next_seq || seq + 1);
      TPSession.updateSessionNextSeq(sid, inNextSeq);
      return true;
    }
    if (status === 409 && typeof body.expected_seq === "number") {
      inNextSeq = body.expected_seq;
      TPSession.updateSessionNextSeq(sid, inNextSeq);
      continue;
    }
    setStatus("send failed: " + (body.error || status));
    return false;
  }
  return false;
}
async function sendInputBytes(plain) {
  if (!currentSid || !currentKey) return false;
  // If the session looks offline, queue without trying — the SW (or the
  // foreground refresher) will drain when connectivity returns.
  const offline = sessionMissing || !sessionAlive ||
                  TPSession.getConnState() === "offline";
  if (offline) {
    TPSession.enqueuePendingSend(currentSid, _bytesToB64(plain));
    setStatus("queued (offline) — will send when reconnected");
    return false;
  }
  try {
    const ok = await _postInputOnce(plain);
    if (!ok) {
      TPSession.enqueuePendingSend(currentSid, _bytesToB64(plain));
      setStatus("queued — relay rejected; will retry");
    }
    return ok;
  } catch (e) {
    _netMarkFetchError();
    TPSession.enqueuePendingSend(currentSid, _bytesToB64(plain));
    setStatus("queued (send error: " + e.message + ")");
    return false;
  }
}

// Foreground drainer for any messages queued while offline. Runs whenever
// the session looks healthy; the SW handles the closed-tab case.
let _termDrainInflight = false;
async function _drainTerminalQueue() {
  if (_termDrainInflight) return;
  if (!currentSid || !currentKey) return;
  if (sessionMissing || !sessionAlive) return;
  _termDrainInflight = true;
  try {
    const queue = TPSession.pendingSendsFor(currentSid);
    for (const entry of queue) {
      try {
        const plain = Uint8Array.from(atob(entry.plain_b64), c => c.charCodeAt(0));
        const ok = await _postInputOnce(plain);
        if (!ok) break;
        TPSession.dropPendingSend(currentSid, entry.id);
      } catch (e) { break; }
    }
  } finally { _termDrainInflight = false; }
}

document.getElementById("inputform").addEventListener("submit", e => {
  e.preventDefault();
  const v = document.getElementById("msg").value;
  if (!v) return;
  const bytes = new TextEncoder().encode(v + "\r");
  sendInputBytes(bytes);
  document.getElementById("msg").value = "";
});

/* ===== On-screen keyboard (favourites / groups / mod-latching) =========
 *
 * All keyboard logic lives in php/lib/keyboard.js. Here we just give
 * it a bytes-sink (the existing sendInputBytes) and clear its state
 * when the active session goes away. */
if (typeof TPKeyboard !== "undefined") TPKeyboard.init({ sendBytes: sendInputBytes });

document.getElementById("toggle-ctrl")?.addEventListener("click", () => {
  mainEl.classList.toggle("show-ctrl");
});

/* ===== Boot =========================================================== */
(async () => {
  authRequired = await TPSession.checkAuthRequired(RELAY);
  refreshSessions();
  // Polling refresh — paused while the tab is hidden so a backgrounded
  // PWA doesn't churn battery / data plan with 3 s polls forever.
  // visibilitychange relights it; an immediate refresh on visible makes
  // the UI feel fresh after the user comes back.
  let refreshTimer = null;
  function startRefresh() {
    if (refreshTimer == null) refreshTimer = setInterval(refreshSessions, 3000);
  }
  function stopRefresh() {
    if (refreshTimer != null) { clearInterval(refreshTimer); refreshTimer = null; }
  }
  startRefresh();
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { stopRefresh(); }
    else { refreshSessions(); startRefresh(); }
  });
})();

/* ===== Service worker (PWA install + update banner) ==================== */
// Register on load so the page paint isn't blocked. The SW caches the
// static shell only — relay/transcript traffic is never intercepted.
// On detected update we surface a banner with an UPDATE button; clicking
// it tells the waiting SW to skipWaiting() → controllerchange → reload.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", async () => {
    const banner = document.getElementById("termpilot-update-banner");
    const btn = document.getElementById("termpilot-update-btn");

    // No banner on first install: if there's no controller, the SW is
    // installing fresh and there's no "old version" to swap out.
    const hadControllerOnLoad = !!navigator.serviceWorker.controller;

    let waitingWorker = null;
    let updateRequested = false;
    let deferStart = 0;
    let deferTimer = 0;
    const MAX_DEFER_MS = 5 * 60 * 1000;
    const DEFER_RECHECK_MS = 30 * 1000;

    const isInputActive = () => {
      const el = document.activeElement;
      if (!el || el === document.body) return false;
      if (el.isContentEditable) return true;
      const tag = el.tagName;
      return tag === "INPUT" || tag === "TEXTAREA";
    };

    const presentBanner = (worker) => {
      if (!hadControllerOnLoad) return;
      waitingWorker = worker;
      if (isInputActive()) {
        if (!deferStart) deferStart = Date.now();
        if (Date.now() - deferStart < MAX_DEFER_MS) {
          clearTimeout(deferTimer);
          deferTimer = setTimeout(() => presentBanner(worker), DEFER_RECHECK_MS);
          return;
        }
        // Cap exceeded — show anyway.
      }
      banner.hidden = false;
    };

    btn.addEventListener("click", () => {
      if (!waitingWorker) return;
      updateRequested = true;
      btn.disabled = true;
      btn.textContent = "UPDATING…";
      waitingWorker.postMessage({ type: "SKIP_WAITING" });
    });

    // Reload only when *we* asked for the update. Otherwise (e.g. fresh
    // install where activate's clients.claim() fires controllerchange),
    // an unsolicited reload would confuse the user.
    let refreshing = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (!updateRequested || refreshing) return;
      refreshing = true;
      location.reload();
    });

    let registration;
    try {
      registration = await navigator.serviceWorker.register("./sw.js");
    } catch (e) {
      console.warn("SW registration failed:", e);
      return;
    }

    // (a) Worker already waiting from a prior tab.
    if (registration.waiting) presentBanner(registration.waiting);

    // (b) New install detected during this page's lifetime.
    registration.addEventListener("updatefound", () => {
      const installing = registration.installing;
      if (!installing) return;
      installing.addEventListener("statechange", () => {
        if (installing.state === "installed" && navigator.serviceWorker.controller) {
          presentBanner(installing);
        }
      });
    });

    // (c) Periodic update-check. update() forces a re-fetch of sw.js,
    // bypassing the browser's 24 h default check interval.
    const poll = () => { try { registration.update(); } catch (e) {} };
    setInterval(poll, 10 * 60 * 1000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) poll();
    });
    window.addEventListener("focus", poll);
  });
}
