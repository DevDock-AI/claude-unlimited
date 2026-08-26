const CSRF = document.querySelector('meta[name=csrf-token]').content;

// ---- live-update rendering ----
// Every panel in the live-poll loop writes through setLiveHtml() instead of
// a bare `el.innerHTML =`. It patches the DOM in place: unchanged nodes are
// never touched, and the container element is never replaced.
//
// Sibling groups whose children carry [data-id] are reconciled by that key
// rather than by position, so inserting/removing/reordering one item reuses
// real DOM nodes instead of rewriting every sibling.
//
// Because morphing reuses nodes across updates, listeners must be delegated
// on the container — a per-node listener would stack a duplicate on every
// poll for any node that survives the update.
const _lastLiveHtml = {};
function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function _morphAttributes(existing, next) {
  for (const attr of Array.from(existing.attributes)) {
    if (!next.hasAttribute(attr.name)) existing.removeAttribute(attr.name);
  }
  for (const attr of Array.from(next.attributes)) {
    if (existing.getAttribute(attr.name) !== attr.value) existing.setAttribute(attr.name, attr.value);
  }
}

function _isKeyed(node) {
  return node.nodeType !== 1 || node.hasAttribute('data-id');
}

function _morphNode(existing, next) {
  if (existing.nodeType !== next.nodeType || existing.nodeName !== next.nodeName) {
    existing.replaceWith(next);
    return;
  }
  if (existing.nodeType === 3 || existing.nodeType === 8) { // text / comment
    if (existing.nodeValue !== next.nodeValue) existing.nodeValue = next.nodeValue;
    return;
  }
  if (existing.nodeType !== 1) return;

  _morphAttributes(existing, next);
  _morphChildren(existing, Array.from(next.childNodes));
}

// Removes a keyed list item with a brief fade/scale-out instead of an
// instant disappearance.
function _removeWithExit(node) {
  if (prefersReducedMotion() || node.nodeType !== 1) { node.remove(); return; }
  node.classList.add('live-exit');
  const done = () => node.remove();
  node.addEventListener('animationend', done, { once: true });
  setTimeout(done, 500); // safety net if animationend never fires (e.g. display:none ancestor)
}

function _isBlankText(node) {
  return node.nodeType === 3 && !node.nodeValue.trim();
}

function _morphChildren(container, nextChildren) {
  // Repeated blocks are built as `blocks.map(fn).join('')` over multi-line
  // template literals, which leaves a whitespace-only text node between each
  // pair of real elements. A text node never has [data-id], so it is
  // unmatched on every render and re-inserted every time, which desyncs the
  // keyed loop's `cursor` and makes nearly every sibling look like it moved.
  // Strip them from both sides before reconciling.
  for (const n of Array.from(container.childNodes)) {
    if (_isBlankText(n)) container.removeChild(n);
  }
  nextChildren = nextChildren.filter((n) => !_isBlankText(n));

  const existingChildren = Array.from(container.childNodes);
  const keyed = nextChildren.length > 0 && nextChildren.every(_isKeyed) && existingChildren.every(_isKeyed)
             && (nextChildren.some((n) => n.nodeType === 1) || existingChildren.some((n) => n.nodeType === 1));

  if (!keyed) {
    // Positional reconciliation — correct for fixed, non-repeating
    // structure (e.g. a card's internal .p-top/.p-bars/.p-foot), where
    // "the node at index i" IS the semantic identity.
    const max = Math.max(existingChildren.length, nextChildren.length);
    for (let i = 0; i < max; i++) {
      const ec = existingChildren[i];
      const nc = nextChildren[i];
      if (!nc) { ec.remove(); continue; }
      if (!ec) { container.appendChild(nc); continue; }
      _morphNode(ec, nc);
    }
    return;
  }

  // Keyed reconciliation — matches by data-id so a real DOM node is reused
  // (content-morphed, and moved if order changed) rather than torn down
  // and rebuilt just because its position shifted or a sibling was
  // inserted/removed elsewhere in the list.
  const existingByKey = new Map();
  existingChildren.forEach((n) => { if (n.nodeType === 1) existingByKey.set(n.getAttribute('data-id'), n); });

  let cursor = container.firstChild;
  const seen = new Set();
  for (const nc of nextChildren) {
    const key = nc.nodeType === 1 ? nc.getAttribute('data-id') : null;
    const matched = key !== null ? existingByKey.get(key) : null;

    if (matched) {
      seen.add(key);
      _morphNode(matched, nc);
      if (matched !== cursor) container.insertBefore(matched, cursor);
      cursor = matched.nextSibling;
    } else {
      // .live-enter plays once via animation-fill-mode:both, so no JS
      // cleanup is needed.
      if (nc.nodeType === 1 && !prefersReducedMotion()) nc.classList.add('live-enter');
      container.insertBefore(nc, cursor);
    }
  }
  for (const [key, node] of existingByKey) {
    if (!seen.has(key)) _removeWithExit(node);
  }
}

function setLiveHtml(el, html, onRendered) {
  if (!el) return false;
  const key = el.id;
  if (key && _lastLiveHtml[key] === html) return false;
  const isFirstRender = key ? !(key in _lastLiveHtml) : el.childNodes.length === 0;
  if (key) _lastLiveHtml[key] = html;

  const template = document.createElement('template');
  template.innerHTML = html;

  if (isFirstRender || prefersReducedMotion()) {
    // Nothing to visually preserve yet (or reduced-motion preference set) —
    // a plain swap is correct and simpler than morphing against nothing.
    el.innerHTML = '';
    el.appendChild(template.content);
  } else {
    _morphChildren(el, Array.from(template.content.childNodes));
  }
  if (onRendered) onRendered();
  return true;
}

// ---- i18n ----

let _strings = {};

function t(key) {
  return _strings[key] || key;
}

function applyTranslations() {
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  document.querySelectorAll('[data-i18n-title]').forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
}

async function loadLocales() {
  const select = document.getElementById('languageSelect');
  const railSelect = document.getElementById('railLangSelect');
  try {
    const { available, names, current } = await api('/api/locales');
    select.dataset.value = current;
    setupSelectInput(select, available.map((code) => ({ value: code, label: names[code] || code })), (code) => setLanguage(code));
    if (railSelect) {
      railSelect.dataset.value = current;
      railSelect.title = 'Language';
      setupSelectInput(railSelect, available.map((code) => ({ value: code, label: code.toUpperCase() })), (code) => setLanguage(code));
    }
    await setLanguage(current, { persist: false });
  } catch (e) {
    // Dashboard stays in its built-in English strings if this fails.
  }
}

async function setLanguage(code, { persist } = { persist: true }) {
  const { strings } = await api(`/api/locales/${encodeURIComponent(code)}`);
  _strings = strings;
  applyTranslations();
  renderNotifList();
  const updateModeSelect = document.getElementById('updateModeSelect');
  if (updateModeSelect && updateModeSelect._cuRenderValue) {
    updateModeSelect._cuOptions = UPDATE_MODE_OPTIONS();
    updateModeSelect._cuRenderValue();
  }
  // Same treatment as updateModeSelect above — these are wired at module
  // load, before loadLocales() resolves, so their labels start out as raw
  // i18n keys until this runs.
  ['f_codex_model', 'pd_codex_model'].forEach((id) => {
    const el = document.getElementById(id);
    if (el && el._cuRenderValue) { el._cuOptions = CODEX_MODEL_OPTIONS(); el._cuRenderValue(); }
  });
  ['f_codex_reasoning', 'pd_codex_reasoning'].forEach((id) => {
    const el = document.getElementById(id);
    if (el && el._cuRenderValue) { el._cuOptions = CODEX_REASONING_OPTIONS(); el._cuRenderValue(); }
  });
  // The model-help text is set via JS (not data-i18n), so applyTranslations()
  // never touches it — re-derive it here.
  updateCodexModelHelp('f');
  updateCodexModelHelp('pd');
  // Keep the Settings-page language select and the compact rail one in sync
  // no matter which one triggered the change.
  [document.getElementById('languageSelect'), document.getElementById('railLangSelect')].forEach((el) => {
    if (el && el._cuRenderValue) {
      el.dataset.value = code;
      el._cuRenderValue();
    }
  });
  if (persist) await api('/api/settings', { method: 'PATCH', body: JSON.stringify({ language: code }) });
}

// ---- helpers ----

let _csrfRecoveryStarted = false;

async function api(path, opts = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  if (opts.method && opts.method !== 'GET') headers['X-CSRF-Token'] = CSRF;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  const body = await res.json();
  if (!res.ok) {
    if (body.error === 'csrf') {
      // The CSRF token is generated at daemon startup and embedded once into
      // this page's HTML, so it can never match again after a daemon restart
      // while the tab stays open. Reload instead of surfacing a raw 403.
      if (!_csrfRecoveryStarted) {
        _csrfRecoveryStarted = true;
        showToast('info', t('toast.session_refreshing'), t('toast.session_refreshing_sub'));
        setTimeout(() => window.location.reload(), 1200);
      }
      throw new Error(t('toast.session_refreshing'));
    }
    const err = new Error(body.message || body.error || 'request failed');
    err.code = body.error;
    throw err;
  }
  return body;
}

function esc(s) {
  return (s ?? '').toString().replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// Past a day, roll into days and drop the minutes: "163h 12m" doesn't read
// as "about a week", and minute precision on a multi-day countdown is noise.
function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatPastRelative(isoString) {
  const then = new Date(isoString).getTime();
  const diffSeconds = Math.max(0, (Date.now() - then) / 1000);
  if (diffSeconds < 60) return t('activity.moments_ago');
  const minutes = Math.floor(diffSeconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 5) return `${weeks}w ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  const years = Math.floor(days / 365);
  return `${years}y ago`;
}

function formatFutureRelative(isoString) {
  if (!isoString) return null;
  const target = new Date(isoString).getTime();
  const diffSeconds = (target - Date.now()) / 1000;
  // Every caller renders this after "resets in", so the past/now case has to
  // be a duration too rather than a word.
  if (diffSeconds <= 0) return '< 1m';
  return formatDuration(diffSeconds);
}

// ---- theme + chart-type preference (local UI state, not server-side) ----

function applyTheme(theme) {
  document.documentElement.classList.toggle('light-theme', theme === 'light');
  document.getElementById('appRoot').classList.toggle('light-theme', theme === 'light');
  document.querySelectorAll('[data-theme-choice]').forEach((el) => el.classList.toggle('on', el.dataset.themeChoice === theme));
  localStorage.setItem('cu-theme', theme);
}

// Each chart card owns its own bars/round preference via a per-card toggle.
const CHART_TARGETS = {
  tokens: { bars: 'tokensChartBars', round: 'tokensChartRings' },
  model: { bars: 'modelSplitBars', round: 'modelSplitDonut' },
  account: { bars: 'accountUsageBars', round: 'accountUsageRound' },
};

function applyMiniChartStyle(target, style) {
  const ids = CHART_TARGETS[target];
  if (!ids) return;
  document.querySelectorAll(`.mini-chart-toggle[data-chart-target="${target}"] .mini-chart-opt`).forEach((el) => {
    el.classList.toggle('on', el.dataset.chartChoice === style);
  });
  const barsEl = document.getElementById(ids.bars);
  const roundEl = document.getElementById(ids.round);
  if (barsEl) barsEl.style.display = style === 'bars' ? '' : 'none';
  if (roundEl) roundEl.style.display = style === 'round' ? '' : 'none';
  localStorage.setItem(`cu-chart-style-${target}`, style);
}

function currentMiniChartStyle(target) {
  return localStorage.getItem(`cu-chart-style-${target}`) || 'bars';
}

// Overview's Profiles list vs. cells ("2 per row") layout. Purely a CSS
// class on the container — renderProfileCard() emits the same markup either
// way, so the two views can't drift apart. Scoped to the Overview container
// by id; the Profiles page renders a table with its own controls.
function currentProfilesView() {
  return localStorage.getItem('cu-profiles-view') === 'cells' ? 'cells' : 'list';
}

function applyProfilesView(view) {
  localStorage.setItem('cu-profiles-view', view);
  const container = document.getElementById('profiles');
  if (container) container.classList.toggle('cells-view', view === 'cells');
  document.querySelectorAll('#profilesViewToggle .mini-chart-opt').forEach((el) => {
    el.classList.toggle('on', el.dataset.profilesView === view);
  });
}

function wireProfilesViewToggle() {
  const toggle = document.getElementById('profilesViewToggle');
  if (!toggle) return;
  toggle.addEventListener('click', (e) => {
    const opt = e.target.closest('.mini-chart-opt');
    if (opt) applyProfilesView(opt.dataset.profilesView);
  });
  applyProfilesView(currentProfilesView());
}

// Percent vs. absolute token count, independent per card. Only the displayed
// label changes; bar widths and slice geometry always stay percent-of-total.
function currentMetricMode(target) {
  return localStorage.getItem(`cu-metric-mode-${target}`) || 'percent';
}

function applyMetricMode(target, mode) {
  localStorage.setItem(`cu-metric-mode-${target}`, mode);
  document.querySelectorAll(`.mini-metric-toggle[data-metric-target="${target}"] .mini-metric-opt`).forEach((el) => {
    el.classList.toggle('on', el.dataset.metricChoice === mode);
  });
}

// ---- chart hover tooltips ----
// One shared element appended directly to <body> and positioned via
// getBoundingClientRect(). A CSS ::after tooltip would be clipped by any
// overflow:hidden ancestor of the [data-tooltip] element. Delegated on
// `document` because chart content is re-rendered on every poll.
let _tooltipEl = null;
function _tooltipTarget(e) {
  return e.target.closest ? e.target.closest('[data-tooltip]') : null;
}
function _positionTooltip(target) {
  const rect = target.getBoundingClientRect();
  _tooltipEl.style.left = `${rect.left + rect.width / 2}px`;
  _tooltipEl.style.top = `${rect.top}px`;
}
function wireDataTooltips() {
  _tooltipEl = document.createElement('div');
  _tooltipEl.className = 'js-tooltip';
  document.body.appendChild(_tooltipEl);

  document.addEventListener('mouseover', (e) => {
    const target = _tooltipTarget(e);
    if (!target) return;
    _tooltipEl.textContent = target.dataset.tooltip;
    _tooltipEl.classList.add('on');
    _positionTooltip(target);
  });
  document.addEventListener('mousemove', (e) => {
    if (!_tooltipEl.classList.contains('on')) return;
    const target = _tooltipTarget(e);
    if (target) _positionTooltip(target);
  });
  document.addEventListener('mouseout', (e) => {
    const target = _tooltipTarget(e);
    if (!target) return;
    // Moving to a child of the same tooltipped element (e.g. its own SVG
    // content) isn't a real "left the element" — only hide once the
    // pointer is genuinely outside it.
    if (e.relatedTarget && target.contains(e.relatedTarget)) return;
    _tooltipEl.classList.remove('on');
  });
}

// ---- ambient cursor glow ----

function wireCursorGlow() {
  const glow = document.getElementById('cursorGlow');
  if (!glow) return;
  window.addEventListener('mousemove', (e) => {
    glow.style.setProperty('--mx', `${e.clientX}px`);
    glow.style.setProperty('--my', `${e.clientY}px`);
    glow.classList.add('on');
  });
  window.addEventListener('mouseleave', () => glow.classList.remove('on'));
}

// ---- toasts ----

const TOAST_ICONS = {
  info: { icon: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/>', color: 'var(--accent)', bg: 'var(--accent-soft)' },
  success: { icon: '<path d="M20 6 9 17l-5-5"/>', color: 'var(--good)', bg: 'var(--good-soft)' },
  warn: { icon: '<path d="M12 9v4m0 4h.01M10.3 3.9 2.7 17a2 2 0 0 0 1.7 3h15.2a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>', color: 'var(--warn)', bg: 'var(--warn-soft)' },
  error: { icon: '<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/>', color: 'var(--bad)', bg: 'var(--bad-soft)' },
};

function showToast(type, title, sub, { duration } = { duration: 5000 }) {
  const container = document.getElementById('toastContainer');
  if (!container) return;
  const spec = TOAST_ICONS[type] || TOAST_ICONS.info;
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = `
    <div class="toast-icon" style="background:${spec.bg};color:${spec.color}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${spec.icon}</svg>
    </div>
    <div class="toast-body">
      <div class="toast-title">${esc(title)}</div>
      ${sub ? `<div class="toast-sub">${esc(sub)}</div>` : ''}
    </div>
  `;
  const dismiss = () => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 240);
  };
  el.addEventListener('click', dismiss);
  container.appendChild(el);
  setTimeout(dismiss, duration);
}

// ---- connection-lost banner ----

let _connectionOk = true;

async function checkConnection() {
  const banner = document.getElementById('connectionBanner');
  const sub = document.getElementById('connectionBannerSub');
  try {
    const res = await fetch('/health', { cache: 'no-store' });
    if (!res.ok) throw new Error('unhealthy');
    if (!_connectionOk) { _connectionOk = true; refreshCurrentView(); }
    banner.style.display = 'none';
  } catch (e) {
    _connectionOk = false;
    banner.style.display = '';
    if (sub) sub.textContent = `${window.location.host} isn't responding — showing the last known state`;
  }
}

function refreshCurrentView() {
  const activeItem = document.querySelector('.rail-item.active');
  const view = activeItem ? activeItem.dataset.view : 'overview';
  if (view === 'overview') { loadStatus().then(() => loadProfiles()); loadProjectUsage(); loadUsageSummary(); loadActivityPreview(); }
  if (view === 'profiles') loadProfilesTable();
  if (view === 'activity') loadActivity();
  if (view === 'settings') loadSettings();
}

// ---- status / stat strip ----

let _lastStatus = null;

async function loadStatus() {
  try {
    const s = await api('/api/status');
    _lastStatus = s;
    const versionChip = document.getElementById('versionChip');
    if (versionChip) versionChip.textContent = 'v' + s.version;
    const uptimeChip = document.getElementById('uptimeChip');
    if (uptimeChip) uptimeChip.textContent = 'up ' + formatDuration(s.uptime_seconds);
    const hostChip = document.getElementById('hostChip');
    if (hostChip) hostChip.textContent = window.location.host || 'claude.unlimited:4317';
  } catch (e) {
    _lastStatus = null;
  }
}

function renderStatStrip(profiles) {
  const el = document.getElementById('statStrip');
  if (!el) return;

  const activeName = (_lastStatus && _lastStatus.current_profile_name) || t('stat.no_active');
  const enabledCount = profiles.filter((p) => p.enabled).length;

  // Both reset fields must be considered: an oauth Profile populates only
  // usage_7d_resets_at, so checking usage_5h_resets_at alone would skip
  // every plain Claude subscription and report a later reset than the true
  // soonest one.
  let soonest = null;
  for (const p of profiles) {
    for (const iso of [p.usage_5h_resets_at, p.usage_7d_resets_at]) {
      if (!iso) continue;
      const at = new Date(iso).getTime();
      if (soonest === null || at < soonest.at) soonest = { at, name: p.name, iso };
    }
  }
  const nextResetValue = soonest ? `${soonest.name} · ${formatFutureRelative(soonest.iso)}` : t('stat.none');

  setLiveHtml(el, `
    <div class="stat-chip">
      <div class="stat-chip-label">${esc(t('stat.active_profile'))}</div>
      <div class="stat-chip-value accent">${esc(activeName)}</div>
    </div>
    <div class="stat-chip">
      <div class="stat-chip-label">${esc(t('stat.enabled'))}</div>
      <div class="stat-chip-value">${enabledCount} / ${profiles.length}</div>
    </div>
    <div class="stat-chip">
      <div class="stat-chip-label">${esc(t('stat.next_reset'))}</div>
      <div class="stat-chip-value">${esc(nextResetValue)}</div>
    </div>
  `);
}

// ---- profile cards (shared between Overview and Profiles page) ----

// Provider marks — the vendors' own logomarks, used to identify which
// upstream provider a Profile belongs to (nominative use, as on a "Sign in
// with X" button). Both are single-path 24x24 fill="currentColor" SVGs, so
// the existing size/color rules (.p-icon svg, .plan-badge svg, .type-icon
// svg) apply unchanged. Sources: Claude from simple-icons, OpenAI from
// lobe-icons.
//
// OAUTH = Claude's starburst logomark.
const OAUTH_ICON = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"/></svg>';
const API_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>';
// CODEX = OpenAI's blossom/knot logomark. fill-rule="evenodd" is load-bearing
// here: the mark is one self-overlapping path and renders as a filled blob
// without it.
const CODEX_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" fill-rule="evenodd" clip-rule="evenodd"><path d="M9.205 8.658v-2.26c0-.19.072-.333.238-.428l4.543-2.616c.619-.357 1.356-.523 2.117-.523 2.854 0 4.662 2.212 4.662 4.566 0 .167 0 .357-.024.547l-4.71-2.759a.797.797 0 00-.856 0l-5.97 3.473zm10.609 8.8V12.06c0-.333-.143-.57-.429-.737l-5.97-3.473 1.95-1.118a.433.433 0 01.476 0l4.543 2.617c1.309.76 2.189 2.378 2.189 3.948 0 1.808-1.07 3.473-2.76 4.163zM7.802 12.703l-1.95-1.142c-.167-.095-.239-.238-.239-.428V5.899c0-2.545 1.95-4.472 4.591-4.472 1 0 1.927.333 2.712.928L8.23 5.067c-.285.166-.428.404-.428.737v6.898zM12 15.128l-2.795-1.57v-3.33L12 8.658l2.795 1.57v3.33L12 15.128zm1.796 7.23c-1 0-1.927-.332-2.712-.927l4.686-2.712c.285-.166.428-.404.428-.737v-6.898l1.974 1.142c.167.095.238.238.238.428v5.233c0 2.545-1.974 4.472-4.614 4.472zm-5.637-5.303l-4.544-2.617c-1.308-.761-2.188-2.378-2.188-3.948A4.482 4.482 0 014.21 6.327v5.423c0 .333.143.571.428.738l5.947 3.449-1.95 1.118a.432.432 0 01-.476 0zm-.262 3.9c-2.688 0-4.662-2.021-4.662-4.519 0-.19.024-.38.047-.57l4.686 2.71c.286.167.571.167.856 0l5.97-3.448v2.26c0 .19-.07.333-.237.428l-4.543 2.616c-.619.357-1.356.523-2.117.523zm5.899 2.83a5.947 5.947 0 005.827-4.756C22.287 18.339 24 15.84 24 13.296c0-1.665-.713-3.282-1.998-4.448.119-.5.19-.999.19-1.498 0-3.401-2.759-5.947-5.946-5.947-.642 0-1.26.095-1.88.31A5.962 5.962 0 0010.205 0a5.947 5.947 0 00-5.827 4.757C1.713 5.447 0 7.945 0 10.49c0 1.666.713 3.283 1.998 4.448-.119.5-.19 1-.19 1.499 0 3.401 2.759 5.946 5.946 5.946.642 0 1.26-.095 1.88-.309a5.96 5.96 0 004.162 1.713z"/></svg>';

function kindIcon(p) {
  if (p.kind === 'oauth') return OAUTH_ICON;
  if (p.kind === 'codex') return CODEX_ICON;
  return API_ICON;
}

// Raw `plan` values reported for a chatgpt_subscription codex Profile —
// lowercase and sometimes abbreviated ("prolite"), so never displayed as-is.
// An api_key codex Profile has no ChatGPT plan at all (see planBadge below).
const CODEX_PLAN_LABEL_KEYS = {
  free: 'profiles.codex_plan_free',
  go: 'profiles.codex_plan_go',
  plus: 'profiles.codex_plan_plus',
  pro: 'profiles.codex_plan_pro',
  prolite: 'profiles.codex_plan_prolite',
  team: 'profiles.codex_plan_team',
  business: 'profiles.codex_plan_business',
  enterprise: 'profiles.codex_plan_enterprise',
  edu: 'profiles.codex_plan_edu',
};

// Distinct per subscription tier so it reads at a glance: PRO gets a subtle
// sparkle, MAX a gold/purple sweep (see .plan-badge CSS), an API key a plain
// neutral badge since "plan" doesn't apply, and codex its own flat --codex
// accent. No "5x"/"20x" multiplier: neither provider exposes it over an API,
// so there is no way to show it without guessing.
function planBadge(p) {
  if (p.kind === 'codex') {
    // An api_key codex Profile is a raw OpenAI API key — there's no ChatGPT
    // plan tier to show for it at all, so it gets its own label instead of
    // attempting a plan name (or silently showing a stale/wrong one).
    let codexLabel;
    if (p.auth_mode === 'api_key') {
      codexLabel = t('profiles.kind_codex_api_key_badge');
    } else {
      const planKey = p.plan && CODEX_PLAN_LABEL_KEYS[p.plan];
      // p.plan can be null (not yet resolved) or a plan value with no
      // display name here yet — show the raw string rather than hide it.
      codexLabel = planKey ? t(planKey) : (p.plan || t('profiles.kind_codex_badge'));
    }
    return `<span class="plan-badge codex">${CODEX_ICON}<span>${esc(codexLabel)}</span></span>`;
  }
  if (p.kind !== 'oauth') {
    return `<span class="plan-badge api">${API_ICON}<span>${esc(t('profiles.kind_api_badge'))}</span></span>`;
  }
  // Same icon and position as the codex/api badges above, for parity.
  if (p.plan === 'max') {
    return `<span class="plan-badge max" title="${esc(t('profiles.kind_oauth_max'))}">
      <span class="spark"></span><span class="spark"></span><span class="spark"></span>
      ${OAUTH_ICON}<span>${esc(t('profiles.plan_badge_max'))}</span></span>`;
  }
  if (p.plan === 'pro') {
    return `<span class="plan-badge pro" title="${esc(t('profiles.kind_oauth_pro'))}">
      <span class="spark"></span><span class="spark"></span><span class="spark"></span>
      ${OAUTH_ICON}<span>${esc(t('profiles.plan_badge_pro'))}</span></span>`;
  }
  return `<span class="plan-badge api" title="${esc(t('profiles.kind_oauth_unknown'))}">${OAUTH_ICON}<span>${esc(t('profiles.plan_badge_unknown'))}</span></span>`;
}

const STATUS_COLORS = {
  healthy: { color: 'var(--good)', bg: 'var(--good-soft)' },
  'near threshold': { color: 'var(--warn)', bg: 'var(--warn-soft)' },
  exhausted: { color: 'var(--bad)', bg: 'var(--bad-soft)' },
  cooldown: { color: 'var(--warn)', bg: 'var(--warn-soft)' },
  'needs re-auth': { color: 'var(--bad)', bg: 'var(--bad-soft)' },
};

// Percentage points below switch_threshold at which a still-healthy bar
// starts warning, ahead of the rotation/exhaustion states that already have
// their own colors via STATUS_COLORS.
const NEAR_THRESHOLD_BAND = 15;
const ALMOST_EXHAUSTED_BAND = 5;

// "healthy" and "disabled" both mean "nothing is alerting about this
// Profile". A disabled Profile's last-observed usage is still real and still
// worth coloring by proximity to the threshold, rather than falling through
// to a default color because "disabled" has no STATUS_COLORS entry.
function _isNeutralStatus(statusWord) {
  return statusWord === 'healthy' || statusWord === 'disabled';
}

function barColor(statusWord, percent, threshold) {
  if (_isNeutralStatus(statusWord) && percent !== null && percent !== undefined && threshold !== null && threshold !== undefined) {
    const remaining = threshold - percent;
    if (remaining <= ALMOST_EXHAUSTED_BAND) return 'var(--bad)';
    if (remaining <= NEAR_THRESHOLD_BAND) return 'var(--warn)';
    return 'var(--good)';
  }
  const c = STATUS_COLORS[statusWord];
  return c ? c.color : 'var(--good)';
}

// The 7d/weekly bar has no switch_threshold concept to be relative to (see
// renderProfileCard's bar7d call, threshold is always null there) — fixed
// absolute bands instead: green under 75%, yellow 75-90%, red 90%+.
function weeklyBarColor(statusWord, percent) {
  if (_isNeutralStatus(statusWord) && percent !== null && percent !== undefined) {
    if (percent >= 90) return 'var(--bad)';
    if (percent >= 75) return 'var(--warn)';
    return 'var(--good)';
  }
  const c = STATUS_COLORS[statusWord];
  return c ? c.color : 'var(--good)';
}

// Keeps a bar-end label from clipping against the card edge at either
// extreme — centered in the middle of the track, but pinned to the left
// or right edge once the position gets close enough to it.
function barTagStyle(pct) {
  const clamped = Math.max(0, Math.min(100, pct));
  let transform = 'translateX(-50%)';
  if (clamped < 8) transform = 'translateX(0)';
  else if (clamped > 92) transform = 'translateX(-100%)';
  return `left:${clamped}%;transform:${transform}`;
}

// Shared bar visual: an optional threshold tag/marker at thresholdPct, a
// fill bar, and a usage tag. Each number sits at its own x-position on the
// track — usage (colored to match the fill) under where the bar ends,
// threshold (gray) above its marker line — so neither can be misread as the
// other. Both tags take pre-formatted text, so this serves a percentage
// (renderUsageBar) and a raw token count (renderTokenBudgetBar) alike.
function _renderBarVisual(pct, thresholdPct, has, color, usageText, thresholdText) {
  const hasThreshold = thresholdPct !== null && thresholdPct !== undefined;
  const thresholdTag = hasThreshold
    ? `<div class="bar-threshold-tag" style="${barTagStyle(thresholdPct)}">${thresholdText}</div>` : '';
  const thresholdMarker = hasThreshold
    ? `<div class="bar-threshold" style="left:${Math.min(100, thresholdPct)}%"></div>` : '';
  const fill = has ? `<div class="bar-fill" style="width:${Math.min(100, pct)}%;background:${color}"></div>` : '';
  const usageTag = has
    ? `<div class="bar-usage-tag" style="${barTagStyle(pct)};color:${color}">${usageText}</div>`
    : `<div class="bar-usage-tag not-observed">${usageText}</div>`;
  return `
      <div class="bar-visual">
        ${thresholdTag}
        <div class="bar-track">${fill}${thresholdMarker}</div>
        ${usageTag}
      </div>`;
}

function renderUsageBar(label, pct, has, threshold, color) {
  const usageText = has ? `${pct}%` : esc(t('profile.not_observed'));
  const thresholdText = (threshold !== null && threshold !== undefined) ? `${threshold}%` : '';
  return `
    <div class="bar-group">
      <div class="bar-label-row"><span>${esc(label)}</span></div>
      ${_renderBarVisual(has ? pct : 0, threshold, has, color, usageText, thresholdText)}
    </div>`;
}

// API-kind Profile analogue of renderUsageBar — an absolute token count
// against a token_threshold budget instead of a percentage against a
// switch_threshold. The budget IS the full width of the bar (no separate
// "how close to the edge" concept the way OAuth's percentage threshold
// has), so the threshold marker always sits at the right edge.
function renderTokenBudgetBar(label, tokensUsed, tokenThreshold, color) {
  const hasLimit = typeof tokenThreshold === 'number' && tokenThreshold > 0;
  const pct = hasLimit ? Math.min(100, (tokensUsed / tokenThreshold) * 100) : 0;
  return `
    <div class="bar-group">
      <div class="bar-label-row"><span>${esc(label)}</span></div>
      ${_renderBarVisual(pct, hasLimit ? 100 : null, true, color, formatTokenCount(tokensUsed),
        hasLimit ? formatTokenCount(tokenThreshold) : '')}
    </div>`;
}

// usage_5h_percent/usage_7d_percent are legacy field names kept for backend
// compatibility; they really mean "first window" and "second window".
// An oauth/api Profile always has Claude's 5h+7d shape (usage_window_label
// null → the "5h session"/"7d weekly" strings). A codex Profile may report a
// real window duration instead, or expose only ONE window — in which case
// usage_7d_percent is null and callers must skip the second bar entirely.
const USAGE_WINDOW_LABEL_KEYS = {
  '5h': 'profiles.window_5h',
  daily: 'profiles.window_daily',
  weekly: 'profiles.window_weekly',
  monthly: 'profiles.window_monthly',
  annual: 'profiles.window_annual',
};

function usageWindowLabel(rawLabel, fallbackKey) {
  if (!rawLabel) return t(fallbackKey);
  const key = USAGE_WINDOW_LABEL_KEYS[rawLabel];
  return key ? t(key) : rawLabel;
}

// Only Claude's OAuth quota is a fixed 5h session / 7d weekly pair, so only
// oauth may fall back to naming those windows. A codex Profile's windows are
// whatever its backend reports (see openai_observation.py); with no label,
// say "Usage" rather than claim a "5h session" the account doesn't have.
function primaryWindowFallbackKey(p) {
  return p.kind === 'oauth' ? 'profile.session_5h' : 'profiles.window_usage';
}
function secondaryWindowFallbackKey(p) {
  return p.kind === 'oauth' ? 'profile.session_7d' : 'profiles.window_usage';
}

// Same lookup as usageWindowLabel, but returns '' instead of falling back to
// the "5h session"/"7d weekly" strings — for spots (the table's narrow bar
// columns) that only want to show a label word when there's a REAL one.
function usageWindowLabelOrBlank(rawLabel) {
  return rawLabel ? usageWindowLabel(rawLabel, '') : '';
}

function renderProfileCard(p) {
  const isActive = _lastStatus && _lastStatus.current_profile_id === p.id;
  const statusColors = STATUS_COLORS[p.status_word] || { color: 'var(--good)', bg: 'var(--good-soft)' };
  // The plan badge already names the tier, so this carries only the detail
  // it has no room for: the base_url. A chatgpt_subscription codex Profile
  // has no base_url (it routes through the `codex` CLI), so it stays blank
  // like oauth.
  const kindLabel = p.kind === 'oauth' ? '' :
    p.kind === 'codex' ? (p.auth_mode === 'api_key' ? esc(p.base_url || 'api.openai.com') : '') :
    esc(p.base_url || 'api.anthropic.com');

  const has5h = p.usage_5h_percent !== null && p.usage_5h_percent !== undefined;
  const has7d = p.usage_7d_percent !== null && p.usage_7d_percent !== undefined;
  const fillColor5h = barColor(p.status_word, p.usage_5h_percent, p.switch_threshold);
  const fillColor7d = weeklyBarColor(p.status_word, p.usage_7d_percent);

  const bar5h = renderUsageBar(usageWindowLabel(p.usage_window_label, primaryWindowFallbackKey(p)), p.usage_5h_percent, has5h, p.switch_threshold, fillColor5h);
  // No second window (codex accounts, typically) — render nothing rather
  // than an empty "7d" bar for a window that doesn't exist.
  const bar7d = has7d ? renderUsageBar(usageWindowLabel(p.usage_window_label_7d, secondaryWindowFallbackKey(p)), p.usage_7d_percent, has7d, null, fillColor7d) : '';

  // API keys have no session-based rate-limit window, so tokens and
  // estimated cost are the meaningful figures for a metered key. With a
  // token_threshold budget set, tokens-used gets the same kind of bar the
  // 5h session does so "how close to the limit" stays visible at a glance.
  const tokensRow = p.token_threshold
    ? renderTokenBudgetBar(t('profile.tokens_used'), p.tokens_total, p.token_threshold,
        barColor(p.status_word, Math.min(100, (p.tokens_total / p.token_threshold) * 100), 100))
    : `<div class="bar-group">
        <div class="bar-label-row"><span>${esc(t('profile.tokens_used'))}</span><span class="mono-num">${esc(formatTokenCount(p.tokens_total))}</span></div>
      </div>`;
  // codex uses switch_threshold like oauth (its backend returns
  // percentage-based quota headers); only api kind gets the token budget.
  const usageBlock = p.kind !== 'api' ? `${bar5h}${bar7d}` : `
    ${tokensRow}
    <div class="bar-group">
      <div class="bar-label-row"><span>${esc(t('profile.cost_estimated'))}</span><span class="mono-num">${p.cost_usd_total !== null ? '$' + p.cost_usd_total.toFixed(2) : '—'}</span></div>
    </div>`;

  const resetIn = formatFutureRelative(p.usage_5h_resets_at);
  const footResets = resetIn ? `<span>${esc(t('profile.resets_in'))} <span class="mono-num">${esc(resetIn)}</span></span>` : '';

  const tagStyle = p.tag_color ? ` style="--tag-color:${esc(p.tag_color)}"` : '';
  const tagClass = p.tag_color ? ' has-tag' : '';

  return `
    <div class="p-card ${isActive ? 'active-card' : ''}${tagClass}" data-id="${esc(p.id)}"${tagStyle}>
      <div class="p-top">
        <div class="p-icon${tagClass}${p.kind === 'codex' ? ' kind-codex' : ''}">${kindIcon(p)}</div>
        <span class="p-name">${esc(p.name)}</span>
        ${planBadge(p)}
        ${p.in_use_now ? `<span class="used-now-pill"><span class="used-now-dot"></span>${esc(t('profiles.used_now_tag'))}</span>` : ''}
        ${kindLabel ? `<span class="p-kind">${kindLabel}</span>` : ''}
        <span class="p-priority" title="${esc(t('profiles.priority_tooltip'))}">
          <svg class="p-priority-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 20V14M12 20V8M18 20V4"/></svg>
          <span class="mono-num">${p.priority}</span>
        </span>
        ${p.enabled
          ? `<span class="p-status-word" style="color:${statusColors.color};background:${statusColors.bg}">${esc(p.status_word)}</span>`
          : `<span class="p-disabled-badge">${esc(t('profile.disabled'))}</span>`}
      </div>
      <div class="p-bars">${usageBlock}</div>
      <div class="p-foot">
        ${footResets}
        <div class="p-foot-spacer"></div>
        <span>${p.automatic ? esc(t('profile.auto_rotation_on')) : esc(t('profile.manual_only'))}</span>
        <div class="toggle ${p.enabled ? '' : 'off'} toggle-enabled"><div class="toggle-knob"></div></div>
        <div class="kebab-btn" data-action="kebab">
          <svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg>
        </div>
      </div>
    </div>
  `;
}

// Delegated and wired once, not per render: setLiveHtml reuses card nodes
// across updates, so a per-node listener would stack a duplicate on every
// poll for any card that survives.
document.getElementById('profiles').addEventListener('click', async (e) => {
  const kebabBtn = e.target.closest('[data-action="kebab"]');
  if (kebabBtn) {
    e.stopPropagation();
    const id = kebabBtn.closest('.p-card').dataset.id;
    openProfileKebabMenu(kebabBtn, id);
    return;
  }
  const toggle = e.target.closest('.toggle-enabled');
  if (!toggle) return;
  const id = toggle.closest('.p-card').dataset.id;
  const enabled = toggle.classList.contains('off');
  await api(`/api/profiles/${id}`, { method: 'PATCH', body: JSON.stringify({ enabled }) });
  await loadProfiles();
  await loadStatus();
  renderStatStrip((await api('/api/profiles')).profiles);
});

let _lastProfiles = [];

async function loadProfiles() {
  const el = document.getElementById('profiles');
  try {
    const { profiles } = await api('/api/profiles');
    _lastProfiles = profiles;
    renderStatStrip(profiles);
    if (!profiles.length) {
      setLiveHtml(el, `<div class="empty">${esc(t('empty.no_profiles_hint'))}</div>`);
      return;
    }
    setLiveHtml(el, profiles.map((p) => renderProfileCard(p)).join(''));
  } catch (e) {
    setLiveHtml(el, `<div class="empty">${esc(t('empty.profiles_load_error_prefix'))} ${esc(e.message)}</div>`);
  }
}

// ---- Profiles page: table, search, filter, sort, drag-reorder, kebab menu ----

let _profileSearch = '';
let _profileStatusFilter = 'all';
let _profileSort = 'priority';

function oauthKindLabel(p) {
  if (p.kind === 'codex') {
    return p.auth_mode === 'chatgpt_subscription'
      ? t('profiles.kind_codex_chatgpt')
      : `${t('profiles.kind_codex_api')} · ${esc(p.base_url || 'api.openai.com')}`;
  }
  if (p.kind !== 'oauth') return `${t('profiles.kind_api')} · ${esc(p.base_url || 'api.anthropic.com')}`;
  // p.plan is detected from Anthropic's account-profile endpoint (see
  // connection_test.py / anthropic_oauth.py), never assumed. A Profile whose
  // plan was never detected reads as "unknown" until its credential is next
  // touched (re-import or a fresh add).
  if (p.plan === 'max') return t('profiles.kind_oauth_max');
  if (p.plan === 'pro') return t('profiles.kind_oauth_pro');
  return t('profiles.kind_oauth_unknown');
}

function renderProfileTableRow(p) {
  const isActive = _lastStatus && _lastStatus.current_profile_id === p.id;
  const kindLabel = oauthKindLabel(p);
  const has5h = p.usage_5h_percent !== null && p.usage_5h_percent !== undefined;
  const has7d = p.usage_7d_percent !== null && p.usage_7d_percent !== undefined;

  const barCell = (rawLabel, pct, has, isWeekly) => {
    // The 7d/weekly column has no switch_threshold concept (same as the
    // card and Detail modal) — no marker to draw, and its own fixed-band
    // color rule (weeklyBarColor) instead of the 5h column's
    // threshold-relative one.
    const fillColor = isWeekly ? weeklyBarColor(p.status_word, pct) : barColor(p.status_word, pct, p.switch_threshold);
    const marker = isWeekly ? '' : `<div class="bar-threshold" style="left:${Math.min(100, p.switch_threshold)}%"></div>`;
    // Only show a label word when there is a real one (a codex window
    // duration) — the column header already says "5h"/"7d" for oauth, and
    // this column is narrow.
    const labelSpan = rawLabel ? `<span style="color:var(--text-faint);font-size:10px;">${esc(rawLabel)}</span>` : '';
    return has
      ? `<div class="bar-cell"><div class="bar-label-row">${labelSpan}<span class="mono-num" style="color:${fillColor}">${pct}%</span></div>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, pct)}%;background:${fillColor}"></div>
          ${marker}</div></div>`
      : `<div class="bar-cell"><div class="bar-label-row">${labelSpan}<span class="mono-num" style="color:var(--text-faint);font-size:10px;">${esc(t('profile.not_observed'))}</span></div>
          <div class="bar-track">${marker}</div></div>`;
  };

  // API keys have no session-based rate-limit window, so tokens and
  // estimated cost take over these same two column slots.
  const usageCell = (label, value) =>
    `<div class="bar-cell"><div class="bar-label-row"><span style="color:var(--text-dim);font-size:10px;">${esc(label)}</span></div>
      <div class="mono-num" style="font-size:13px;">${esc(value)}</div></div>`;

  // The second column keeps its grid slot even with no second window — this
  // row is a CSS grid with a fixed column template, so an omitted child
  // would shift every column after it.
  const usageCells = p.kind !== 'api'
    ? `${barCell(usageWindowLabelOrBlank(p.usage_window_label), p.usage_5h_percent, has5h, false)}${has7d ? barCell(usageWindowLabelOrBlank(p.usage_window_label_7d), p.usage_7d_percent, has7d, true) : '<div class="bar-cell"></div>'}`
    : `${usageCell(t('profile.tokens_used'), formatTokenCount(p.tokens_total))}${usageCell(t('profile.cost_estimated'), p.cost_usd_total !== null ? '$' + p.cost_usd_total.toFixed(2) : '—')}`;

  const tagStyle = p.tag_color ? ` style="--tag-color:${esc(p.tag_color)}"` : '';
  const tagClass = p.tag_color ? ' has-tag' : '';

  return `
    <div class="row ${p.enabled ? '' : 'dim'}${tagClass}" data-id="${esc(p.id)}" draggable="true"${tagStyle}>
      <div class="drag-handle" title="${esc(t('profiles.drag_hint'))}">
        <svg viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="6" r="1.4"/><circle cx="9" cy="12" r="1.4"/><circle cx="9" cy="18" r="1.4"/><circle cx="15" cy="6" r="1.4"/><circle cx="15" cy="12" r="1.4"/><circle cx="15" cy="18" r="1.4"/></svg>
      </div>
      <div class="row-name-cell">
        <div class="p-icon${tagClass}${p.kind === 'codex' ? ' kind-codex' : ''}">${kindIcon(p)}</div>
        <div class="row-name-text">
          <div class="p-name">${esc(p.name)} ${planBadge(p)}${p.in_use_now ? `<span class="used-now-pill"><span class="used-now-dot"></span>${esc(t('profiles.used_now_tag'))}</span>` : ''}</div>
          <div class="p-kind">${kindLabel}</div>
        </div>
      </div>
      ${usageCells}
      <div class="threshold-chip" data-action="threshold" title="${esc(t('profiles.edit_threshold'))}">
        ${p.kind === 'api' ? (p.token_threshold ? esc(formatTokenCount(p.token_threshold)) : esc(t('modal.detail.token_threshold_placeholder'))) : `${p.switch_threshold}%`}
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20l4.2-.7L19 8.5a2 2 0 0 0 0-2.8l-.7-.7a2 2 0 0 0-2.8 0L4.7 15.8 4 20Z"/></svg>
      </div>
      <div class="priority-cell"><div class="priority-num">${p.priority}</div></div>
      <div class="row-actions">
        <div class="toggle ${p.enabled ? '' : 'off'} toggle-enabled"><div class="toggle-knob"></div></div>
        <div class="kebab-btn" data-action="kebab">
          <svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg>
        </div>
      </div>
    </div>
  `;
}

function filteredSortedProfiles(profiles) {
  let list = profiles.filter((p) => {
    if (_profileStatusFilter === 'enabled' && !p.enabled) return false;
    if (_profileStatusFilter === 'disabled' && p.enabled) return false;
    if (_profileSearch && !p.name.toLowerCase().includes(_profileSearch.toLowerCase())) return false;
    return true;
  });
  const sorters = {
    priority: (a, b) => a.priority - b.priority,
    name: (a, b) => a.name.localeCompare(b.name),
    usage: (a, b) => (b.usage_5h_percent ?? -1) - (a.usage_5h_percent ?? -1),
  };
  return list.slice().sort(sorters[_profileSort] || sorters.priority);
}

let _profileDragActive = false;

async function loadProfilesTable() {
  if (_profileDragActive) return; // never yank the table out from under an in-progress reorder
  const el = document.getElementById('profilesTableBody');
  const sub = document.getElementById('profilesSub');
  try {
    const { profiles } = await api('/api/profiles');
    _lastProfiles = profiles;
    if (sub) {
      const enabledCount = profiles.filter((p) => p.enabled).length;
      const activeName = (_lastStatus && _lastStatus.current_profile_name) || null;
      sub.textContent = `${profiles.length} ${t('profiles.configured')} · ${enabledCount} ${t('stat.enabled').toLowerCase()}` + (activeName ? ` · ${activeName} ${t('profiles.is_active')}` : '');
    }
    const visible = filteredSortedProfiles(profiles);
    if (!profiles.length) {
      setLiveHtml(el, `<div class="empty" style="border:none;">${esc(t('empty.no_profiles'))}</div>`);
      return;
    }
    if (!visible.length) {
      setLiveHtml(el, `<div class="empty" style="border:none;">${esc(t('profiles.no_matches'))}</div>`);
      return;
    }
    setLiveHtml(el, visible.map(renderProfileTableRow).join(''));
  } catch (e) {
    setLiveHtml(el, `<div class="empty" style="border:none;">${esc(t('empty.profiles_load_error_prefix'))} ${esc(e.message)}</div>`);
  }
}

// Delegated and wired once, not per render — see the #profiles click
// listener above for why.
{
  const profilesTableBody = document.getElementById('profilesTableBody');
  let _dragRowId = null;

  profilesTableBody.addEventListener('click', async (e) => {
    const toggle = e.target.closest('.toggle-enabled');
    if (toggle) {
      const row = toggle.closest('.row');
      const id = row.dataset.id;
      const enabled = toggle.classList.contains('off');
      await api(`/api/profiles/${id}`, { method: 'PATCH', body: JSON.stringify({ enabled }) });
      showToast('success', enabled ? t('toast.profile_enabled') : t('toast.profile_disabled'), row.querySelector('.p-name').textContent);
      await loadProfilesTable();
      return;
    }
    const chip = e.target.closest('[data-action="threshold"]');
    if (chip) {
      const row = chip.closest('.row');
      const id = row.dataset.id;
      const current = _lastProfiles.find((p) => p.id === id);
      if (current && current.kind === 'api') {
        const next = window.prompt(t('profiles.prompt_token_threshold'), current.token_threshold != null ? String(current.token_threshold) : '');
        if (next === null) return;
        const trimmed = next.trim();
        if (trimmed === '') {
          await api(`/api/profiles/${id}`, { method: 'PATCH', body: JSON.stringify({ token_threshold: null }) });
        } else {
          const num = Number(trimmed);
          if (!Number.isFinite(num) || num < 0) return;
          await api(`/api/profiles/${id}`, { method: 'PATCH', body: JSON.stringify({ token_threshold: Math.round(num) }) });
        }
        await loadProfilesTable();
        await loadProfiles();
        return;
      }
      const next = window.prompt(t('profiles.prompt_threshold'), current ? String(current.switch_threshold) : '98');
      if (next === null) return;
      const num = Number(next);
      if (!Number.isFinite(num) || num < 1 || num > 100) return;
      await api(`/api/profiles/${id}`, { method: 'PATCH', body: JSON.stringify({ switch_threshold: num }) });
      await loadProfilesTable();
      await loadProfiles();
      return;
    }
    const kebabBtn = e.target.closest('[data-action="kebab"]');
    if (kebabBtn) {
      e.stopPropagation();
      const row = kebabBtn.closest('.row');
      openProfileKebabMenu(kebabBtn, row.dataset.id);
    }
  });

  profilesTableBody.addEventListener('dragstart', (e) => {
    const row = e.target.closest('.row');
    if (!row) return;
    _dragRowId = row.dataset.id;
    row.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    _profileDragActive = true; // live-poll must not re-render the table out from under an in-progress drag
  });
  profilesTableBody.addEventListener('dragend', (e) => {
    const row = e.target.closest('.row');
    if (row) row.classList.remove('dragging');
    _profileDragActive = false;
  });
  profilesTableBody.addEventListener('dragover', (e) => {
    const row = e.target.closest('.row');
    if (!row) return;
    e.preventDefault();
    if (row.dataset.id !== _dragRowId) row.classList.add('drag-over');
  });
  profilesTableBody.addEventListener('dragleave', (e) => {
    const row = e.target.closest('.row');
    if (row) row.classList.remove('drag-over');
  });
  profilesTableBody.addEventListener('drop', async (e) => {
    const row = e.target.closest('.row');
    if (!row) return;
    e.preventDefault();
    row.classList.remove('drag-over');
    const targetId = row.dataset.id;
    if (!_dragRowId || _dragRowId === targetId) return;

    // Reordering renumbers the whole list sequentially (1, 2, 3, ...) from
    // the drop position rather than copying one Profile's priority onto
    // another, which is a no-op whenever the two already match. Computed
    // against ALL Profiles, not just the ones the current search/status
    // filter shows: priority is a global rotation order, so a hidden Profile
    // still needs a defined position in it.
    const ordered = _lastProfiles.slice().sort((a, b) => a.priority - b.priority).map((p) => p.id);
    const fromIdx = ordered.indexOf(_dragRowId);
    const toIdx = ordered.indexOf(targetId);
    if (fromIdx === -1 || toIdx === -1) return;
    ordered.splice(toIdx, 0, ordered.splice(fromIdx, 1)[0]);

    const byId = new Map(_lastProfiles.map((p) => [p.id, p]));
    const updates = ordered
      .map((id, i) => ({ id, priority: i + 1 }))
      .filter(({ id, priority }) => byId.get(id)?.priority !== priority);
    await Promise.all(updates.map(({ id, priority }) =>
      api(`/api/profiles/${id}`, { method: 'PATCH', body: JSON.stringify({ priority }) })));
    await loadProfilesTable();
  });
}

// ---- kebab context menu ----

function closeKebabMenu() {
  const existing = document.querySelector('.menu-pop');
  if (existing) existing.remove();
  document.querySelectorAll('.kebab-btn.open').forEach((b) => b.classList.remove('open'));
}

function openProfileKebabMenu(anchorBtn, profileId) {
  const already = anchorBtn.classList.contains('open');
  closeKebabMenu();
  if (already) return;
  anchorBtn.classList.add('open');

  const profile = _lastProfiles.find((p) => p.id === profileId);
  if (!profile) return;

  const items = [
    { icon: '<path d="M4 20l4.2-.7L19 8.5a2 2 0 0 0 0-2.8l-.7-.7a2 2 0 0 0-2.8 0L4.7 15.8 4 20Z"/>', label: t('profiles.menu_edit'), action: 'edit' },
    { icon: '<path d="M3 12h4l2.5-7L13 19l2-7h6"/>', label: t('profiles.menu_view_activity'), action: 'activity' },
    { icon: '<path d="M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z"/><path d="M9 12l2 2 4-4"/>', label: t('profiles.menu_test_connection'), action: 'test' },
    { divider: true },
    { icon: '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z"/>', label: t('profiles.menu_take_over'), action: 'take_over', title: t('profiles.take_over_tooltip') },
    { divider: true },
    { icon: '<path d="M4.9 4.9l14.2 14.2"/><circle cx="12" cy="12" r="9"/>', label: profile.enabled ? t('profiles.menu_disable') : t('profiles.menu_enable'), action: 'toggle' },
    { icon: '<path d="M4 7h16M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2m-9 0 1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13"/>', label: t('profile.remove'), action: 'remove', danger: true },
  ];

  const pop = document.createElement('div');
  pop.className = 'menu-pop';
  pop.innerHTML = items.map((it) => it.divider
    ? '<div class="menu-divider"></div>'
    : `<div class="menu-item ${it.danger ? 'danger' : ''}" data-menu-action="${it.action}" ${it.title ? `title="${esc(it.title)}"` : ''}>
         <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${it.icon}</svg>
         ${esc(it.label)}
       </div>`
  ).join('');

  document.body.appendChild(pop);
  const rect = anchorBtn.getBoundingClientRect();
  pop.style.top = `${window.scrollY + rect.bottom + 6}px`;
  pop.style.left = `${Math.min(window.scrollX + rect.right - pop.offsetWidth, window.innerWidth - pop.offsetWidth - 12)}px`;

  pop.querySelectorAll('[data-menu-action]').forEach((item) => {
    item.addEventListener('click', async (e) => {
      e.stopPropagation();
      const action = item.dataset.menuAction;
      closeKebabMenu();
      if (action === 'edit') openProfileDetailModal(profileId);
      if (action === 'activity') switchView('activity');
      if (action === 'toggle') {
        await api(`/api/profiles/${profileId}`, { method: 'PATCH', body: JSON.stringify({ enabled: !profile.enabled }) });
        showToast('success', profile.enabled ? t('toast.profile_disabled') : t('toast.profile_enabled'), profile.name);
        await loadProfilesTable();
        await loadProfiles();
      }
      if (action === 'take_over') {
        try {
          await api(`/api/profiles/${profileId}/take-over`, { method: 'POST' });
          showToast('success', t('toast.take_over_ok'), profile.name);
          await loadProfilesTable();
          await loadProfiles();
        } catch (err) {
          showToast('error', t('toast.take_over_failed'), err.message);
        }
      }
      if (action === 'test') {
        showToast('info', t('toast.test_connection_running'), profile.name, { duration: 4000 });
        try {
          const result = await api(`/api/profiles/${profileId}/test`, { method: 'POST' });
          if (result.ok) {
            showToast('success', t('toast.test_connection_ok'), `${profile.name} · ${result.elapsed_ms}ms`);
          } else {
            showToast('error', t('toast.test_connection_failed'), result.message || `HTTP ${result.status}`);
          }
        } catch (err) {
          showToast('error', t('toast.test_connection_failed'), err.message);
        }
        await loadActivity();
      }
      if (action === 'remove') openConfirmModal({
        title: `${t('modal.confirm.remove_title_prefix')} "${profile.name}"?`,
        body: t('modal.confirm.remove_body'),
        confirmText: profile.name,
        actionLabel: t('profile.remove'),
        onConfirm: async () => {
          await api(`/api/profiles/${profileId}`, { method: 'DELETE' });
          showToast('success', t('toast.profile_removed'), profile.name);
          await loadProfilesTable();
          await loadProfiles();
        },
      });
    });
  });
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('.menu-pop') && !e.target.closest('[data-action="kebab"]')) closeKebabMenu();
});

// ---- generic Confirm modal (type-to-confirm) ----

let _confirmCallback = null;

function openConfirmModal({ title, body, confirmText, actionLabel, onConfirm, hint }) {
  document.getElementById('confirm_title').textContent = title;
  document.getElementById('confirm_body').textContent = body;
  document.getElementById('confirm_input').value = '';
  document.getElementById('confirm_input').style.borderColor = '';
  document.getElementById('confirm_input').placeholder = confirmText;
  document.getElementById('confirm_input').dataset.expected = confirmText;
  document.querySelector('.confirm-hint').textContent = hint || `${t('modal.confirm.hint_prefix')} "${confirmText}" ${t('modal.confirm.hint_suffix')}`;
  document.getElementById('confirmActionBtn').textContent = actionLabel;
  _confirmCallback = onConfirm;
  document.getElementById('confirmScrim').classList.add('open');
}

function closeConfirmModal() {
  document.getElementById('confirmScrim').classList.remove('open');
  _confirmCallback = null;
}

async function submitConfirmModal() {
  const input = document.getElementById('confirm_input');
  if (input.value !== input.dataset.expected) {
    input.style.borderColor = 'var(--bad)';
    return;
  }
  const cb = _confirmCallback;
  closeConfirmModal();
  if (cb) await cb();
}

// ---- Profile Detail modal ----

let _pdProfileId = null;

function openProfileDetailModal(profileId) {
  const p = _lastProfiles.find((x) => x.id === profileId);
  if (!p) return;
  _pdProfileId = profileId;

  const pdIcon = document.getElementById('pd_icon');
  pdIcon.innerHTML = kindIcon(p);
  pdIcon.classList.toggle('has-tag', !!p.tag_color);
  pdIcon.classList.toggle('kind-codex', p.kind === 'codex');
  pdIcon.style.setProperty('--tag-color', p.tag_color || '');
  document.getElementById('pd_name').firstChild.textContent = p.name + ' ';
  document.getElementById('pd_name_input').value = p.name;
  const statusTag = document.getElementById('pd_status_tag');
  if (p.enabled && p.status_word !== 'healthy') {
    statusTag.textContent = p.status_word;
    statusTag.style.display = '';
  } else {
    statusTag.style.display = 'none';
  }
  document.getElementById('pd_sub').textContent = `${oauthKindLabel(p)} · ${t('profile.priority')} ${p.priority}`;

  // API keys have no session-based rate-limit window and no percentage
  // switch threshold, so they get editable base_url/credential fields, a
  // lifetime tokens+cost readout instead of bars, and a token-count budget
  // (see profiles.py's token_threshold). codex sits with oauth on the
  // percentage side of that split but also has its own field block,
  // pd_codex_fields: an optional base_url/credential (api_key auth_mode
  // only), a read-only codex_home, and the model/reasoning-effort overrides.
  const isApi = p.kind === 'api';
  const isCodex = p.kind === 'codex';
  const codexIsApiKey = isCodex && p.auth_mode === 'api_key';
  document.getElementById('pd_api_fields').style.display = isApi ? '' : 'none';
  document.getElementById('pd_base_url_input').value = p.base_url || '';
  document.getElementById('pd_credential_input').value = ''; // never pre-filled — the daemon never sends the real secret back
  document.getElementById('pd_budget_input').value = p.monthly_budget_cap != null ? p.monthly_budget_cap.toFixed(2) : '';

  document.getElementById('pd_codex_fields').style.display = isCodex ? '' : 'none';
  document.getElementById('pd_codex_base_url_wrap').style.display = codexIsApiKey ? '' : 'none';
  document.getElementById('pd_codex_credential_wrap').style.display = codexIsApiKey ? '' : 'none';
  document.getElementById('pd_codex_home_row').style.display = (isCodex && p.auth_mode === 'chatgpt_subscription') ? '' : 'none';
  if (isCodex) {
    document.getElementById('pd_codex_base_url_input').value = p.base_url || '';
    document.getElementById('pd_codex_credential_input').value = ''; // never pre-filled, same reason as pd_credential_input
    document.getElementById('pd_codex_home_display').textContent = p.codex_home || '—';
    document.getElementById('pd_codex_home_display').title = p.codex_home || '';
    const modelSelect = document.getElementById('pd_codex_model');
    modelSelect.dataset.value = p.codex_model || '';
    modelSelect._cuRenderValue();
    updateCodexModelHelp('pd');
    const reasoningSelect = document.getElementById('pd_codex_reasoning');
    reasoningSelect.dataset.value = p.codex_reasoning_effort || '';
    reasoningSelect._cuRenderValue();
  }

  document.getElementById('pd_bars').style.display = isApi ? 'none' : '';
  document.getElementById('pd_api_usage').style.display = isApi ? '' : 'none';
  document.getElementById('pd_threshold_row_oauth').style.display = isApi ? 'none' : '';
  document.getElementById('pd_threshold_row_api').style.display = isApi ? '' : 'none';

  if (isApi) {
    const tokensRow = p.token_threshold
      ? renderTokenBudgetBar(t('profile.tokens_used'), p.tokens_total, p.token_threshold,
          barColor(p.status_word, Math.min(100, (p.tokens_total / p.token_threshold) * 100), 100))
      : `<div class="field-row" style="padding:0;border:none;">
          <div class="field-text"><div class="field-title">${esc(t('profile.tokens_used'))}</div></div>
          <div class="mono-num">${esc(formatTokenCount(p.tokens_total))}</div>
        </div>`;
    document.getElementById('pd_api_usage').innerHTML = `
      ${tokensRow}
      <div class="field-row" style="padding:0;border:none;">
        <div class="field-text"><div class="field-title">${esc(t('profile.cost_estimated'))}</div></div>
        <div class="mono-num">${p.cost_usd_total !== null ? '$' + p.cost_usd_total.toFixed(2) : '—'}</div>
      </div>`;
    document.getElementById('pd_token_threshold_input').value = p.token_threshold != null ? p.token_threshold : '';
  } else {
    const has5h = p.usage_5h_percent !== null && p.usage_5h_percent !== undefined;
    const has7d = p.usage_7d_percent !== null && p.usage_7d_percent !== undefined;
    const barGroup = (label, pct, has, showThreshold) =>
      renderUsageBar(label, pct, has, showThreshold ? p.switch_threshold : null,
        showThreshold ? barColor(p.status_word, pct, p.switch_threshold) : weeklyBarColor(p.status_word, pct));
    const resetIn = formatFutureRelative(p.usage_5h_resets_at);
    // No second window (codex accounts, typically) — render nothing rather
    // than an empty "7d" bar-group. See usageWindowLabel.
    document.getElementById('pd_bars').innerHTML =
      barGroup(usageWindowLabel(p.usage_window_label, primaryWindowFallbackKey(p)), p.usage_5h_percent, has5h, true) +
      (has7d ? barGroup(usageWindowLabel(p.usage_window_label_7d, secondaryWindowFallbackKey(p)), p.usage_7d_percent, has7d, false) : '') +
      `<div class="field-sub">${resetIn ? `${t('profile.resets_in')} ${resetIn}` : t('profiles.no_reset_data')}</div>`;
    document.getElementById('pd_threshold_val').textContent = String(p.switch_threshold);
  }

  document.getElementById('pd_priority_val').textContent = String(p.priority);
  document.getElementById('pd_automatic_toggle').classList.toggle('off', !p.automatic);
  document.getElementById('pd_automatic_sub').textContent = p.automatic ? t('profiles.automatic_on_sub') : t('profiles.automatic_off_sub');
  _pdSelectedTagColor = p.tag_color || null;
  renderDetailTagRow();

  document.getElementById('profileDetailScrim').classList.add('open');
}

function closeProfileDetailModal() {
  document.getElementById('profileDetailScrim').classList.remove('open');
  _pdProfileId = null;
}

async function saveProfileDetail() {
  if (!_pdProfileId) return;
  const p = _lastProfiles.find((x) => x.id === _pdProfileId);
  if (!p) return;
  const name = document.getElementById('pd_name_input').value.trim();
  if (!name) {
    showToast('error', t('toast.profile_name_required'), '');
    return;
  }
  const isApi = p.kind === 'api';
  const isCodex = p.kind === 'codex';
  const codexIsApiKey = isCodex && p.auth_mode === 'api_key';
  const payload = {
    name,
    priority: Number(document.getElementById('pd_priority_val').textContent),
    automatic: !document.getElementById('pd_automatic_toggle').classList.contains('off'),
    tag_color: _pdSelectedTagColor,
  };
  if (isApi) {
    const rawThreshold = document.getElementById('pd_token_threshold_input').value.trim();
    payload.token_threshold = rawThreshold === '' ? null : Number(rawThreshold);
    payload.base_url = document.getElementById('pd_base_url_input').value.trim() || null;
    const rawBudget = document.getElementById('pd_budget_input').value.replace(/[^0-9.]/g, '');
    payload.monthly_budget_cap = rawBudget === '' ? null : Number(rawBudget);
  } else {
    payload.switch_threshold = Number(document.getElementById('pd_threshold_val').textContent);
  }
  if (isCodex) {
    payload.codex_model = document.getElementById('pd_codex_model').dataset.value || null;
    payload.codex_reasoning_effort = document.getElementById('pd_codex_reasoning').dataset.value || null;
    if (codexIsApiKey) payload.base_url = document.getElementById('pd_codex_base_url_input').value.trim() || null;
  }

  try {
    await api(`/api/profiles/${_pdProfileId}`, { method: 'PATCH', body: JSON.stringify(payload) });
  } catch (e) {
    showToast('error', t('toast.profile_save_failed'), e.message);
    return;
  }

  const newCredential = isApi ? document.getElementById('pd_credential_input').value.trim()
    : codexIsApiKey ? document.getElementById('pd_codex_credential_input').value.trim() : '';
  if (newCredential) {
    try {
      await api(`/api/profiles/${_pdProfileId}/credential`, { method: 'POST', body: JSON.stringify({ credential: newCredential }) });
    } catch (e) {
      showToast('error', t('toast.credential_update_failed'), e.message);
      return; // the rest of the Profile did save — only the key rotation failed
    }
  }

  showToast('success', t('toast.profile_saved'), '');
  closeProfileDetailModal();
  await loadProfilesTable();
  await loadProfiles();
}

function removeProfileFromDetail() {
  const p = _lastProfiles.find((x) => x.id === _pdProfileId);
  if (!p) return;
  closeProfileDetailModal();
  openConfirmModal({
    title: `${t('modal.confirm.remove_title_prefix')} "${p.name}"?`,
    body: t('modal.confirm.remove_body'),
    confirmText: p.name,
    actionLabel: t('profile.remove'),
    onConfirm: async () => {
      await api(`/api/profiles/${p.id}`, { method: 'DELETE' });
      showToast('success', t('toast.profile_removed'), p.name);
      await loadProfilesTable();
      await loadProfiles();
    },
  });
}

// ---- usage by project (experimental) ----

const PROJECT_COLORS = ['#43C6FF', '#FF5FA6', '#35D07F', '#FFB020', '#C97BFF', '#2FD9C4', '#FF5C5C', '#5C5C63'];

let _lastProjects = [];

function renderProjectUsageList(projects) {
  const el = document.getElementById('projectUsageList');
  if (!el) return;
  if (!projects.length) {
    setLiveHtml(el, `<div class="empty">${esc(t('empty.no_project_usage'))}</div>`);
    return;
  }
  const mode = currentMetricMode('project');
  setLiveHtml(el, projects.map((p, i) => {
    const color = PROJECT_COLORS[i % PROJECT_COLORS.length];
    const valueText = mode === 'tokens' ? formatTokenCount(p.tokens) : `${p.percent}%`;
    return `
      <div class="model-row">
        <span class="model-dot" style="background:${color}"></span>
        <span class="model-name" title="${esc(p.project_id)}">${esc(p.display_name)}</span>
        <div class="model-bar-track"><div class="model-bar-fill" style="width:${p.percent}%;background:${color}"></div></div>
        <span class="model-pct mono-num">${valueText}</span>
      </div>`;
  }).join(''));
}

async function loadProjectUsage() {
  const el = document.getElementById('projectUsageList');
  if (!el) return;
  try {
    const { projects } = await api('/api/usage/projects');
    _lastProjects = projects;
    renderProjectUsageList(projects);
  } catch (e) {
    setLiveHtml(el, `<div class="empty">${esc(e.message)}</div>`);
  }
}

// ---- usage summary charts (tokens/day, model split, busiest hours, cost) ----

// Ordered so any two adjacent entries sit in different hue families;
// same-family neighbors are hard to tell apart as adjacent donut slices.
// Gray stays last as the lowest-priority overflow color.
const MODEL_COLORS = ['#43C6FF', '#FF5FA6', '#35D07F', '#FFB020', '#C97BFF', '#2FD9C4', '#FF5C5C', '#5C5C63'];

function formatTokenCount(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(n);
}

const CHART_BUCKET_LABEL_OPTS = {
  day: { weekday: 'short' },
  week: { month: 'short', day: 'numeric' },
  month: { month: 'short' },
};

function chartBucketLabel(dateStr, granularity, bucketHours) {
  // Hourly buckets carry a full local ISO datetime (they need the hour,
  // not just the date) — parse directly. Every other granularity's `date`
  // is a plain YYYY-MM-DD, which needs an explicit local-midnight suffix
  // or JS parses it as UTC midnight, shifting the label a day in
  // negative-UTC-offset timezones.
  if (granularity === 'hour') {
    const start = new Date(dateStr);
    const span = bucketHours || 1;
    if (span <= 1) return start.toLocaleTimeString(undefined, { hour: 'numeric' });
    // Multi-hour bucket (e.g. the "1d" range's 4x6h bars): a plain
    // "09-15" 24h range reads faster at this width than two AM/PM labels.
    const pad = (n) => String(n % 24).padStart(2, '0');
    return `${pad(start.getHours())}-${pad(start.getHours() + span)}`;
  }
  const opts = CHART_BUCKET_LABEL_OPTS[granularity] || CHART_BUCKET_LABEL_OPTS.day;
  return new Date(dateStr + 'T00:00:00').toLocaleDateString(undefined, opts);
}

function renderTokensChart(dailyTotals, granularity, bucketHours) {
  const max = Math.max(1, ...dailyTotals.map((d) => d.tokens));
  const barsRow = document.getElementById('tokensBarsRow');
  if (barsRow) {
    setLiveHtml(barsRow, dailyTotals.map((d) => {
      const pct = Math.round((d.tokens / max) * 100);
      const label = chartBucketLabel(d.date, granularity, bucketHours);
      return `<div class="bars-col">
        <div class="bars-col-track"><div class="bars-col-fill" data-tooltip="${esc(label)}: ${esc(formatTokenCount(d.tokens))}" style="height:${pct}%"></div></div>
        <div class="bars-col-label">${esc(label)}</div>
      </div>`;
    }).join(''));
  }
  const ringsRow = document.getElementById('tokensRingsRow');
  if (ringsRow) {
    const circ = 2 * Math.PI * 19;
    setLiveHtml(ringsRow, dailyTotals.map((d) => {
      const frac = d.tokens / max;
      const label = chartBucketLabel(d.date, granularity, bucketHours);
      return `<div class="ring-item" data-tooltip="${esc(label)}: ${esc(formatTokenCount(d.tokens))}">
        <svg width="48" height="48" viewBox="0 0 48 48">
          <circle cx="24" cy="24" r="19" fill="none" stroke="var(--track)" stroke-width="5"/>
          <circle cx="24" cy="24" r="19" fill="none" stroke="var(--accent)" stroke-width="5" stroke-linecap="round"
                  stroke-dasharray="${(frac * circ).toFixed(1)} ${circ.toFixed(1)}" style="--ring-full:${circ.toFixed(1)}"
                  transform="rotate(-90 24 24)"/>
        </svg>
        <div class="ring-item-label">${esc(label)}</div>
      </div>`;
    }).join(''));
  }
  const total = dailyTotals.reduce((sum, d) => sum + d.tokens, 0);
  const totalChip = document.getElementById('tokensTotalChip');
  if (totalChip) totalChip.textContent = formatTokenCount(total);
}

let _lastModelSplit = [];

function renderModelSplit(modelSplit) {
  _lastModelSplit = modelSplit;
  const mode = currentMetricMode('model');
  const valueText = (m) => (mode === 'tokens' ? formatTokenCount(m.tokens) : `${m.percent}%`);
  const list = document.getElementById('modelSplitList');
  if (list) {
    if (!modelSplit.length) {
      setLiveHtml(list, `<div class="empty" style="border:none;padding:8px 0;">${esc(t('empty.no_usage_yet'))}</div>`);
    } else {
      setLiveHtml(list, modelSplit.map((m, i) => {
        const color = MODEL_COLORS[i % MODEL_COLORS.length];
        const tip = `${esc(m.model)}: ${esc(formatTokenCount(m.tokens))} (${m.percent}%)`;
        return `<div class="model-row">
          <span class="model-dot" style="background:${color}"></span>
          <span class="model-name">${esc(m.model)}</span>
          <div class="model-bar-track"><div class="model-bar-fill" data-tooltip="${tip}" style="width:${m.percent}%;background:${color}"></div></div>
          <span class="model-pct mono-num">${valueText(m)}</span>
        </div>`;
      }).join(''));
    }
  }
  const donutWrap = document.getElementById('modelSplitDonutWrap');
  if (donutWrap) {
    if (!modelSplit.length) {
      setLiveHtml(donutWrap, `<div class="empty" style="border:none;padding:8px 0;">${esc(t('empty.no_usage_yet'))}</div>`);
    } else {
      const r = 36, circ = 2 * Math.PI * r;
      let cum = 0;
      const circles = modelSplit.map((m, i) => {
        const color = MODEL_COLORS[i % MODEL_COLORS.length];
        // Slice geometry stays proportional to token share even in "tokens"
        // display mode — only the label text changes. A donut sized by raw
        // token counts wouldn't sum to a full circle.
        const len = (m.percent / 100) * circ;
        const rotate = -90 + (cum / 100) * 360;
        cum += m.percent;
        // data-tooltip doesn't render on SVG shape elements; a <title> child
        // is the native equivalent, so these slices use the browser's
        // built-in tooltip rather than the styled one.
        return `<circle cx="43" cy="43" r="${r}" fill="none" stroke="${color}" stroke-width="9"
                  stroke-dasharray="${len.toFixed(1)} ${circ.toFixed(1)}" style="--ring-full:${circ.toFixed(1)}"
                  transform="rotate(${rotate.toFixed(1)} 43 43)"><title>${esc(m.model)}: ${esc(formatTokenCount(m.tokens))} (${m.percent}%)</title></circle>`;
      }).join('');
      const legend = modelSplit.map((m, i) => `<div class="donut-legend-row"><span class="model-dot" style="background:${MODEL_COLORS[i % MODEL_COLORS.length]}"></span>${esc(m.model)} · <span class="mono-num">${valueText(m)}</span></div>`).join('');
      setLiveHtml(donutWrap, `
        <svg width="86" height="86" viewBox="0 0 86 86">
          <circle cx="43" cy="43" r="${r}" fill="none" stroke="var(--track)" stroke-width="9"/>
          ${circles}
        </svg>
        <div class="donut-legend">${legend}</div>`);
    }
  }
}

function renderHeatmap(hourly) {
  const el = document.getElementById('heatGrid');
  if (!el) return;
  const max = Math.max(1, ...hourly);
  setLiveHtml(el, hourly.map((v, i) => {
    const t = v / max;
    const alpha = (0.08 + t * 0.75).toFixed(2);
    return `<div class="heat-cell" style="background:rgba(67,198,255,${alpha});animation-delay:${i * 12}ms" data-tooltip="${i}:00 · ${v}"></div>`;
  }).join(''));
}

function renderCostRow(profiles, costByProfile) {
  const el = document.getElementById('costRow');
  if (!el) return;
  if (!profiles.length) {
    setLiveHtml(el, `<div class="empty" style="border:none;">${esc(t('empty.no_profiles'))}</div>`);
    return;
  }
  setLiveHtml(el, profiles.map((p) => {
    // A chatgpt_subscription codex Profile is flat-fee, like oauth. Only an
    // api-kind Profile, or a codex Profile in api_key auth_mode, is metered.
    const isFlatFee = p.kind === 'oauth' || (p.kind === 'codex' && p.auth_mode === 'chatgpt_subscription');
    if (isFlatFee) {
      const hasPct = p.usage_5h_percent !== null && p.usage_5h_percent !== undefined;
      const pct = hasPct ? `${p.usage_5h_percent}%` : '—';
      const pctColor = hasPct ? barColor(p.status_word, p.usage_5h_percent, p.switch_threshold) : 'inherit';
      return `<div class="cost-card">
        <div class="cost-label-row"><span class="cost-label">${esc(p.name)}</span><span class="cost-tag" data-i18n-skip>${esc(t('panel.cost.flat_fee'))}</span></div>
        <div class="cost-value" style="color:${pctColor}">${pct} ${t('panel.cost.used')}</div>
        <div class="cost-sub">${esc(t('panel.cost.subscription_plan'))}</div>
      </div>`;
    }
    const cost = costByProfile[p.id];
    return `<div class="cost-card">
      <div class="cost-label-row"><span class="cost-label">${esc(p.name)}</span><span class="cost-tag metered">${esc(t('panel.cost.metered'))}</span></div>
      <div class="cost-value mono-num">${cost !== undefined ? '$' + cost.toFixed(2) : '—'}</div>
      <div class="cost-sub">${esc(t('panel.cost.estimated'))}</div>
    </div>`;
  }).join(''));
}

// Each stacked segment is tinted with its Profile's tag_color. A Profile
// with no tag_color cycles through TAG_COLORS by position so it still gets a
// distinct, stable color instead of every untagged Profile sharing one.
function accountColorFor(profileId, profileColors, orderedIds) {
  if (profileColors[profileId]) return profileColors[profileId];
  const idx = orderedIds.indexOf(profileId);
  return TAG_COLORS[(idx < 0 ? 0 : idx) % TAG_COLORS.length];
}

let _lastAccountUsage = null; // { dailyTotals, profileColors, profileNames } — re-render target on toggle click

function renderAccountUsageChart(dailyTotals, profileColors, profileNames) {
  _lastAccountUsage = { dailyTotals, profileColors, profileNames };
  const barsRow = document.getElementById('accountUsageBarsRow');
  const ringsRow = document.getElementById('accountUsageRingsRow');
  const legendEl = document.getElementById('accountUsageLegend');
  if (!barsRow || !legendEl) return;

  const mode = currentMetricMode('account');
  const orderedIds = Object.keys(profileNames).sort();
  const max = Math.max(1, ...dailyTotals.map((d) => Object.values(d.profiles).reduce((a, b) => a + b, 0)));
  const grandTotals = {};

  setLiveHtml(barsRow, dailyTotals.map((d) => {
    const dayTotal = Object.values(d.profiles).reduce((a, b) => a + b, 0);
    const pct = Math.round((dayTotal / max) * 100);
    const segs = Object.entries(d.profiles)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([pid, tokens]) => {
        grandTotals[pid] = (grandTotals[pid] || 0) + tokens;
        const share = dayTotal ? (tokens / dayTotal) * 100 : 0;
        const color = accountColorFor(pid, profileColors, orderedIds);
        const name = esc(profileNames[pid] || pid);
        const valueText = mode === 'tokens' ? formatTokenCount(tokens) : `${Math.round(share)}%`;
        return `<div class="stack-bar-seg" style="height:${share}%;background:${color}" data-tooltip="${name}: ${valueText}"></div>`;
      }).join('');
    const label = new Date(d.date + 'T00:00:00').toLocaleDateString(undefined, { weekday: 'short' });
    return `<div class="stack-bars-col">
      <div class="stack-bars-track" style="height:${pct}%">${segs}</div>
      <div class="stack-bars-col-label">${esc(label)}</div>
    </div>`;
  }).join(''));

  // Round mode — one multi-arc ring per day, using the same
  // stroke-dasharray/rotate technique as the Model split donut, with each
  // arc proportional to that Profile's share of that day.
  if (ringsRow) {
    const r = 19, circ = 2 * Math.PI * r;
    setLiveHtml(ringsRow, dailyTotals.map((d) => {
      const dayTotal = Object.values(d.profiles).reduce((a, b) => a + b, 0);
      const label = new Date(d.date + 'T00:00:00').toLocaleDateString(undefined, { weekday: 'short' });
      let cum = 0;
      const arcs = dayTotal ? Object.entries(d.profiles).sort(([a], [b]) => a.localeCompare(b)).map(([pid, tokens]) => {
        const color = accountColorFor(pid, profileColors, orderedIds);
        const name = esc(profileNames[pid] || pid);
        const sharePct = (tokens / dayTotal) * 100;
        const len = (sharePct / 100) * circ;
        const rotate = -90 + (cum / 100) * 360;
        cum += sharePct;
        const valueText = mode === 'tokens' ? esc(formatTokenCount(tokens)) : `${Math.round(sharePct)}%`;
        return `<circle cx="24" cy="24" r="${r}" fill="none" stroke="${color}" stroke-width="5"
                  stroke-dasharray="${len.toFixed(1)} ${circ.toFixed(1)}" style="--ring-full:${circ.toFixed(1)}"
                  transform="rotate(${rotate.toFixed(1)} 24 24)"><title>${name}: ${valueText}</title></circle>`;
      }).join('') : '';
      return `<div class="ring-item" ${dayTotal ? `data-tooltip="${esc(label)}: ${esc(formatTokenCount(dayTotal))}"` : ''}>
        <svg width="48" height="48" viewBox="0 0 48 48">
          <circle cx="24" cy="24" r="${r}" fill="none" stroke="var(--track)" stroke-width="5"/>
          ${arcs}
        </svg>
        <div class="ring-item-label">${esc(label)}</div>
      </div>`;
    }).join(''));
  }

  const legendIds = Object.keys(grandTotals).sort((a, b) => grandTotals[b] - grandTotals[a]);
  if (!legendIds.length) {
    setLiveHtml(legendEl, `<div class="empty" style="border:none;padding:8px 0;">${esc(t('empty.no_usage_yet'))}</div>`);
    return;
  }
  const totalAll = legendIds.reduce((sum, pid) => sum + grandTotals[pid], 0) || 1;
  setLiveHtml(legendEl, legendIds.map((pid) => {
    const color = accountColorFor(pid, profileColors, orderedIds);
    const name = esc(profileNames[pid] || pid);
    const valueText = mode === 'tokens'
      ? formatTokenCount(grandTotals[pid])
      : `${Math.round((grandTotals[pid] / totalAll) * 100)}%`;
    return `<div class="stack-legend-row"><span class="stack-legend-dot" style="background:${color}"></span>${name} · <span class="mono-num">${valueText}</span></div>`;
  }).join(''));
}

const RANGE_CAPTION_KEYS = {
  '1h': 'panel.usage.range_caption_1h', '1d': 'panel.usage.range_caption_1d',
  '1w': 'panel.usage.range_caption_1w', '1m': 'panel.usage.range_caption_1m',
  '1y': 'panel.usage.range_caption_1y',
};

let _usageRange = localStorage.getItem('cu-usage-range') || '1w';

async function loadUsageSummary() {
  try {
    const summary = await api(`/api/usage/summary?range=${encodeURIComponent(_usageRange)}`);
    applyMiniChartStyle('tokens', currentMiniChartStyle('tokens'));
    applyMiniChartStyle('model', currentMiniChartStyle('model'));
    applyMiniChartStyle('account', currentMiniChartStyle('account'));
    applyMetricMode('model', currentMetricMode('model'));
    applyMetricMode('account', currentMetricMode('account'));
    renderTokensChart(summary.daily_totals, summary.granularity, summary.bucket_hours);
    renderAccountUsageChart(summary.daily_totals_by_profile, summary.profile_colors, summary.profile_names);
    renderModelSplit(summary.model_split);
    renderHeatmap(summary.hourly_histogram);
    renderCostRow(_lastProfiles.length ? _lastProfiles : (await api('/api/profiles')).profiles, summary.cost_by_profile);
    const caption = document.getElementById('tokensSubCaption');
    if (caption) caption.textContent = t(RANGE_CAPTION_KEYS[summary.range] || RANGE_CAPTION_KEYS['1w']);
  } catch (e) {
    // Overview stays usable even if usage data can't be loaded.
  }
}

// ---- activity ----

const CATEGORY_COLORS = { rotation: '#FFB020', session: '#43C6FF', config: '#35D07F', error: '#FF5C5C' };
const CATEGORY_ICONS = {
  rotation: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/></svg>',
  session: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="3.2"/><path d="M5 20c0-3.6 3.1-6 7-6s7 2.4 7 6"/></svg>',
  config: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
  error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4m0 4h.01M10.3 3.9 2.7 17a2 2 0 0 0 1.7 3h15.2a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>',
};

function renderActivityRow(ev, { compact }) {
  const color = CATEGORY_COLORS[ev.category] || '#6E6E76';
  const icon = CATEGORY_ICONS[ev.category] || '';
  const fullTimestamp = esc(new Date(ev.timestamp).toLocaleString());
  const relativeTime = esc(formatPastRelative(ev.timestamp));
  // The event's timestamp (microsecond-precision ISO8601, unique per event
  // from a single local daemon) doubles as its stable key, which
  // renderActivityFeed() needs to tell a shifted row from a brand-new one.
  const key = esc(ev.timestamp);
  if (compact) {
    return `
      <div class="activity-row" data-id="${key}">
        <span class="activity-icon" style="background:${color}22;color:${color}">${icon}</span>
        <div class="activity-body">
          <span class="activity-time">${relativeTime}<span class="activity-time-tip">${fullTimestamp}</span></span>
          <span class="activity-text">${esc(ev.text)}</span>
        </div>
      </div>`;
  }
  return `
    <div class="activity-row" data-id="${key}">
      <span class="activity-icon" style="background:${color}22;color:${color}">${icon}</span>
      <div class="activity-body">
        <span class="activity-time">${relativeTime}<span class="activity-time-tip">${fullTimestamp}</span></span>
        <span class="activity-text">${esc(ev.text)}${ev.meta ? `<span class="activity-meta">${esc(ev.meta)}</span>` : ''}</span>
      </div>
    </div>`;
}

// setLiveHtml()'s keyed morphing already handles what changed (new rows fade
// in via .live-enter, dropped rows leave via _removeWithExit, unchanged rows
// are untouched). This layers a FLIP position transition on top so rows that
// shifted down a slot slide there instead of teleporting.
function renderActivityFeed(el, events, compact) {
  if (!events.length) {
    setLiveHtml(el, `<div class="empty">${esc(t('empty.no_activity'))}</div>`);
    return;
  }
  const html = events.map((ev) => renderActivityRow(ev, { compact })).join('');
  if (prefersReducedMotion()) { setLiveHtml(el, html); return; }

  const firstRects = new Map();
  for (const row of el.children) {
    if (row.dataset && row.dataset.id) firstRects.set(row.dataset.id, row.getBoundingClientRect());
  }
  // Rows on their way out must be excluded from the FLIP shift below: a row
  // missing from the new `events` list is still a child of `el` right after
  // setLiveHtml() returns, because its .live-exit animation hasn't finished
  // (see _removeWithExit). Setting an inline transform on it would interrupt
  // that animation — both target `transform` — so animationend would never
  // fire and removal would fall back to the 500ms safety timeout.
  const currentKeys = new Set(events.map((ev) => esc(ev.timestamp)));

  if (!setLiveHtml(el, html)) return; // nothing actually changed

  for (const row of el.children) {
    const key = row.dataset && row.dataset.id;
    if (!key || !currentKeys.has(key)) continue; // new row (.live-enter) or exiting row (.live-exit) — leave both alone
    const first = firstRects.get(key);
    if (!first) continue; // genuinely new row — .live-enter already covers it
    const dy = first.top - row.getBoundingClientRect().top;
    if (Math.abs(dy) < 1) continue;
    row.style.transition = 'none';
    row.style.transform = `translateY(${dy}px)`;
    requestAnimationFrame(() => {
      row.style.transition = 'transform 380ms cubic-bezier(.23,1,.32,1)';
      row.style.transform = '';
      row.addEventListener('transitionend', () => { row.style.transition = ''; }, { once: true });
    });
  }
}

async function loadActivityPreview() {
  const el = document.getElementById('activityPreview');
  try {
    const { events } = await api('/api/activity?limit=8');
    renderActivityFeed(el, events, false);
  } catch (e) {
    setLiveHtml(el, `<div class="empty">${esc(t('empty.activity_load_error_prefix'))} ${esc(e.message)}</div>`);
  }
}

let _activityFilter = null;

function renderActivityFilters() {
  const el = document.getElementById('activityFilters');
  if (!el) return;
  const cats = [
    { key: null, label: t('activity.filter_all'), color: null },
    { key: 'rotation', label: t('activity.category.rotation'), color: CATEGORY_COLORS.rotation },
    { key: 'session', label: t('activity.category.session'), color: CATEGORY_COLORS.session },
    { key: 'config', label: t('activity.category.config'), color: CATEGORY_COLORS.config },
    { key: 'error', label: t('activity.category.error'), color: CATEGORY_COLORS.error },
  ];
  el.innerHTML = cats.map((c) => `
    <div class="filter-chip ${_activityFilter === c.key ? 'on' : ''}" data-filter="${c.key ?? ''}">
      ${c.color ? `<span class="dot" style="background:${c.color}"></span>` : ''}${esc(c.label)}
    </div>
  `).join('');
  el.querySelectorAll('.filter-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      _activityFilter = chip.dataset.filter || null;
      renderActivityFilters();
      loadActivity();
    });
  });
}

function activityQueryParams() {
  const params = new URLSearchParams({ limit: '200' });
  if (_activityFilter) params.set('category', _activityFilter);
  const since = document.getElementById('activitySince');
  const until = document.getElementById('activityUntil');
  // datetime-local inputs give a value with no timezone (the machine's own local
  // wall-clock time) — `new Date('YYYY-MM-DDTHH:MM')` parses that as local time,
  // so .toISOString() converts it to the UTC the activity log is actually stored in.
  if (since && since.value) params.set('since', new Date(since.value).toISOString());
  if (until && until.value) params.set('until', new Date(until.value).toISOString());
  return params;
}

async function loadActivity() {
  const el = document.getElementById('activityList');
  try {
    const { events } = await api(`/api/activity?${activityQueryParams()}`);
    renderActivityFeed(el, events, false);
  } catch (e) {
    setLiveHtml(el, `<div class="empty">${esc(t('empty.activity_load_error_prefix'))} ${esc(e.message)}</div>`);
  }
}

function exportActivityLog() {
  const params = activityQueryParams();
  params.delete('limit');
  window.location.href = `/api/activity/export?${params}`;
  showToast('success', t('toast.activity_exported'), '');
}

function clearActivityDateRange() {
  document.getElementById('activitySince').value = '';
  document.getElementById('activityUntil').value = '';
  loadActivity();
}

// ---- generic custom <select> replacement ----

function closeAllSelectPops() {
  document.querySelectorAll('.select-pop').forEach((p) => p.remove());
  // Every trigger setupSelectInput() wires gets data-cu-select regardless of
  // the CSS class it's styled with (.select-input, .rail-lang, ...). Match on
  // that attribute, not on a class — a trigger left with a dangling `open`
  // class never reopens, since the next click sees isOpen already true.
  document.querySelectorAll('[data-cu-select].open').forEach((s) => s.classList.remove('open'));
}

function setupSelectInput(el, options, onChange) {
  // options: [{value, label}]. Reads/writes el.dataset.value; the caller owns
  // persistence via onChange. Reads el._cuOptions rather than the `options`
  // parameter so a later reassignment (e.g. re-translated labels after a
  // language change) is honored instead of closing over a stale array.
  el.dataset.cuSelect = '1';
  function renderValue() {
    const current = el._cuOptions.find((o) => o.value === el.dataset.value) || el._cuOptions[0];
    const span = el.querySelector('.select-value');
    if (span && current) span.textContent = current.label;
  }
  el._cuOptions = options;
  el._cuRenderValue = renderValue;
  renderValue();

  el.addEventListener('click', (e) => {
    e.stopPropagation();
    const isOpen = el.classList.contains('open');
    closeAllSelectPops();
    if (isOpen) return;
    el.classList.add('open');
    const pop = document.createElement('div');
    pop.className = 'select-pop';
    pop.innerHTML = el._cuOptions.map((o) => `<div class="select-pop-item ${o.value === el.dataset.value ? 'sel' : ''}" data-value="${esc(o.value)}">${esc(o.label)}</div>`).join('');
    // Appended to <body> as position:fixed rather than nested inside `el`:
    // a trigger inside an overflow:hidden card (.section, .table) would
    // otherwise have its popup clipped by that ancestor.
    pop.style.position = 'fixed';
    pop.style.right = 'auto';
    pop.style.zIndex = '1000';
    const rect = el.getBoundingClientRect();
    // minWidth must be set BEFORE the popup is measured below. With the
    // popup position:fixed on <body>, the CSS `.select-pop { min-width:
    // 100% }` resolves against the viewport, so an unset minWidth makes
    // getBoundingClientRect().width report the full viewport width and
    // corrupts the left-offset math into always flooring at 8px.
    pop.style.minWidth = rect.width + 'px';
    document.body.appendChild(pop);
    const popRect = pop.getBoundingClientRect();
    const left = Math.min(rect.left, window.innerWidth - popRect.width - 8);
    pop.style.left = Math.max(8, left) + 'px';
    const fitsBelow = rect.bottom + 6 + popRect.height <= window.innerHeight - 8;
    pop.style.top = (fitsBelow ? rect.bottom + 6 : Math.max(8, rect.top - popRect.height - 6)) + 'px';
    pop.querySelectorAll('.select-pop-item').forEach((item) => {
      item.addEventListener('click', (ev) => {
        ev.stopPropagation();
        el.dataset.value = item.dataset.value;
        el._cuRenderValue();
        closeAllSelectPops();
        onChange(item.dataset.value);
      });
    });
  });
}

document.addEventListener('click', () => closeAllSelectPops());
document.addEventListener('scroll', () => closeAllSelectPops(), true);
window.addEventListener('resize', () => closeAllSelectPops());

// ---- Settings ----

const UPDATE_MODE_OPTIONS = () => [
  { value: 'auto_install', label: t('settings.updates.mode_auto_install') },
  { value: 'auto_download', label: t('settings.updates.mode_auto_download') },
  { value: 'manual', label: t('settings.updates.mode_manual') },
];

function setupUpdateModeSelect() {
  const el = document.getElementById('updateModeSelect');
  setupSelectInput(el, UPDATE_MODE_OPTIONS(), async (value) => {
    await api('/api/settings', { method: 'PATCH', body: JSON.stringify({ update_mode: value }) });
    showToast('success', t('toast.settings_saved'), '');
  });
}

// ---- updates (Settings) ----

// Renders GET /api/update. Purely a read: the endpoint reports the last known
// state and never reaches the network itself, so this is safe to call on
// every Settings load.
function renderUpdateState(state) {
  const installed = document.getElementById('updateInstalledVersion');
  const latest = document.getElementById('updateLatestVersion');
  const sub = document.getElementById('updateLatestSub');
  if (!installed || !latest || !sub) return;

  // Only overwrite what the payload actually carries: a response missing a
  // field must leave the displayed value alone rather than blanking it.
  if (state.current_version) installed.textContent = state.current_version;
  const available = state.available;
  if (available) latest.textContent = available.version;
  else if (state.checked_at && state.current_version) latest.textContent = state.current_version;

  let key = 'settings.updates.never_checked';
  if (state.error) key = null;
  else if (available && state.action === 'downloaded') key = 'settings.updates.downloaded';
  else if (available) key = 'settings.updates.available';
  else if (state.checked_at) key = 'settings.updates.up_to_date';
  sub.textContent = key ? t(key) : state.error;
  sub.style.color = state.error ? 'var(--bad)' : '';
  latest.style.color = available ? 'var(--warn)' : '';

  // Grey out only once a check has actually proven there is nothing newer.
  // Before any check the button is still useful: it checks and installs in
  // one go, so disabling it then would remove the only way to act.
  const installBtn = document.getElementById('updateInstallBtn');
  if (installBtn) {
    const knownUpToDate = Boolean(state.checked_at) && !available && !state.error;
    installBtn.classList.toggle('btn-disabled', knownUpToDate);
  }
}

async function refreshUpdateState() {
  try {
    renderUpdateState(await api('/api/update'));
  } catch (e) { /* leave whatever is shown */ }
}

function closeUpdateModal() {
  document.getElementById('updateScrim').classList.remove('open');
}

function _updateModalPhase(phase, { title, sub } = {}) {
  document.getElementById('updateModalLoading').style.display = phase === 'loading' ? '' : 'none';
  document.getElementById('updateModalResult').style.display = phase === 'loading' ? 'none' : '';
  if (title) document.querySelector('#updateScrim .modal-title').textContent = title;
  if (sub) document.getElementById('updateModalSub').textContent = sub;
}

// Check on demand: opens the modal, spins while the request is in flight, then
// says plainly whether there is anything to install. Installing stays a
// separate, deliberate press — the check never installs anything by itself.
async function runUpdateCheckModal() {
  document.getElementById('updateScrim').classList.add('open');
  document.getElementById('updateModalError').style.display = 'none';
  document.getElementById('updateModalInstallBtn').style.display = 'none';
  document.getElementById('updateModalNotesWrap').style.display = 'none';
  _updateModalPhase('loading', { title: t('modal.update.title'), sub: t('modal.update.checking') });

  let state;
  try {
    state = await api('/api/update/check', { method: 'POST' });
  } catch (e) {
    _updateModalPhase('result', { title: t('modal.update.failed'), sub: '' });
    const err = document.getElementById('updateModalError');
    err.textContent = String(e.message || e);
    err.style.display = '';
    return;
  }

  renderUpdateState(state);
  document.getElementById('updateModalInstalled').textContent = state.current_version || '—';

  if (state.error) {
    _updateModalPhase('result', { title: t('modal.update.failed'), sub: '' });
    document.getElementById('updateModalLatestRow').style.display = 'none';
    const err = document.getElementById('updateModalError');
    err.textContent = state.error;
    err.style.display = '';
    return;
  }

  const available = state.available;
  document.getElementById('updateModalLatestRow').style.display = available ? '' : 'none';
  if (!available) {
    _updateModalPhase('result', {
      title: t('modal.update.up_to_date'), sub: t('modal.update.up_to_date_sub'),
    });
    return;
  }

  document.getElementById('updateModalLatest').textContent = available.version;
  if (available.notes) {
    document.getElementById('updateModalNotes').textContent = available.notes;
    document.getElementById('updateModalNotesWrap').style.display = '';
  }
  document.getElementById('updateModalInstallBtn').style.display = '';
  _updateModalPhase('result', { title: t('modal.update.available'), sub: '' });
}

async function runUpdateInstall() {
  document.getElementById('updateScrim').classList.add('open');
  document.getElementById('updateModalError').style.display = 'none';
  document.getElementById('updateModalInstallBtn').style.display = 'none';
  _updateModalPhase('loading', {
    title: t('modal.update.installing'), sub: t('modal.update.installing_sub'),
  });
  try {
    // force: an explicit press overrides the idle guard that holds back
    // automatic installs during an active session.
    const res = await api('/api/update/install', {
      method: 'POST', body: JSON.stringify({ force: true }),
    });
    if (res.installed) {
      _updateModalPhase('result', {
        title: t('modal.update.installed'), sub: t('modal.update.installed_sub'),
      });
      showToast('ok', t('toast.update_installed'), t('toast.update_installed_sub'));
      // The daemon restarts itself now, so the page must wait for the new one
      // to answer before reading a version — asking too early either fails or
      // reports the version that is on its way out.
      setTimeout(() => window.location.reload(), 6000);
      return;
    } else {
      _updateModalPhase('result', {
        title: t('modal.update.up_to_date'), sub: t('modal.update.up_to_date_sub'),
      });
    }
  } catch (e) {
    _updateModalPhase('result', { title: t('modal.update.failed'), sub: '' });
    const err = document.getElementById('updateModalError');
    err.textContent = String(e.message || e);
    err.style.display = '';
  }
  await refreshUpdateState();
}

function wireUpdateButtons() {
  const checkBtn = document.getElementById('updateCheckBtn');
  const installBtn = document.getElementById('updateInstallBtn');
  if (!checkBtn || !installBtn) return;

  // Both buttons hit the network, so they say what they are doing. Without
  // this the button simply sat there and the whole thing read as broken.
  function withProgress(btn, labelKey, work) {
    return async () => {
      if (btn.classList.contains('btn-disabled')) return;
      const label = btn.querySelector('span');
      const original = label ? label.textContent : '';
      if (label) label.textContent = t(labelKey);
      btn.classList.add('btn-disabled');
      try {
        await work();
      } finally {
        btn.classList.remove('btn-disabled');
        if (label) label.textContent = original;
      }
    };
  }

  checkBtn.addEventListener('click', withProgress(checkBtn, 'settings.updates.checking', runUpdateCheckModal));

  installBtn.addEventListener('click', withProgress(installBtn, 'settings.updates.installing', runUpdateInstall));

  document.getElementById('updateModalClose').addEventListener('click', closeUpdateModal);
  document.getElementById('updateModalCloseBtn').addEventListener('click', closeUpdateModal);
  document.getElementById('updateScrim').addEventListener('click', (e) => {
    if (e.target.id === 'updateScrim') closeUpdateModal();
  });
  document.getElementById('updateModalInstallBtn').addEventListener('click', runUpdateInstall);
}

async function loadSettings() {
  try {
    const { settings } = await api('/api/settings');
    document.getElementById('updateModeSelect').dataset.value = settings.update_mode;
    document.getElementById('updateModeSelect')._cuOptions = UPDATE_MODE_OPTIONS();
    document.getElementById('updateModeSelect')._cuRenderValue();
    setToggleState(document.getElementById('notifMasterToggle'), settings.notifications_enabled);
    renderNotifList(settings);
    refreshUpdateState();
  } catch (e) {
    // leave defaults if this fails
  }
  await loadDaemonServiceStatus();
  await loadProcessStats();
  await loadPlaceholderToken();
  const hostsLine = document.getElementById('hostsLine');
  if (hostsLine) hostsLine.textContent = '127.0.0.1  claude.unlimited';
  const shellLine = document.getElementById('shellLine');
  if (shellLine) {
    const token = document.getElementById('placeholderTokenLine')?.textContent || '';
    // Claude Code sends ANTHROPIC_AUTH_TOKEN as `Authorization: Bearer
    // <token>`, which is what the daemon's proxy path checks. The two vars
    // are read independently: ANTHROPIC_BASE_URL only picks the endpoint, so
    // without this line every request is rejected with 401.
    shellLine.textContent = `export ANTHROPIC_BASE_URL="http://${window.location.host || '127.0.0.1:4317'}"\nexport ANTHROPIC_AUTH_TOKEN="${token}"`;
  }
}

async function sendTestNotification() {
  try {
    await api('/api/notifications/test', { method: 'POST', body: '{}' });
    showToast('success', t('toast.test_notification_sent'), t('toast.test_notification_sub'));
  } catch (e) {
    showToast('error', t('toast.test_notification_failed'), e.message);
  }
}

function setToggleState(el, on) {
  if (!el) return;
  el.classList.toggle('off', !on);
  el.dataset.on = on ? '1' : '';
}

const NOTIF_ROWS = [
  { field: 'notify_update_available', key: 'settings.notifications.update_available', icon: '<path d="M12 15V3m0 12-4-4m4 4 4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>' },
  { field: 'notify_approaching_threshold', key: 'settings.notifications.approaching_threshold', icon: '<path d="M12 9v4m0 4h.01M10.3 3.9 2.7 17a2 2 0 0 0 1.7 3h15.2a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>' },
  { field: 'notify_rotated', key: 'settings.notifications.rotated', icon: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/>' },
  { field: 'notify_quota_reset', key: 'settings.notifications.quota_reset', icon: '<path d="M12 8v4m0 4h.01"/><circle cx="12" cy="12" r="9"/>' },
  { field: 'notify_needs_attention', key: 'settings.notifications.needs_attention', icon: '<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/>' },
];

let _lastSettings = null;

function renderNotifList(settings) {
  if (settings) _lastSettings = settings;
  const el = document.getElementById('notifList');
  if (!el || !_lastSettings) return;
  el.innerHTML = NOTIF_ROWS.map((row) => `
    <div class="notif-row">
      <div class="notif-text">
        <div class="notif-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${row.icon}</svg></div>
        <span class="field-title">${esc(t(row.key))}</span>
        ${row.field === 'notify_needs_attention' ? `<span class="field-sub" style="margin-left:2px;">${esc(t('settings.notifications.needs_attention_sub'))}</span>` : ''}
      </div>
      <div class="toggle ${_lastSettings[row.field] ? '' : 'off'} notif-toggle" data-field="${row.field}"><div class="toggle-knob"></div></div>
    </div>
  `).join('');
  el.querySelectorAll('.notif-toggle').forEach((toggle) => {
    toggle.addEventListener('click', async () => {
      const field = toggle.dataset.field;
      const next = toggle.classList.contains('off');
      await api('/api/settings', { method: 'PATCH', body: JSON.stringify({ [field]: next }) });
      _lastSettings[field] = next;
      toggle.classList.toggle('off', !next);
    });
  });
}

async function toggleNotificationsMaster() {
  const el = document.getElementById('notifMasterToggle');
  const next = el.classList.contains('off');
  await api('/api/settings', { method: 'PATCH', body: JSON.stringify({ notifications_enabled: next }) });
  setToggleState(el, next);
}

async function loadDaemonServiceStatus() {
  const toggle = document.getElementById('autostartToggle');
  const sub = document.getElementById('daemonStatusSub');
  try {
    const s = await api('/api/service');
    setToggleState(toggle, s.installed);
    const uptime = _lastStatus ? formatDuration(_lastStatus.uptime_seconds) : '';
    const host = window.location.host || 'claude.unlimited:4317';
    let statusText = `up ${uptime} · listening on ${host}`;
    if (s.installed) statusText += s.running ? ' · installed (auto-start on login)' : ' · installed but not running via launchd';
    if (sub) sub.textContent = statusText;
  } catch (e) {
    if (sub) sub.textContent = '';
  }
}

async function toggleAutostart() {
  const toggle = document.getElementById('autostartToggle');
  const installing = toggle.classList.contains('off');
  try {
    if (installing) {
      const port = Number(window.location.port) || 4317;
      await api('/api/service/install', { method: 'POST', body: JSON.stringify({ port }) });
    } else {
      await api('/api/service/uninstall', { method: 'POST', body: '{}' });
    }
  } catch (e) {
    showToast('error', t('toast.autostart_failed'), e.message);
  }
  await loadDaemonServiceStatus();
}

async function loadProcessStats() {
  try {
    const p = await api('/api/process');
    const pidEl = document.getElementById('processPidValue');
    const memEl = document.getElementById('processMemoryValue');
    const upEl = document.getElementById('processUptimeValue');
    if (pidEl) pidEl.textContent = String(p.pid);
    if (memEl) memEl.textContent = `${p.memory_mb.toFixed(1)} MB`;
    if (upEl) upEl.textContent = formatDuration(p.uptime_seconds);

    const restartBtn = document.getElementById('restartProcessBtn');
    const restartSub = document.getElementById('processRestartSub');
    if (restartBtn) {
      restartBtn.classList.toggle('btn-disabled', !p.installed_as_service);
      restartSub.textContent = p.installed_as_service
        ? t('settings.process.restart_sub')
        : t('settings.process.restart_unavailable_sub');
    }
  } catch (e) {
    // leave the last-known values on a missed poll — same policy as pollLiveUpdate
  }
}

function killProcess() {
  openConfirmModal({
    title: t('toast.kill_title'),
    body: t('toast.kill_body'),
    confirmText: 'KILL',
    actionLabel: t('settings.process.kill_btn'),
    hint: t('modal.confirm.type_kill'),
    onConfirm: async () => {
      const res = await api('/api/process/kill', { method: 'POST', body: '{}' });
      if (res.was_service) {
        showToast('success', t('toast.kill_service_done'), t('toast.kill_service_sub'));
        setTimeout(loadProcessStats, 3000);
      } else {
        showToast('success', t('toast.kill_done'), t('toast.kill_sub'));
      }
    },
  });
}

function restartProcess() {
  const btn = document.getElementById('restartProcessBtn');
  if (btn && btn.classList.contains('btn-disabled')) return;
  openConfirmModal({
    title: t('toast.restart_title'),
    body: t('toast.restart_body'),
    confirmText: 'RESTART',
    actionLabel: t('settings.process.restart_btn'),
    hint: t('modal.confirm.type_restart'),
    onConfirm: async () => {
      try {
        await api('/api/process/restart', { method: 'POST', body: '{}' });
        showToast('success', t('toast.restart_done'), t('toast.restart_sub'));
        setTimeout(loadProcessStats, 3000);
      } catch (e) {
        showToast('error', t('toast.restart_failed'), e.message);
      }
    },
  });
}

async function loadPlaceholderToken() {
  try {
    const { token } = await api('/api/placeholder-token');
    const el = document.getElementById('placeholderTokenLine');
    if (el) el.textContent = token;
  } catch (e) {
    // leave placeholder text
  }
}

function regeneratePlaceholderToken() {
  openConfirmModal({
    title: t('toast.regenerate_title'),
    body: t('toast.regenerate_body'),
    confirmText: 'REGENERATE',
    actionLabel: t('settings.network.regenerate'),
    hint: t('modal.confirm.type_regenerate'),
    onConfirm: async () => {
      const { token } = await api('/api/placeholder-token/regenerate', { method: 'POST', body: '{}' });
      const el = document.getElementById('placeholderTokenLine');
      if (el) el.textContent = token;
      showToast('success', t('toast.token_regenerated'), '');
    },
  });
}

function wireCopyButtons() {
  document.querySelectorAll('.copy-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const targetId = btn.dataset.copyTarget;
      const targetEl = document.getElementById(targetId);
      if (!targetEl) return;
      try {
        await navigator.clipboard.writeText(targetEl.textContent);
        const label = btn.querySelector('span');
        const original = label.textContent;
        label.textContent = t('settings.network.copied');
        setTimeout(() => { label.textContent = original; }, 1200);
      } catch (e) {
        // clipboard access denied — nothing to recover from here
      }
    });
  });
}

function resetAllProfiles() {
  openConfirmModal({
    title: t('toast.reset_title'),
    body: t('toast.reset_body'),
    confirmText: 'RESET',
    actionLabel: t('settings.danger.reset_btn'),
    hint: t('modal.confirm.type_reset'),
    onConfirm: async () => {
      const { removed } = await api('/api/reset', { method: 'POST', body: '{}' });
      await loadProfiles();
      await loadProfilesTable();
      showToast('success', t('toast.reset_done'), `${removed} ${t('toast.profiles_removed_count')}`);
    },
  });
}

// ---- Add profile modal ----

// Must stay in sync with profiles.py's _TAG_COLORS — that server-side
// allowlist accepts or rejects whatever a save sends from here.
const TAG_COLORS = ['#43C6FF', '#35D07F', '#FFB020', '#C97BFF', '#FF5FA6', '#2FD9C4', '#FF5C5C', '#FFD93D', '#6C8EFF', '#5C5C63'];
let _selectedType = 'oauth';
let _selectedAuthMode = 'api_key';
// codex's sub-choice (chatgpt_subscription vs api_key), kept separate from
// _selectedAuthMode above (api-kind's api_key/bearer toggle) so picking one
// doesn't clobber the other's 'on' state. Both share the .seg-btn look but
// are wired independently.
let _selectedCodexAuthMode = 'chatgpt_subscription';
let _selectedTagColor = null;

// Shared by the Add Profile form (f_tag_row) and the Profile Detail modal
// (pd_tag_row). Each keeps its own selected-color state and passes it in,
// since they edit different Profiles (or a not-yet-created one).
function renderTagRow(elId, selected, onSelect) {
  const el = document.getElementById(elId);
  el.innerHTML = TAG_COLORS.map((c) => `<button type="button" class="tag-dot ${c === selected ? 'sel' : ''}" style="background:${c}" data-color="${c}"></button>`).join('');
  el.querySelectorAll('.tag-dot').forEach((dot) => {
    dot.addEventListener('click', () => onSelect(selected === dot.dataset.color ? null : dot.dataset.color));
  });
}

function renderAddProfileTagRow() {
  renderTagRow('f_tag_row', _selectedTagColor, (color) => {
    _selectedTagColor = color;
    renderAddProfileTagRow();
  });
}

let _pdSelectedTagColor = null;

function renderDetailTagRow() {
  renderTagRow('pd_tag_row', _pdSelectedTagColor, (color) => {
    _pdSelectedTagColor = color;
    renderDetailTagRow();
  });
}

function selectType(type) {
  _selectedType = type;
  document.querySelectorAll('.type-card').forEach((card) => card.classList.toggle('sel', card.dataset.typeCard === type));
  document.getElementById('importPane').style.display = type === 'import' ? '' : 'none';
  document.getElementById('oauthAddAccountPane').style.display = type === 'oauth' ? '' : 'none';

  document.getElementById('f_codex_authmode_wrap').style.display = type === 'codex' ? '' : 'none';
  const codexChatgpt = type === 'codex' && _selectedCodexAuthMode === 'chatgpt_subscription';
  const codexApiKey = type === 'codex' && _selectedCodexAuthMode === 'api_key';
  document.getElementById('codexAddAccountPane').style.display = codexChatgpt ? '' : 'none';

  // manualPane (name + credential + advanced settings) covers 'api' and
  // codex's api_key sub-choice. A chatgpt_subscription codex account is
  // CLI-only, like oauth's add-account flow (see codexAddAccountPane), so it
  // never uses this form.
  document.getElementById('manualPane').style.display = (type === 'api' || codexApiKey) ? '' : 'none';
  document.getElementById('f_base_url_wrap').style.display = type === 'api' ? '' : 'none';
  document.getElementById('f_codex_base_url_wrap').style.display = codexApiKey ? '' : 'none';
  document.getElementById('f_authmode_wrap').style.display = type === 'api' ? '' : 'none';
  document.getElementById('f_credential_wrap').style.display = type === 'api' ? '' : 'none';
  document.getElementById('f_codex_credential_wrap').style.display = codexApiKey ? '' : 'none';
  document.getElementById('f_default_model_wrap').style.display = type === 'api' ? '' : 'none';
  document.getElementById('f_budget_wrap').style.display = type === 'api' ? '' : 'none';
  // Shown for both codex sub-choices, since the model mapping is relevant
  // either way, but codexChatgpt's parent (manualPane) stays hidden — that
  // account is CLI-created with no submit path here, so its overrides are
  // set afterward from the Edit menu.
  document.getElementById('f_codex_model_wrap').style.display = type === 'codex' ? '' : 'none';
  document.getElementById('f_codex_mapping_wrap').style.display = type === 'codex' ? '' : 'none';
  // API keys have no session-% concept, so an absolute token budget takes
  // switch_threshold's slot — the same swap as the Profile Detail modal and
  // the Profiles table chip. codex uses switch_threshold like oauth.
  document.getElementById('f_threshold_wrap').style.display = type === 'api' ? 'none' : '';
  document.getElementById('f_token_threshold_wrap').style.display = type === 'api' ? '' : 'none';
  document.getElementById('saveBtn').style.display = (type === 'import' || type === 'oauth' || codexChatgpt) ? 'none' : '';
}

// Closes a modal on a genuine backdrop click, but not when the click is the
// tail end of a text-selection drag that started inside the modal and was
// released over the scrim. A 'click' listener alone can't tell those apart,
// since the browser fires 'click' on whatever element the mouse was released
// over, so require that both mousedown and click landed on the scrim.
function wireScrimClickOutside(scrimEl, closeFn) {
  let downOnScrim = false;
  scrimEl.addEventListener('mousedown', (e) => {
    downOnScrim = e.target === scrimEl;
  });
  scrimEl.addEventListener('click', (e) => {
    if (downOnScrim && e.target === scrimEl) closeFn();
    downOnScrim = false;
  });
}

function openModal() {
  document.getElementById('scrim').classList.add('open');
  selectType('oauth');
  document.getElementById('f_name').value = '';
  document.getElementById('f_base_url').value = '';
  document.getElementById('f_credential').value = '';
  document.getElementById('f_codex_base_url').value = '';
  document.getElementById('f_codex_credential').value = '';
  document.getElementById('f_default_model').value = '';
  document.getElementById('f_budget').value = '';
  document.getElementById('f_token_threshold').value = '';
  document.getElementById('f_threshold_val').textContent = '98';
  document.getElementById('f_codex_model').dataset.value = '';
  document.getElementById('f_codex_model')._cuRenderValue();
  updateCodexModelHelp('f');
  document.getElementById('f_codex_reasoning').dataset.value = '';
  document.getElementById('f_codex_reasoning')._cuRenderValue();
  _selectedCodexAuthMode = 'chatgpt_subscription';
  document.querySelectorAll('.seg-btn[data-codex-auth-mode]').forEach((b) => b.classList.toggle('on', b.dataset.codexAuthMode === 'chatgpt_subscription'));
  // Next free slot in the rotation order, not a hardcoded '1': a new Profile
  // should slot in after the existing ones rather than tie with whichever
  // one already holds top priority.
  const nextPriority = _lastProfiles.length ? Math.max(...(_lastProfiles.map((p) => p.priority))) + 1 : 1;
  document.getElementById('f_priority_val').textContent = String(nextPriority);
  _selectedAuthMode = 'api_key';
  document.querySelectorAll('.seg-btn[data-auth-mode]').forEach((b) => b.classList.toggle('on', b.dataset.authMode === 'api_key'));
  _selectedTagColor = null;
  renderAddProfileTagRow();
  document.getElementById('f_automatic_toggle').classList.remove('off');
  document.getElementById('advancedBody').classList.add('open');
  document.getElementById('advancedToggle').classList.add('open');
  document.getElementById('importStatus').style.display = 'none';
}

function closeModal() {
  document.getElementById('scrim').classList.remove('open');
  document.getElementById('f_error').style.display = 'none';
}

async function submitProfile() {
  if (_selectedType === 'import') {
    await importCurrentLogin();
    return;
  }
  // codex's chatgpt_subscription sub-choice never reaches here: saveBtn is
  // hidden for it (see selectType) because that account is CLI-only.
  const kind = _selectedType === 'api' ? 'api' : _selectedType === 'codex' ? 'codex' : 'oauth';
  const payload = {
    name: document.getElementById('f_name').value,
    kind,
    credential: kind === 'codex' ? document.getElementById('f_codex_credential').value : document.getElementById('f_credential').value,
    switch_threshold: Number(document.getElementById('f_threshold_val').textContent),
    priority: Number(document.getElementById('f_priority_val').textContent),
    automatic: !document.getElementById('f_automatic_toggle').classList.contains('off'),
  };
  if (_selectedTagColor) payload.tag_color = _selectedTagColor;
  if (kind === 'api') {
    const bu = document.getElementById('f_base_url').value;
    if (bu) payload.base_url = bu;
    payload.auth_mode = _selectedAuthMode;
    const model = document.getElementById('f_default_model').value;
    if (model) payload.default_model = model;
    const budgetRaw = document.getElementById('f_budget').value.replace(/[^0-9.]/g, '');
    if (budgetRaw) payload.monthly_budget_cap = Number(budgetRaw);
    const tokenThresholdRaw = document.getElementById('f_token_threshold').value.trim();
    if (tokenThresholdRaw) payload.token_threshold = Math.round(Number(tokenThresholdRaw));
  }
  if (kind === 'codex') {
    payload.auth_mode = 'api_key';
    const bu = document.getElementById('f_codex_base_url').value;
    if (bu) payload.base_url = bu;
    const model = document.getElementById('f_codex_model').dataset.value;
    if (model) payload.codex_model = model;
    const effort = document.getElementById('f_codex_reasoning').dataset.value;
    if (effort) payload.codex_reasoning_effort = effort;
  }

  const errEl = document.getElementById('f_error');
  try {
    await api('/api/profiles', { method: 'POST', body: JSON.stringify(payload) });
    closeModal();
    await loadProfiles();
    await loadProfilesTable();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.style.display = '';
  }
}

async function importCurrentLogin() {
  const statusEl = document.getElementById('importStatus');
  statusEl.style.display = '';
  statusEl.className = 'import-status';
  statusEl.textContent = 'Looking for a Claude Code login on this Mac…';
  try {
    const result = await api('/api/import-claude-code', { method: 'POST', body: '{}' });
    statusEl.className = 'import-status ok';
    statusEl.textContent = `Imported ${result.account.email || result.profile.name}${result.account.org_name ? ' · ' + result.account.org_name : ''}.`;
    await loadProfiles();
    await loadProfilesTable();
    setTimeout(closeModal, 900);
  } catch (e) {
    statusEl.className = 'import-status err';
    statusEl.textContent = e.message;
  }
}

// ---- Export ----

function openExportModal() {
  document.getElementById('exportScrim').classList.add('open');
  const box = document.getElementById('exp_profiles_box');
  setCheckbox(box, false);
  box.classList.remove('warn-on');
  onExportProfilesChange();
}

function closeExportModal() {
  document.getElementById('exportScrim').classList.remove('open');
  document.getElementById('exp_error').style.display = 'none';
}

function setCheckbox(boxEl, on) {
  boxEl.classList.toggle('on', on);
  boxEl.dataset.on = on ? '1' : '';
}

function isChecked(boxEl) {
  return boxEl.classList.contains('on');
}

function onExportProfilesChange() {
  const needsPassphrase = isChecked(document.getElementById('exp_profiles_box'));
  document.getElementById('exp_passphrase_wrap').style.display = needsPassphrase ? '' : 'none';
}

async function submitExport() {
  const errEl = document.getElementById('exp_error');
  const includeProfiles = isChecked(document.getElementById('exp_profiles_box'));
  const payload = {
    include_profiles: includeProfiles,
    include_settings: isChecked(document.getElementById('exp_settings_box')),
    include_activity: isChecked(document.getElementById('exp_activity_box')),
  };
  const passphrase = document.getElementById('exp_passphrase').value;
  if (passphrase) payload.passphrase = passphrase;

  try {
    const res = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
      body: JSON.stringify(payload),
    });
    const text = await res.text();
    if (!res.ok) {
      const body = JSON.parse(text);
      throw new Error(body.message || body.error || 'export failed');
    }
    const blob = new Blob([text], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    a.href = url;
    a.download = `claude-unlimited-export-${stamp}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    closeExportModal();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.style.display = '';
  }
}

// ---- Import ----

let _importBundleText = null;

function openImportModal() {
  document.getElementById('importScrim').classList.add('open');
}

function closeImportModal() {
  document.getElementById('importScrim').classList.remove('open');
  document.getElementById('imp_error').style.display = 'none';
  document.getElementById('imp_preview').style.display = 'none';
  document.getElementById('imp_file').value = '';
  document.getElementById('imp_passphrase').value = '';
  document.getElementById('imp_passphrase_wrap').style.display = 'none';
  document.getElementById('importPreviewBtn').style.display = '';
  document.getElementById('importApplyBtn').style.display = 'none';
  document.getElementById('imp_dropzone').style.display = '';
  document.getElementById('imp_file_row').style.display = 'none';
  _importBundleText = null;
}

function selectImportFile(file) {
  if (!file) return;
  const dt = new DataTransfer();
  dt.items.add(file);
  document.getElementById('imp_file').files = dt.files;
  document.getElementById('imp_dropzone').style.display = 'none';
  const row = document.getElementById('imp_file_row');
  row.style.display = '';
  document.getElementById('imp_file_name').textContent = file.name;
  document.getElementById('imp_file_meta').textContent = `${(file.size / 1024).toFixed(1)} KB`;
}

function readImportFile() {
  return new Promise((resolve, reject) => {
    const input = document.getElementById('imp_file');
    if (!input.files.length) { reject(new Error('Choose a bundle file first.')); return; }
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('Could not read the file.'));
    reader.readAsText(input.files[0]);
  });
}

async function previewImport() {
  const errEl = document.getElementById('imp_error');
  errEl.style.display = 'none';
  try {
    _importBundleText = await readImportFile();
    const passphrase = document.getElementById('imp_passphrase').value;
    const body = { bundle: _importBundleText };
    if (passphrase) body.passphrase = passphrase;

    let result;
    try {
      result = await api('/api/import/preview', { method: 'POST', body: JSON.stringify(body) });
    } catch (e) {
      if (e.message === 'This bundle is encrypted — a passphrase is required.') {
        document.getElementById('imp_passphrase_wrap').style.display = '';
      }
      throw e;
    }

    const bodyEl = document.getElementById('imp_preview_body');
    const lines = [];
    if (result.profiles.length) {
      lines.push(`<div><strong>${result.profiles.length}</strong> profile(s): ${result.profiles.map((p) => esc(p.name)).join(', ')}</div>`);
    } else {
      lines.push('<div>No profiles in this bundle.</div>');
    }
    lines.push(`<div>${result.settings_included ? 'Includes settings.' : 'No settings in this bundle.'}</div>`);
    if (result.activity_count) lines.push(`<div>${result.activity_count} activity event(s) (not importable — a machine's own history is its own).</div>`);
    bodyEl.innerHTML = lines.join('');

    document.getElementById('imp_profiles_wrap').style.display = result.profiles.length ? '' : 'none';
    document.getElementById('imp_conflict_wrap').style.display = result.profiles.length ? '' : 'none';
    setCheckbox(document.getElementById('imp_apply_profiles_box'), result.profiles.length > 0);
    document.getElementById('imp_settings_wrap').style.display = result.settings_included ? '' : 'none';
    setCheckbox(document.getElementById('imp_apply_settings_box'), result.settings_included);
    document.getElementById('imp_preview').style.display = '';
    document.getElementById('importPreviewBtn').style.display = 'none';
    document.getElementById('importApplyBtn').style.display = '';
  } catch (e) {
    errEl.textContent = e.message;
    errEl.style.display = '';
  }
}

async function applyImport() {
  const errEl = document.getElementById('imp_error');
  errEl.style.display = 'none';
  try {
    const passphrase = document.getElementById('imp_passphrase').value;
    const body = {
      bundle: _importBundleText,
      import_profiles: isChecked(document.getElementById('imp_apply_profiles_box')),
      import_settings: isChecked(document.getElementById('imp_apply_settings_box')),
      conflict_strategy: document.querySelector('#imp_conflict .seg-btn.on').dataset.conflictValue,
    };
    if (passphrase) body.passphrase = passphrase;
    await api('/api/import/apply', { method: 'POST', body: JSON.stringify(body) });
    closeImportModal();
    await loadProfiles();
    await loadProfilesTable();
    await loadSettings();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.style.display = '';
  }
}

// ---- View switching ----

let _currentView = 'overview';

function switchView(view) {
  closeKebabMenu();
  closeAllSelectPops();
  _currentView = view;
  document.querySelectorAll('.rail-item').forEach((el) => el.classList.toggle('active', el.dataset.view === view));
  document.querySelectorAll('[data-view-panel]').forEach((el) => {
    el.style.display = el.dataset.viewPanel === view ? '' : 'none';
  });

  if (view === 'overview') { loadStatus().then(() => loadProfiles()); loadProjectUsage(); loadUsageSummary(); loadActivityPreview(); }
  if (view === 'profiles') loadProfilesTable();
  if (view === 'activity') { renderActivityFilters(); loadActivity(); }
  if (view === 'settings') loadSettings();
}

// ---- live-update poll — refreshes whichever view is on screen every
// second; setLiveHtml (top of file) keeps unchanged content from flickering.
// Skipped while the tab is hidden, a modal is open (don't pull content out
// from under active input), or a Profiles-table drag is in progress.
async function pollLiveUpdate() {
  if (document.hidden) return;
  if (document.querySelector('.modal-scrim.open')) return;
  try {
    // loadStatus() supplies "who's active" and must run on every tick
    // regardless of the open view — otherwise a rotation that happens while
    // another view is on screen leaves the wrong Profile marked active
    // indefinitely, since nothing else refreshes it.
    await loadStatus();
    if (_currentView === 'overview') {
      await Promise.all([loadProfiles(), loadProjectUsage(), loadUsageSummary(), loadActivityPreview()]);
    } else if (_currentView === 'profiles') {
      await loadProfilesTable();
    } else if (_currentView === 'activity') {
      await loadActivity();
    } else if (_currentView === 'settings') {
      await loadProcessStats();
    }
  } catch (e) {
    // a single missed poll tick isn't worth surfacing — the connection
    // banner (checkConnection, polling independently) reports a genuinely
    // unreachable daemon.
  }
}

// A backgrounded tab skips every poll tick (see document.hidden above), so
// refresh immediately on return rather than showing stale active-profile and
// usage data for up to a full poll interval.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) pollLiveUpdate();
});
setInterval(pollLiveUpdate, 1000);

document.querySelectorAll('.rail-item').forEach((el) => {
  el.addEventListener('click', () => switchView(el.dataset.view));
});

setupUpdateModeSelect();
document.getElementById('resetBtn').addEventListener('click', resetAllProfiles);
document.getElementById('notifMasterToggle').addEventListener('click', toggleNotificationsMaster);
document.getElementById('autostartToggle').addEventListener('click', toggleAutostart);
document.getElementById('regenTokenBtn').addEventListener('click', regeneratePlaceholderToken);
document.getElementById('killProcessBtn').addEventListener('click', killProcess);
document.getElementById('restartProcessBtn').addEventListener('click', restartProcess);
document.getElementById('testNotificationBtn').addEventListener('click', sendTestNotification);

// Overview: theme, chart-type, refresh
document.querySelectorAll('[data-theme-choice]').forEach((el) => {
  el.addEventListener('click', () => applyTheme(el.dataset.themeChoice));
});
document.querySelectorAll('.mini-chart-opt').forEach((el) => {
  el.addEventListener('click', () => {
    const target = el.closest('.mini-chart-toggle').dataset.chartTarget;
    applyMiniChartStyle(target, el.dataset.chartChoice);
  });
});
document.querySelectorAll('.mini-metric-opt').forEach((el) => {
  el.addEventListener('click', () => {
    const target = el.closest('.mini-metric-toggle').dataset.metricTarget;
    applyMetricMode(target, el.dataset.metricChoice);
    if (target === 'model') renderModelSplit(_lastModelSplit);
    else if (target === 'project') renderProjectUsageList(_lastProjects);
    else if (target === 'account' && _lastAccountUsage) {
      renderAccountUsageChart(_lastAccountUsage.dailyTotals, _lastAccountUsage.profileColors, _lastAccountUsage.profileNames);
    }
  });
});
{
  const rangeEl = document.getElementById('usageRangeSelect');
  rangeEl.dataset.value = _usageRange; // reflect any persisted value before first render
  setupSelectInput(rangeEl, [
    { value: '1h', label: '1h' },
    { value: '1d', label: '1d' },
    { value: '1w', label: '1w' },
    { value: '1m', label: '1m' },
    { value: '1y', label: '1y' },
  ], (value) => {
    _usageRange = value;
    localStorage.setItem('cu-usage-range', _usageRange);
    loadUsageSummary();
  });
}
document.getElementById('refreshBtn').addEventListener('click', async () => {
  const btn = document.getElementById('refreshBtn');
  btn.classList.add('spinning');
  await Promise.all([loadStatus().then(() => loadProfiles()), loadProjectUsage(), loadUsageSummary(), loadActivityPreview()]);
  setTimeout(() => btn.classList.remove('spinning'), 400);
  showToast('success', t('toast.refreshed'), '');
});
document.getElementById('connectionBannerRetry').addEventListener('click', checkConnection);

// Profiles page: search / filter / sort
document.getElementById('profileSearchInput').addEventListener('input', (e) => {
  _profileSearch = e.target.value;
  loadProfilesTable();
});
document.querySelectorAll('#profileStatusFilters .filter-chip').forEach((chip) => {
  chip.addEventListener('click', () => {
    _profileStatusFilter = chip.dataset.statusFilter;
    document.querySelectorAll('#profileStatusFilters .filter-chip').forEach((c) => c.classList.toggle('on', c === chip));
    loadProfilesTable();
  });
});
setupSelectInput(document.getElementById('profileSortSelect'), [
  { value: 'priority', label: t('profiles.sort_priority') },
  { value: 'name', label: t('profiles.sort_name') },
  { value: 'usage', label: t('profiles.sort_usage') },
], (value) => { _profileSort = value; loadProfilesTable(); });

// Activity page: export + date range
document.getElementById('activityExportBtn').addEventListener('click', exportActivityLog);
document.getElementById('activitySince').addEventListener('change', loadActivity);
document.getElementById('activityUntil').addEventListener('change', loadActivity);
document.getElementById('activityDateClearBtn').addEventListener('click', clearActivityDateRange);

// Profile Detail modal
document.getElementById('profileDetailCloseBtn').addEventListener('click', closeProfileDetailModal);
document.getElementById('profileDetailCancelBtn').addEventListener('click', closeProfileDetailModal);
document.getElementById('pd_save_btn').addEventListener('click', saveProfileDetail);
document.getElementById('pd_remove_btn').addEventListener('click', removeProfileFromDetail);
document.getElementById('pd_automatic_toggle').addEventListener('click', (e) => e.currentTarget.classList.toggle('off'));
document.querySelectorAll('[data-pd-step]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const field = btn.dataset.pdStep;
    const valEl = document.getElementById(`pd_${field}_val`);
    const delta = Number(btn.dataset.delta);
    const max = field === 'threshold' ? 100 : 99;
    const next = Math.max(1, Math.min(max, Number(valEl.textContent) + delta));
    valEl.textContent = String(next);
  });
});
wireScrimClickOutside(document.getElementById('profileDetailScrim'), closeProfileDetailModal);

// Confirm modal
document.getElementById('confirmCancelBtn').addEventListener('click', closeConfirmModal);
document.getElementById('confirmActionBtn').addEventListener('click', submitConfirmModal);
wireScrimClickOutside(document.getElementById('confirmScrim'), closeConfirmModal);

document.querySelectorAll('.add-profile-btn').forEach((btn) => btn.addEventListener('click', openModal));
document.getElementById('cancelBtn').addEventListener('click', closeModal);
document.getElementById('cancelBtn2').addEventListener('click', closeModal);
document.getElementById('saveBtn').addEventListener('click', submitProfile);
document.getElementById('importBtn').addEventListener('click', importCurrentLogin);
document.getElementById('credentialEyeBtn').addEventListener('click', () => {
  const input = document.getElementById('f_credential');
  input.type = input.type === 'password' ? 'text' : 'password';
});
document.getElementById('codexCredentialEyeBtn').addEventListener('click', () => {
  const input = document.getElementById('f_codex_credential');
  input.type = input.type === 'password' ? 'text' : 'password';
});
document.querySelectorAll('.type-card').forEach((card) => {
  card.addEventListener('click', () => selectType(card.dataset.typeCard));
});
document.querySelectorAll('.seg-btn[data-auth-mode]').forEach((btn) => {
  btn.addEventListener('click', () => {
    _selectedAuthMode = btn.dataset.authMode;
    document.querySelectorAll('.seg-btn[data-auth-mode]').forEach((b) => b.classList.toggle('on', b === btn));
  });
});
// Separate selector and state from the api-kind api_key/bearer toggle above
// — see _selectedCodexAuthMode for why they can't share a data attribute.
document.querySelectorAll('.seg-btn[data-codex-auth-mode]').forEach((btn) => {
  btn.addEventListener('click', () => {
    _selectedCodexAuthMode = btn.dataset.codexAuthMode;
    document.querySelectorAll('.seg-btn[data-codex-auth-mode]').forEach((b) => b.classList.toggle('on', b === btn));
    selectType('codex');
  });
});
// The helper text under the model-override dropdown reflects the live
// selection: on Automatic it explains the mapping, and on a manual override
// it states that the chosen model is always used regardless of what Claude
// Code requests. Called from setupSelectInput's onChange plus every place a
// select's value is set programmatically (modal open/reset, language change).
function updateCodexModelHelp(prefix) {
  const help = document.getElementById(`${prefix}_codex_model_help`);
  const select = document.getElementById(`${prefix}_codex_model`);
  if (!help || !select) return;
  const model = select.dataset.value;
  help.innerHTML = model
    ? `${esc(t('modal.add_profile.codex_model_help_manual_prefix'))} <code class="mono">${esc(model)}</code> ${esc(t('modal.add_profile.codex_model_help_manual_suffix'))}`
    : esc(t('modal.add_profile.codex_model_help_auto'));
}

const CODEX_MODEL_OPTIONS = () => [
  { value: '', label: t('modal.add_profile.automatic_option') },
  { value: 'gpt-5.6-sol', label: 'gpt-5.6-sol' },
  { value: 'gpt-5.6-terra', label: 'gpt-5.6-terra' },
  { value: 'gpt-5.6-luna', label: 'gpt-5.6-luna' },
  { value: 'gpt-5.5', label: 'gpt-5.5' },
  { value: 'gpt-5.2', label: 'gpt-5.2' },
];
// A flat union of every model's reasoning-effort levels, not gated by the
// model select above (gpt-5.6-luna tops out at "max", gpt-5.5/gpt-5.2 at
// "xhigh"). Picking a level a model doesn't support falls back through the
// backend's own mapping, like any other automatic default.
const CODEX_REASONING_OPTIONS = () => [
  { value: '', label: t('modal.add_profile.automatic_option') },
  { value: 'low', label: 'low' },
  { value: 'medium', label: 'medium' },
  { value: 'high', label: 'high' },
  { value: 'xhigh', label: 'xhigh' },
  { value: 'max', label: 'max' },
  { value: 'ultra', label: 'ultra' },
];
setupSelectInput(document.getElementById('f_codex_model'), CODEX_MODEL_OPTIONS(), () => updateCodexModelHelp('f'));
setupSelectInput(document.getElementById('f_codex_reasoning'), CODEX_REASONING_OPTIONS(), () => {});
setupSelectInput(document.getElementById('pd_codex_model'), CODEX_MODEL_OPTIONS(), () => updateCodexModelHelp('pd'));
setupSelectInput(document.getElementById('pd_codex_reasoning'), CODEX_REASONING_OPTIONS(), () => {});
updateCodexModelHelp('f');
updateCodexModelHelp('pd');
document.querySelectorAll('.stepper-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const targetId = btn.dataset.step;
    const valEl = document.getElementById(`${targetId}_val`);
    const delta = Number(btn.dataset.delta);
    // min is 1 for every stepper this handler covers (priority, threshold).
    const min = 1;
    const max = targetId === 'f_priority' ? 99 : 100;
    const next = Math.max(min, Math.min(max, Number(valEl.textContent) + delta));
    valEl.textContent = String(next);
  });
});
document.getElementById('f_automatic_toggle').addEventListener('click', (e) => {
  e.currentTarget.classList.toggle('off');
});
document.getElementById('advancedToggle').addEventListener('click', () => {
  document.getElementById('advancedToggle').classList.toggle('open');
  document.getElementById('advancedBody').classList.toggle('open');
});
// Same disclosure pattern as advancedToggle/advancedBody above, collapsed by
// default: the static mapping table is reference material, secondary to the
// codex section's editable fields.
document.getElementById('pdCodexMappingToggle').addEventListener('click', () => {
  document.getElementById('pdCodexMappingToggle').classList.toggle('open');
  document.getElementById('pdCodexMappingBody').classList.toggle('open');
});
wireScrimClickOutside(document.getElementById('scrim'), closeModal);

document.getElementById('exportOpenBtn').addEventListener('click', openExportModal);
document.getElementById('exportCancelBtn').addEventListener('click', closeExportModal);
document.getElementById('exportCancelBtn2').addEventListener('click', closeExportModal);
document.getElementById('exportSaveBtn').addEventListener('click', submitExport);
document.getElementById('exp_profiles_row').addEventListener('click', () => {
  const box = document.getElementById('exp_profiles_box');
  const next = !isChecked(box);
  setCheckbox(box, next);
  box.classList.toggle('warn-on', next); // this one checkbox reads warn/yellow when on — it's the sensitive one
  onExportProfilesChange();
});
document.getElementById('exp_settings_row').addEventListener('click', () => {
  const box = document.getElementById('exp_settings_box');
  setCheckbox(box, !isChecked(box));
});
document.getElementById('exp_activity_row').addEventListener('click', () => {
  const box = document.getElementById('exp_activity_box');
  setCheckbox(box, !isChecked(box));
});
wireScrimClickOutside(document.getElementById('exportScrim'), closeExportModal);

document.getElementById('importOpenBtn').addEventListener('click', openImportModal);
document.getElementById('importCancelBtn').addEventListener('click', closeImportModal);
document.getElementById('importCancelBtn2').addEventListener('click', closeImportModal);
document.getElementById('importPreviewBtn').addEventListener('click', previewImport);
document.getElementById('importApplyBtn').addEventListener('click', applyImport);
document.getElementById('imp_profiles_wrap').addEventListener('click', () => {
  const box = document.getElementById('imp_apply_profiles_box');
  setCheckbox(box, !isChecked(box));
});
document.getElementById('imp_settings_wrap').addEventListener('click', () => {
  const box = document.getElementById('imp_apply_settings_box');
  setCheckbox(box, !isChecked(box));
});
wireScrimClickOutside(document.getElementById('importScrim'), closeImportModal);
document.querySelectorAll('#imp_conflict .seg-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#imp_conflict .seg-btn').forEach((b) => b.classList.toggle('on', b === btn));
  });
});

// Import dropzone: click-to-upload + drag & drop
document.getElementById('imp_dropzone').addEventListener('click', () => document.getElementById('imp_file').click());
document.getElementById('imp_file_change').addEventListener('click', () => document.getElementById('imp_file').click());
document.getElementById('imp_file').addEventListener('change', (e) => {
  if (e.target.files.length) selectImportFile(e.target.files[0]);
});
['dragover', 'dragleave', 'drop'].forEach((evt) => {
  document.getElementById('imp_dropzone').addEventListener(evt, (e) => {
    e.preventDefault();
    document.getElementById('imp_dropzone').classList.toggle('drag-over', evt === 'dragover');
    if (evt === 'drop' && e.dataTransfer.files.length) selectImportFile(e.dataTransfer.files[0]);
  });
});

wireCopyButtons();
renderAddProfileTagRow();
applyTheme(localStorage.getItem('cu-theme') || 'dark');
applyMiniChartStyle('tokens', currentMiniChartStyle('tokens'));
applyMiniChartStyle('model', currentMiniChartStyle('model'));
applyMiniChartStyle('account', currentMiniChartStyle('account'));
applyMetricMode('model', currentMetricMode('model'));
applyMetricMode('project', currentMetricMode('project'));
applyMetricMode('account', currentMetricMode('account'));
wireProfilesViewToggle();
wireUpdateButtons();
wireCursorGlow();
wireDataTooltips();
loadLocales().then(() => {
  loadStatus().then(() => loadProfiles());
  loadProjectUsage();
  loadUsageSummary();
});
loadActivityPreview();
checkConnection();
setInterval(checkConnection, 20000);
