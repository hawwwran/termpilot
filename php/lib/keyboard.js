/**
 * TermPilot on-screen keyboard.
 *
 * A two-tier soft keyboard for the terminal view:
 *   - fav row: user-curated favourites, always visible by default,
 *     wraps automatically. Leftmost is `+` (open groups). Rightmost is
 *     a small edit button (open reorder modal).
 *   - groups row: 6 group buttons (Nav, Fx, =<, 0-9, a-z, Mod). Drilling
 *     into a group replaces the strip with that group's keys.
 *
 * Modifier keys (Mod group) latch. Latched mods appear in a separate
 * bar above the strip until consumed by sending a non-mod key, toggled
 * off, cleared via Cancel, or the session is hidden.
 *
 * Favourites identity is (sorted-mods, baseKeyId). `c` plain and
 * `Ctrl+C` are two different favourites. Tapping a favourite UNIONs
 * its stored mods with the currently-latched mods and sends that
 * combo — so mod latching composes with favourites naturally.
 *
 * Public surface (window.TPKeyboard):
 *   init({ sendBytes })     — set up DOM hooks, restore favourites
 *   onSessionHidden()       — clear latched mods + reset to fav view
 */
(function () {
  'use strict';

  // ===== Constants ========================================================

  // Modifier order is fixed so labels and stored mod arrays are stable.
  const MOD_ORDER = ['ctrl', 'alt', 'shift', 'super'];
  const MOD_ABBR  = { ctrl:'CT', alt:'AL', shift:'SH', super:'SP' };
  const MOD_LABEL = { ctrl:'Ctrl', alt:'Alt', shift:'Shift', super:'Super' };

  // Long-press timing. 500ms is the de-facto standard for mobile UIs.
  const LONG_PRESS_MS = 500;
  // Tolerate this many pixels of finger drift during a long-press before
  // we conclude the user meant to scroll/swipe and abort.
  const LONG_PRESS_MOVE_TOL = 10;

  const FAV_STORAGE_KEY = 'termpilot-kbd-favorites';

  // ===== Group / key definitions ==========================================
  //
  // Each key descriptor:
  //   id       — stable identifier (also the lookup key)
  //   label    — displayed text/glyph
  //   plain    — bytes to send when no Shift latched
  //   shift    — bytes to send with Shift latched (optional; for letters
  //              we auto-uppercase if not specified)
  //   isMod    — modifier marker (only present in the Mod group)

  const GROUPS = {
    Nav: [
      { id:'Left',  label:'←',    plain:'\x1b[D' },
      { id:'Up',    label:'↑',    plain:'\x1b[A' },
      { id:'Down',  label:'↓',    plain:'\x1b[B' },
      { id:'Right', label:'→',    plain:'\x1b[C' },
      { id:'Enter', label:'Enter',plain:'\r' },
      { id:'Esc',   label:'Esc',  plain:'\x1b' },
      { id:'Tab',   label:'Tab',  plain:'\t', shift:'\x1b[Z' },
      { id:'Backspace', label:'⌫', plain:'\x7f' },
      { id:'Home',  label:'Home', plain:'\x1b[H' },
      { id:'End',   label:'End',  plain:'\x1b[F' },
      { id:'PgUp',  label:'PgUp', plain:'\x1b[5~' },
      { id:'PgDn',  label:'PgDn', plain:'\x1b[6~' },
      { id:'Del',   label:'Del',  plain:'\x1b[3~' },
      { id:'Ins',   label:'Ins',  plain:'\x1b[2~' },
    ],
    'Fx': [
      { id:'F1',  label:'F1',  plain:'\x1bOP' },
      { id:'F2',  label:'F2',  plain:'\x1bOQ' },
      { id:'F3',  label:'F3',  plain:'\x1bOR' },
      { id:'F4',  label:'F4',  plain:'\x1bOS' },
      { id:'F5',  label:'F5',  plain:'\x1b[15~' },
      { id:'F6',  label:'F6',  plain:'\x1b[17~' },
      { id:'F7',  label:'F7',  plain:'\x1b[18~' },
      { id:'F8',  label:'F8',  plain:'\x1b[19~' },
      { id:'F9',  label:'F9',  plain:'\x1b[20~' },
      { id:'F10', label:'F10', plain:'\x1b[21~' },
    ],
    '=<': [
      // Symbols not on the digit row (or whose Shift form lives on
      // a separate physical key). Each sends its literal character.
      { id:'sym_grave',    label:'`',  plain:'`' },
      { id:'sym_tilde',    label:'~',  plain:'~' },
      { id:'sym_excl',     label:'!',  plain:'!' },
      { id:'sym_at',       label:'@',  plain:'@' },
      { id:'sym_hash',     label:'#',  plain:'#' },
      { id:'sym_dollar',   label:'$',  plain:'$' },
      { id:'sym_pct',      label:'%',  plain:'%' },
      { id:'sym_caret',    label:'^',  plain:'^' },
      { id:'sym_amp',      label:'&',  plain:'&' },
      { id:'sym_lparen',   label:'(',  plain:'(' },
      { id:'sym_rparen',   label:')',  plain:')' },
      { id:'sym_lbracket', label:'[',  plain:'[' },
      { id:'sym_rbracket', label:']',  plain:']' },
      { id:'sym_lbrace',   label:'{',  plain:'{' },
      { id:'sym_rbrace',   label:'}',  plain:'}' },
      { id:'sym_bslash',   label:'\\', plain:'\\' },
      { id:'sym_pipe',     label:'|',  plain:'|' },
      { id:'sym_semi',     label:';',  plain:';' },
      { id:'sym_colon',    label:':',  plain:':' },
      { id:'sym_squote',   label:"'",  plain:"'" },
      { id:'sym_dquote',   label:'"',  plain:'"' },
      { id:'sym_comma',    label:',',  plain:',' },
      { id:'sym_dot',      label:'.',  plain:'.' },
      { id:'sym_lt',       label:'<',  plain:'<' },
      { id:'sym_gt',       label:'>',  plain:'>' },
      { id:'sym_qm',       label:'?',  plain:'?' },
    ],
    '0-9': [
      { id:'d0',     label:'0', plain:'0', shift:')' },
      { id:'d1',     label:'1', plain:'1', shift:'!' },
      { id:'d2',     label:'2', plain:'2', shift:'@' },
      { id:'d3',     label:'3', plain:'3', shift:'#' },
      { id:'d4',     label:'4', plain:'4', shift:'$' },
      { id:'d5',     label:'5', plain:'5', shift:'%' },
      { id:'d6',     label:'6', plain:'6', shift:'^' },
      { id:'d7',     label:'7', plain:'7', shift:'&' },
      { id:'d8',     label:'8', plain:'8', shift:'*' },
      { id:'d9',     label:'9', plain:'9', shift:'(' },
      // Quick-access operators per the user's spec.
      { id:'op_minus', label:'-', plain:'-', shift:'_' },
      { id:'op_plus',  label:'+', plain:'+' },
      { id:'op_eq',    label:'=', plain:'=', shift:'+' },
      { id:'op_slash', label:'/', plain:'/', shift:'?' },
      { id:'op_star',  label:'*', plain:'*' },
    ],
    'a-z': (function () {
      const out = [];
      for (let c = 97; c <= 122; c++) {
        const ch = String.fromCharCode(c);
        out.push({ id:'letter_' + ch, label:ch, plain:ch });
      }
      return out;
    })(),
    Mod: [
      // Modifiers don't send bytes — tapping toggles the latch.
      { id:'ctrl',  label:'Ctrl',  isMod:true },
      { id:'alt',   label:'Alt',   isMod:true },
      { id:'super', label:'Super', isMod:true },
      { id:'shift', label:'Shift', isMod:true },
    ],
  };

  const GROUP_ORDER = ['Nav', 'Fx', '=<', '0-9', 'a-z', 'Mod'];

  // First-run favourites: Enter, Esc, Tab, plus the four arrows.
  const DEFAULT_FAVS = [
    { mods:[], baseKeyId:'Enter' },
    { mods:[], baseKeyId:'Esc' },
    { mods:[], baseKeyId:'Tab' },
    { mods:[], baseKeyId:'Left' },
    { mods:[], baseKeyId:'Up' },
    { mods:[], baseKeyId:'Down' },
    { mods:[], baseKeyId:'Right' },
  ];

  // ===== Lookup helpers ===================================================

  function findKey(baseKeyId) {
    for (const g of GROUP_ORDER) {
      const k = GROUPS[g].find(k => k.id === baseKeyId);
      if (k) return k;
    }
    return null;
  }

  function sortMods(mods) {
    return MOD_ORDER.filter(m => mods.includes(m));
  }

  function modsEq(a, b) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  // ===== Byte mapping =====================================================
  //
  // Compose the byte sequence for a (mods, baseKey) combo. Order of
  // application: Shift → Ctrl → Alt prefix. Super is accepted but does
  // not currently transform the bytes (terminal support is too uneven
  // for a one-size-fits-all encoding — documented in the README).

  function computeBytes(mods, baseKey) {
    if (!baseKey || baseKey.isMod) return null;
    const hasShift = mods.includes('shift');
    const hasCtrl  = mods.includes('ctrl');
    const hasAlt   = mods.includes('alt');

    // 1. Shift: explicit `shift` field wins; else uppercase single
    //    lowercase letters; else fall back to the plain bytes.
    let bytes;
    if (hasShift && baseKey.shift) {
      bytes = baseKey.shift;
    } else if (hasShift && baseKey.plain.length === 1 &&
               baseKey.plain >= 'a' && baseKey.plain <= 'z') {
      bytes = baseKey.plain.toUpperCase();
    } else {
      bytes = baseKey.plain;
    }

    // 2. Ctrl: meaningful for single-character bytes only. Ctrl-A..Z
    //    maps to 0x01..0x1A. A few other classic combinations have
    //    canonical bytes too. Anything else: pass through unchanged.
    if (hasCtrl && bytes.length === 1) {
      const c = bytes.charCodeAt(0);
      if (c >= 0x41 && c <= 0x5A)       bytes = String.fromCharCode(c - 0x40); // Ctrl-A..Z
      else if (c >= 0x61 && c <= 0x7A)  bytes = String.fromCharCode(c - 0x60); // Ctrl-a..z
      else if (bytes === ' ')           bytes = '\x00'; // Ctrl-Space
      else if (bytes === '[')           bytes = '\x1b'; // Ctrl-[ = ESC
      else if (bytes === '\\')          bytes = '\x1c';
      else if (bytes === ']')           bytes = '\x1d';
      else if (bytes === '^')           bytes = '\x1e';
      else if (bytes === '_')           bytes = '\x1f';
      else if (bytes === '?')           bytes = '\x7f';
    }

    // 3. Alt: prefix with ESC (xterm convention).
    if (hasAlt) bytes = '\x1b' + bytes;

    return bytes;
  }

  function strToBytes(s) {
    const out = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i) & 0xff;
    return out;
  }

  // ===== Module state =====================================================

  let viewState   = 'fav';      // 'fav' | 'groups' | 'in:<groupName>'
  let latchedMods = new Set();  // active modifier ids
  let favorites   = [];         // [{mods:[...], baseKeyId:'...'}]
  let sendBytesFn = null;
  let stripEl     = null;
  let latchedEl   = null;

  // ===== Favourites persistence ==========================================

  function loadFavorites() {
    try {
      const raw = localStorage.getItem(FAV_STORAGE_KEY);
      if (raw) {
        const arr = JSON.parse(raw);
        if (Array.isArray(arr)) {
          favorites = arr
            .filter(f => f && typeof f.baseKeyId === 'string' && Array.isArray(f.mods))
            .filter(f => findKey(f.baseKeyId)) // drop favourites whose key was removed in an update
            .map(f => ({ mods: sortMods(f.mods.filter(m => MOD_ORDER.includes(m))),
                         baseKeyId: f.baseKeyId }));
          return;
        }
      }
    } catch (_) { /* fall through */ }
    favorites = DEFAULT_FAVS.map(f => ({ mods: f.mods.slice(), baseKeyId: f.baseKeyId }));
    saveFavorites();
  }

  function saveFavorites() {
    try { localStorage.setItem(FAV_STORAGE_KEY, JSON.stringify(favorites)); }
    catch (_) { /* full storage; tolerable, in-memory copy still works */ }
  }

  function favIndex(mods, baseKeyId) {
    const sorted = sortMods(mods);
    return favorites.findIndex(f =>
      f.baseKeyId === baseKeyId && modsEq(f.mods, sorted));
  }

  function addFavorite(mods, baseKeyId) {
    if (favIndex(mods, baseKeyId) >= 0) return false;
    favorites.push({ mods: sortMods(mods), baseKeyId });
    saveFavorites();
    return true;
  }

  function removeFavoriteAt(idx) {
    if (idx < 0 || idx >= favorites.length) return false;
    favorites.splice(idx, 1);
    saveFavorites();
    return true;
  }

  function moveFavorite(idx, delta) {
    const dst = idx + delta;
    if (idx < 0 || idx >= favorites.length || dst < 0 || dst >= favorites.length) return false;
    const [item] = favorites.splice(idx, 1);
    favorites.splice(dst, 0, item);
    saveFavorites();
    return true;
  }

  // ===== Labels ===========================================================

  function favLabel(fav) {
    const baseKey = findKey(fav.baseKeyId);
    if (!baseKey) return '?';
    if (fav.mods.length === 0) return baseKey.label;
    return fav.mods.map(m => MOD_ABBR[m]).join('+') + '+' + baseKey.label;
  }

  // ===== Sending ==========================================================

  // Tapping a non-mod key. Composes the current latched mods (consumed
  // afterwards) with extraMods (used by favourites to add their stored
  // mods on top of whatever is currently latched).
  function sendCombo(baseKey, extraMods) {
    const mods = sortMods([
      ...latchedMods,
      ...(extraMods || []),
    ]);
    const bytes = computeBytes(mods, baseKey);
    if (!bytes) return;
    if (sendBytesFn) sendBytesFn(strToBytes(bytes));
    // Consume latched mods. Stored fav mods are not state, just identity.
    latchedMods.clear();
    showFavorites();
  }

  // ===== Modifier latching ===============================================

  function toggleMod(modId) {
    if (latchedMods.has(modId)) latchedMods.delete(modId);
    else latchedMods.add(modId);
    render();
  }

  function clearAllMods() {
    latchedMods.clear();
    render();
  }

  // ===== State transitions ===============================================

  function setView(state) { viewState = state; render(); }
  function showFavorites() { setView('fav'); }
  function showGroups()    { setView('groups'); }
  function showGroup(name) { setView('in:' + name); }

  function viewIsGroup() { return viewState.startsWith('in:'); }
  function viewGroupName() { return viewIsGroup() ? viewState.slice(3) : null; }

  // ===== Rendering ========================================================

  function render() {
    cancelActivePress();
    renderLatched();
    renderStrip();
  }

  function renderLatched() {
    if (!latchedEl) return;
    if (latchedMods.size === 0) {
      latchedEl.hidden = true;
      latchedEl.innerHTML = '';
      return;
    }
    latchedEl.hidden = false;
    const sorted = sortMods([...latchedMods]);
    const frag = document.createDocumentFragment();
    for (const m of sorted) {
      const pill = document.createElement('button');
      pill.type = 'button';
      pill.className = 'kbd-latched-pill';
      pill.textContent = MOD_LABEL[m];
      pill.title = 'Tap to unlatch ' + MOD_LABEL[m];
      pill.addEventListener('click', () => { latchedMods.delete(m); render(); });
      frag.appendChild(pill);
    }
    const spacer = document.createElement('span');
    spacer.className = 'kbd-latched-spacer';
    frag.appendChild(spacer);
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'kbd-cancel';
    cancel.textContent = 'Cancel';
    cancel.title = 'Clear all latched modifiers';
    cancel.addEventListener('click', clearAllMods);
    frag.appendChild(cancel);
    latchedEl.replaceChildren(frag);
  }

  function renderStrip() {
    if (!stripEl) return;
    stripEl.replaceChildren();
    if (viewState === 'fav')      renderFavRow();
    else if (viewState === 'groups') renderGroupsRow();
    else if (viewIsGroup())          renderInGroupRow(viewGroupName());
  }

  function makeBtn(label, cls, opts) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = cls;
    b.textContent = label;
    if (opts && opts.title) b.title = opts.title;
    return b;
  }

  function renderFavRow() {
    const more = makeBtn('+', 'kbd-nav', { title:'More keys' });
    more.addEventListener('click', showGroups);
    stripEl.appendChild(more);
    favorites.forEach((fav, idx) => {
      const btn = makeBtn(favLabel(fav), 'kbd-fav');
      attachLongPress(btn, {
        onTap: () => {
          const baseKey = findKey(fav.baseKeyId);
          if (baseKey) sendCombo(baseKey, fav.mods);
        },
        onLong: () => {
          removeFavoriteAt(idx);
          render();
        },
      });
      stripEl.appendChild(btn);
    });
    if (favorites.length > 0) {
      const edit = makeBtn('⋮', 'kbd-edit-fav', { title:'Reorder favourites' });
      edit.addEventListener('click', openReorderModal);
      stripEl.appendChild(edit);
    }
  }

  function renderGroupsRow() {
    const back = makeBtn('←', 'kbd-nav', { title:'Back to favourites' });
    back.addEventListener('click', showFavorites);
    stripEl.appendChild(back);
    for (const g of GROUP_ORDER) {
      const btn = makeBtn(g, 'kbd-group');
      btn.addEventListener('click', () => showGroup(g));
      stripEl.appendChild(btn);
    }
  }

  function renderInGroupRow(groupName) {
    const back = makeBtn('←', 'kbd-nav', { title:'Back to groups' });
    back.addEventListener('click', showGroups);
    stripEl.appendChild(back);
    const keys = GROUPS[groupName] || [];
    const isModGroup = groupName === 'Mod';
    for (const key of keys) {
      const btn = makeBtn(key.label, isModGroup ? 'kbd-key kbd-mod' : 'kbd-key');
      if (isModGroup && latchedMods.has(key.id)) btn.classList.add('latched');
      // Favourite marker: only when the (latched-mods, this-key) combo
      // is in favourites. Mod keys are never favouritable (they latch,
      // they don't send bytes).
      if (!isModGroup && favIndex([...latchedMods], key.id) >= 0) {
        btn.classList.add('favorited');
      }
      if (isModGroup) {
        btn.addEventListener('click', () => toggleMod(key.id));
      } else {
        attachLongPress(btn, {
          onTap:  () => sendCombo(key),
          onLong: () => {
            addFavorite([...latchedMods], key.id);
            // Mods stay latched so the user can immediately favourite
            // another combo with the same prefix. Returning to the fav
            // view confirms the addition visually.
            showFavorites();
          },
        });
      }
      stripEl.appendChild(btn);
    }
  }

  // ===== Long-press handler ==============================================
  //
  // One shared press-state slot — only one button can be pressed at a
  // time on a single-touch keyboard, and ignoring concurrent touches
  // also prevents weird multi-finger interactions.

  let activePress = null; // { btn, x, y, timer, fired, onTap, onLong }

  function cancelActivePress() {
    if (!activePress) return;
    clearTimeout(activePress.timer);
    activePress.btn.classList.remove('long-pressing');
    activePress = null;
  }

  function attachLongPress(btn, opts) {
    btn.addEventListener('pointerdown', (e) => {
      // Suppress synthetic mouse events + iOS callout. preventDefault
      // here also prevents the click event from firing later, which is
      // why we explicitly call onTap in pointerup.
      e.preventDefault();
      cancelActivePress();
      activePress = {
        btn, x: e.clientX, y: e.clientY, fired: false,
        onTap: opts.onTap, onLong: opts.onLong,
      };
      activePress.timer = setTimeout(() => {
        if (!activePress || activePress.btn !== btn) return;
        activePress.fired = true;
        btn.classList.add('long-pressing');
        // Fire immediately on long-press recognition so the user gets
        // feedback without waiting for finger-up.
        try { activePress.onLong && activePress.onLong(); }
        finally { cancelActivePress(); }
      }, LONG_PRESS_MS);
    });
    btn.addEventListener('pointermove', (e) => {
      if (!activePress || activePress.btn !== btn) return;
      const dx = e.clientX - activePress.x;
      const dy = e.clientY - activePress.y;
      if (dx * dx + dy * dy > LONG_PRESS_MOVE_TOL * LONG_PRESS_MOVE_TOL) {
        cancelActivePress();
      }
    });
    btn.addEventListener('pointerup', (e) => {
      if (!activePress || activePress.btn !== btn) return;
      clearTimeout(activePress.timer);
      const fired = activePress.fired;
      const onTap = activePress.onTap;
      cancelActivePress();
      if (!fired && onTap) onTap();
    });
    btn.addEventListener('pointercancel', cancelActivePress);
    btn.addEventListener('pointerleave', (e) => {
      // Treat leave as cancel only if we're tracking this button.
      if (!activePress || activePress.btn !== btn) return;
      cancelActivePress();
    });
  }

  // ===== Reorder modal ===================================================

  let openModalBackdrop = null;

  function openReorderModal() {
    // If something externally removed our backdrop (e.g. a parent
    // .modal-backdrop wipe in a test or container reset), clear the
    // stale handle so we can re-open instead of getting wedged.
    if (openModalBackdrop && !document.body.contains(openModalBackdrop)) {
      openModalBackdrop = null;
    }
    if (openModalBackdrop) return;
    const back = document.createElement('div');
    back.className = 'modal-backdrop';
    const modal = document.createElement('div');
    modal.className = 'modal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-label', 'Reorder favourites');
    modal.innerHTML = `
      <h2>Reorder favourites</h2>
      <p>Tap ↑ / ↓ to swap a favourite with its neighbour. Changes apply immediately.</p>
      <div class="kbd-reorder-list"></div>
      <div class="row actions"><button type="button" class="kbd-reorder-ok">OK</button></div>
    `;
    back.appendChild(modal);
    document.body.appendChild(back);
    openModalBackdrop = back;

    const listEl = modal.querySelector('.kbd-reorder-list');
    function renderList() {
      listEl.replaceChildren();
      favorites.forEach((fav, idx) => {
        const row = document.createElement('div');
        row.className = 'kbd-reorder-row';
        const up = document.createElement('button');
        up.type = 'button';
        up.textContent = '↑';
        up.disabled = idx === 0;
        up.addEventListener('click', () => {
          moveFavorite(idx, -1);
          renderList();
          render();
        });
        const lbl = document.createElement('div');
        lbl.className = 'kbd-reorder-label';
        lbl.textContent = favLabel(fav);
        const down = document.createElement('button');
        down.type = 'button';
        down.textContent = '↓';
        down.disabled = idx === favorites.length - 1;
        down.addEventListener('click', () => {
          moveFavorite(idx, +1);
          renderList();
          render();
        });
        row.append(up, lbl, down);
        listEl.appendChild(row);
      });
      if (favorites.length === 0) {
        const empty = document.createElement('div');
        empty.style.color = '#9aa';
        empty.style.fontSize = '12px';
        empty.style.padding = '8px';
        empty.textContent = 'No favourites yet. Long-press a key inside a group to favourite it.';
        listEl.appendChild(empty);
      }
    }
    renderList();

    function close() {
      back.remove();
      openModalBackdrop = null;
    }
    modal.querySelector('.kbd-reorder-ok').addEventListener('click', close);
    back.addEventListener('click', (e) => { if (e.target === back) close(); });
  }

  // ===== Public surface ==================================================

  function init(opts) {
    stripEl   = document.getElementById('kbd-strip');
    latchedEl = document.getElementById('kbd-latched');
    if (!stripEl || !latchedEl) return;
    sendBytesFn = opts && opts.sendBytes;
    loadFavorites();
    render();
  }

  function onSessionHidden() {
    // Whenever the active session goes away, drop modifier state so we
    // don't surprise the user when they come back.
    if (latchedMods.size > 0) latchedMods.clear();
    if (viewState !== 'fav') showFavorites();
    else render();
  }

  window.TPKeyboard = {
    init,
    onSessionHidden,
  };
})();
