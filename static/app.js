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
const confirmPanel = $('confirm-panel');
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
const dashboardScreen = $('dashboard-screen');

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
  state.user = null;
  authScreen.classList.remove('hidden');
  appScreen.classList.add('hidden');
});

// ── App init ─────────────────────────────────────────────────────────────────
async function showApp() {
  authScreen.classList.add('hidden');
  appScreen.classList.remove('hidden');
  await goToToday();   // loads the day log + quick-picks (the date shows in #day-nav)
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
  confirmPanel.classList.remove('hidden');
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
  srcEl.textContent = srcLabel ? `📊 Nutrition from ${srcLabel}` : '';
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
    confirmPanel.classList.add('hidden');
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
  confirmPanel.classList.add('hidden');
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
function showResultCard(result, photoUrl = '') {
  setPolaroid($('result-photo'), photoUrl);
  $('result-summary').textContent = result.summary || '';
  const t = $('result-transcript');
  const showTranscript = !!result.transcript;
  t.textContent = showTranscript ? `“${result.transcript}”` : '';
  t.classList.toggle('hidden', !showTranscript);
  renderResultEntries(result.entries || []);
  $('result-card').classList.remove('hidden');
  confirmPanel.classList.add('hidden');
}

function renderResultEntries(entries) {
  const wrap = $('result-entries');
  if (!entries.length) {
    wrap.innerHTML = '';
    return;
  }
  wrap.innerHTML = entries.map(e => `
    <div class="result-entry" data-id="${e.id}">
      <div class="result-entry-info">
        <div class="result-entry-name">${esc(e.food_name)}${e.food_brand ? ' · ' + esc(e.food_brand) : ''}</div>
        <div class="result-entry-meta">${Math.round(e.quantity_g)}g · ${e.calories.toFixed(0)} cal · 📊 ${esc(e.food_source || '')}</div>
      </div>
      <button class="result-adjust link-btn" data-id="${e.id}" data-food="${e.food_id}" data-qty="${e.quantity_g}">Adjust</button>
      <button class="result-undo link-btn" data-id="${e.id}">Undo</button>
    </div>`).join('');

  wrap.querySelectorAll('.result-undo').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        await api.del(`/api/log/${btn.dataset.id}`);
        btn.closest('.result-entry').remove();
        await goToToday();
      } catch (err) { showToast(err.message, 'error'); }
    });
  });

  // Adjust = undo the entry, then reopen the classic confirm panel prefilled
  // with the same food/quantity so the user can correct and re-log it.
  wrap.querySelectorAll('.result-adjust').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        const food = await api.get(`/api/foods/${btn.dataset.food}`);
        await api.del(`/api/log/${btn.dataset.id}`);
        await goToToday();
        $('result-card').classList.add('hidden');
        openConfirm(food, parseFloat(btn.dataset.qty) || 100, 'manual');
      } catch (err) { showToast(err.message, 'error'); }
    });
  });
}

$('result-close').addEventListener('click', () => $('result-card').classList.add('hidden'));

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

function entryHtml(e) {
  return `<div class="log-entry">
    <span class="log-source-icon">${sourceIcon(e.source)}</span>
    <div class="log-info">
      <div class="log-name">${esc(e.food_name)}</div>
      <div class="log-meta">${e.quantity_g}g${e.food_brand ? ' · ' + esc(e.food_brand) : ''}</div>
      ${e.food_source ? `<div class="log-src">📊 ${esc(e.food_source)}</div>` : ''}
    </div>
    <div class="log-nutrition">
      <div class="log-cal">${e.calories.toFixed(0)}</div>
      <div class="log-macros">P${e.protein_g.toFixed(0)} C${e.carbs_g.toFixed(0)} F${e.fat_g.toFixed(0)}</div>
    </div>
    <button class="log-delete" data-id="${e.id}" title="Remove">✕</button>
  </div>`;
}

function renderLog() {
  if (!state.dayLog.length) {
    logList.innerHTML = '<p class="empty-state">Nothing logged today.<br>Search a food or tap the mic 🎤</p>';
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
    `<span class="glass${i < glasses ? ' filled' : ''}" data-i="${i}">💧</span>`).join('');
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
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    showVoiceMsg('Microphone is blocked. Allow mic access for this site in your browser settings.', 8000);
    return;
  }

  voiceStatus.classList.add('hidden');
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
      showVoiceMsg("Didn't record anything — tap 🎤 and try again.", 5000);
      return;
    }
    submitAgentLog({ audio: blob });
  };

  openVoiceOverlay();
  mediaRecorder.start(250);   // flush chunks as we go
  setListening(true);
}

voiceBtn.addEventListener('click', startVoiceCapture);

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

photoBtn.addEventListener('click', () => photoInput.click());

photoInput.addEventListener('change', async () => {
  const file = photoInput.files && photoInput.files[0];
  photoInput.value = '';  // allow re-selecting the same file later
  if (!file) return;

  photoBtn.classList.add('busy');
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

// ── Dashboard ─────────────────────────────────────────────────────────────────
let _dashDays = 7;

$('dashboard-btn').addEventListener('click', openDashboard);
$('dash-back-btn').addEventListener('click', () => {
  dashboardScreen.classList.add('hidden');
  appScreenEl.classList.remove('hidden');
});

document.querySelectorAll('.range-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    _dashDays = +btn.dataset.days;
    loadChart();
  });
});

async function openDashboard() {
  appScreenEl.classList.add('hidden');
  dashboardScreen.classList.remove('hidden');
  prefillGoals();
  renderGoalProgress();
  initReminders();
  await loadChart();
}

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
        <span class="gp-vals">${v} / ${Math.round(goal)}${unit}${over ? ' ⚠' : ''}</span></div>
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
        <button class="reminder-del" title="Remove">✕</button>
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
      `<button class="chip${star ? ' chip-star' : ''}" data-idx="${i}">${star ? '★ ' : ''}${esc(f.name)}</button>`
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
      <button class="ci-del" title="Remove">✕</button>
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
        <button class="cm-del" title="Delete">🗑</button>
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
  return { manual: '✏️', voice: '🎤', photo: '📷', shared: '🤝' }[source] || '•';
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
