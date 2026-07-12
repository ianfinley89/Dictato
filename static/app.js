'use strict';

// ── API helper ──────────────────────────────────────────────────────────────
const api = {
  async _req(method, path, body) {
    const opts = { method, credentials: 'same-origin' };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(path, opts);
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || 'Request failed');
    }
    return r.json();
  },
  get: (path) => api._req('GET', path),
  post: (path, body) => api._req('POST', path, body),
  put: (path, body) => api._req('PUT', path, body),
  del: (path) => api._req('DELETE', path),
};

// ── State ───────────────────────────────────────────────────────────────────
const state = {
  user: null,
  dayLog: [],         // entries for the currently-viewed day
  viewDate: null,     // local YYYY-MM-DD being viewed
  searchResults: [],
  pendingFood: null,
};

// ── DOM refs ─────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const authScreen = $('auth-screen');
const appScreen = $('app-screen');
const loginForm = $('login-form');
const registerForm = $('register-form');
const confirmOverlay = $('confirm-overlay');
const confirmQty = $('confirm-qty');
const confirmServings = $('confirm-servings');
const confirmServingsRow = $('confirm-servings-row');
const confirmGramsRow = $('confirm-grams-row');
const confirmServingEq = $('confirm-serving-eq');
const searchInput = $('food-search');
const searchResults = $('search-results');
const voiceBtn = $('voice-btn');
const voiceStatus = $('voice-status');
const photoBtn = $('photo-btn');
const photoInput = $('photo-input');
const logList = $('log-list');
const toast = $('toast');
const appScreenEl = $('app-screen');

// ── Inline SVG icons (stroke-based, sized by font, inherit currentColor) ─────
const ICONS = {
  mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/>',
  camera: '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>',
  pencil: '<path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/>',
  users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  drop: '<path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/>',
  x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
  star: '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
  db: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
};

function icon(name, cls = '') {
  return `<svg class="icon${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;
}

// ── Toast ───────────────────────────────────────────────────────────────────
let toastTimer;
function showToast(msg, type = '') {
  toast.textContent = msg;
  toast.className = `show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = ''; }, 3000);
}

// ── Auth tab switching ────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const target = tab.dataset.tab;
    loginForm.classList.toggle('hidden', target !== 'login');
    registerForm.classList.toggle('hidden', target !== 'register');
  });
});

loginForm.addEventListener('submit', async e => {
  e.preventDefault();
  const errEl = $('login-error');
  errEl.classList.add('hidden');
  try {
    state.user = await api.post('/api/auth/login', {
      email: $('login-email').value,
      password: $('login-password').value,
    });
    showApp();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove('hidden');
  }
});

registerForm.addEventListener('submit', async e => {
  e.preventDefault();
  const pw = $('reg-password').value;
  const errEl = $('reg-error');
  errEl.classList.add('hidden');
  if (pw.length < 8) {
    errEl.textContent = 'Password must be at least 8 characters.';
    errEl.classList.remove('hidden');
    return;
  }
  try {
    state.user = await api.post('/api/auth/register', {
      email: $('reg-email').value,
      password: pw,
      display_name: $('reg-name').value,
    });
    showApp();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove('hidden');
  }
});

$('logout-btn').addEventListener('click', async () => {
  await api.post('/api/auth/logout', {});
  leaveApp();
});

// ── App init ─────────────────────────────────────────────────────────────────
async function showApp() {
  authScreen.classList.add('hidden');
  $('topnav').classList.remove('hidden');
  // Login/register responses carry only id+name; fetch the full profile
  // (email for the account pane, goals for the ring).
  try { state.user = await api.get('/api/auth/me'); } catch { /* keep what we have */ }
  $('admin-nav-btn').classList.toggle('hidden', !(state.user && state.user.is_admin));
  await showPane('home');
  handleLaunchAction();
}

// Home-screen shortcuts (manifest `shortcuts`, or a bookmarked /?action=…) jump
// straight into a capture. The launch tap usually counts as the gesture the
// mic/camera need; if the browser still blocks it, the buttons are right there.
function handleLaunchAction() {
  const action = new URLSearchParams(location.search).get('action');
  if (!action) return;
  history.replaceState({}, '', location.pathname);   // don't re-fire on refresh
  if (action === 'voice') startVoiceCapture();
  else if (action === 'photo') photoInput.click();
}

// ── Day navigation ────────────────────────────────────────────────────────────
function localDateStr(d = new Date()) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

async function goToToday() {
  state.viewDate = localDateStr();
  await loadDayLog();
  loadQuickPicks();   // recents/favorites refresh as the day changes
}

function shiftDay(delta) {
  const d = new Date(state.viewDate + 'T00:00');
  d.setDate(d.getDate() + delta);
  const next = localDateStr(d);
  if (next > localDateStr()) return;   // never navigate into the future
  state.viewDate = next;
  loadDayLog();
}

function renderDayNav() {
  const today = localDateStr();
  const label = $('day-label');
  if (state.viewDate === today) {
    label.textContent = 'Today';
  } else {
    const d = new Date(state.viewDate + 'T00:00');
    const yest = new Date(); yest.setDate(yest.getDate() - 1);
    label.textContent = (state.viewDate === localDateStr(yest))
      ? 'Yesterday'
      : d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
  }
  $('day-next').disabled = state.viewDate >= today;
}

$('day-prev').addEventListener('click', () => shiftDay(-1));
$('day-next').addEventListener('click', () => shiftDay(1));

// ── Manual add (search) panel — tucked behind a toggle; voice/photo lead ─────
$('manual-toggle').addEventListener('click', () => {
  const panel = $('manual-panel');
  const open = panel.classList.toggle('hidden');
  $('manual-toggle').textContent = open ? 'Add manually instead' : 'Hide manual add';
  if (!open) searchInput.focus();
  else { hideSearch(); searchInput.value = ''; }
});

// ── Food search ───────────────────────────────────────────────────────────────
let searchDebounce;

searchInput.addEventListener('input', () => {
  const q = searchInput.value.trim();
  clearTimeout(searchDebounce);
  if (q.length < 2) { hideSearch(); return; }
  showSearchLoading();
  searchDebounce = setTimeout(() => doSearch(q), 350);
});

searchInput.addEventListener('keydown', e => {
  if (e.key === 'Escape') { hideSearch(); searchInput.value = ''; }
});

document.addEventListener('click', e => {
  if (!e.target.closest('#input-area')) hideSearch();
});

function showSearchLoading() {
  searchResults.innerHTML = '<div class="search-loading">Searching…</div>';
  searchResults.classList.remove('hidden');
}

function hideSearch() {
  searchResults.classList.add('hidden');
}

async function doSearch(q) {
  try {
    const results = await api.get(`/api/foods/search?q=${encodeURIComponent(q)}`);
    state.searchResults = results;
    renderSearchResults(results);
  } catch (err) {
    searchResults.innerHTML = `<div class="search-loading">Error: ${esc(err.message)}</div>`;
  }
}

function renderSearchResults(results) {
  if (!results.length) {
    searchResults.innerHTML = '<div class="search-loading">No results found.</div>';
    searchResults.classList.remove('hidden');
    return;
  }
  searchResults.innerHTML = results.map((f, i) => {
    const cal = (f.nutrients_per_100g.calories || 0).toFixed(0);
    const brand = f.brand ? ` · ${esc(f.brand)}` : '';
    return `<div class="search-item" data-idx="${i}">
      <div class="s-name">${esc(f.name)}</div>
      <div class="s-meta">${cal} cal/100g${brand}</div>
    </div>`;
  }).join('');
  searchResults.classList.remove('hidden');

  searchResults.querySelectorAll('.search-item').forEach(el => {
    el.addEventListener('click', () => {
      // Manual pick: default to 1 serving when known, else 100 g.
      openConfirm(state.searchResults[+el.dataset.idx], null, 'manual', 1);
      hideSearch();
      searchInput.value = '';
    });
  });
}

// ── Single-item confirm panel ─────────────────────────────────────────────────
let _confirmSource = 'manual';
let _servingMode = false;   // true when the food has a known serving size

// `defaultGrams`/`defaultServings` may both be provided; serving size decides which
// input leads. Logging always sends grams (kept in sync from servings).
function openConfirm(food, defaultGrams = 100, source = 'manual', defaultServings = null, summary = '', photoUrl = '') {
  _confirmSource = source;
  setPolaroid($('confirm-photo'), photoUrl);
  setAiSummary($('confirm-ai-summary'), summary);
  $('confirm-picker').classList.add('hidden');   // reset any open picker
  applyConfirmFood(food, defaultGrams, defaultServings);
  confirmOverlay.classList.remove('hidden');
  const lead = _servingMode ? confirmServings : confirmQty;
  lead.focus();
  lead.select();
}

// Render a food (and its serving/grams inputs) into the single confirm panel.
// Reused both on open and when the user swaps to a different food.
function applyConfirmFood(food, defaultGrams = 100, defaultServings = null) {
  state.pendingFood = food;
  $('confirm-food-name').textContent = food.name;
  $('confirm-food-brand').textContent = food.brand || '';
  const srcEl = $('confirm-source');
  const srcLabel = foodSourceLabel(food.source);
  srcEl.innerHTML = srcLabel ? `${icon('db')} Nutrition from ${esc(srcLabel)}` : '';
  srcEl.classList.toggle('hidden', !srcLabel);
  renderFavStar();

  _servingMode = !!food.serving_g;
  confirmServingsRow.classList.toggle('hidden', !_servingMode);
  confirmGramsRow.classList.toggle('secondary', _servingMode);

  if (_servingMode) {
    const servings = defaultServings != null ? defaultServings
                   : defaultGrams ? defaultGrams / food.serving_g : 1;
    confirmServings.value = _trimNum(servings);
    confirmQty.value = Math.round(servings * food.serving_g);
  } else {
    confirmQty.value = Math.round(defaultGrams || 100);
  }
  refreshConfirmMacros();
}

// Servings → grams
confirmServings.addEventListener('input', () => {
  const f = state.pendingFood;
  if (!f || !_servingMode) return;
  confirmQty.value = Math.round((parseFloat(confirmServings.value) || 0) * f.serving_g);
  refreshConfirmMacros();
});

// Grams → servings (keep both in sync)
confirmQty.addEventListener('input', () => {
  const f = state.pendingFood;
  if (f && _servingMode && f.serving_g) {
    confirmServings.value = _trimNum((parseFloat(confirmQty.value) || 0) / f.serving_g);
  }
  refreshConfirmMacros();
});

function refreshConfirmMacros() {
  const food = state.pendingFood;
  if (!food) return;
  const n = food.nutrients_per_100g;
  const grams = parseFloat(confirmQty.value) || 0;
  const f = grams / 100;

  if (_servingMode) {
    const label = food.serving_desc ? ` · ${food.serving_desc}` : '';
    confirmServingEq.textContent = `= ${Math.round(grams)} g${label}`;
  }
  $('confirm-macros').textContent =
    `${((n.calories || 0) * f).toFixed(0)} cal · ` +
    `P ${((n.protein_g || 0) * f).toFixed(1)}g · ` +
    `C ${((n.carbs_g || 0) * f).toFixed(1)}g · ` +
    `F ${((n.fat_g || 0) * f).toFixed(1)}g`;
}

$('confirm-log-btn').addEventListener('click', async () => {
  if (!state.pendingFood) return;
  const qty = parseFloat(confirmQty.value);
  if (!qty || qty <= 0) { showToast('Enter a quantity.', 'error'); return; }
  const foodName = state.pendingFood.name;
  try {
    await api.post('/api/log/', { food_id: state.pendingFood.id, quantity_g: qty, source: _confirmSource });
    confirmOverlay.classList.add('hidden');
    state.pendingFood = null;
    showToast(`Logged ${foodName}!`, 'success');
    await goToToday();   // new entries land on today — show it
  } catch (err) {
    showToast(err.message, 'error');
  }
});

// Drop trailing .0 so "1" shows instead of "1.0", but keep "0.5".
function _trimNum(x) {
  return Math.round(x * 100) / 100;
}

$('confirm-cancel-btn').addEventListener('click', () => {
  confirmOverlay.classList.add('hidden');
  state.pendingFood = null;
});

// "Wrong match?" — pick a different food, keeping the current quantity.
$('confirm-change-btn').addEventListener('click', () => {
  const picker = $('confirm-picker');
  const show = picker.classList.contains('hidden');
  picker.classList.toggle('hidden', !show);
  if (show) {
    mountFoodPicker(picker, (food) => {
      applyConfirmFood(food, parseFloat(confirmQty.value) || 100);
      picker.classList.add('hidden');
    });
  }
});

// ── Result card: what the agent just logged, with per-entry Undo/Adjust ───────
let _lastResult = null;      // kept so an issue report can attach what just happened
let _reviseCaptureId = null; // set when the next capture refines the previous log

function showResultCard(result, photoUrl = '') {
  _lastResult = result;
  setPolaroid($('result-photo'), photoUrl);
  $('result-summary').textContent = result.summary || '';
  const t = $('result-transcript');
  const showTranscript = !!result.transcript;
  t.textContent = showTranscript ? `“${result.transcript}”` : '';
  t.classList.toggle('hidden', !showTranscript);
  renderResultAnnotation(result.annotation || {}, !!photoUrl);
  renderResultEntries(result.entries || []);
  // Follow-ups refine THIS capture ("say more" after a photo, photo after voice).
  $('result-followup').classList.toggle('hidden', !result.capture_id);
  $('result-card').classList.remove('hidden');
  confirmOverlay.classList.add('hidden');
}

$('followup-voice').addEventListener('click', () => {
  _reviseCaptureId = _lastResult && _lastResult.capture_id;
  startVoiceCapture();
});

$('followup-photo').addEventListener('click', () => {
  _reviseCaptureId = _lastResult && _lastResult.capture_id;
  photoInput.click();
});

// Meal / tag chips + a gentle nudge when the capture was vague.
function renderResultAnnotation(a, isPhoto = false) {
  const el = $('result-annotation');
  const chips = [];
  if (a.meal) chips.push(a.meal);
  if (a.meal_label && a.meal_label !== a.meal) chips.push(a.meal_label);
  for (const tag of (a.tags || [])) chips.push(tag.replace(':', ': '));
  let html = chips.map(c => `<span class="ann-chip">${esc(c)}</span>`).join('');
  if (a.specificity === 'low') {
    html += isPhoto
      ? `<div class="ann-hint">Rough estimate — next time include a fork, can, or card in the shot; a known-size object gives the AI a scale reference.</div>`
      : `<div class="ann-hint">Rough estimate — more detail next time ("two beef tacos from…") sharpens the numbers.</div>`;
  }
  el.innerHTML = html;
  el.classList.toggle('hidden', !html);
}

function renderResultEntries(entries) {
  const wrap = $('result-entries');
  if (!entries.length) {
    wrap.innerHTML = '';
    return;
  }
  wrap.innerHTML = entries.map(e => {
    const equiv = servingEquiv(e.quantity_g, e.serving_g, e.serving_desc);
    return `
    <div class="result-entry" data-id="${e.id}">
      <div class="result-entry-info">
        <div class="result-entry-name">${esc(e.food_name)}${e.food_brand ? ' · ' + esc(e.food_brand) : ''}</div>
        <div class="result-entry-meta">${Math.round(e.quantity_g)}g${equiv ? ` (${esc(equiv)})` : ''} · ${e.calories.toFixed(0)} cal · ${icon('db')} ${esc(e.food_source || '')}</div>
      </div>
      <button class="result-adjust link-btn" data-id="${e.id}" data-food="${e.food_id}" data-qty="${e.quantity_g}">Adjust</button>
      <button class="result-undo link-btn" data-id="${e.id}">Undo</button>
    </div>`;
  }).join('');

  wrap.querySelectorAll('.result-undo').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        await api.del(`/api/log/${btn.dataset.id}`);
        btn.closest('.result-entry').remove();
        await goToToday();
      } catch (err) { showToast(err.message, 'error'); }
    });
  });

  wrap.querySelectorAll('.result-adjust').forEach(btn => {
    btn.addEventListener('click', () =>
      adjustEntry(btn.dataset.id, btn.dataset.food, btn.dataset.qty));
  });
}

$('result-close').addEventListener('click', () => $('result-card').classList.add('hidden'));

// ── Report an issue ───────────────────────────────────────────────────────────
let _issueContext = {};

function openIssue(context = {}) {
  _issueContext = { ...context, ua: navigator.userAgent, url: location.pathname };
  $('issue-text').value = '';
  $('issue-overlay').classList.remove('hidden');
  $('issue-text').focus();
}

$('issue-close').addEventListener('click', () => $('issue-overlay').classList.add('hidden'));
$('issue-cancel').addEventListener('click', () => $('issue-overlay').classList.add('hidden'));

$('issue-send').addEventListener('click', async () => {
  const message = $('issue-text').value.trim();
  if (!message) { showToast('Describe the issue first.', 'error'); return; }
  try {
    await api.post('/api/issues/', {
      message,
      context: _issueContext,
      capture_id: _issueContext.capture_id || null,
    });
    $('issue-overlay').classList.add('hidden');
    showToast('Thanks — report sent.', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
});

$('result-report').addEventListener('click', () => openIssue({
  from: 'result-card',
  capture_id: _lastResult && _lastResult.capture_id,
  transcript: _lastResult && _lastResult.transcript,
  summary: _lastResult && _lastResult.summary,
  entries: _lastResult ? (_lastResult.entries || []).map(e => `${e.food_name} ${e.quantity_g}g`) : [],
}));

$('report-issue-btn').addEventListener('click', () => openIssue({ from: 'settings' }));

// Show the model's "what I saw" sentence in a banner, or hide it when empty.
function setAiSummary(el, summary) {
  if (summary) {
    el.textContent = summary;
    el.classList.remove('hidden');
  } else {
    el.textContent = '';
    el.classList.add('hidden');
  }
}

// Inline "pick a different food" searcher. Renders an input + live results into
// `container`; calls onPick(food) when the user chooses one. Shared by both panels.
function mountFoodPicker(container, onPick) {
  container.innerHTML =
    '<input type="search" class="food-picker-input" placeholder="Search the correct food…" autocomplete="off">' +
    '<div class="food-picker-results"></div>';
  const input = container.querySelector('.food-picker-input');
  const resultsEl = container.querySelector('.food-picker-results');
  let deb;

  async function run() {
    const q = input.value.trim();
    if (q.length < 2) { resultsEl.innerHTML = ''; return; }
    resultsEl.innerHTML = '<div class="search-loading">Searching…</div>';
    try {
      const results = await api.get(`/api/foods/search?q=${encodeURIComponent(q)}`);
      if (!results.length) { resultsEl.innerHTML = '<div class="search-loading">No results.</div>'; return; }
      resultsEl.innerHTML = results.map((f, i) => {
        const cal = (f.nutrients_per_100g.calories || 0).toFixed(0);
        const brand = f.brand ? ` · ${esc(f.brand)}` : '';
        return `<div class="search-item" data-idx="${i}">
          <div class="s-name">${esc(f.name)}</div>
          <div class="s-meta">${cal} cal/100g${brand}</div>
        </div>`;
      }).join('');
      resultsEl.querySelectorAll('.search-item').forEach(el =>
        el.addEventListener('click', () => onPick(results[+el.dataset.idx])));
    } catch (err) {
      resultsEl.innerHTML = `<div class="search-loading">${esc(err.message)}</div>`;
    }
  }

  input.addEventListener('input', () => { clearTimeout(deb); deb = setTimeout(run, 350); });
  input.focus();
}

// Show the polaroid snapshot in a confirm panel, or hide it when there's no photo.
function setPolaroid(figureEl, url) {
  const img = figureEl.querySelector('img');
  if (url) {
    img.src = url;
    figureEl.classList.remove('hidden');
  } else {
    img.removeAttribute('src');
    figureEl.classList.add('hidden');
  }
}

// ── Day log ───────────────────────────────────────────────────────────────────
async function loadDayLog() {
  renderDayNav();
  try {
    const tz = new Date().getTimezoneOffset();
    state.dayLog = await api.get(`/api/log/today?tz_offset=${tz}&date=${state.viewDate}`);
    renderLog();
    renderSummary();
    loadWater();
  } catch (err) {
    logList.innerHTML = `<p class="empty-state">Could not load log: ${esc(err.message)}</p>`;
  }
}

const MEAL_ORDER = ['Breakfast', 'Lunch', 'Dinner', 'Snacks'];
const collapsedMeals = new Set();   // meals the user has folded (persists across re-renders)

// Bucket an entry by its LOCAL hour (eaten_at is UTC; new Date() converts).
function mealOf(eatenAt) {
  const h = new Date(eatenAt).getHours();
  if (h >= 5 && h < 11) return 'Breakfast';
  if (h >= 11 && h < 16) return 'Lunch';
  if (h >= 16 && h < 22) return 'Dinner';
  return 'Snacks';
}

// "45g" is hard to picture; the food's own household serving usually isn't.
// Returns "≈ 5 cakes", "≈ 1.9 cups (240ml)", "≈ 300 ml", "≈ 2 × 3 cookies",
// or '' when the food has no usable serving info (or it would just repeat grams).
function servingEquiv(qtyG, servingG, desc) {
  if (!servingG || servingG <= 0 || !desc) return '';
  const d = String(desc).trim();
  const n = qtyG / servingG;
  if (!isFinite(n) || n < 0.1 || n > 50) return '';
  const nice = Math.abs(n - Math.round(n)) < 0.05 ? String(Math.round(n)) : n.toFixed(1);

  // Pure-volume serving ("600.0 ml") → convert the whole quantity to ml.
  const ml = d.match(/^([\d.]+)\s*ml$/i);
  if (ml) return `≈ ${Math.round(n * parseFloat(ml[1]))} ml`;
  // A gram-shaped desc ("30 g") would just repeat the number we already show.
  if (/^[\d.]+\s*(g|grm|gram|grams)$/i.test(d)) return '';

  // "1 cake" → "≈ 5 cakes"; a multi-unit serving ("3 cookies") → "≈ 2 × 3 cookies".
  const one = d.match(/^1\s+(.*)$/);
  if (one) {
    let unit = one[1];
    if (nice !== '1') {
      unit = unit.replace(/^([A-Za-z]+)/, w => (/(s|x|z|ch|sh)$/i.test(w) ? w + 'es' : w + 's'));
    }
    return `≈ ${nice} ${unit}`;
  }
  return nice === '1' ? `≈ ${d}` : `≈ ${nice} × ${d}`;
}

function entryHtml(e) {
  const equiv = servingEquiv(e.quantity_g, e.serving_g, e.serving_desc);
  return `<div class="log-entry" data-id="${e.id}" data-food="${e.food_id}" data-qty="${e.quantity_g}">
    <span class="log-source-icon">${sourceIcon(e.source)}</span>
    <div class="log-info">
      <div class="log-name">${esc(e.food_name)}</div>
      <div class="log-meta">${e.quantity_g}g${equiv ? ' · ' + esc(equiv) : ''}${e.food_brand ? ' · ' + esc(e.food_brand) : ''}</div>
      ${e.food_source ? `<div class="log-src">${icon('db')} ${esc(e.food_source)}</div>` : ''}
    </div>
    <div class="log-nutrition">
      <div class="log-cal">${e.calories.toFixed(0)}</div>
      <div class="log-macros">P${e.protein_g.toFixed(0)} C${e.carbs_g.toFixed(0)} F${e.fat_g.toFixed(0)}</div>
    </div>
    <button class="log-delete" data-id="${e.id}" title="Remove">${icon('x')}</button>
  </div>`;
}

// Tap an entry to adjust it: remove it, then reopen the confirm panel prefilled
// with the same food and quantity so it can be corrected and re-logged.
async function adjustEntry(entryId, foodId, qty) {
  try {
    const food = await api.get(`/api/foods/${foodId}`);
    await api.del(`/api/log/${entryId}`);
    await goToToday();
    $('result-card').classList.add('hidden');
    openConfirm(food, parseFloat(qty) || 100, 'manual');
  } catch (err) { showToast(err.message, 'error'); }
}

function renderLog() {
  if (!state.dayLog.length) {
    logList.innerHTML = `<div class="empty-state">Nothing logged yet.<br>Say it, snap it, or add it manually.
      <button id="log-empty-home" class="btn-secondary" type="button">Home</button></div>`;
    $('log-empty-home').addEventListener('click', () => showPane('home'));
    return;
  }

  const groups = {};
  for (const e of state.dayLog) (groups[mealOf(e.eaten_at)] ||= []).push(e);

  logList.innerHTML = MEAL_ORDER.filter(m => groups[m]).map(meal => {
    const entries = groups[meal];
    const cal = entries.reduce((s, e) => s + e.calories, 0);
    const collapsed = collapsedMeals.has(meal);
    return `<div class="meal-group${collapsed ? ' collapsed' : ''}" data-meal="${meal}">
      <button class="meal-header" type="button">
        <span class="meal-toggle">▾</span>
        <span class="meal-name">${meal}</span>
        <span class="meal-count">${entries.length}</span>
        <span class="meal-subtotal">${cal.toFixed(0)} cal</span>
      </button>
      <div class="meal-entries">${entries.map(entryHtml).join('')}</div>
    </div>`;
  }).join('');

  logList.querySelectorAll('.meal-group').forEach(g => {
    g.querySelector('.meal-header').addEventListener('click', () => {
      const meal = g.dataset.meal;
      if (collapsedMeals.has(meal)) collapsedMeals.delete(meal);
      else collapsedMeals.add(meal);
      g.classList.toggle('collapsed');
    });
  });

  logList.querySelectorAll('.log-delete').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();   // don't toggle the meal section
      try {
        await api.del(`/api/log/${btn.dataset.id}`);
        await loadDayLog();
      } catch (err) {
        showToast(err.message, 'error');
      }
    });
  });

  // Tap anywhere else on an entry to adjust its quantity / swap the food.
  logList.querySelectorAll('.log-entry').forEach(el => {
    el.addEventListener('click', () =>
      adjustEntry(el.dataset.id, el.dataset.food, el.dataset.qty));
  });
}

const RING_CIRC = 2 * Math.PI * 52;
const MACRO_REF = { protein: 150, carbs: 250, fat: 70 };   // fallback scale when no goal set

function renderSummary() {
  const t = state.dayLog.reduce(
    (a, e) => ({ cal: a.cal + e.calories, p: a.p + e.protein_g, c: a.c + e.carbs_g, f: a.f + e.fat_g }),
    { cal: 0, p: 0, c: 0, f: 0 }
  );
  const u = state.user || {};
  const ring = $('ring-fill');
  ring.style.strokeDasharray = `${RING_CIRC}`;

  const goal = u.calorie_goal;
  let pct;
  if (goal) {
    pct = Math.min(t.cal / goal, 1);
    const left = Math.round(goal - t.cal);
    $('ring-num').textContent = Math.abs(left).toLocaleString();
    $('ring-label').textContent = left >= 0 ? 'cal left' : 'cal over';
  } else {
    pct = t.cal > 0 ? 1 : 0;
    $('ring-num').textContent = Math.round(t.cal).toLocaleString();
    $('ring-label').textContent = 'calories';
  }
  ring.style.strokeDashoffset = `${RING_CIRC * (1 - pct)}`;
  ring.classList.toggle('over', !!(goal && t.cal > goal));

  setMacroBar('protein', t.p, u.protein_g);
  setMacroBar('carbs', t.c, u.carbs_g);
  setMacroBar('fat', t.f, u.fat_g);
}

function setMacroBar(name, val, goal) {
  const denom = goal || MACRO_REF[name];
  $('bar-' + name).style.width = Math.min(val / denom * 100, 100) + '%';
  $('bar-' + name).classList.toggle('over', !!(goal && val > goal));
  $('val-' + name).textContent = goal ? `${Math.round(val)}/${Math.round(goal)}g` : `${Math.round(val)}g`;
}

// ── Water ─────────────────────────────────────────────────────────────────────
async function loadWater() {
  try {
    const tz = new Date().getTimezoneOffset();
    const { glasses, goal } = await api.get(`/api/log/water?tz_offset=${tz}&date=${state.viewDate}`);
    renderWater(glasses, goal);
  } catch { /* best-effort */ }
}

function renderWater(glasses, goal) {
  const prev = state.water || 0;
  state.water = glasses;
  $('water-count').textContent = `${glasses} / ${goal}`;
  const n = Math.max(goal, glasses);
  const el = $('water-glasses');
  el.innerHTML = Array.from({ length: n }, (_, i) =>
    `<span class="glass${i < glasses ? ' filled' : ''}" data-i="${i}">${icon('drop')}</span>`).join('');
  el.querySelectorAll('.glass').forEach(g => {
    g.addEventListener('click', () => {
      const i = +g.dataset.i;
      setWater(i + 1 === glasses ? i : i + 1);   // tap the top filled glass to remove one
    });
  });
  if (glasses > prev) {
    const g = el.querySelector(`.glass[data-i="${glasses - 1}"]`);
    if (g) g.classList.add('pop');
  }
}

async function setWater(g) {
  try {
    const tz = new Date().getTimezoneOffset();
    const r = await api.post('/api/log/water', { glasses: g, date: state.viewDate, tz_offset: tz });
    renderWater(r.glasses, r.goal);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// ── Voice input (MediaRecorder → server-side Whisper → agent) ─────────────────
let mediaRecorder = null;
let recChunks = [];
let recCancelled = false;
let recTimer = null;

function setListening(on) {
  voiceBtn.classList.toggle('listening', on);
}

function showVoiceMsg(msg, autohideMs = 0) {
  voiceStatus.textContent = msg;
  voiceStatus.classList.remove('hidden');
  if (autohideMs) setTimeout(() => voiceStatus.classList.add('hidden'), autohideMs);
}

// Capture overlay refs
const voiceOverlay = $('voice-overlay');
const voiceOverlayStatus = $('voice-overlay-status');
const voiceOverlayTranscript = $('voice-overlay-transcript');

function openVoiceOverlay() {
  voiceOverlayStatus.textContent = 'Recording…';
  voiceOverlayTranscript.innerHTML = '<span class="voice-hint">Speak now — tap <b>Done</b> when you finish</span>';
  voiceOverlay.classList.remove('hidden');
  const t0 = Date.now();
  recTimer = setInterval(() => {
    const s = Math.floor((Date.now() - t0) / 1000);
    voiceOverlayStatus.textContent = `Recording… 0:${String(s % 60).padStart(2, '0')}`;
  }, 1000);
}

function closeVoiceOverlay() {
  clearInterval(recTimer);
  voiceOverlay.classList.add('hidden');
}

// Chrome/Android record webm/opus; iOS Safari records mp4/AAC. Whisper on the
// server decodes both, so we just pick whatever this browser supports.
function pickAudioMime() {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
  return candidates.find(m => MediaRecorder.isTypeSupported(m)) || '';
}

async function startVoiceCapture() {
  if (mediaRecorder && mediaRecorder.state === 'recording') return;
  if (!window.isSecureContext) {
    showVoiceMsg('Mic needs a secure connection. Use http://localhost, or an https:// URL when on your phone.', 8000);
    return;
  }
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    showVoiceMsg("Audio recording isn't supported in this browser.", 6000);
    return;
  }

  let stream;
  try {
    // Ask for mic processing explicitly: autoGainControl in particular rescues
    // quiet phone mics that otherwise record near-silence (which Whisper then
    // hallucinates into "thank you" / "bye bye"). These are ideal-constraints,
    // so browsers that can't honor them still return a stream.
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch {
    showVoiceMsg('Microphone is blocked. Allow mic access for this site in your browser settings.', 8000);
    return;
  }

  voiceStatus.classList.add('hidden');
  $('result-card').classList.add('hidden');   // a new capture hands the last one to the log
  recChunks = [];
  recCancelled = false;
  const mime = pickAudioMime();
  try {
    mediaRecorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
  } catch {
    mediaRecorder = new MediaRecorder(stream);
  }

  mediaRecorder.ondataavailable = (e) => { if (e.data && e.data.size) recChunks.push(e.data); };
  mediaRecorder.onstop = () => {
    stream.getTracks().forEach(t => t.stop());
    setListening(false);
    closeVoiceOverlay();
    if (recCancelled) return;
    const blob = new Blob(recChunks, { type: mediaRecorder.mimeType || 'audio/webm' });
    if (blob.size < 1000) {
      showVoiceMsg("Didn't record anything — tap Say it and try again.", 5000);
      return;
    }
    submitAgentLog({ audio: blob });
  };

  openVoiceOverlay();
  mediaRecorder.start(250);   // flush chunks as we go
  setListening(true);
}

voiceBtn.addEventListener('click', () => { _reviseCaptureId = null; startVoiceCapture(); });

$('voice-overlay-stop').addEventListener('click', () => {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
});

$('voice-overlay-cancel').addEventListener('click', () => {
  recCancelled = true;
  if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
  else closeVoiceOverlay();
});

// ── Agent logging (voice/photo → auto-logged entries + result card) ──────────
async function submitAgentLog({ audio = null, image = null, photoUrl = '' }) {
  showVoiceMsg(audio ? 'Transcribing & logging…' : 'Analyzing & logging…');

  const form = new FormData();
  form.append('tz_offset', String(new Date().getTimezoneOffset()));
  const reviseId = _reviseCaptureId;   // one-shot: consumed by this submission
  _reviseCaptureId = null;
  if (reviseId) form.append('revise_capture_id', String(reviseId));
  if (audio) form.append('audio', audio, 'voice-note');
  if (image) form.append('image', image, 'meal.jpg');

  let result;
  try {
    const r = await fetch('/api/agent/log', { method: 'POST', credentials: 'same-origin', body: form });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || 'Logging failed');
    }
    result = await r.json();
  } catch (err) {
    showVoiceMsg(err.message, 8000);
    return;
  }

  voiceStatus.classList.add('hidden');
  await goToToday();   // entries are already saved — refresh the day view
  showResultCard(result, photoUrl);
}

// ── Photo input ───────────────────────────────────────────────────────────────
let _currentPhotoUrl = null;   // object URL of the last compressed snapshot

photoBtn.addEventListener('click', () => { _reviseCaptureId = null; photoInput.click(); });

photoInput.addEventListener('change', async () => {
  const file = photoInput.files && photoInput.files[0];
  photoInput.value = '';  // allow re-selecting the same file later
  if (!file) return;

  photoBtn.classList.add('busy');
  $('result-card').classList.add('hidden');   // a new capture hands the last one to the log
  voiceStatus.textContent = 'Compressing photo…';
  voiceStatus.classList.remove('hidden');

  try {
    const blob = await compressImage(file, 1024, 0.8);
    // Keep a thumbnail URL of the exact (compressed) image we sent to the model.
    if (_currentPhotoUrl) URL.revokeObjectURL(_currentPhotoUrl);
    _currentPhotoUrl = URL.createObjectURL(blob);

    await submitAgentLog({ image: blob, photoUrl: _currentPhotoUrl });
  } catch (err) {
    voiceStatus.textContent = err.message;
    voiceStatus.classList.remove('hidden');
  } finally {
    photoBtn.classList.remove('busy');
  }
});

// Compress to JPEG with the longest edge capped at maxEdge (hard rule #3).
function compressImage(file, maxEdge, quality) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      let { width, height } = img;
      if (width > maxEdge || height > maxEdge) {
        const scale = maxEdge / Math.max(width, height);
        width = Math.round(width * scale);
        height = Math.round(height * scale);
      }
      const canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      canvas.getContext('2d').drawImage(img, 0, 0, width, height);
      canvas.toBlob(
        b => (b ? resolve(b) : reject(new Error('Could not process image'))),
        'image/jpeg',
        quality
      );
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('Could not read image')); };
    img.src = url;
  });
}

// ── Pane navigation (top nav) ─────────────────────────────────────────────────
// Home = capture + the immediate result; Log = journal, analytics & your foods;
// Coach = on-demand chat over your data; Settings = goals, notifications, account.
// Leaving Home hands the result card over to the log.
const PANES = {
  home: appScreenEl, log: $('log-screen'),
  coach: $('coach-screen'), settings: $('settings-screen'),
  admin: $('admin-screen'),
};

async function showPane(name) {
  Object.entries(PANES).forEach(([k, el]) => el.classList.toggle('hidden', k !== name));
  document.querySelectorAll('.topnav-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.pane === name));
  $('result-card').classList.add('hidden');
  confirmOverlay.classList.add('hidden');

  if (name === 'home') {
    await goToToday();
  } else if (name === 'log') {
    await loadDayLog();
    renderGoalProgress();
    await loadChart();
  } else if (name === 'coach') {
    await loadCoach();
  } else if (name === 'settings') {
    prefillGoals();
    renderAccount();
    initReminders();
  } else if (name === 'admin') {
    await loadAdmin();
  }
}

document.querySelectorAll('.topnav-btn').forEach(btn =>
  btn.addEventListener('click', () => showPane(btn.dataset.pane)));

// ── Log pane: chart range + account section ───────────────────────────────────
let _dashDays = 7;

document.querySelectorAll('.range-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    _dashDays = +btn.dataset.days;
    loadChart();
  });
});

function renderAccount() {
  const u = state.user || {};
  $('acct-name').textContent = u.display_name || '';
  $('acct-email').textContent = u.email || '';
}

$('delete-account-btn').addEventListener('click', async () => {
  const pw = $('delete-password').value;
  if (!pw) { showToast('Enter your password to confirm.', 'error'); return; }
  if (!confirm('Delete your account and ALL of your data? This cannot be undone.')) return;
  try {
    await api._req('DELETE', '/api/auth/account', { password: pw });
    $('delete-password').value = '';
    leaveApp();
    showToast('Your account and data have been deleted.');
  } catch (err) {
    showToast(err.message, 'error');
  }
});

// Hide the app chrome and return to the auth screen (logout / account deletion).
function leaveApp() {
  state.user = null;
  _coachLoaded = false;
  $('coach-messages').innerHTML = '';
  $('admin-nav-btn').classList.add('hidden');
  Object.values(PANES).forEach(el => el.classList.add('hidden'));
  $('topnav').classList.add('hidden');
  authScreen.classList.remove('hidden');
}

// ── Admin: production usage & evaluation dashboard (maintainer only) ──────────
async function loadAdmin() {
  try {
    const [s, f, tr, is_, er] = await Promise.all([
      api.get('/api/admin/stats?days=14'),
      api.get('/api/admin/failures?days=14'),
      api.get('/api/admin/traces?limit=30'),
      api.get('/api/admin/issues?limit=30'),
      api.get('/api/admin/errors?limit=30'),
    ]);
    renderAdmin(s, f.failures);
    renderAdminTelemetry(tr.traces, is_.issues, er.errors);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

// Where each report routes: infra → fix the code, model → training-dataset
// candidate, capture → mic/photo/STT pipeline. Auto-triaged; relabel here.
const ISSUE_CATS = { infra: 'infrastructure', model: 'model → dataset', capture: 'capture / device', other: 'other' };

function renderAdminTelemetry(traces, issues, errors) {
  $('admin-issues').innerHTML = issues.length ? issues.map(i => {
    let ctx = {};
    try { ctx = JSON.parse(i.context_json || '{}'); } catch { /* show raw below */ }
    const detail = [ctx.transcript && `said: “${ctx.transcript}”`, ctx.summary && `agent: ${ctx.summary}`]
      .filter(Boolean).join(' · ');
    const opts = ['', ...Object.keys(ISSUE_CATS)].map(c =>
      `<option value="${c}"${(i.category || '') === c ? ' selected' : ''}>${c ? ISSUE_CATS[c] : 'unsorted'}</option>`).join('');
    return `<div class="fail-item">
      <div class="fail-meta">${esc(i.display_name)} · ${esc(i.created_at.slice(0, 16))}${i.capture_id ? ` · capture #${i.capture_id}` : ''}
        <select class="issue-cat" data-id="${i.id}" title="Route this report">${opts}</select>
      </div>
      <div class="fail-transcript">${esc(i.message)}</div>
      ${detail ? `<div class="fail-summary">${esc(detail)}</div>` : ''}
    </div>`;
  }).join('') : '<p class="empty-state">No reports.</p>';

  $('admin-issues').querySelectorAll('.issue-cat').forEach(sel => {
    sel.addEventListener('change', async () => {
      try {
        await api.post(`/api/admin/issues/${sel.dataset.id}/category`, { category: sel.value || null });
        showToast('Report re-routed.', 'success');
      } catch (err) { showToast(err.message, 'error'); }
    });
  });

  $('admin-traces').innerHTML = traces.length ? traces.map(t => {
    let resp = {};
    try { resp = JSON.parse(t.response_json || '{}'); } catch { /* leave empty */ }
    const tokens = `${(t.input_tokens || 0).toLocaleString()}→${(t.output_tokens || 0).toLocaleString()}`;
    const status = t.error ? 'ERROR' : (t.stop_reason || '');
    const calls = (resp.tool_calls || []).map(c => c.name).join(', ');
    return `<details class="trace-item${t.error ? ' trace-error' : ''}">
      <summary>
        <span class="trace-when">${esc(t.created_at.slice(5, 16))}</span>
        <span class="trace-who">${esc(t.display_name || '—')}</span>
        <span class="trace-what">${esc(t.feature)} · ${esc(t.model)}</span>
        <span class="trace-nums">${t.latency_ms ?? '—'}ms · ${tokens} tok · ${esc(status)}</span>
      </summary>
      ${t.error ? `<pre class="trace-pre">${esc(t.error)}</pre>` : ''}
      ${calls ? `<div class="fail-summary">tools: ${esc(calls)}</div>` : ''}
      ${resp.text ? `<pre class="trace-pre">${esc(resp.text)}</pre>` : ''}
      ${(resp.tool_calls || []).length ? `<pre class="trace-pre">${esc(JSON.stringify(resp.tool_calls, null, 1))}</pre>` : ''}
    </details>`;
  }).join('') : '<p class="empty-state">No model calls traced yet.</p>';

  $('admin-errors').innerHTML = errors.length ? errors.map(e => `
    <div class="fail-item">
      <div class="fail-meta">${esc(e.created_at.slice(0, 16))} · ${esc(e.method || '')} ${esc(e.path || '')}</div>
      <div class="fail-summary">${esc(e.error || '')}</div>
    </div>`).join('') : '<p class="empty-state">No unhandled errors. Nice.</p>';
}

function renderAdmin(s, failures) {
  const t = s.totals;
  $('admin-tiles').innerHTML = [
    [t.captures, 'captures'],
    [t.active_users, 'active users'],
    [`${t.fast_path_pct}%`, 'fast path'],
    [`${t.zero_entry_pct}%`, 'logged nothing'],
    [t.coach_messages, 'coach msgs'],
    [`$${t.est_cost_usd}`, 'est. AI cost'],
  ].map(([val, label]) =>
    `<div class="stat-cell"><div class="stat-val">${esc(String(val))}</div><div class="stat-label">${label}</div></div>`
  ).join('');

  // Captures per day — single-series bars, same anatomy as the calories chart.
  const daily = s.daily;
  const max = Math.max(...daily.map(d => d.captures), 1);
  const step = daily.length > 8 ? 2 : 1;
  $('admin-chart').innerHTML = daily.length ? daily.map((d, i) => {
    const label = (i % step === 0) ? d.day.slice(5).replace('-', '/') : '';
    return `<div class="bar-col" title="${d.day}: ${d.captures} captures (${d.voice} voice, ${d.photo} photo, ${d.text} text)">
      <div class="bar-wrap"><div class="bar" style="height:${(d.captures / max) * 100}%"></div></div>
      <span class="bar-label">${label}</span>
    </div>`;
  }).join('') : '<p class="empty-state">No captures yet.</p>';
  const tv = daily.reduce((a, d) => ({ v: a.v + d.voice, p: a.p + d.photo, t: a.t + d.text }), { v: 0, p: 0, t: 0 });
  $('admin-chart-caption').textContent =
    daily.length ? `${tv.v} voice · ${tv.p} photo · ${tv.t} text` : '';

  // Grounding mix — labeled magnitude meters, one hue.
  const totalEntries = s.entry_sources.reduce((a, r) => a + r.n, 0) || 1;
  $('admin-sources').innerHTML = s.entry_sources.length ? s.entry_sources.map(r => `
    <div class="gp-row">
      <div class="gp-head"><span class="gp-label">${esc(foodSourceLabel(r.source) || r.source)}</span>
        <span class="gp-vals">${r.n} (${Math.round(100 * r.n / totalEntries)}%)</span></div>
      <div class="gp-track"><div class="gp-fill" style="width:${(r.n / totalEntries) * 100}%"></div></div>
    </div>`).join('') : '<p class="empty-state">No entries yet.</p>';

  $('admin-users').innerHTML =
    '<tr><th>User</th><th>Joined</th><th>Last active</th><th>Captures</th><th>Entries</th><th>Tokens</th></tr>' +
    s.per_user.map(u => `<tr>
      <td>${esc(u.display_name)}</td>
      <td>${esc(u.joined || '')}</td>
      <td>${u.last_capture ? esc(u.last_capture.slice(0, 10)) : '—'}</td>
      <td>${u.captures}</td><td>${u.entries}</td><td>${(u.tokens || 0).toLocaleString()}</td>
    </tr>`).join('');

  $('admin-failures').innerHTML = failures.length ? failures.map(f => `
    <div class="fail-item">
      <div class="fail-meta">${esc(f.display_name)} · ${esc(f.input_type)} · ${esc(f.created_at.slice(0, 16))}</div>
      ${f.transcript ? `<div class="fail-transcript">“${esc(f.transcript)}”</div>` : '<div class="fail-transcript">(photo — no transcript)</div>'}
      ${f.summary ? `<div class="fail-summary">${esc(f.summary)}</div>` : ''}
    </div>`).join('') : '<p class="empty-state">No failed captures. Nice.</p>';
}

// ── Coach: on-demand chat over the user's own logs, notes, goals & profile ────
let _coachLoaded = false;
let _coachBusy = false;

async function loadCoach() {
  if (_coachLoaded) return;
  try {
    const { messages } = await api.get('/api/coach/history');
    const box = $('coach-messages');
    box.innerHTML = '';
    if (!messages.length) {
      addCoachMessage('assistant',
        "Hey! I'm your coach. I can see your logs, goals, and the notes you leave when you log food. " +
        "Ask me how you're tracking, or just tell me about your goals and I'll remember.");
    } else {
      messages.forEach(m => addCoachMessage(m.role, m.content));
    }
    _coachLoaded = true;
    scrollCoachToBottom();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

function addCoachMessage(role, content) {
  const box = $('coach-messages');
  const el = document.createElement('div');
  el.className = `coach-msg coach-${role === 'user' ? 'user' : 'bot'}`;
  el.textContent = content;
  box.appendChild(el);
  return el;
}

function scrollCoachToBottom() {
  const box = $('coach-messages');
  box.scrollTop = box.scrollHeight;
}

async function sendCoach(message) {
  const text = (message || '').trim();
  if (!text || _coachBusy) return;
  _coachBusy = true;
  $('coach-input').value = '';
  addCoachMessage('user', text);
  const thinking = addCoachMessage('bot', '…');
  thinking.classList.add('coach-thinking');
  scrollCoachToBottom();
  try {
    const { reply } = await api.post('/api/coach/chat', { message: text });
    thinking.classList.remove('coach-thinking');
    thinking.textContent = reply;
  } catch (err) {
    thinking.classList.remove('coach-thinking');
    thinking.classList.add('coach-error');
    thinking.textContent = err.message;
  } finally {
    _coachBusy = false;
    scrollCoachToBottom();
  }
}

$('coach-composer').addEventListener('submit', e => {
  e.preventDefault();
  sendCoach($('coach-input').value);
});

document.querySelectorAll('.coach-suggest').forEach(btn =>
  btn.addEventListener('click', () => sendCoach(btn.dataset.prompt)));

function prefillGoals() {
  const u = state.user || {};
  $('goal-cal').value = u.calorie_goal ?? '';
  $('goal-protein').value = u.protein_g ?? '';
  $('goal-carbs').value = u.carbs_g ?? '';
  $('goal-fat').value = u.fat_g ?? '';
  updateGoalsCaption();
}

// ── Goal macro calculator (Atwater: 4·P + 4·C + 9·F = calories) ────────────────
const ATWATER = { protein: 4, carbs: 4, fat: 9 };
let _goalsRecalcing = false;

const _gnum = (id) => parseFloat($(id).value) || 0;
const _gset = (id, v) => { $(id).value = v > 0 ? Math.round(v) : ''; };

function _macroCalories() {
  return ATWATER.protein * _gnum('goal-protein')
       + ATWATER.carbs * _gnum('goal-carbs')
       + ATWATER.fat * _gnum('goal-fat');
}

function updateGoalsCaption() {
  const cal = _gnum('goal-cal');
  const el = $('goals-caption');
  if (!cal) { el.innerHTML = 'Calories auto-calculate from your macros (4·P + 4·C + 9·F).'; return; }
  const p = _gnum('goal-protein'), c = _gnum('goal-carbs'), f = _gnum('goal-fat');
  const pct = (v, factor) => Math.round((factor * v) / cal * 100);
  el.innerHTML =
    `<span class="gc-pct">${pct(p, 4)}%</span> protein · ` +
    `<span class="gc-pct">${pct(c, 4)}%</span> carbs · ` +
    `<span class="gc-pct">${pct(f, 9)}%</span> fat`;
}

// Edit a macro → recompute calories.
['goal-protein', 'goal-carbs', 'goal-fat'].forEach(id => {
  $(id).addEventListener('input', () => {
    if (_goalsRecalcing) return;
    _goalsRecalcing = true;
    const cal = _macroCalories();
    $('goal-cal').value = cal > 0 ? Math.round(cal) : '';
    updateGoalsCaption();
    _goalsRecalcing = false;
  });
});

// Edit calories → keep protein, scale carbs & fat to fit (their ratio preserved).
$('goal-cal').addEventListener('input', () => {
  if (_goalsRecalcing) return;
  _goalsRecalcing = true;
  const cal = _gnum('goal-cal');
  const p = _gnum('goal-protein'), c = _gnum('goal-carbs'), f = _gnum('goal-fat');
  if (cal > 0) {
    if (p === 0 && c === 0 && f === 0) {
      // Blank form: seed a balanced 30 / 40 / 30 split (protein / carbs / fat).
      _gset('goal-protein', 0.30 * cal / ATWATER.protein);
      _gset('goal-carbs', 0.40 * cal / ATWATER.carbs);
      _gset('goal-fat', 0.30 * cal / ATWATER.fat);
      updateGoalsCaption();
      _goalsRecalcing = false;
      return;
    }
    const remaining = cal - ATWATER.protein * p;
    if (remaining <= 0) {
      // Protein alone exceeds the target → scale all three down proportionally.
      const cur = _macroCalories();
      if (cur > 0) {
        const s = cal / cur;
        _gset('goal-protein', p * s); _gset('goal-carbs', c * s); _gset('goal-fat', f * s);
      }
    } else {
      // Split the remaining calories across carbs & fat by their current ratio.
      const carbCal = ATWATER.carbs * c, fatCal = ATWATER.fat * f;
      const carbShare = (carbCal + fatCal) > 0 ? carbCal / (carbCal + fatCal) : 0.6;
      _gset('goal-carbs', (remaining * carbShare) / ATWATER.carbs);
      _gset('goal-fat', (remaining * (1 - carbShare)) / ATWATER.fat);
    }
  }
  updateGoalsCaption();
  _goalsRecalcing = false;
});

// Today vs goals — uses the already-loaded today log.
function renderGoalProgress() {
  const u = state.user || {};
  const t = state.dayLog.reduce(
    (a, e) => ({ cal: a.cal + e.calories, p: a.p + e.protein_g, c: a.c + e.carbs_g, f: a.f + e.fat_g }),
    { cal: 0, p: 0, c: 0, f: 0 }
  );
  const rows = [
    ['Calories', 'cal', t.cal, u.calorie_goal, ''],
    ['Protein', 'protein', t.p, u.protein_g, 'g'],
    ['Carbs', 'carbs', t.c, u.carbs_g, 'g'],
    ['Fat', 'fat', t.f, u.fat_g, 'g'],
  ];
  $('goal-progress').innerHTML = rows.map(([label, cls, val, goal, unit]) => {
    const v = Math.round(val);
    if (!goal) {
      return `<div class="gp-row">
        <div class="gp-head"><span class="gp-label">${label}</span>
          <span class="gp-vals">${v}${unit} · <span class="gp-nogoal">no goal</span></span></div>
        <div class="gp-track"><div class="gp-fill ${cls}" style="width:0%"></div></div>
      </div>`;
    }
    const pct = Math.min((val / goal) * 100, 100);
    const over = val > goal;
    return `<div class="gp-row">
      <div class="gp-head"><span class="gp-label">${label}</span>
        <span class="gp-vals">${v} / ${Math.round(goal)}${unit}${over ? ' <span class="gp-over">over</span>' : ''}</span></div>
      <div class="gp-track"><div class="gp-fill ${cls}${over ? ' over' : ''}" style="width:${pct}%"></div></div>
    </div>`;
  }).join('');
}

async function loadChart() {
  let summary;
  try {
    summary = await api.get(`/api/log/summary?days=${_dashDays}&tz_offset=${new Date().getTimezoneOffset()}`);
  } catch (err) {
    $('cal-chart').innerHTML = `<p class="empty-state">${esc(err.message)}</p>`;
    return;
  }
  renderChart(summary);
  renderMacroAverages(summary);
}

function renderChart(summary) {
  const goal = state.user?.calorie_goal || 0;
  const maxCal = Math.max(goal, ...summary.map(d => d.calories), 1);
  // For 30-day view, only label ~every 5th day to avoid clutter.
  const step = summary.length > 10 ? 5 : 1;

  $('cal-chart').innerHTML = summary.map((d, i) => {
    const h = (d.calories / maxCal) * 100;
    const over = goal && d.calories > goal;
    const label = (i % step === 0) ? new Date(d.date + 'T00:00').toLocaleDateString('en-US', { month: 'numeric', day: 'numeric' }) : '';
    return `<div class="bar-col" title="${d.date}: ${Math.round(d.calories)} cal">
      <div class="bar-wrap"><div class="bar${over ? ' over' : ''}" style="height:${h}%"></div></div>
      <span class="bar-label">${label}</span>
    </div>`;
  }).join('');

  const avg = summary.reduce((s, d) => s + d.calories, 0) / summary.length;
  const goalTxt = goal ? ` · goal ${goal}` : '';
  $('cal-avg').textContent = `Avg ${Math.round(avg)} cal/day over ${summary.length} days${goalTxt}`;
}

function renderMacroAverages(summary) {
  const n = summary.length || 1;
  const sum = summary.reduce(
    (a, d) => ({ p: a.p + d.protein_g, c: a.c + d.carbs_g, f: a.f + d.fat_g }),
    { p: 0, c: 0, f: 0 }
  );
  const cells = [
    ['Protein', sum.p / n],
    ['Carbs', sum.c / n],
    ['Fat', sum.f / n],
  ];
  $('macro-averages').innerHTML =
    `<div class="macro-avg-grid">` +
    cells.map(([label, v]) =>
      `<div class="macro-avg-cell">
        <div class="macro-avg-val">${Math.round(v)}g</div>
        <div class="macro-avg-label">${label}</div>
      </div>`
    ).join('') +
    `</div>`;
}

$('goals-form').addEventListener('submit', async e => {
  e.preventDefault();
  const num = (id) => {
    const v = $(id).value.trim();
    return v === '' ? null : Number(v);
  };
  try {
    state.user = await api.put('/api/auth/goals', {
      calorie_goal: num('goal-cal'),
      protein_g: num('goal-protein'),
      carbs_g: num('goal-carbs'),
      fat_g: num('goal-fat'),
    });
    showToast('Goals saved!', 'success');
    renderGoalProgress();
    renderSummary();   // refresh the main screen's calorie goal bar too
    await loadChart();
  } catch (err) {
    showToast(err.message, 'error');
  }
});

// ── Push notifications & reminders ────────────────────────────────────────────
const pushSupported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;

function setPushStatus(msg, kind = '') {
  const el = $('push-status');
  el.textContent = msg;
  el.className = `push-status ${kind}`;
}

function urlBase64ToUint8Array(base64) {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64);
  return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
}

async function initReminders() {
  // iOS only allows web push from an installed (home-screen) PWA.
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
  $('ios-push-hint').classList.toggle('hidden', !(isIOS && !standalone));

  if (!pushSupported) {
    $('enable-push-btn').disabled = true;
    setPushStatus('Notifications are not supported in this browser.', 'err');
    return;
  }
  // Already subscribed? Skip straight to the controls.
  const reg = await navigator.serviceWorker.ready.catch(() => null);
  const sub = reg && await reg.pushManager.getSubscription();
  if (sub && Notification.permission === 'granted') {
    showReminderControls();
  } else {
    $('reminders-controls').classList.add('hidden');
    $('enable-push-btn').classList.remove('hidden');
  }
}

async function enablePush() {
  try {
    setPushStatus('Requesting permission…');
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { setPushStatus('Permission denied. Enable it in your browser settings.', 'err'); return; }

    const reg = await navigator.serviceWorker.ready;
    const { public_key } = await api.get('/api/push/vapid-key');
    if (!public_key) { setPushStatus('Push is not configured on the server.', 'err'); return; }

    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(public_key),
      });
    }
    const json = sub.toJSON();
    await api.post('/api/push/subscribe', { endpoint: json.endpoint, keys: json.keys });
    setPushStatus('Notifications enabled ✓', 'ok');
    showReminderControls();
  } catch (err) {
    setPushStatus(`Could not enable: ${err.message}`, 'err');
  }
}

function showReminderControls() {
  $('enable-push-btn').classList.add('hidden');
  $('reminders-controls').classList.remove('hidden');
  loadReminders();
}

async function loadReminders() {
  const list = $('reminders-list');
  try {
    const reminders = await api.get('/api/reminders/');
    if (!reminders.length) {
      list.innerHTML = '<p class="reminders-intro">No reminder times yet. Add one below.</p>';
      return;
    }
    list.innerHTML = reminders.map(r => `
      <div class="reminder-row${r.enabled ? '' : ' off'}" data-id="${r.id}">
        <input type="checkbox" class="reminder-toggle" ${r.enabled ? 'checked' : ''}>
        <span class="reminder-time-label">${fmtTime(r.time_local)}</span>
        <button class="reminder-del" title="Remove">${icon('x')}</button>
      </div>`).join('');

    list.querySelectorAll('.reminder-row').forEach(row => {
      const id = row.dataset.id;
      row.querySelector('.reminder-toggle').addEventListener('change', async function () {
        try { await api.put(`/api/reminders/${id}`, { enabled: this.checked }); row.classList.toggle('off', !this.checked); }
        catch (err) { showToast(err.message, 'error'); }
      });
      row.querySelector('.reminder-del').addEventListener('click', async () => {
        try { await api.del(`/api/reminders/${id}`); loadReminders(); }
        catch (err) { showToast(err.message, 'error'); }
      });
    });
  } catch (err) {
    list.innerHTML = `<p class="push-status err">${esc(err.message)}</p>`;
  }
}

// "13:00" → "1:00 PM"
function fmtTime(hhmm) {
  const [h, m] = hhmm.split(':').map(Number);
  const ampm = h < 12 ? 'AM' : 'PM';
  const h12 = h % 12 || 12;
  return `${h12}:${String(m).padStart(2, '0')} ${ampm}`;
}

$('enable-push-btn').addEventListener('click', enablePush);

$('add-reminder-btn').addEventListener('click', async () => {
  const time = $('reminder-time').value;
  if (!time) return;
  try {
    await api.post('/api/reminders/', { time_local: time, tz_offset: new Date().getTimezoneOffset() });
    loadReminders();
  } catch (err) {
    showToast(err.message, 'error');
  }
});

$('test-push-btn').addEventListener('click', async () => {
  try {
    await api.post('/api/push/test', {});
    setPushStatus('Test sent — check your notifications.', 'ok');
  } catch (err) {
    setPushStatus(err.message, 'err');
  }
});

// ── Quick-picks, favorites & recipes (Phase 8) ────────────────────────────────
state.favoriteIds = new Set();

function numOr0(id) {
  const v = $(id).value.trim();
  return v === '' ? 0 : Number(v);
}

async function loadQuickPicks() {
  try {
    const { favorites, recents } = await api.get('/api/foods/quick');
    state.favoriteIds = new Set(favorites.map(f => f.id));
    const chips = [
      ...favorites.map(f => ({ f, star: true })),
      ...recents.map(f => ({ f, star: false })),
    ];
    const el = $('quick-picks');
    el.innerHTML = chips.map(({ f, star }, i) =>
      `<button class="chip${star ? ' chip-star' : ''}" data-idx="${i}">${star ? icon('star', 'chip-star-ic') : ''}${esc(f.name)}</button>`
    ).join('');
    el.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => {
      const food = chips[+c.dataset.idx].f;
      openConfirm(food, food.serving_g || 100, 'manual', food.serving_g ? 1 : null);
    }));
  } catch { /* quick-picks are best-effort */ }
}

// Favorite star in the confirm panel
function renderFavStar() {
  const star = $('confirm-fav-star');
  const food = state.pendingFood;
  star.classList.toggle('on', !!(food && state.favoriteIds.has(food.id)));
}

$('confirm-fav-star').addEventListener('click', async () => {
  const food = state.pendingFood;
  if (!food) return;
  const on = state.favoriteIds.has(food.id);
  try {
    if (on) { await api.del(`/api/foods/${food.id}/favorite`); state.favoriteIds.delete(food.id); }
    else { await api.post(`/api/foods/${food.id}/favorite`, {}); state.favoriteIds.add(food.id); }
    renderFavStar();
    loadQuickPicks();
  } catch (err) { showToast(err.message, 'error'); }
});

// ── Create recipe / custom food ───────────────────────────────────────────────
let _cfMode = 'ingredients';
let _cfIngredients = [];   // [{food, quantity_g}]

function openCreate(prefillName = '') {
  $('cf-name').value = prefillName;
  _cfIngredients = [];
  $('cf-servings').value = 1;
  $('cf-serving-label').value = '';
  $('cf-manual-label').value = '';
  ['cf-cal', 'cf-protein', 'cf-carbs', 'cf-fat'].forEach(id => { $(id).value = ''; });
  $('cf-add-ingredient').innerHTML = '';
  $('cf-web-note').classList.add('hidden');
  setCfMode('ingredients');
  renderCfIngredients();
  loadMyFoods();
  $('create-overlay').classList.remove('hidden');
  $('cf-name').focus();
}

function closeCreate() { $('create-overlay').classList.add('hidden'); }

function setCfMode(mode) {
  _cfMode = mode;
  document.querySelectorAll('.ctab').forEach(t => t.classList.toggle('active', t.dataset.mode === mode));
  $('cf-ingredients-mode').classList.toggle('hidden', mode !== 'ingredients');
  $('cf-manual-mode').classList.toggle('hidden', mode !== 'manual');
}

function renderCfIngredients() {
  const el = $('cf-ingredient-list');
  el.innerHTML = _cfIngredients.map((ing, i) => `
    <div class="cf-ingredient" data-idx="${i}">
      <span class="ci-name">${esc(ing.food.name)}</span>
      <input type="number" class="ci-qty" value="${Math.round(ing.quantity_g)}" min="1">
      <span class="ci-unit">g</span>
      <button class="ci-del" title="Remove">${icon('x')}</button>
    </div>`).join('');
  el.querySelectorAll('.cf-ingredient').forEach(row => {
    const i = +row.dataset.idx;
    row.querySelector('.ci-qty').addEventListener('input', function () {
      _cfIngredients[i].quantity_g = parseFloat(this.value) || 0;
      renderCfPreview();
    });
    row.querySelector('.ci-del').addEventListener('click', () => {
      _cfIngredients.splice(i, 1);
      renderCfIngredients();
    });
  });
  renderCfPreview();
}

function renderCfPreview() {
  let cal = 0, w = 0;
  for (const ing of _cfIngredients) {
    cal += (ing.food.nutrients_per_100g.calories || 0) * ing.quantity_g / 100;
    w += ing.quantity_g;
  }
  const servings = parseFloat($('cf-servings').value) || 1;
  $('cf-preview').textContent = _cfIngredients.length
    ? `${Math.round(w)} g total · ~${Math.round(cal / servings)} cal per serving`
    : 'Add ingredients to compute nutrition.';
}

$('cf-add-ingredient-btn').addEventListener('click', () => {
  const container = $('cf-add-ingredient');
  mountFoodPicker(container, (food) => {
    _cfIngredients.push({ food, quantity_g: food.serving_g || 100 });
    container.innerHTML = '';
    renderCfIngredients();
  });
});

$('cf-servings').addEventListener('input', renderCfPreview);
document.querySelectorAll('.ctab').forEach(t => t.addEventListener('click', () => setCfMode(t.dataset.mode)));
$('create-food-btn').addEventListener('click', () => openCreate());
$('create-close').addEventListener('click', closeCreate);
$('cf-cancel-btn').addEventListener('click', closeCreate);

$('cf-save-btn').addEventListener('click', async () => {
  const name = $('cf-name').value.trim();
  if (!name) { showToast('Name is required.', 'error'); return; }

  let body;
  if (_cfMode === 'ingredients') {
    if (!_cfIngredients.length) { showToast('Add an ingredient, or use Quick macros.', 'error'); return; }
    body = {
      name,
      servings: parseFloat($('cf-servings').value) || 1,
      serving_label: $('cf-serving-label').value.trim() || null,
      ingredients: _cfIngredients.map(i => ({ food_id: i.food.id, quantity_g: i.quantity_g })),
    };
  } else {
    body = {
      name,
      serving_label: $('cf-manual-label').value.trim() || null,
      calories: numOr0('cf-cal'), protein_g: numOr0('cf-protein'),
      carbs_g: numOr0('cf-carbs'), fat_g: numOr0('cf-fat'),
    };
  }

  try {
    const food = await api.post('/api/recipes/', body);
    closeCreate();
    showToast(`Saved ${food.name}!`, 'success');
    loadQuickPicks();
    openConfirm(food, food.serving_g || 100, 'manual', food.serving_g ? 1 : null);  // offer to log it now
  } catch (err) {
    showToast(err.message, 'error');
  }
});

async function loadMyFoods() {
  const el = $('cf-mine-list');
  try {
    const foods = await api.get('/api/foods/mine');
    if (!foods.length) { el.innerHTML = '<p class="reminders-intro">Nothing saved yet.</p>'; return; }
    el.innerHTML = foods.map(f => `
      <div class="cf-mine-row" data-id="${f.id}">
        <span class="cm-name">${esc(f.name)}</span>
        <span class="cm-kind">${f.source === 'recipe' ? 'recipe' : 'custom'}</span>
        <button class="cm-del" title="Delete">${icon('x')}</button>
      </div>`).join('');
    el.querySelectorAll('.cf-mine-row').forEach(row => {
      row.querySelector('.cm-del').addEventListener('click', async () => {
        try { await api.del(`/api/recipes/${row.dataset.id}`); loadMyFoods(); loadQuickPicks(); }
        catch (err) { showToast(err.message, 'error'); }
      });
    });
  } catch (err) {
    el.innerHTML = `<p class="push-status err">${esc(err.message)}</p>`;
  }
}

// (The old brand-miss overlay is gone — the agent now resolves brands itself,
// searching the web and creating the food when the database misses.)

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(str) {
  return (str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function sourceIcon(source) {
  const name = { manual: 'pencil', voice: 'mic', photo: 'camera', shared: 'users' }[source] || 'pencil';
  return icon(name, 'src-ic');
}

// Which database the *nutrition* came from (foods.source), for transparency.
const FOOD_SOURCE_LABELS = {
  usda: 'USDA', off: 'Open Food Facts', fatsecret: 'FatSecret',
  user: 'your custom food', recipe: 'your recipe', manual: 'manual entry',
  web: 'the web (published)', estimate: 'AI estimate',
};
function foodSourceLabel(source) {
  if (!source) return '';
  return FOOD_SOURCE_LABELS[source] || source;
}

// ── Service worker registration ───────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// ── Bootstrap: check existing session ────────────────────────────────────────
(async () => {
  try {
    state.user = await api.get('/api/auth/me');
    await showApp();
  } catch {
    // Not logged in — show auth screen (already visible by default)
  }
})();
