/* ==========================================================================
   Live console — bot control, token price chart, trades.

   The page only ever READS the bot's state file and POSTs control actions; it
   never drives the trading loop itself. The bot runs as its own process so a
   hung websocket cannot take the dashboard down with it.

   Charts are hand-rolled SVG so the mark specs stay exact: 2px lines, >=8px
   markers with a 2px surface ring, hairline solid gridlines, <=24px bars with a
   4px rounded data-end.

   Colour: UP and DOWN are categorical slots 1 and 2 from the validated palette
   (adjacent CVD dE 24.7 light / 26.8 dark). The hue is bound to the OUTCOME,
   never to which line happens to be higher.

   Cadence: the chart polls at 200ms. The bot samples at 200ms and writes the
   state file at 200ms, and the server trims the response to the ONE market
   being drawn (`?chart=`), because every market's full 5-minute history five
   times a second is megabytes for nothing. Tables rebuild only when their own
   data changes, so a text selection is not wiped five times a second.
   ========================================================================== */

'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const WINDOW_S = 300;           // a market is five minutes; the axis is fixed to it

const state = {
  live: null,
  bot: null,
  //: which bot the page shows (its state file and trade log). ?bot= in the URL
  //  wins; else remembered; else the main bot, the one this page can start.
  botId: (() => {
    const q = new URLSearchParams(location.search).get('bot');
    if (q) return q;
    try { return localStorage.getItem('tpb-bot') || 'main'; } catch { return 'main'; }
  })(),
  bots: [],              // /api/bots: every bot with a state file
  page: 'market',        // which tab is open; gates the per-tick work
  selected: null,        // slug shown in the price chart
  timer: null,
  //: earnings card: which calendar period, and whose money. ?period= and
  //  ?scope= make a particular view linkable, the same way ?chart= does.
  earn: (() => {
    const q = new URLSearchParams(location.search);
    const period = q.get('period'), scope = q.get('scope');
    return {
      period: ['5m', 'hour', 'day', 'week', 'month'].includes(period) ? period : 'day',
      scope: ['all', 'paper', 'live'].includes(scope) ? scope : 'all',
      data: null,
    };
  })(),
  //: draw the strike distance on the token chart (as implied P(up)) instead
  //  of in its own panel. ?overlay=1|0 in the URL wins; else remembered.
  overlay: (() => {
    const q = new URLSearchParams(location.search).get('overlay');
    if (q === '1' || q === '0') return q === '1';
    try { return localStorage.getItem('tpb-overlay') === '1'; } catch { return false; }
  })(),
  //: mark where the crypto and token trends turned (▲ / ▼). On by default;
  //  ?turns=1|0 in the URL wins; else remembered.
  turns: (() => {
    const q = new URLSearchParams(location.search).get('turns');
    if (q === '1' || q === '0') return q === '1';
    try { return localStorage.getItem('tpb-turns') !== '0'; } catch { return true; }
  })(),
};

/* The order list beside the market chart (see "the order list" below). Kept
   here, with the page's other state, because the chart reads `hot` on every
   redraw. */
const ORDERS = {
  map: new Map(),        // order id -> the order, with its market's slug
  drawn: new Map(),      // id -> outcome at the last draw, to flash what changed
  seeded: false,         // the recent past has been loaded from the forensics log
  primed: false,         // drawn once after seeding: from now on, changes flash
  hot: null,             // the id under the pointer, in the list or on a chart
  //: 'all' markets, or only the 'market' on the chart. Remembered.
  scope: (() => { try { return localStorage.getItem('tpb-orders-scope') === 'market' ? 'market' : 'all'; } catch { return 'all'; } })(),
};

function applyOverlayLayout() {
  state.overlay = false;                       // the user, 2026-10-05: no strike-distance drawing anywhere
  const on = false;
  $('#overlay-toggle').checked = on;
  $('#strike-panel').hidden = true;            // the distance panel stays hidden whatever the overlay state
  $('#price-panel').classList.toggle('price-col-wide', on);
  $('#price-panel-title').textContent = on ? 'Token price with strike distance' : 'Token price';
}

/* Every clock time on the page is GMT+9 and carries its date, whatever the
   browser's own timezone. GMT+9 has no daylight saving, so shifting the epoch
   and reading the UTC fields is exact -- and far cheaper than Intl in a
   tooltip that re-renders on every mouse move. The server buckets earnings on
   the same clock (DISPLAY_TZ in server.py). */
const TZ_LABEL = 'GMT+9';
const TZ_SHIFT_MS = 9 * 3600 * 1000;
const pad2 = (n) => String(n).padStart(2, '0');
function tzParts(ms) {
  const d = new Date(Number(ms) + TZ_SHIFT_MS);
  return {
    y: d.getUTCFullYear(), mo: pad2(d.getUTCMonth() + 1), d: pad2(d.getUTCDate()),
    h: pad2(d.getUTCHours()), mi: pad2(d.getUTCMinutes()), s: pad2(d.getUTCSeconds()),
    tenth: Math.floor(d.getUTCMilliseconds() / 100),
  };
}
const validTs = (ms) => ms != null && ms !== '' && Number.isFinite(Number(ms));

const fmt = {
  money: (v) => (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 }),
  signed: (v) => (v >= 0 ? '+' : '-') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 }),
  pct: (v) => (v * 100).toFixed(1) + '%',
  num: (v, d = 0) => Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }),
  ms: (v) => Math.round(v) + ' ms',
  cents: (v) => v.toFixed(3),
  // 2026-09-29 21:38:05
  clock: (ms) => {
    if (!validTs(ms)) return '—';
    const p = tzParts(ms);
    return `${p.y}-${p.mo}-${p.d} ${p.h}:${p.mi}:${p.s}`;
  },
  // a chart sample's own time, to the tenth of a second (samples are 200 ms apart)
  clockTenths: (ms) => (validTs(ms) ? `${fmt.clock(ms)}.${tzParts(ms).tenth}` : '—'),
  // 2026-09-29 21:38: a window or a file, where seconds are noise
  stamp: (ms) => {
    if (!validTs(ms)) return '—';
    const p = tzParts(ms);
    return `${p.y}-${p.mo}-${p.d} ${p.h}:${p.mi}`;
  },
  // 09-29 21:38:05.4: a compact row, where the year is the same on every line
  short: (ms) => {
    if (!validTs(ms)) return '—';
    const p = tzParts(ms);
    return `${p.mo}-${p.d} ${p.h}:${p.mi}:${p.s}.${p.tenth}`;
  },
  // 21:38 / 21:38:05: an axis label; the date is in the chart's caption
  hm: (ms) => { const p = tzParts(ms); return `${p.h}:${p.mi}`; },
  hms: (ms) => { const p = tzParts(ms); return `${p.h}:${p.mi}:${p.s}`; },
  // holds here run from under a second to a whole window, so seconds stay
  // seconds rather than becoming an unreadable 0:06
  dur: (s) => (s < 60 ? `${s < 10 ? s.toFixed(1) : Math.round(s)}s`
    : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, '0')}s`),
  bytes: (b) => {
    if (!b) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB'];
    const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
    const v = b / (1024 ** i);
    return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
  },
  mkt: (slug) => (slug || '').replace('-updown-5m-', ' '),
  mmss: (s) => {
    const t = Math.max(0, Math.round(s));
    return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
  },
};

/* ═════════════════════════════════ theme ═════════════════════════════════ */

function initTheme() {
  // ?theme=dark|light wins, the same way ?overlay= does. It makes both themes
  // reachable without touching stored state, which is what a screenshot or a
  // second window comparing the two needs.
  const q = new URLSearchParams(location.search).get('theme');
  const stored = (() => { try { return localStorage.getItem('tpb-theme'); } catch { return null; } })();
  const pick = (q === 'dark' || q === 'light') ? q : stored;
  if (pick === 'dark' || pick === 'light') document.documentElement.dataset.theme = pick;
  themeLabel();
  $('#theme-toggle').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('tpb-theme', next); } catch { /* private mode */ }
    themeLabel();
    render(true);
    refreshTrade();
  });
}
function themeLabel() {
  const dark = (document.documentElement.dataset.theme
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
  $('[data-theme-label]').textContent = dark ? 'Light' : 'Dark';
}

/* ═════════════════════════════════ session ═════════════════════════════════ */

/* The session can lapse while the page is open -- it has an idle timeout, and
   the server forgets every session when it restarts. Each poll and every push
   would then quietly fail, leaving a dashboard that looks alive and shows
   numbers that stop moving. Bouncing to the login page is the honest answer.
   `next` carries the hash so signing back in returns to the page you were on. */
let SIGNED_OUT = false;
function toLogin() {
  if (SIGNED_OUT) return;                 // one redirect, however many calls fail
  SIGNED_OUT = true;
  const back = encodeURIComponent(location.pathname + location.search + location.hash);
  location.href = `/login?next=${back}`;
}

/* Wraps fetch so a 401 from any endpoint lands on the login page instead of
   being parsed as data. Returns null on a lapsed session. */
async function authFetch(url, opts) {
  const r = await fetch(url, opts);
  if (r.status === 401) { toLogin(); return null; }
  return r;
}

function initSession() {
  const btn = $('#btn-logout');
  if (!btn) return;
  fetch('/api/session')
    .then((r) => (r.ok ? r.json() : null))
    .then((s) => { if (s && s.email) btn.title = `Signed in as ${s.email}`; })
    .catch(() => { /* the button still works */ });
  btn.addEventListener('click', async () => {
    try { await fetch('/api/logout', { method: 'POST' }); } catch { /* go anyway */ }
    SIGNED_OUT = true;
    location.href = '/login';
  });
}

/* ══════════════════════════════ bot control ══════════════════════════════ */

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (r.status === 401) { toLogin(); return {}; }
  return r.json();
}

function notice(msg, isError = true) {
  const box = $('#notice');
  if (!msg) { box.hidden = true; return; }
  box.textContent = msg;
  box.hidden = false;
  box.dataset.kind = isError ? 'error' : 'info';
}

function botArgs() {
  return {
    balance: Number($('#bot-balance').value) || 100,
    assets: ($('#bot-assets').value || 'BTC,ETH').trim(),
  };
}

async function control(action) {
  const buttons = ['#btn-start', '#btn-stop', '#btn-restart'];
  buttons.forEach((b) => { $(b).disabled = true; });
  notice('');
  try {
    const res = await post(`/api/bot/${action}`, { ...botArgs(), bot: state.botId });
    if (res.reason) notice(res.reason);
    if (state.botId !== 'main' && typeof res.running === 'boolean') {
      // pm2 answered: show its state now rather than waiting for the state file to go stale
      const info = state.bots.find((x) => x.id === state.botId);
      if (info) { info.running = res.running; info.pm2 = res.app; }
      renderBotStatus(state.bot);
    }
  } catch (e) {
    notice('Could not reach the server: ' + e);
  } finally {
    buttons.forEach((b) => { $(b).disabled = false; });
    poll();
    loadBots();
  }
}

/* Start / Stop / Restart drive the selected bot: the MAIN bot as this server's own child process, any other bot
   through pm2 by its data tag (the user, 2026-10-06). A pm2 bot's balance and assets come from its ecosystem file,
   so those two fields stay off for it. */
function renderBotStatus(b) {
  const badge = $('#bot-badge');
  const other = state.botId !== 'main';
  ['#bot-balance', '#bot-assets'].forEach((sel) => {
    const n = $(sel);
    n.title = other ? `"${state.botId}" takes its balance and assets from its pm2 ecosystem file` : '';
    n.disabled = other;
  });
  if (other) {
    const info = state.bots.find((x) => x.id === state.botId);
    const running = !!(info && info.running);
    badge.dataset.state = running ? 'external' : 'stopped';
    $('#bot-state').textContent = running ? 'running (pm2)' : 'stopped';
    const tip = `pm2 ${info && info.pm2 ? info.pm2 : state.botId}`;
    ['#btn-start', '#btn-stop', '#btn-restart'].forEach((sel) => { $(sel).title = tip; });
    $('#btn-start').disabled = running;
    $('#btn-stop').disabled = !running;
    $('#btn-restart').disabled = !running;
    return;
  }
  ['#btn-start', '#btn-stop', '#btn-restart'].forEach((sel) => { $(sel).title = ''; });
  $('#bot-balance').disabled = false;
  $('#bot-assets').disabled = false;
  const running = b && b.running;
  badge.dataset.state = running ? (b.managed ? 'running' : 'external') : 'stopped';
  $('#bot-state').textContent = !running ? 'stopped'
    : b.managed ? `running · ${Math.round((b.uptime_s || 0) / 60)}m`
    : 'running (external)';
  $('#btn-start').disabled = !!running;
  $('#btn-stop').disabled = !running;
  $('#btn-restart').disabled = !!(running && !b.managed);
  if (b && b.error) notice(b.error);
}

/* ═══════════════════════════════ polling ════════════════════════════════ */

/* Two cadences. The chart wants 200ms data; process status does not change
   five times a second and its endpoint is the one that can block on a
   subprocess handle, so it gets its own slower timer. */

let inFlight = false;

/* The server sends only the points we do not have yet, so the drawn history
   is accumulated here rather than re-downloaded every tick. Without this a
   100 ms poll would pull ~375 KB each time; with it a steady tick is ~1 KB. */
let histCache = { slug: null, pts: [] };
const HISTORY_CAP = 2000;               // matches LiveBot.HISTORY_POINTS

/* Apply one live payload, whichever transport carried it. The websocket and
   the polling fallback send byte-identical messages, so this is the only place
   that understands the shape. */
/* A bot whose state file has not been written for this long is not running;
   its last state is not shown as if it were live. Matches the server. */
const BOT_STALE_S = 90;

/* Charts redraw at most this often (2026-10-05). The state arrives up to four times a second and each redraw
   rebuilds three SVG charts; drawing every frame kept the page busy and made clicks wait their turn. */
const RENDER_MIN_MS = 200;
let renderQueued = false;
let lastRenderAt = 0;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  const wait = Math.max(0, RENDER_MIN_MS - (performance.now() - lastRenderAt));
  setTimeout(() => requestAnimationFrame(() => {
    renderQueued = false;
    lastRenderAt = performance.now();
    render();
  }), wait);
}

/* The last state of each bot seen this session: switching back to a bot draws it at once, before its stream
   catches up. */
const LIVE_CACHE = new Map();
const LIVE_CACHE_MAX = 8;
function cacheLive(bot) {
  if (!state.live) return;
  LIVE_CACHE.delete(bot);
  LIVE_CACHE.set(bot, { live: state.live, hist: histCache });
  if (LIVE_CACHE.size > LIVE_CACHE_MAX) LIVE_CACHE.delete(LIVE_CACHE.keys().next().value);
}

function showLiveBlock(usable) {
  const other = state.botId !== 'main';
  $('#idle').hidden = !!usable || other;
  $('#idle-other').hidden = !!usable || !other;
  $('#live').hidden = !usable;
}

function applyLive(live, slug) {
  // a frame from the stream of a bot the page just switched away from
  if (live && live.bot && live.bot !== state.botId) return;
  const stale = !!(live && live.stale_s != null && live.stale_s > BOT_STALE_S);
  const usable = live && live.running !== false && live.equity != null && !stale;
  if (usable) {
    if (!slug) {
      histCache = { slug: null, pts: [] };
    } else {
      const incoming = (live.price_history || {})[slug] || [];
      // append only when this is a delta for the market we already hold;
      // a switched market or a full resend replaces what we had
      if (histCache.slug === slug && live.history_partial) {
        histCache.pts = histCache.pts.concat(incoming);
        if (histCache.pts.length > HISTORY_CAP) {
          histCache.pts = histCache.pts.slice(-HISTORY_CAP);
        }
      } else {
        histCache = { slug, pts: incoming };
      }
      live.price_history = { [slug]: histCache.pts };
    }
  }
  state.live = usable ? live : null;
  if (usable) absorbOrderMarks(live.order_marks);
  const other = state.botId !== 'main';
  // the main bot's empty state says "press Start"; another bot's cannot
  $('#idle').hidden = !!usable || other;
  $('#idle-other').hidden = !!usable || !other;
  if (!usable && other) {
    $('#idle-other-title').textContent = `"${state.botId}" is not running`;
    $('#idle-other-detail').textContent = stale
      ? `Its last update was ${fmtAge(live.stale_s)} ago.`
      : 'No state file for it yet.';
  }
  $('#live').hidden = !usable;
  if (usable) {
    cacheLive(state.botId);
    scheduleRender();
    // Not inside render(): that only runs while the market page is open, and
    // a window closing while the operator reads the ledger must still be
    // saved. The export draws offscreen, so it needs no visible chart.
    archiveClosedWindows();
  }
}

async function pollLive() {
  if (wsLive && wsLive.readyState === WebSocket.OPEN) return;   // push is driving
  if (inFlight) return;                 // never stack requests on a slow link
  inFlight = true;
  try {
    // only the market being drawn comes back with its history, and only the
    // part of it we are missing; everything else is a count in history_meta
    const slug = state.selected;
    const bot = state.botId;
    const p = new URLSearchParams({ bot });
    if (slug) {
      p.set('chart', slug);
      if (histCache.slug === slug && histCache.pts.length) {
        p.set('since', String(histCache.pts[histCache.pts.length - 1].t));
      }
    }
    const live = await authFetch('/api/live?' + p.toString())
      .then((r) => (r ? r.json() : null)).catch(() => null);
    applyLive(live, slug);
  } catch { /* transient; next tick retries */ } finally {
    inFlight = false;
  }
}

/* ─────────────────────────── the push transport ───────────────────────────
   The bot writes state at 10 Hz and the server pushes each write down this
   socket, so the chart redraws when the data changes rather than when a timer
   happens to fire. Polling stays wired as the fallback: if the socket will not
   open or drops, `pollLive` takes over on the next tick without the page
   noticing, and the payloads are identical either way. */
let wsLive = null;
let wsRetry = 0;

/* patch=1: after its first frame the server sends only the parts of the state that changed (2026-10-05).
   wsBase is the whole state those patches apply to; price_history is never carried over (each frame brings only
   its own new points). */
let wsBase = null;
function mergeLive(msg) {
  if (!msg || !msg._patch) {
    wsBase = msg ? { ...msg } : null;
    return msg;
  }
  if (!wsBase || wsBase.bot !== msg.bot) {
    // a patch with nothing to apply it to: reconnect, which starts with a full frame
    try { if (wsLive) wsLive.close(); } catch { /* reconnects either way */ }
    return null;
  }
  const merged = { ...wsBase };
  (msg._del || []).forEach((k) => { delete merged[k]; });
  Object.entries(msg).forEach(([k, v]) => { if (k !== '_patch' && k !== '_del') merged[k] = v; });
  wsBase = { ...merged };
  merged.price_history = msg.price_history || {};
  return merged;
}

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const slug = state.selected;
  const p = new URLSearchParams({ bot: state.botId, patch: '1' });
  if (slug) p.set('chart', slug);
  return `${proto}//${location.host}/ws?${p.toString()}`;
}

function connectLive() {
  let sock;
  try {
    sock = new WebSocket(wsUrl());
  } catch {
    return;                             // polling is already running
  }
  wsLive = sock;
  wsBase = null;
  sock.onopen = () => { wsRetry = 0; };
  sock.onmessage = (ev) => {
    if (wsLive !== sock) return;                 // an old socket's last frames
    try {
      const live = mergeLive(JSON.parse(ev.data));
      if (live) applyLive(live, state.selected);
    } catch { /* one bad frame must not stop the stream */ }
  };
  sock.onclose = () => {
    if (wsLive === sock) wsLive = null;
    // back off to a few seconds so a server restart does not get hammered;
    // pollLive is serving the page in the meantime
    wsRetry = Math.min(wsRetry + 1, 5);
    setTimeout(connectLive, 500 * wsRetry);
  };
  sock.onerror = () => { try { sock.close(); } catch { /* already gone */ } };
}

/* Tell the socket which market to stream. Cheap enough to call on every
   selection change; no reconnect, and the server answers with a full history
   for the new market rather than a delta against the old one's timestamps. */
function setSelected(slug) {
  if (state.selected === slug) return;
  state.selected = slug;
  histCache = { slug: null, pts: [] };   // the new market shares no points
  sendChartSelection();
}

function sendChartSelection() {
  if (wsLive && wsLive.readyState === WebSocket.OPEN) {
    try {
      wsLive.send(JSON.stringify({ chart: state.selected || null }));
    } catch { /* the close handler will reconnect */ }
  }
}

/* ───────────────────── real Polymarket account (read only) ─────────────────
   A separate panel on purpose. It reports the venue's own numbers for one
   wallet, fetched from the public Data API; it shares nothing with the paper
   bot above and can place no orders. Refreshed on a slow timer because the
   endpoint is public and free -- the server caches it too. */
const ACCOUNT_POLL_MS = 15000;
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function renderAccount(a) {
  const card = $('#account-card');
  if (!a || a.configured === false) { card.hidden = true; return; }
  card.hidden = false;

  const stale = a.age_s != null && a.age_s > 60;
  $('#account-age').textContent = a.error ? `stale · ${esc(a.error)}`
    : (a.age_s == null ? '' : `updated ${a.age_s.toFixed(0)}s ago`);
  $('#account-age').className = 'badge';

  const pos = a.positions || [];
  const kpis = [
    { l: 'Portfolio value', v: a.value == null ? '—' : fmt.money(a.value),
      n: a.value_error ? esc(a.value_error) : `wallet ${esc((a.wallet || '').slice(0, 10))}…` },
    { l: 'Open positions', v: fmt.num(pos.length),
      n: a.positions_error ? esc(a.positions_error) : `${fmt.money(a.positions_value || 0)} at market` },
    { l: 'Unrealised', v: fmt.signed(a.unrealized_pnl || 0), n: 'on open positions' },
    { l: 'Trades', v: fmt.num((a.trades || []).length),
      n: a.trades_error ? esc(a.trades_error) : 'most recent first' },
  ];
  $('#account-kpis').innerHTML = kpis.map((k) => `
    <div class="kpi"><span class="kpi-label">${k.l}</span>
    <span class="kpi-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');

  const pcols = ['Market', 'Outcome', 'Shares', 'Avg', 'Now', 'Value', 'Unrealised'];
  $('#account-positions').innerHTML = pos.length
    ? '<thead><tr>' + pcols.map((h) => `<th scope="col">${h}</th>`).join('') + '</tr></thead><tbody>'
      + pos.map((p) => `<tr>
          <td>${esc(p.title)}</td><td>${esc(p.outcome)}</td>
          <td>${fmt.num(p.size, 2)}</td>
          <td>${p.avg_price == null ? '—' : p.avg_price.toFixed(3)}</td>
          <td>${p.current_price == null ? '—' : p.current_price.toFixed(3)}</td>
          <td>${fmt.money(p.value || 0)}</td>
          <td class="${(p.unrealized_pnl || 0) >= 0 ? 'pos' : 'neg'}">${fmt.signed(p.unrealized_pnl || 0)}</td>
        </tr>`).join('') + '</tbody>'
    : '<tbody><tr><td class="live-empty">No open positions.</td></tr></tbody>';

  const tcols = ['Time', 'Side', 'Market', 'Outcome', 'Shares', 'Price', 'Notional'];
  const trades = (a.trades || []).slice(0, 50);
  $('#account-trades').innerHTML = trades.length
    ? '<thead><tr>' + tcols.map((h) => `<th scope="col">${h}</th>`).join('') + '</tr></thead><tbody>'
      + trades.map((t) => `<tr>
          <td>${fmt.clock((t.ts || 0) * 1000)}</td>
          <td class="${t.side === 'BUY' ? 'pos' : 'neg'}">${esc(t.side)}</td>
          <td>${esc(t.title)}</td><td>${esc(t.outcome)}</td>
          <td>${fmt.num(t.size, 2)}</td><td>${(t.price || 0).toFixed(3)}</td>
          <td>${fmt.money(t.usdc || 0)}</td>
        </tr>`).join('') + '</tbody>'
    : '<tbody><tr><td class="live-empty">No trades on this wallet yet.</td></tr></tbody>';
}

async function pollAccount() {
  if (state.page !== 'ledger') return;
  try {
    const a = await fetch('/api/account').then((r) => r.json()).catch(() => null);
    renderAccount(a);
  } catch { /* the panel keeps its last render */ }
}

async function pollBot() {
  try {
    const bot = await authFetch('/api/bot')
      .then((r) => (r ? r.json() : null)).catch(() => null);
    state.bot = bot;
    renderBotStatus(bot);
  } catch { /* transient */ }
}

async function poll() {
  await Promise.all([pollBot(), pollLive()]);
}

/* ─────────────────────────────── bot picker ───────────────────────────────
   Every bot process writes its own state file; /api/bots lists them. The
   picker shows which are running, their mode and strategy, and marks the ones
   trading real money. Switching reconnects the stream to the other bot's file;
   nothing is sent to any bot. */
const BOTS_POLL_MS = 5000;

function fmtAge(s) {
  if (s == null) return '?';
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

function botLabel(b) {
  const real = ['micro-live', 'live'].includes(b.deploy_mode) || String(b.mode || '').startsWith('LIVE');
  const parts = [b.id];
  if (b.deploy_mode || b.mode) parts.push(b.deploy_mode || b.mode);
  if (b.strategy) parts.push(b.strategy + (b.event_driven ? ' (event)' : ''));
  if (real) parts.push('REAL MONEY');
  return `${b.running ? '●' : '○'} ${parts.join(' · ')}${b.running ? '' : ` · stopped ${fmtAge(b.stale_s)} ago`}`;
}

function renderBotPicker() {
  const sel = $('#bot-select');
  const bots = state.bots.slice();
  // the selected bot stays listed even before its file exists
  if (!bots.some((b) => b.id === state.botId)) bots.push({ id: state.botId, running: false, stale_s: null });
  const html = bots.map((b) =>
    `<option value="${esc(b.id)}"${b.id === state.botId ? ' selected' : ''}>${esc(botLabel(b))}</option>`).join('');
  if (sel.dataset.html !== html) {           // do not reset an open dropdown for nothing
    sel.innerHTML = html;
    sel.dataset.html = html;
  }
}

async function loadBots() {
  try {
    const d = await authFetch('/api/bots').then((r) => (r ? r.json() : null)).catch(() => null);
    if (d && Array.isArray(d.bots)) {
      state.bots = d.bots;
      renderBotPicker();
      renderBotStatus(state.bot);
    }
  } catch { /* the picker keeps its last list */ }
}

const ORDERS_CACHE = new Map();               // bot -> its order map, for an instant switch back

function switchBot(id) {
  if (!id || id === state.botId) return;
  ORDERS_CACHE.set(state.botId, ORDERS.map);
  state.botId = id;
  try { localStorage.setItem('tpb-bot', id); } catch { /* private mode */ }
  try {
    const u = new URL(location.href);
    if (id === 'main') u.searchParams.delete('bot'); else u.searchParams.set('bot', id);
    history.replaceState(null, '', u);
  } catch { /* the choice still applies to this page */ }
  // nothing of the old bot's may survive: its history, its drawn state, its
  // stream, and a trade of its that is open in the dialog
  closeTrade();
  resetOrders();
  histCache = { slug: null, pts: [] };
  state.live = null;
  historyPage = 1;
  // what this page last saw of the new bot, drawn now; its stream replaces it within a moment
  const cached = LIVE_CACHE.get(id);
  if (cached) {
    histCache = cached.hist;
    state.live = cached.live;
    if (cached.hist && cached.hist.slug) state.selected = cached.hist.slug;
  }
  showLiveBlock(!!state.live);
  if (state.page === 'market') { render(true); renderOrderList(true); }
  if (wsLive) {
    const old = wsLive;
    wsLive = null;
    old.onclose = null;
    try { old.close(); } catch { /* already gone */ }
  }
  connectLive();
  renderBotPicker();
  renderBotStatus(state.bot);
  pollLive();
  pollHistory();                     // the ledger page draws it; any other page keeps it warm for later
}

/* ══════════════════════════════ svg helpers ═════════════════════════════ */

const NS = 'http://www.w3.org/2000/svg';
const el = (tag, attrs = {}) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};
/* On-screen charts are drawn this many times their original height (2x on
   2026-09-24, then 0.7 of that). Exported PNGs (forExport) keep the original
   size, so the images already in the Saved markets archive and the new ones
   stay the same shape. */
const CHART_SCALE = 1.4;
/* The three market panels (token price, distance from strike, crypto price)
   at 0.7 of that since 2026-09-29, so they sit beside the order list without
   a long scroll. Exports are unaffected (fixed 300 px). */
const MARKET_CHART_SCALE = CHART_SCALE * 0.7 * 0.7;     // 2026-10-05 the user: 0.7x again (the charts sit right of the figures)

function mount(host, height) {
  host.innerHTML = '';
  const w = Math.max(host.clientWidth || 600, 260);
  const svg = el('svg', { width: w, height, viewBox: `0 0 ${w} ${height}`, role: 'img' });
  host.appendChild(svg);
  return { svg, w, h: height };
}
function barPath(x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, w, h / 2));
  return `M${x},${y} H${x + w - rr} A${rr},${rr} 0 0 1 ${x + w},${y + rr}`
       + ` V${y + h - rr} A${rr},${rr} 0 0 1 ${x + w - rr},${y + h} H${x} Z`;
}

const tip = $('#tooltip');
function showTip(html, ev) {
  tip.innerHTML = html;
  tip.hidden = false;
  const pad = 14;
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - pad;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}
const hideTip = () => { tip.hidden = true; };
const tipRows = (title, rows) =>
  `<div class="tt-title">${title}</div>` + rows.map(([k, v, c]) => `
    <div class="tt-row"><span class="tt-key">${
      c ? `<span class="tt-swatch" style="background:${c}"></span>` : ''}${k}</span>
    <span class="tt-val">${v}</span></div>`).join('');

function legend(host, items) {
  host.innerHTML = '';
  items.forEach((it) => {
    const s = document.createElement('span');
    s.className = 'legend-item';
    s.innerHTML = it.glyph
      ? `<span class="legend-glyph" style="color:${it.color}">${it.glyph}</span>${it.label}`
      : `<span class="legend-key" style="background:${it.color}"></span>${it.label}`;
    host.appendChild(s);
  });
}

/* Round tick values INSIDE a range: a step of 1, 2, 2.5 or 5 x 10^k, so an
   axis reads 83,600 / 83,610 / 83,620 rather than 83,607.4 / 83,613.9 / ...
   (niceTicks, for the earnings chart, instead widens the range to round
   bounds.) */
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
function roundTicks(lo, hi, target = 5) {
  if (!(hi > lo)) return { ticks: [lo], step: 1, decimals: 2 };
  const step = niceStep((hi - lo) / target);
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) ticks.push(Number(v.toFixed(10)));
  let decimals = 0;
  while (decimals < 8 && Math.abs(step * 10 ** decimals - Math.round(step * 10 ** decimals)) > 1e-6) decimals += 1;
  return { ticks, step, decimals };
}

/* The y-axis unit, above the top-left corner of the plot: a tick value
   without its unit is half a label. Needs ~24 px of top margin. */
function axisCaption(svg, m, text) {
  const t = el('text', { class: 'axis-caption', x: m.l, y: m.t - 10 });
  t.textContent = text;
  svg.appendChild(t);
}

/* ─────────────────────── one pointer, three panels ───────────────────────
   The market panels share one time axis, so they share one pointer: hovering
   any of them puts a crosshair and a value dot on all of them at the same
   instant, and every panel's readout switches from "now" to that instant.
   The panels re-render several times a second, so the hovered time lives
   here, outside any one render, and each fresh render restores it. */
const LINK = { panels: new Map(), t: null };

function linkPanel(name, api) {
  LINK.panels.set(name, api);
  const p = LINK.t != null ? api.at(LINK.t) : null;
  if (p) api.show(p); else api.clear();
}
function linkHover(t) {
  LINK.t = t;
  LINK.panels.forEach((api) => { const p = api.at(t); if (p) api.show(p); else api.clear(); });
}
function linkLeave() {
  LINK.t = null;
  LINK.panels.forEach((api) => api.clear());
}

/* the point nearest a timestamp (points are in time order) */
function nearestByT(pts, t) {
  if (!pts.length) return null;
  let lo = 0, hi = pts.length - 1;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (pts[mid].t < t) lo = mid + 1; else hi = mid; }
  const a = pts[Math.max(0, lo - 1)], b = pts[lo];
  const best = Math.abs(a.t - t) <= Math.abs(b.t - t) ? a : b;
  return Math.abs(best.t - t) <= 3000 ? best : null;
}

/* A crosshair plus one hidden dot per series, positioned by show(p). */
function hoverLayer(svg, m, ph, series) {
  const cross = el('line', { class: 'crosshair', y1: m.t, y2: m.t + ph, opacity: 0 });
  svg.appendChild(cross);
  const dots = series.map((sr) => {
    const c = el('circle', { r: 4, fill: sr.color, stroke: css('--surface-1'), 'stroke-width': 2, opacity: 0 });
    c.style.pointerEvents = 'none';
    svg.appendChild(c);
    return [sr, c];
  });
  return {
    show(x, p) {
      cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('opacity', 1);
      dots.forEach(([sr, c]) => {
        const y = sr.y(p);
        if (y == null || !Number.isFinite(y)) { c.setAttribute('opacity', 0); return; }
        c.setAttribute('cx', x); c.setAttribute('cy', y); c.setAttribute('opacity', 1);
      });
    },
    clear() {
      cross.setAttribute('opacity', 0);
      dots.forEach(([, c]) => c.setAttribute('opacity', 0));
    },
  };
}

/* The panel's readout line: the values at the pointer, or the latest when
   nothing is hovered. Values lead (strong), names follow; a short line in the
   series colour carries identity, never the text colour. Only numbers we
   formatted and fixed labels go in, so innerHTML is safe here. */
function readout(host, when, items) {
  if (!host) return;
  host.innerHTML = `<span class="ro-when">${when}</span>` + items.map((it) =>
    `<span class="ro-item">${it.color ? `<span class="ro-key" style="background:${it.color}"></span>` : ''}`
    + `<strong>${it.value}</strong>${it.label ? ` <span class="ro-label">${it.label}</span>` : ''}</span>`).join('');
}
const roWhen = (p, live) => (live ? 'now' : `${fmt.mmss(p.left || 0)} left`);
const signed = (v, d = 1) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(d)}`;

/* ═══════════════════════════ the fixed 5m axis ══════════════════════════ */

/* x is elapsed time into the window, not sample index. Mapping x to the
   sample index made every new point recompress the whole line leftward; a
   fixed 0..300s domain lets the line grow rightward and stay put. Points
   before the open are not drawn; post-close samples (left clamps to 0) sit at
   the right edge. */

const elapsedOf = (p) => WINDOW_S - Math.max(0, Math.min(WINDOW_S, p.left));
/* "14:05:23.4 · 1:37 left": a chart point's clock time and where it sits in the window */
const tipTime = (p) => (p && p.t ? `${fmt.clockTenths(p.t)} · ` : '') + `${fmt.mmss((p && p.left) || 0)} left`;
const inWindow = (p) => p.left <= WINDOW_S;

/* The window's time axis, in time LEFT: a hairline gridline every 30 s
   (stronger on the minute) and a small tick every 10 s, labelled every 30 s
   on one row -- every minute on a chart too narrow for that. It used to draw
   a gridline every 5 s (59 lines behind the data) and stagger its labels over
   two rows: more ink than the series it framed. Minute labels are bold. */
const AXIS_GRID_S = 30;
const AXIS_TICK_S = 10;
const AXIS_LABEL_PX = 36;
function fixedAxis(svg, m, pw, H) {
  const X = (p) => m.l + (elapsedOf(p) / WINDOW_S) * pw;
  const Xe = (elapsed) => m.l + (elapsed / WINDOW_S) * pw;
  const base = H - m.b;
  for (let s = AXIS_GRID_S; s < WINDOW_S; s += AXIS_GRID_S) {
    svg.appendChild(el('line', { class: s % 60 === 0 ? 'grid-line' : 'grid-line-minor',
      x1: Xe(s), x2: Xe(s), y1: m.t, y2: base }));
  }
  svg.appendChild(el('line', { class: 'axis-line', x1: m.l, x2: m.l + pw, y1: base, y2: base }));
  const step = [30, 60, 150].find((st) => pw / (WINDOW_S / st) >= AXIS_LABEL_PX) || 150;
  for (let s = 0; s <= WINDOW_S; s += AXIS_TICK_S) {
    const labelled = s % step === 0;
    svg.appendChild(el('line', { class: 'axis-line', x1: Xe(s), x2: Xe(s), y1: base, y2: base + (labelled ? 5 : 3) }));
    if (!labelled) continue;
    const lab = el('text', {
      class: s % 60 === 0 ? 'axis-text axis-text-major' : 'axis-text',
      x: Xe(s), y: base + 18,
      'text-anchor': s === 0 ? 'start' : s === WINDOW_S ? 'end' : 'middle',
    });
    lab.textContent = fmt.mmss(WINDOW_S - s);
    svg.appendChild(lab);
  }
  return { X, Xe };
}

/* The bid-ask band of one token: the prices a buyer and a seller actually
   get, as a wash around the mid. Drawn in runs, so a stretch with no bid or
   no ask (a one-sided book) is a gap, never a bridge. */
function spreadBand(svg, pts, X, Y, bidKey, askKey, color) {
  let run = [];
  const flush = () => {
    if (run.length >= 2) {
      const top = run.map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(p[askKey])}`).join(' ');
      const bot = run.slice().reverse().map((p) => `L${X(p)},${Y(p[bidKey])}`).join(' ');
      svg.appendChild(el('path', { class: 'spread-band', d: `${top} ${bot} Z`, fill: color }));
    }
    run = [];
  };
  pts.forEach((p) => {
    if (typeof p[bidKey] === 'number' && typeof p[askKey] === 'number' && p[askKey] >= p[bidKey]) run.push(p);
    else flush();
  });
  flush();
}

/* nearest drawn point to a mouse x, by elapsed time */
function nearestByX(pts, ev, svg, m, pw) {
  const bx = svg.getBoundingClientRect();
  const elapsed = Math.max(0, Math.min(WINDOW_S, ((ev.clientX - bx.left - m.l) / (pw || 1)) * WINDOW_S));
  let best = null, bd = Infinity;
  for (const p of pts) {
    const d = Math.abs(elapsedOf(p) - elapsed);
    if (d < bd) { bd = d; best = p; }
  }
  return best;
}

/* ═══════════════════════ trend turning points ═══════════════════════ */

/* Where a price path changed direction: a ZigZag. A turn is CONFIRMED once
   the price has come back at least `thr` from the extreme reached since the
   last turn, and it is drawn AT that extreme -- the peak or trough, where the
   trend actually changed. The newest leg is never marked until it too is
   confirmed, so a live chart never shows a turn the data has not proven.
   `conf` is the sample at which it became knowable: the gap between the two
   is the lag any "sell when the trend turns" rule has to live with. */
function trendTurns(pts, valueOf, thr) {
  const out = [];
  let dir = 0, hi = null, lo = null, hiP = null, loP = null, start = null, lastExt = null;
  for (const p of pts) {
    const v = valueOf(p);
    if (v == null || !Number.isFinite(v)) continue;
    if (hi === null) { hi = lo = start = lastExt = v; hiP = loP = p; continue; }
    if (v > hi) { hi = v; hiP = p; }
    if (v < lo) { lo = v; loP = p; }
    if (dir >= 0 && hi - v >= thr) {
      // a real up-leg must precede a peak; falling from the very start is
      // just a downtrend being established, not a turn
      if (dir > 0 || hi - start >= thr) out.push({ at: hiP, value: hi, dir: 'down', conf: p, confValue: v, leg: hi - lastExt });
      lastExt = hi; dir = -1; lo = v; loP = p;
    } else if (dir <= 0 && v - lo >= thr) {
      if (dir < 0 || start - lo >= thr) out.push({ at: loP, value: lo, dir: 'up', conf: p, confValue: v, leg: lo - lastExt });
      lastExt = lo; dir = 1; hi = v; hiP = p;
    }
  }
  return out;
}

/* Per-second volatility of the spot path itself, in bps, from ~1 s steps --
   so a threshold means the same thing on BTC and on DOGE, and on a saved
   window as on a live one. */
function pathSigmaBps(pts) {
  let prev = null, acc = 0, n = 0;
  for (const p of pts) {
    if (p.spot == null || !(p.spot > 0)) continue;
    if (prev && p.t - prev.t >= 1000) {
      const dt = (p.t - prev.t) / 1000, r = Math.log(p.spot / prev.spot);
      acc += (r * r) / dt; n += 1;
      prev = p;
    } else if (!prev) prev = p;
  }
  return n >= 10 ? Math.sqrt(acc / n) * 1e4 : null;
}

/* Confirmation lag and marker COUNT trade off directly, because the ZigZag
   only confirms a turn once the price has retraced past the threshold -- a
   smaller threshold confirms sooner but fires on more of the market's own
   noise. Measured on 150 archived windows, no smoothing (smoothing itself
   adds lag; see the crypto side below for that setting):

       thr 2c  (too many, 2026-09-23):  78.8 turns/window, lag median 0.7s p90  2.6s
       thr 8c  (now):                   16.4 turns/window, lag median 2.5s p90  9.6s
       thr 5c smoothed 3s (original):   11.0 turns/window, lag median 6.5s p90 12.5s

   8c keeps roughly the same COUNT of turns as the original setting -- one
   per real swing, not one per tick -- while confirming each one about 2.5x
   faster, because dropping the smoothing (which itself costs lag: the
   smoothed series has to move by the threshold too, and it moves less per
   raw tick) recovers speed the wider threshold alone gives back up. */
const TOKEN_TURN = 0.08;

/* the Up token's turns on the chart's own points (post-close samples excluded),
   on the raw price -- no smoothing, which is itself a source of lag */
function tokenTurns(pts) {
  const live = pts.filter((p) => typeof p.up === 'number' && p.left > 0);
  return trendTurns(live, (p) => p.up, TOKEN_TURN);
}
function spotTurnBps(pts, slug) {
  const asset = (slug || '').split('-')[0].toUpperCase();
  const live = ((state.live && state.live.sigma_bps_per_sec) || {})[asset];
  const sigma = pathSigmaBps(pts) || live || 1;
  // measured on 150 archived windows: 0.5 sigma (median lag 0.9s, p90 3.0s,
  // 75.7 turns/window) was still slower than it needs to be; 0.15 sigma
  // (now) confirms in median 0.47s / p90 1.5s, ~2x as many turns (156.7/window).
  // Below this the lag stops improving (0.1 sigma: 0.45s median) because it
  // is bumping into the ~200-250ms sampling interval itself -- there is
  // nothing faster left to buy by lowering the threshold further.
  return Math.max(0.3, 0.15 * sigma);
}

/* Draw each turn where it became KNOWN: the triangle sits on the sample at
   which the price had retraced enough to confirm the change of direction --
   the earliest moment a rule could act on it. A small hollow dot marks the
   peak or trough it refers to, joined by a thin line, so the lag between the
   real turn and the moment it was knowable is visible on the chart. ▲ = the
   trend turned up, ▼ = turned down, in the Up/Down hues. Circles are the
   strategy's decisions and diamonds its fills, so the three never read as
   one another. */
function drawTurns(svg, turns, X, Yof, label, fmtVal, fmtLeg) {
  const cUp = css('--series-1'), cDown = css('--series-2');
  turns.forEach((tn) => {
    const up = tn.dir === 'up';
    const colour = up ? cUp : cDown;
    const px = X(tn.at), py = Yof(tn.at);             // the peak / trough
    const cx = X(tn.conf), cy = Yof(tn.conf);         // where it became known
    svg.appendChild(el('line', { x1: px, y1: py, x2: cx, y2: cy, stroke: colour,
                                  'stroke-width': 1, 'stroke-dasharray': '2 3', opacity: 0.7 }));
    svg.appendChild(el('circle', { cx: px, cy: py, r: 3, fill: 'none', stroke: colour, 'stroke-width': 1.5 }));
    const s = 6, ty = cy + (up ? 10 : -10);
    const d = up ? `M${cx},${ty - s} L${cx + s},${ty + s * 0.8} L${cx - s},${ty + s * 0.8} Z`
      : `M${cx},${ty + s} L${cx + s},${ty - s * 0.8} L${cx - s},${ty - s * 0.8} Z`;
    svg.appendChild(el('path', { d, fill: colour, stroke: css('--surface-1'), 'stroke-width': 1 }));
    const hit = el('circle', { class: 'hit', cx, cy: ty, r: 9 });
    svg.appendChild(hit);
    const lagS = Math.max(0, (tn.conf.t - tn.at.t) / 1000);
    hit.addEventListener('mousemove', (ev) => showTip(tipRows(`${label} trend turned ${up ? 'UP' : 'DOWN'}`, [
      ['Known at', `${fmtVal(tn.confValue)} · ${tipTime(tn.conf)}`, colour],
      [up ? 'Trough was' : 'Peak was', `${fmtVal(tn.value)} · ${tipTime(tn.at)}`],
      ['Lag to confirm', `${lagS.toFixed(1)} s`],
      ['Previous leg', fmtLeg(tn.leg)],
    ]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });
}

/* The bot's OWN detections (2026-10-01): a filled triangle at the sample where the spot-lag
   engine saw a sudden move -- ▲ up, ▼ down -- bright when it became a signal, hollow with the
   reason when a gate skipped it. Unlike the trend turns above (hindsight, confirmed after the
   retrace) these are exactly the moments the algorithm acted or could have acted. */
function drawShocks(svg, marks, pts, X, Yof) {
  const cUp = css('--series-1'), cDown = css('--series-2');
  (marks || []).forEach((k) => {
    let at = null, best = Infinity;
    pts.forEach((p) => { const d = Math.abs(p.t - k.t); if (d < best) { best = d; at = p; } });
    if (!at || best > 3000) return;
    const up = k.dir > 0, colour = up ? cUp : cDown;
    const cx = X(at), cy = Yof(at), s = 7, ty = cy + (up ? 14 : -14);
    const d = up ? `M${cx},${ty - s} L${cx + s},${ty + s * 0.8} L${cx - s},${ty + s * 0.8} Z`
      : `M${cx},${ty + s} L${cx + s},${ty - s * 0.8} L${cx - s},${ty - s * 0.8} Z`;
    svg.appendChild(el('path', { d, fill: k.ok ? colour : 'none', stroke: colour, 'stroke-width': 1.5,
                                  opacity: k.ok ? 0.95 : 0.6 }));
    const hit = el('circle', { class: 'hit', cx, cy: ty, r: 9 });
    svg.appendChild(hit);
    if (k.trigger === 'high_zone' && k.hz) {
      const h = k.hz;
      hit.addEventListener('mousemove', (ev) => showTip(tipRows(`high zone ${up ? 'UP' : 'DOWN'} · ${k.ok ? 'BOUGHT' : 'not bought'}`, [
        ['At', tipTime(at), colour],
        ['Token ask', `${Number(h.ask).toFixed(2)} (was ${Number(h.ask_then).toFixed(2)} ${h.lookback_s} s ago)`],
        ['Rule', 'still rising over the high-zone price: 5 shares every 5 s'],
        [k.ok ? 'Result' : 'Why not bought', k.ok ? 'bought' : (k.why || '—')],
      ]), ev));
      hit.addEventListener('mouseleave', hideTip);
      return;
    }
    const fl = k.trigger === 'flow' && k.flow ? k.flow : null;
    const sg = (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(2)}`);
    if (fl) {
      // a cross-exchange flow signal: the score and its four parts, who moved first, the moves in bp
      const mv = Object.entries(fl.moves_bp || {}).map(([x, v]) => `${x} ${v >= 0 ? '+' : ''}${v}`).join(' · ');
      hit.addEventListener('mousemove', (ev) => showTip(tipRows(`flow ${up ? 'UP' : 'DOWN'} · ${k.ok ? 'SIGNAL' : 'not bought'}`, [
        ['At', tipTime(at), colour],
        ['Score', `${sg(fl.score)} (needs ±0.70)`],
        ['Momentum', `${sg(fl.cx)} · ${fl.confirm}/${fl.fresh} exchanges confirm`],
        ['Trade flow', sg(fl.flow)],
        ['Acceleration', sg(fl.acc)],
        ['First mover', `${fl.first_mover || '—'} (${sg(fl.first)})`],
        ['Moves, bp', mv || '—'],
        [k.ok ? 'Result' : 'Why not bought', k.ok ? 'bought' : (k.why || '—')],
      ]), ev));
      hit.addEventListener('mouseleave', hideTip);
      return;
    }
    hit.addEventListener('mousemove', (ev) => showTip(tipRows(`sudden ${up ? 'rise' : 'fall'} detected · ${k.ok ? 'SIGNAL' : 'skipped'}`, [
      ['At', tipTime(at), colour],
      ['1 s move', `${k.r1_bps == null ? '—' : (k.r1_bps >= 0 ? '+' : '') + Number(k.r1_bps).toFixed(1) + ' bps'} (${k.z1 == null ? '—' : Number(k.z1).toFixed(2)} σ)`],
      ['3 s', k.z3 == null ? '—' : `${Number(k.z3).toFixed(2)} σ`],
      ['Trend', k.trend_bps == null ? '—' : `${k.trend_bps >= 0 ? '+' : ''}${Number(k.trend_bps).toFixed(1)} bps`],
      ['Trigger', k.trigger || 'sigma'],
      [k.ok ? 'Result' : 'Why skipped', k.ok ? 'passed the signal gates' : (k.why || '—')],
    ]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });
}

function shocksLegend(items, marks) {
  if (!(marks || []).length) return;
  items.push({ label: '▲▼ direction signal (filled = bought, hollow = not bought; hover for the reason)', color: css('--text-secondary') });
}

function turnsLegend(items, label) {
  items.push({ label: `▲ ${label} turned up`, color: css('--series-1') },
             { label: `▼ ${label} turned down`, color: css('--series-2') });
}

/* ════════════════════════════ price chart ═══════════════════════════════ */

/* Only the CURRENT and NEXT window per asset. From the bot's own
   exchange-aligned clock: 0 < secs_left <= 300 is the window running now,
   300 < secs_left <= 600 is the one after; a closed window has 0 and drops
   off the picker rather than lingering until it is reaped. */
function pickerMarkets() {
  return (state.live.live_markets || [])
    .filter((m) => m.secs_left > 0 && m.secs_left <= 2 * WINDOW_S)
    .sort((a, b) => ((a.secs_left > WINDOW_S) - (b.secs_left > WINDOW_S))
                    || a.slug.localeCompare(b.slug));
}

let pickerSig = '';

function renderPicker() {
  const host = $('#market-picker');
  const markets = pickerMarkets();
  if (!markets.length) { host.innerHTML = ''; pickerSig = ''; return; }
  const meta = state.live.history_meta || {};
  const held = new Set(markets.filter((m) => m.up_shares > 0 || m.down_shares > 0).map((m) => m.slug));
  const slugs = new Set(markets.map((m) => m.slug));

  if (state.selected && !slugs.has(state.selected)) setSelected(null);

  // Default to something worth looking at: a market we hold, else the struck
  // current window closest to resolving, else a current window, else next.
  if (!state.selected) {
    const rank = (m) => {
      if (held.has(m.slug)) return [0, m.secs_left];
      if (m.strike > 0 && m.secs_left <= WINDOW_S) return [1, m.secs_left];
      if (m.secs_left <= WINDOW_S) return [2, m.secs_left];
      return [3, m.secs_left];
    };
    setSelected(markets.slice().sort((a, b) => {
      const ra = rank(a), rb = rank(b);
      return ra[0] - rb[0] || ra[1] - rb[1];
    })[0].slug);
  }

  // rebuilding eight buttons five times a second is pointless and eats clicks
  const sig = JSON.stringify(markets.map((m) => [m.slug, m.secs_left > WINDOW_S, held.has(m.slug), (meta[m.slug] || 0) >= 2]))
    + '|' + state.selected;
  if (sig === pickerSig) return;
  pickerSig = sig;

  host.innerHTML = '';
  markets.forEach((m) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chip';
    b.dataset.on = m.slug === state.selected ? '1' : '0';
    b.innerHTML = `${fmt.mkt(m.slug)}`
      + (m.secs_left > WINDOW_S ? '<span class="pending">next</span>' : '')
      + (held.has(m.slug) ? '<span class="held">held</span>' : '');
    b.addEventListener('click', () => { setSelected(m.slug); pollLive(); });
    host.appendChild(b);
  });
}

/* The selected market's position (the user, 2026-10-09): what the bot holds of each token, what it paid (fees in,
   net of any sells), what that is worth at the bid, and the market's P&L now and if UP / DOWN wins
   (live.py _market_position). Rebuilt only when those figures move. */
function renderPosition() {
  const host = $('#market-position');
  const slug = state.selected;
  const pos = slug ? (state.live.market_positions || {})[slug] : null;
  if (!changed('position', JSON.stringify([slug, pos || null]))) return;
  // a bot still on the code from before this panel sends no positions at all: nothing to show (not "none held")
  host.hidden = !slug || !state.live.market_positions;
  if (host.hidden) return;
  if (!pos) {
    host.innerHTML = `<p class="mkt-pos-empty">No tokens held in ${fmt.mkt(slug)}.</p>`;
    return;
  }
  const usd = (v) => (v == null ? '—' : '$' + fmt.num(v, 2));
  const pnl = (v) => (v == null ? '<span class="dim">—</span>' : `<span class="${v >= 0 ? 'pos' : 'neg'}">${fmt.signed(v)}</span>`);
  const sh = (v) => (v == null ? '—' : fmt.num(v, 2));
  const px = (v, d = 3) => (v == null ? '—' : Number(v).toFixed(d));
  const tiles = [
    { l: 'P&L now', v: pnl(pos.pnl_now), n: 'if every token were sold at the bid' },
    { l: 'If UP wins', v: pnl(pos.pnl_up_win), n: `${sh(pos.up.shares)} UP pay ${usd(pos.up.shares)}` },
    { l: 'If DOWN wins', v: pnl(pos.pnl_down_win), n: `${sh(pos.down.shares)} DOWN pay ${usd(pos.down.shares)}` },
    { l: 'Total cost', v: usd(pos.cost), n: `fees ${usd((pos.up.fees || 0) + (pos.down.fees || 0))}` },
  ];
  const row = (side, t) => `<tr><td><span class="side-tag" data-side="${side}">${side}</span></td>
      <td>${sh(t.shares)}</td><td>${usd(t.cost)}</td><td>${px(t.avg)}</td><td>${px(t.bid, 2)}</td><td>${usd(t.value)}</td></tr>`;
  host.innerHTML = `
    <div class="mkt-pos-tiles">${tiles.map((k) => `<div class="mkt-pos-tile"><span class="kpi-label">${k.l}</span>`
      + `<span class="mkt-pos-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('')}</div>
    <div class="mkt-pos-table"><table class="data-table">
      <thead><tr><th scope="col">Token</th><th scope="col">Shares</th><th scope="col">Cost</th>
        <th scope="col">Avg price</th><th scope="col">Bid</th><th scope="col">Value at bid</th></tr></thead>
      <tbody>${row('UP', pos.up)}${row('DOWN', pos.down)}</tbody>
    </table></div>`;
}

/* Format a crypto price at a sensible precision for its own magnitude --
   0.01 on BTC is noise, 0.01 on XRP is real. */
function fmtSpot(v, ref = v) {
  if (v == null) return '—';
  // precision follows the PRICE's magnitude (ref), so a difference of 0.26 on
  // a 780 coin reads 0.26 like the price beside it, not 0.2600; one decimal
  // finer than a quote screen, because a basis point is what matters here
  const a = Math.abs(ref);
  const d = a >= 1000 ? 1 : a >= 100 ? 2 : a >= 1 ? 3 : 5;
  return v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}

function emptyChart(host, legendHost, forExport, hist) {
  if (forExport) return null;
  const opensIn = hist && hist.length ? Math.round(hist[hist.length - 1].left - WINDOW_S) : null;
  host.innerHTML = `<p class="live-empty">${
    opensIn != null && opensIn > 0 ? `Window opens in ${opensIn}s.` : 'Collecting price samples…'}</p>`;
  legend(legendHost, []);
  return null;
}

/* Crypto spot price against the strike -- the left half of the split. Same
   fixed time axis as the token chart (both sampled into the same
   price_history point), but its own price scale since spot lives in dollars,
   not [0,1]. */
function renderSpotChart(slug = state.selected, host = $('#spot-chart'),
                         forExport = false) {
  const hist = (state.live.price_history || {})[slug];
  const rec = (state.live.closed_markets || []).find((x) => x.slug === slug)
    || (state.live.live_markets || []).find((x) => x.slug === slug);
  const pts = (hist || []).filter((p) => p.spot != null && inWindow(p));
  if (!pts.length) return emptyChart(host, $('#spot-legend'), forExport, hist);

  const cSpot = css('--text-primary'), strike = rec && rec.strike ? rec.strike : null;
  const H = forExport ? 300 : Math.round(220 * MARKET_CHART_SCALE), m = { t: 30, r: 72, b: 34, l: 64 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;

  const vals = pts.map((p) => p.spot);
  if (strike) vals.push(strike);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.12 || Math.abs(lo) * 0.001 || 1;
  lo -= pad; hi += pad;
  const Y = (v) => m.t + ph - ((v - lo) / (hi - lo)) * ph;

  // round ticks inside the range, at the precision their step needs
  const { ticks, decimals } = roundTicks(lo, hi, 5);
  ticks.forEach((v) => {
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(v), y2: Y(v) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: Y(v) + 4, 'text-anchor': 'end' });
    lab.textContent = v.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    svg.appendChild(lab);
  });
  const asset = (slug || '').split('-')[0].toUpperCase();
  axisCaption(svg, m, `${asset || 'Spot'} price (USD) · composite of the exchanges`);
  const { X } = fixedAxis(svg, m, pw, H);

  // the strike is a reference threshold, not data -- dashed on purpose, to
  // read as "target" rather than as a second series
  if (strike != null && strike > 0) {
    const sy = Y(strike);
    svg.appendChild(el('line', {
      x1: m.l, x2: m.l + pw, y1: sy, y2: sy,
      stroke: css('--axis'), 'stroke-width': 1.5, 'stroke-dasharray': '5 4',
    }));
    const lab = el('text', { class: 'value-label', x: m.l + pw + 8, y: sy + 4 });
    lab.textContent = `strike ${fmtSpot(strike)}`;
    svg.appendChild(lab);
  }

  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(p.spot)}`).join(' ');
  svg.appendChild(el('path', { class: 'series-line', d, stroke: cSpot }));
  const last = pts[pts.length - 1];
  const cx = X(last), cy = Y(last.spot);
  svg.appendChild(el('circle', { cx, cy, r: 5, fill: cSpot, stroke: css('--surface-1'), 'stroke-width': 2 }));
  const endLab = el('text', { class: 'value-label-strong', x: cx + 10, y: cy + 4 });
  endLab.textContent = fmtSpot(last.spot);
  svg.appendChild(endLab);

  const hov = hoverLayer(svg, m, ph, [{ color: cSpot, y: (p) => Y(p.spot) }]);
  const hit = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', (ev) => {
    const p = nearestByX(pts, ev, svg, m, pw);
    if (!p) return;
    if (forExport) hov.show(X(p), p); else linkHover(p.t);
    const rows = [['Price', fmtSpot(p.spot), cSpot]];
    if (strike) rows.push(['vs strike', `${p.spot >= strike ? '+' : '−'}${fmtSpot(Math.abs(p.spot - strike), strike)}`]);
    if (p.srcs) {
      Object.entries(p.srcs).sort().forEach(([ex, px]) => rows.push([ex, fmtSpot(px)]));
    }
    showTip(tipRows(tipTime(p), rows), ev);
  });
  hit.addEventListener('mouseleave', () => { if (forExport) hov.clear(); else linkLeave(); hideTip(); });

  // drawn after the hover layer so each marker keeps its own tooltip
  const turns = state.turns ? trendTurns(pts.filter((p) => p.left > 0), (p) => Math.log(p.spot) * 1e4, spotTurnBps(pts, slug)) : [];
  drawTurns(svg, turns, X, (p) => Y(p.spot), 'Crypto', (v) => fmtSpot(Math.exp(v / 1e4)),
            (l) => `${l >= 0 ? '+' : ''}${l.toFixed(1)} bps`);
  const shocks = ((state.live.shock_marks || {})[slug] || []);
  drawShocks(svg, shocks, pts, X, (p) => Y(p.spot));

  if (!forExport) {
    const items = [{ label: 'Price', color: cSpot }];
    if (strike) items.push({ label: 'Strike', color: css('--axis') });
    if (turns.length) turnsLegend(items, 'trend');
    shocksLegend(items, shocks);
    legend($('#spot-legend'), items);
    const spotReadout = (p, live) => {
      const items2 = [{ color: cSpot, value: fmtSpot(p.spot), label: asset }];
      if (strike) {
        const diff = p.spot - strike;
        items2.push({ value: fmtSpot(strike), label: 'strike' },
                    { value: `${diff >= 0 ? '+' : '−'}${fmtSpot(Math.abs(diff), strike)}`,
                      label: `${diff >= 0 ? 'above' : 'below'} (${signed(Math.log(p.spot / strike) * 1e4)} bps)` });
      }
      const sig = ((state.live && state.live.sigma_bps_per_sec) || {})[asset];
      if (sig) items2.push({ value: `${sig.toFixed(2)} bps/s`, label: 'volatility' });
      readout($('#spot-readout'), roWhen(p, live), items2);
    };
    linkPanel('spot', {
      at: (t) => nearestByT(pts, t),
      show: (p) => { hov.show(X(p), p); spotReadout(p, false); },
      clear: () => { hov.clear(); spotReadout(last, true); },
    });
  }
  return svg;
}

/* Distance from the strike, in basis points, with the +-1 sigma envelope of
   the settling TWAP. This is the pricer's input laid bare: how far the price
   is from the line that decides the window, against how far it is expected
   to be able to move in the time left. Above the strike favours Up (series 1),
   below favours Down (series 2) -- the same hue-to-outcome binding as the
   token chart, so the two panels read as one story. */
/* Standardised Student-t(4) CDF, exactly as pricing/digital.py: the closed
   form for four degrees of freedom, evaluated at z * sqrt(nu / (nu - 2)). */
function t4cdf(z) {
  const t = z * Math.SQRT2;
  return 0.5 + (t * (t * t + 6)) / (2 * Math.pow(t * t + 4, 1.5));
}

function twapSd(left, sigmaBps) {
  const u = Math.max(0, left);
  const v = u >= 60 ? u - 40 : (u * u * u) / 10800;
  return sigmaBps * Math.sqrt(Math.max(v, 0));
}

function renderStrikeChart(slug = state.selected, host = $('#strike-chart'), forExport = false) {
  const hist = (state.live.price_history || {})[slug];
  const rec = (state.live.closed_markets || []).find((x) => x.slug === slug)
    || (state.live.live_markets || []).find((x) => x.slug === slug);
  const strike = rec && rec.strike ? rec.strike : null;
  const pts = (hist || []).filter((p) => p.spot != null && inWindow(p));
  if (!pts.length || !strike) {
    if (forExport) return null;
    host.innerHTML = `<p class="live-empty">${pts.length ? 'No strike yet for this window.' : 'Collecting price samples…'}</p>`;
    legend($('#strike-legend'), []);
    return null;
  }
  const asset = (slug || '').split('-')[0].toUpperCase();
  const sigma = (state.live.sigma_bps_per_sec || {})[asset] || 0;
  const cUp = css('--series-1'), cDown = css('--series-2'), cBand = css('--axis');
  const bpsOf = (p) => Math.log(p.spot / strike) * 1e4;

  const H = forExport ? 300 : Math.round(300 * MARKET_CHART_SCALE), m = { t: 30, r: 64, b: 34, l: 52 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const bandAt = (p) => twapSd(p.left, sigma);
  let ext = 5;
  pts.forEach((p) => { ext = Math.max(ext, Math.abs(bpsOf(p)), bandAt(p)); });
  ext *= 1.12;
  const Y = (v) => m.t + ph / 2 - (v / ext) * (ph / 2);

  // symmetric gridlines at a round step
  const rawStep = ext / 3.5;
  const step = [1, 2, 5, 10, 20, 25, 50, 100, 200].find((s) => s >= rawStep) || 500;
  for (let v = -Math.floor(ext / step) * step; v <= ext; v += step) {
    if (v === 0) continue;
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(v), y2: Y(v) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: Y(v) + 4, 'text-anchor': 'end' });
    lab.textContent = `${v > 0 ? '+' : ''}${v}`;
    svg.appendChild(lab);
  }
  axisCaption(svg, m, 'Distance from strike (bps) · band = ±1σ still possible');
  const { X } = fixedAxis(svg, m, pw, H);

  // the +-1 sigma envelope: what the settling average can still do
  if (sigma > 0) {
    const top = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(bandAt(p))}`).join(' ');
    const bottom = pts.slice().reverse().map((p) => `L${X(p)},${Y(-bandAt(p))}`).join(' ');
    svg.appendChild(el('path', { class: 'band-fill', d: `${top} ${bottom} Z`, fill: cBand }));
    // no end label: it collided with the line's own, and the readout carries it
  }

  // zero is the strike
  svg.appendChild(el('line', { x1: m.l, x2: m.l + pw, y1: Y(0), y2: Y(0),
    stroke: css('--axis'), 'stroke-width': 1.5, 'stroke-dasharray': '5 4' }));
  const zl = el('text', { class: 'value-label', x: m.l + pw + 8, y: Y(0) + 4 });
  zl.textContent = 'strike';
  svg.appendChild(zl);

  // area between the line and the strike, coloured by which side it is on
  const uid = `clip-${Math.random().toString(36).slice(2, 8)}`;
  const defs = el('defs');
  const above = el('clipPath', { id: `${uid}-a` });
  above.appendChild(el('rect', { x: m.l, y: m.t, width: pw, height: Math.max(0, Y(0) - m.t) }));
  const below = el('clipPath', { id: `${uid}-b` });
  below.appendChild(el('rect', { x: m.l, y: Y(0), width: pw, height: Math.max(0, m.t + ph - Y(0)) }));
  defs.appendChild(above); defs.appendChild(below);
  svg.appendChild(defs);
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(bpsOf(p))}`).join(' ');
  const area = `${line} L${X(pts[pts.length - 1])},${Y(0)} L${X(pts[0])},${Y(0)} Z`;
  svg.appendChild(el('path', { class: 'series-area', d: area, fill: cUp, 'clip-path': `url(#${uid}-a)` }));
  svg.appendChild(el('path', { class: 'series-area', d: area, fill: cDown, 'clip-path': `url(#${uid}-b)` }));
  svg.appendChild(el('path', { class: 'series-line', d: line, stroke: css('--text-primary') }));

  const last = pts[pts.length - 1], lv = bpsOf(last);
  const color = lv >= 0 ? cUp : cDown;
  const cx = X(last), cy = Y(lv);
  svg.appendChild(el('circle', { cx, cy, r: 5, fill: color, stroke: css('--surface-1'), 'stroke-width': 2 }));
  const endLab = el('text', { class: 'value-label-strong', x: cx + 10, y: cy + 4 });
  endLab.textContent = `${lv >= 0 ? '+' : ''}${lv.toFixed(1)} bps`;
  svg.appendChild(endLab);

  const hov = hoverLayer(svg, m, ph, [{ color: css('--text-primary'), y: (p) => Y(bpsOf(p)) }]);
  const hit = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(hit);
  hit.addEventListener('mousemove', (ev) => {
    const p = nearestByX(pts, ev, svg, m, pw);
    if (!p) return;
    if (forExport) hov.show(X(p), p); else linkHover(p.t);
    const v = bpsOf(p), sd = bandAt(p);
    const rows = [
      [v >= 0 ? 'Above strike' : 'Below strike', `${v >= 0 ? '+' : ''}${v.toFixed(1)} bps`, v >= 0 ? cUp : cDown],
      ['Price', fmtSpot(p.spot)],
      ['Strike', fmtSpot(strike)],
    ];
    if (sd > 0) rows.push(['±1σ to settle', `${sd.toFixed(1)} bps`, cBand], ['z', (v / sd).toFixed(2)]);
    if (p.up != null) rows.push(['Market Up', p.up.toFixed(3)]);
    showTip(tipRows(tipTime(p), rows), ev);
  });
  hit.addEventListener('mouseleave', () => { if (forExport) hov.clear(); else linkLeave(); hideTip(); });

  // the same crypto turns as the spot panel, on the distance-from-strike line
  const turns = state.turns ? trendTurns(pts.filter((p) => p.left > 0), bpsOf, spotTurnBps(pts, slug)) : [];
  drawTurns(svg, turns, X, (p) => Y(bpsOf(p)), 'Crypto', (v) => `${v >= 0 ? '+' : ''}${v.toFixed(1)} bps vs strike`,
            (l) => `${l >= 0 ? '+' : ''}${l.toFixed(1)} bps`);

  if (!forExport) {
    const items = [
      { label: 'Above strike', color: cUp }, { label: 'Below strike', color: cDown },
      { label: '±1σ expected move', color: cBand },
    ];
    if (turns.length) turnsLegend(items, 'trend');
    legend($('#strike-legend'), items);
    const strikeReadout = (p, live) => {
      const v = bpsOf(p), sd = bandAt(p);
      const items2 = [{ color: v >= 0 ? cUp : cDown, value: `${signed(v)} bps`, label: v >= 0 ? 'above strike' : 'below strike' }];
      if (sd > 0) {
        items2.push({ value: `±${sd.toFixed(1)} bps`, label: '1σ still possible' },
                    { value: signed(v / sd, 2), label: 'z' },
                    { value: t4cdf(v / sd).toFixed(2), label: 'model P(Up)' });
      }
      if (typeof p.up === 'number') items2.push({ value: p.up.toFixed(2), label: 'market Up' });
      readout($('#strike-readout'), roWhen(p, live), items2);
    };
    const lastPt = pts[pts.length - 1];
    linkPanel('strike', {
      at: (t) => nearestByT(pts, t),
      show: (p) => { hov.show(X(p), p); strikeReadout(p, false); },
      clear: () => { hov.clear(); strikeReadout(lastPt, true); },
    });
  }
  return svg;
}

/* "bid 0.53 · ask 0.55" -- the executable prices; a missing side says so */
function bidAsk(bid, ask) {
  const f = (v) => (typeof v === 'number' ? v.toFixed(2) : '—');
  return `bid ${f(bid)} · ask ${f(ask)}`;
}
const spreadCents = (bid, ask) => (typeof bid === 'number' && typeof ask === 'number'
  ? `${((ask - bid) * 100).toFixed(1)}¢ spread` : 'one-sided book');

function priceReadout(p, live) {
  if (!p) return;
  readout($('#price-readout'), roWhen(p, live), [
    { color: css('--series-1'), value: p.up.toFixed(3), label: `Up · ${bidAsk(p.ub, p.ua)} · ${spreadCents(p.ub, p.ua)}` },
    { color: css('--series-2'), value: p.down.toFixed(3), label: `Down · ${bidAsk(p.db, p.da)} · ${spreadCents(p.db, p.da)}` },
  ]);
}

/* Parameterised so a CLOSED market can be drawn into an offscreen host for
   PNG export without disturbing what is on screen. */
function renderPriceChart(slug = state.selected, host = $('#price-chart'),
                          forExport = false) {
  const hist = (state.live.price_history || {})[slug];
  const pts = (hist || []).filter(inWindow);
  if (pts.length < 2) return emptyChart(host, $('#price-legend'), forExport, hist);

  const cUp = css('--series-1'), cDown = css('--series-2');
  const H = forExport ? 300 : Math.round(300 * MARKET_CHART_SCALE), m = { t: 30, r: 56, b: 34, l: 48 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const Y = (v) => m.t + ph - v * ph;                 // prices live in [0,1]

  [0, 0.25, 0.5, 0.75, 1].forEach((t) => {
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(t), y2: Y(t) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: Y(t) + 4, 'text-anchor': 'end' });
    lab.textContent = t.toFixed(2);
    svg.appendChild(lab);
  });
  axisCaption(svg, m, 'Share price ($) · band = bid to ask');
  const { X } = fixedAxis(svg, m, pw, H);

  // what can actually be traded: each side's bid-ask band under its mid line
  spreadBand(svg, pts, X, Y, 'ub', 'ua', cUp);
  spreadBand(svg, pts, X, Y, 'db', 'da', cDown);

  [['up', cUp], ['down', cDown]].forEach(([key, color]) => {
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(p[key])}`).join(' ');
    svg.appendChild(el('path', { class: 'series-line', d, stroke: color }));
    const last = pts[pts.length - 1];
    const cx = X(last), cy = Y(last[key]);
    svg.appendChild(el('circle', { cx, cy, r: 5, fill: color, stroke: css('--surface-1'), 'stroke-width': 2 }));
    const t = el('text', { class: 'value-label-strong', x: cx + 10, y: cy + 4 });
    t.textContent = last[key].toFixed(2);
    svg.appendChild(t);
  });

  // the strike distance, expressed on THIS axis as the Up probability it
  // implies (distance / expected move through the pricer's own CDF), so the
  // model's number sits directly against the market's without a second scale
  let implied = null;
  if (state.overlay && !forExport) {
    const rec = (state.live.closed_markets || []).find((x) => x.slug === slug)
      || (state.live.live_markets || []).find((x) => x.slug === slug);
    const strike = rec && rec.strike ? rec.strike : null;
    const asset = (slug || '').split('-')[0].toUpperCase();
    const sigma = (state.live.sigma_bps_per_sec || {})[asset] || 0;
    if (strike && sigma > 0) {
      implied = new Map();
      pts.forEach((p) => {
        if (p.spot == null) return;
        const bps = Math.log(p.spot / strike) * 1e4;
        const sd = twapSd(p.left, sigma);
        implied.set(p, { bps, sd, p: sd > 0 ? t4cdf(bps / sd) : (bps >= 0 ? 1 : 0) });
      });
      const ip = pts.filter((p) => implied.has(p));
      if (ip.length) {
        const d = ip.map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(implied.get(p).p)}`).join(' ');
        svg.appendChild(el('path', { class: 'series-line', d, stroke: css('--text-primary'),
                                      'stroke-dasharray': '5 4' }));
        const last = ip[ip.length - 1], v = implied.get(last);
        const cx = X(last), cy = Y(v.p);
        svg.appendChild(el('circle', { cx, cy, r: 4.5, fill: css('--text-primary'),
                                        stroke: css('--surface-1'), 'stroke-width': 2 }));
        const t = el('text', { class: 'value-label', x: cx + 10, y: cy + 4 });
        t.textContent = `model ${v.p.toFixed(2)} (${v.bps >= 0 ? '+' : ''}${v.bps.toFixed(1)} bps)`;
        svg.appendChild(t);
      }
    }
  }

  // the hover layer goes UNDER the markers: drawn on top it swallowed every
  // marker's own tooltip (a transparent fill still catches the pointer)
  const hov = hoverLayer(svg, m, ph, [
    { color: cUp, y: (p) => (typeof p.up === 'number' ? Y(p.up) : null) },
    { color: cDown, y: (p) => (typeof p.down === 'number' ? Y(p.down) : null) },
  ].concat(implied ? [{ color: css('--text-primary'), y: (p) => (implied.has(p) ? Y(implied.get(p).p) : null) }] : []));
  const hitLayer = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(hitLayer);
  hitLayer.addEventListener('mousemove', (ev) => {
    const p = nearestByX(pts, ev, svg, m, pw);
    if (!p) return;
    if (forExport) hov.show(X(p), p); else linkHover(p.t);
    const rows = [
      ['Up', `${p.up.toFixed(3)} · ${bidAsk(p.ub, p.ua)}`, cUp],
      ['Down', `${p.down.toFixed(3)} · ${bidAsk(p.db, p.da)}`, cDown],
      ['Sum of mids', (p.up + p.down).toFixed(3)],
    ];
    const v = implied && implied.get(p);
    if (v) {
      rows.push(['Model P(up)', v.p.toFixed(3), css('--text-primary')],
                ['Distance', `${v.bps >= 0 ? '+' : ''}${v.bps.toFixed(1)} bps`],
                ['±1σ to settle', `${v.sd.toFixed(1)} bps`],
                ['Model − Up', `${(v.p - p.up) >= 0 ? '+' : ''}${(v.p - p.up).toFixed(3)}`]);
    }
    showTip(tipRows(tipTime(p), rows), ev);
  });
  hitLayer.addEventListener('mouseleave', () => { if (forExport) hov.clear(); else linkLeave(); hideTip(); });

  // every ask the strategy looked at, coloured by what it decided: the
  // "why didn't we buy" answer drawn on the price path itself
  const marks = ((state.live.signal_marks || {})[slug] || []).filter((k) => k.ask != null);
  const markColor = (dec) => (
    dec === 'SIGNAL_GENERATED' ? css('--ok') || '#2e9e5b'
    : dec === 'SIGNAL_REJECTED_LOW_EDGE' ? css('--warn') || '#d9962b'
    : dec === 'SIGNAL_REJECTED_TOKEN_ALREADY_MOVED' ? '#8a5cf6'
    : dec === 'SIGNAL_REJECTED_STALE_QUOTE' ? css('--bad') || '#d64545'
    : dec === 'SIGNAL_REJECTED_LIQUIDITY' ? '#3aa7c9'
    : css('--text-tertiary') || '#8a8f98');
  const markLabel = (dec) => (dec || '').replace('SIGNAL_REJECTED_', '').replace('SIGNAL_', '').toLowerCase().replace(/_/g, ' ');
  const seen = new Set();
  marks.forEach((k) => {
    let at = pts[0], best = Infinity;
    pts.forEach((p) => { const d = Math.abs(p.t - k.t); if (d < best) { best = d; at = p; } });
    if (best > 3000) return;
    const color = markColor(k.decision);
    seen.add(k.decision);
    const cx = X(at), cy = Y(k.ask);
    svg.appendChild(el('circle', { cx, cy, r: k.decision === 'SIGNAL_GENERATED' ? 5 : 3.5, fill: color,
                                    stroke: css('--surface-1'), 'stroke-width': 1, opacity: 0.9 }));
    const hit = el('circle', { class: 'hit', cx, cy, r: 9 });
    svg.appendChild(hit);
    hit.addEventListener('mousemove', (ev) => showTip(tipRows(`${markLabel(k.decision)} · ${k.side}`, [
      ['Ask seen', Number(k.ask).toFixed(3), color],
      ['Expected', k.p != null ? Number(k.p).toFixed(3) : '—'],
      ['Net edge', k.net != null ? (k.net >= 0 ? '+' : '') + Number(k.net).toFixed(4) : '—'],
      ['Time left', fmt.mmss(at.left || 0)],
      ['Why', k.detail || '—'],
    ]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });

  // where we actually bought — the whole point of plotting this (sells are drawn
  // from the order points below, as hollow diamonds)
  const fills = (state.live.ledger || []).filter(
    (r) => r.event === 'fill' && r.slug === slug && r.action !== 'SELL');
  const boughtAt = fills.map((f) => f.ts);
  fills.forEach((f) => {
    let at = pts[0], best = Infinity;
    pts.forEach((p) => { const d = Math.abs(p.t - f.ts); if (d < best) { best = d; at = p; } });
    const color = f.side === 'UP' ? cUp : cDown;
    const cx = X(at), cy = Y(f.price);
    svg.appendChild(el('path', {
      d: `M${cx},${cy - 7} L${cx + 7},${cy} L${cx},${cy + 7} L${cx - 7},${cy} Z`,
      fill: color, stroke: css('--surface-1'), 'stroke-width': 2,
    }));
    const hit = el('circle', { class: 'hit', cx, cy, r: 13 });
    svg.appendChild(hit);
    hit.addEventListener('mousemove', (ev) => showTip(tipRows('Bought ' + f.side, [
      ['Price', f.price.toFixed(3), color],
      ['Shares', fmt.num(f.size, 1)],
      ['Cost', fmt.money(f.cost || f.price * f.size)],
      ['Our fair', (f.fair ?? 0).toFixed(3)],
      ['Time left', fmt.mmss(f.secs_left || 0)],
    ]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });

  // every ORDER the strategy sent, at its limit price: × = killed at the venue (the
  // ask was gone, the cap refused it...), □ = still in flight, ◇ = an exit that sold.
  // A filled entry is the diamond above; its order point is left out.
  const orders = ((state.live.order_marks || {})[slug] || []).filter((o) => o.limit != null);
  const seenOrders = new Set();
  orders.forEach((o) => {
    let at = pts[0], best = Infinity;
    pts.forEach((p) => { const d = Math.abs(p.t - o.t); if (d < best) { best = d; at = p; } });
    if (best > 3000) return;
    const isExit = orderIsSell(o);
    const filled = o.outcome === 'filled' || o.outcome === 'partial';
    if (filled && !isExit && boughtAt.some((ts) => Math.abs(ts - (o.t_done || o.t)) < 5000)) return;
    const color = o.side === 'UP' ? cUp : cDown;
    const cx = X(at), cy = Y(filled && o.price != null ? o.price : o.limit);
    // grouped with a halo, so hovering its row in the order list lights it up
    const g = el('g', { class: `order-mark${ORDERS.hot === o.id ? ' is-hot' : ''}`, 'data-oid': o.id || '' });
    g.appendChild(el('circle', { class: 'mark-halo', cx, cy, r: 12, fill: color }));
    svg.appendChild(g);
    let title;
    if (filled) {
      g.appendChild(el('path', { d: `M${cx},${cy - 7} L${cx + 7},${cy} L${cx},${cy + 7} L${cx - 7},${cy} Z`,
                                   fill: isExit ? css('--surface-1') : color, stroke: color, 'stroke-width': 2 }));
      seenOrders.add(isExit ? 'sold' : 'bought');
      title = `${isExit ? 'Sold' : 'Bought'} ${o.side}${isExit ? ' · ' + String(o.kind).toLowerCase().replace(/_/g, ' ') : ''}`;
    } else if (o.outcome === 'sent') {
      g.appendChild(el('rect', { x: cx - 4, y: cy - 4, width: 8, height: 8, fill: css('--surface-1'),
                                   stroke: color, 'stroke-width': 1.5 }));
      seenOrders.add('sent');
      title = `${isExit ? 'Exit' : 'Entry'} order in flight · ${o.side}`;
    } else {
      g.appendChild(el('path', { d: `M${cx - 5},${cy - 5} L${cx + 5},${cy + 5} M${cx - 5},${cy + 5} L${cx + 5},${cy - 5}`,
                                   stroke: color, 'stroke-width': 2, fill: 'none', opacity: 0.9 }));
      seenOrders.add('killed');
      title = `${isExit ? 'Exit' : 'Entry'} order ${o.outcome} · ${o.side}`;
    }
    const hit = el('circle', { class: 'hit', cx, cy, r: 11 });
    g.appendChild(hit);
    hit.addEventListener('mouseenter', () => hotOrder(o.id, true));
    hit.addEventListener('mousemove', (ev) => showTip(tipRows(title, [
      ['Limit', Number(o.limit).toFixed(3), color],
      ['Shares', fmt.num(o.size || 0, 1)],
      ...(filled && o.price != null ? [['Filled at', Number(o.price).toFixed(3)]] : []),
      ...(o.reason ? [['Why', String(o.reason).replace(/_/g, ' ')]] : []),
      ...(o.t_done ? [['Took', `${((o.t_done - o.t) / 1000).toFixed(2)} s`]] : []),
      ['Time left', fmt.mmss(at.left || 0)],
      ['Sent at', fmt.clockTenths(o.t)],
    ]), ev));
    hit.addEventListener('mouseleave', () => { hotOrder(o.id, false); hideTip(); });
  });

  // where the Up token's own trend turned (3 c swings); Down mirrors it
  const turns = state.turns ? tokenTurns(pts) : [];
  drawTurns(svg, turns, X, (p) => Y(p.up), 'Up token', (v) => v.toFixed(3),
            (l) => `${l >= 0 ? '+' : ''}${(l * 100).toFixed(1)}c`);

  if (!forExport) {
    const items = [{ label: 'Up', color: cUp }, { label: 'Down', color: cDown }];
    if (implied) items.push({ label: 'Model P(up) from strike distance', color: css('--text-primary') });
    if (turns.length) turnsLegend(items, 'Up token');
    if (fills.length || seenOrders.has('bought')) items.push({ label: '◆ bought', color: css('--text-primary') });
    if (seenOrders.has('sold')) items.push({ label: '◇ sold', color: css('--text-primary') });
    if (seenOrders.has('killed')) items.push({ label: '× order not filled', color: css('--text-primary') });
    if (seenOrders.has('sent')) items.push({ label: '□ order in flight', color: css('--text-primary') });
    ['SIGNAL_GENERATED', 'SIGNAL_REJECTED_LOW_EDGE', 'SIGNAL_REJECTED_TOKEN_ALREADY_MOVED',
     'SIGNAL_REJECTED_STALE_QUOTE', 'SIGNAL_REJECTED_LIQUIDITY'].forEach((dec) => {
      if (seen.has(dec)) items.push({ label: `ask: ${markLabel(dec)}`, color: markColor(dec) });
    });
    if ([...seen].some((d) => !['SIGNAL_GENERATED', 'SIGNAL_REJECTED_LOW_EDGE', 'SIGNAL_REJECTED_TOKEN_ALREADY_MOVED',
                                'SIGNAL_REJECTED_STALE_QUOTE', 'SIGNAL_REJECTED_LIQUIDITY'].includes(d))) {
      items.push({ label: 'ask: other rejection', color: css('--text-tertiary') || '#8a8f98' });
    }
    legend($('#price-legend'), items);
  }

  if (forExport) return svg;
  const lastPt = pts[pts.length - 1];
  linkPanel('price', {
    at: (t) => nearestByT(pts, t),
    show: (p) => { hov.show(X(p), p); priceReadout(p, false); },
    clear: () => { hov.clear(); priceReadout(lastPt, true); },
  });
  const mk = (state.live.live_markets || []).find((x) => x.slug === slug);
  $('#chart-sub').textContent = mk
    ? `${fmt.mkt(slug)} · strike ${mk.strike ? mk.strike.toLocaleString(undefined, { maximumFractionDigits: 4 }) : '—'} · ${fmt.mmss(mk.secs_left)} left · ${mk.status || ''}`
    : 'Where the price sits against the strike, what the market charges for each side, and the raw price path.';
  return svg;
}

/* ═══════════════════════ closed-window PNG export ═══════════════════════ */

/* When a window closes its chart is exported once, then the view moves on to
   a live market. Rendering happens in the browser rather than in Python so the
   saved image IS the chart on screen — a second plotting implementation server
   side would drift away from this one. The bot separately archives the raw
   points to data/charts/<slug>.json, so a window is never lost when the page
   is shut. */

/* Saving an image for every window is on by default, because the archive is
   the point of keeping them. It is also the one thing here that grows without
   bound on disk, so it is a switch: ?savecharts=0|1 wins, else what was
   chosen last, else on. Turning it off stops new images only — the bot still
   archives the raw points, so nothing about the record is lost. */
state.saveCharts = (() => {
  const q = new URLSearchParams(location.search).get('savecharts');
  if (q === '1' || q === '0') return q === '1';
  try { return localStorage.getItem('tpb-save-charts') !== '0'; } catch { return true; }
})();

const exported = (() => {
  try { return new Set(JSON.parse(localStorage.getItem('tpb-charts') || '[]')); }
  catch { return new Set(); }
})();
function rememberExported(slug) {
  exported.add(slug);
  try {
    // keep the list bounded; it only exists to avoid re-POSTing on reload
    localStorage.setItem('tpb-charts', JSON.stringify([...exported].slice(-400)));
  } catch { /* private mode */ }
}

/* The chart is styled by an external stylesheet. Serialised on its own, an SVG
   loses all of that and renders as unstyled black — so every computed paint
   property is copied onto the clone as a presentation attribute first. */
const PAINT = ['fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-linecap',
               'stroke-linejoin', 'opacity', 'font-size', 'font-family',
               'font-weight', 'text-anchor', 'shape-rendering'];

function inlineStyles(src) {
  const clone = src.cloneNode(true);
  const from = [src, ...src.querySelectorAll('*')];
  const to = [clone, ...clone.querySelectorAll('*')];
  from.forEach((node, i) => {
    const cs = getComputedStyle(node);
    PAINT.forEach((prop) => {
      const v = cs.getPropertyValue(prop);
      if (v) to[i].setAttribute(prop, v.trim());
    });
    to[i].removeAttribute('class');
  });
  return clone;
}

async function exportChartPng(slug) {
  // the main poll only carries the market being drawn; fetch this one's own
  // history explicitly, and hand it to the renderer
  const own = await fetch(`/api/live?bot=${encodeURIComponent(state.botId)}&chart=${encodeURIComponent(slug)}`)
    .then((r) => r.json()).catch(() => null);
  const pts = own && own.price_history && own.price_history[slug];
  if (!pts || pts.length < 2 || !state.live) return false;
  state.live.price_history = Object.assign({}, state.live.price_history, { [slug]: pts });

  // draw the closed market offscreen so the visible chart is untouched
  const stage = document.createElement('div');
  stage.style.cssText = 'position:fixed;left:-10000px;top:0;width:1100px';
  document.body.appendChild(stage);
  try {
    const svgEl = renderPriceChart(slug, stage, true);
    if (!svgEl) return false;

    const w = Number(svgEl.getAttribute('width'));
    const h = Number(svgEl.getAttribute('height'));
    const CAP = 46;                         // caption band under the plot
    const out = inlineStyles(svgEl);
    out.setAttribute('xmlns', NS);
    out.setAttribute('width', w);
    out.setAttribute('height', h + CAP);
    out.setAttribute('viewBox', `0 0 ${w} ${h + CAP}`);

    // SVG is transparent; a PNG needs a real background
    const bg = el('rect', { x: 0, y: 0, width: w, height: h + CAP,
                            fill: css('--surface-1') });
    out.insertBefore(bg, out.firstChild);

    const rec = (state.live.closed_markets || []).find((x) => x.slug === slug)
      || (state.live.live_markets || []).find((x) => x.slug === slug) || {};
    const caption = el('text', {
      x: 14, y: h + 20, 'font-size': 13, 'font-weight': 600,
      'font-family': css('--sans') || 'sans-serif', fill: css('--text-primary'),
    });
    caption.textContent = fmt.mkt(slug)
      + (rec.strike ? `  ·  strike ${rec.strike.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
                    : '  ·  never struck')
      + (rec.outcome ? `  ·  ${rec.outcome} won` : '')
      + (rec.traded ? `  ·  ${fmt.signed(rec.pnl || 0)}` : '  ·  not traded');
    out.appendChild(caption);

    const sub = el('text', {
      x: 14, y: h + 37, 'font-size': 11,
      'font-family': css('--sans') || 'sans-serif', fill: css('--text-muted'),
    });
    sub.textContent = `${fmt.clock(Date.now())} ${TZ_LABEL}  ·  paper trading, simulated fills`;
    out.appendChild(sub);

    const xml = new XMLSerializer().serializeToString(out);
    const url = URL.createObjectURL(new Blob([xml], { type: 'image/svg+xml;charset=utf-8' }));
    try {
      const img = new Image();
      await new Promise((res, rej) => {
        img.onload = res;
        img.onerror = () => rej(new Error('svg decode failed'));
        img.src = url;
      });
      const SCALE = 2;                      // readable on a high-dpi screen
      const canvas = document.createElement('canvas');
      canvas.width = w * SCALE;
      canvas.height = (h + CAP) * SCALE;
      const ctx = canvas.getContext('2d');
      ctx.scale(SCALE, SCALE);
      ctx.drawImage(img, 0, 0);
      const png = await new Promise((r) => canvas.toBlob(r, 'image/png'));
      if (!png) return false;
      const res = await fetch(`/api/chart/${encodeURIComponent(slug)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'image/png' },
        body: png,
      }).then((r) => r.json());
      return !!res.ok;
    } finally {
      URL.revokeObjectURL(url);
    }
  } catch (e) {
    console.warn('chart export failed for', slug, e);
    return false;
  } finally {
    stage.remove();
  }
}

let exporting = false;

async function archiveClosedWindows() {
  if (exporting || !state.live || !state.saveCharts) return;
  const meta = state.live.history_meta || {};

  // Wait for the bot to publish the RESOLVED window rather than firing the
  // moment the clock hits zero. Settlement lands ~40s after close, and a chart
  // captioned without its outcome is missing the point of keeping it.
  const resolved = state.live.closed_markets || [];
  const done = resolved
    .map((m) => m.slug)
    .filter((slug) => !exported.has(slug) && (meta[slug] || 0) >= 10);
  if (!done.length) return;

  exporting = true;
  try {
    for (const slug of done) {
      rememberExported(slug);               // mark first: never retry in a loop
      const ok = await exportChartPng(slug);
      if (ok) notice(`Saved chart for ${fmt.mkt(slug)} → data/charts/${slug}.png`, false);
    }
  } finally {
    exporting = false;
  }
  // a closed window has left the picker; renderPicker re-selects on the next tick
}

/* ═══════════════════════════════ tables ═════════════════════════════════ */

function modeLabel(d) {
  if (d.mode === 'LIVE-ARMED') return 'REAL MONEY · ';
  if (d.mode === 'live-dry-run') return 'live dry run · ';
  return '';
}

function liveLine(lv) {
  if (!lv) return '';
  const bits = [];
  if (lv.venue_balance != null) bits.push(`account ${fmt.money(lv.venue_balance)}`);
  if (lv.pending_redemption) bits.push(`pending redemption ${fmt.money(lv.pending_redemption)}`);
  if (lv.halted) bits.push(`HALTED: ${lv.halted}`);
  if (lv.kill_file_present) bits.push('KILL FILE PRESENT');
  return bits.length ? ' · ' + bits.join(' · ') : '';
}

function byAsset(m) {
  const e = Object.entries(m || {});
  return e.length ? ' · by asset ' + e.map(([a, v]) => `${a} ${fmt.signed(v)}`).join(', ') : '';
}

function feedsLine(f) {
  const e = Object.entries(f || {});
  if (!e.length) return '';
  const down = e.filter(([, v]) => !v.connected);
  return down.length
    ? ' · FEEDS DOWN: ' + down.map(([n, v]) => `${n} ${v.state_age_s != null ? Math.round(v.state_age_s) + 's' : ''}`).join(', ')
    : ` · feeds ok (${e.length})`;
}

/* Regime constants come from the engine as LABEL|LABEL; show only what is
   not the default, in words. */
function fmtRegime(label) {
  const parts = String(label || '').split('|')
    .filter((x) => x && !x.startsWith('NORMAL') && x !== 'SIDEWAYS')
    .map((x) => x.toLowerCase().replace(/_/g, ' '));
  return parts.length ? parts.join(' · ') : 'normal';
}

const STATUS_RANK = { closing: 0, live: 1, pending: 2, 'missed open': 3 };
function marketStatus(m) {
  return m.status || (m.strike ? 'live' : 'pending');
}

function flowCell(dc) {
  if (dc.ofi == null) return '—';
  const v = dc.ofi;
  const cls = v > 0.15 ? 'pos' : v < -0.15 ? 'neg' : '';
  const d = dc.ofi_drift_bps ? ` (${dc.ofi_drift_bps >= 0 ? '+' : ''}${dc.ofi_drift_bps.toFixed(1)}bp)` : '';
  return `<span class="${cls}">${v >= 0 ? '+' : ''}${v.toFixed(2)}</span>${d}`;
}

function bestSide(dc) {
  const s = (dc.sides || []).filter((x) => x.costs);
  if (!s.length) return null;
  return s.reduce((a, b) => (b.costs.net_edge > a.costs.net_edge ? b : a));
}

function renderHero() {
  const d = state.live;
  const h = $('#hero-equity');
  // a real account: the figure is the ACCOUNT's value (venue cash + venue positions at our marks), not the bot's books
  const accEq = d.account_sync && d.account_sync.portfolio != null ? d.account_sync.portfolio : null;
  h.textContent = fmt.money(accEq != null ? accEq : d.equity);
  h.className = 'hero-figure ' + ((accEq != null ? accEq - (d.starting_balance || 0) : d.pnl) >= 0 ? 'pos' : 'neg');
  $('#hero-sub').textContent = modeLabel(d)
    + (accEq != null ? `${fmt.signed(accEq - (d.starting_balance || 0))} from ${fmt.money(d.starting_balance)} at start · the account as the venue reports it · bot books ${fmt.money(d.equity)} · `
      : `${fmt.signed(d.pnl)} from ${fmt.money(d.starting_balance)} · `)
    + `realised ${fmt.signed(d.realised_pnl || 0)} · up ${Math.round((d.uptime_s || 0) / 60)}m`
    + byAsset(d.pnl_by_asset) + feedsLine(d.feeds) + liveLine(d.live);
  $('#hero-sub').classList.toggle('neg', d.mode === 'LIVE-ARMED');

  const s = d.stats || {}, o = d.orders || {}, e = d.evidence || {}, r = d.risk || {};
  // cash and portfolio (the user, 2026-10-05): cash = the account's USDC; positions = the held tokens at their
  // current marks (equity - cash); portfolio = equity = cash + positions
  const cash = d.balance || 0, equity = d.equity || 0, posVal = equity - cash;
  const heldShares = Object.values(d.open_positions || {}).reduce((a, q) => a + (Number(q) || 0), 0);
  // a real account: the venue's own cash and positions (re-read every few seconds), valued at our marks; the
  // bot's books are shown underneath where they differ
  const acc = d.account_sync;
  const split = acc ? [
    ['Cash', fmt.money(acc.cash), `the account · bot books ${fmt.money(cash)}`],
    ['Positions', fmt.money(acc.positions_value),
     acc.positions.length ? `${fmt.num(acc.positions.reduce((a, p) => a + p.shares, 0), 2)} tokens at the venue, at market` : 'nothing held at the venue'],
    ['Portfolio', fmt.money(acc.portfolio), `cash + positions · venue read every ${acc.reconcile_s || 30}s`],
  ] : [
    ['Cash', fmt.money(cash), `of ${fmt.money(d.starting_balance)} at start`],
    ['Positions', fmt.money(posVal), heldShares ? `${fmt.num(heldShares)} tokens held, at market` : 'nothing held'],
    ['Portfolio', fmt.money(equity), `cash + positions · ${fmt.signed(d.pnl)}`],
  ];
  $('#hero-split').innerHTML = split.map(([l, v, n]) => `<div class="hero-split-item"><span class="hero-split-label">${l}</span>`
    + `<span class="hero-split-value">${v}</span><span class="hero-split-note">${n}</span></div>`).join('');
  const kpis = [
    { l: 'Settled', v: fmt.num(s.settled || 0), n: `${s.wins || 0}W / ${s.losses || 0}L` },
    { l: 'Orders', v: fmt.num(o.orders_submitted || 0), n: `${fmt.num(o.orders_filled || 0)} filled` },
    { l: 'Open markets', v: fmt.num((d.live_markets || []).length), n: `${s.windows_seen || 0} seen` },
    spotAgeKpi(d),
    { l: 'Reaction lag', v: fmt.ms(d.reaction_lag_ms || 0), n: 'measured' },
    { l: 'Today', v: fmt.signed(r.realised_today || 0),
      n: `drawdown ${fmt.money(r.drawdown || 0)}${r.halted ? ' · HALTED' : ''}` },
  ];
  $('#kpi-row').innerHTML = kpis.map((k) => `
    <div class="kpi"><span class="kpi-label">${k.l}</span>
    <span class="kpi-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');
}

/* Spot age, measured: how old the newest crypto price was as each quote landed (p50), from the
   exchanges' own timestamps; the note lists each source's delivery delay (p50), fastest first. */
function spotAgeKpi(d) {
  const lat = d.measured_latency_ms || {};
  const from = d.spot_delay_from || {};
  const per = Object.keys(lat).filter((k) => k.startsWith('spot_p50_'))
    .map((k) => [k.slice(9), lat[k]]).sort((a, b) => a[1] - b[1]);
  const note = per.length
    ? per.map(([x, v]) => `${x} ${Math.round(v)}${from[x] === 'trade' ? '*' : ''}`).join(' · ')
      + (per.some(([x]) => from[x] === 'trade') ? ' · *from trades' : '')
    : 'no source data yet';
  return { l: 'Spot age', v: lat.n_spot ? `${fmt.ms(lat.spot_p50)} <small>p95 ${Math.round(lat.spot_p95)}</small>` : '—',
           n: note };
}

function renderMarkets() {
  const rows = state.live.live_markets || [];
  const struck = rows.filter((m) => m.strike > 0).length;
  const cov = state.live.twap_coverage_s || {};
  const warming = Object.entries(cov).filter(([, v]) => v < 59);
  $('#markets-sub').textContent = struck
    ? `${struck} struck and being priced, ${rows.length - struck} awaiting their window open.`
    : warming.length
      ? `None struck yet — the 60s TWAP window is still filling (${
          warming.map(([a, v]) => `${a} ${Math.round(v)}s`).join(', ')}).`
      : 'None struck yet.';
  const ordered = rows.slice().sort((a, b) =>
    (STATUS_RANK[marketStatus(a)] ?? 9) - (STATUS_RANK[marketStatus(b)] ?? 9)
    || a.secs_left - b.secs_left || a.slug.localeCompare(b.slug));
  $('#markets-table').innerHTML = rows.length
    ? '<thead><tr><th scope="col">Market</th><th scope="col">Status</th><th scope="col">Strike</th>'
      + '<th scope="col">Left</th><th scope="col">Model</th><th scope="col">Market</th>'
      + '<th scope="col">Net edge</th><th scope="col">Flow</th><th scope="col">Regime</th>'
      + '<th scope="col">Decision</th><th scope="col">Position</th></tr></thead><tbody>'
      + ordered.map((m) => { const dc = m.decision || {}; const best = bestSide(dc); const st = marketStatus(m); return `<tr>
          <td>${fmt.mkt(m.slug)}</td>
          <td><span class="status" data-status="${st.replace(' ', '-')}">${st}</span></td>
          <td>${m.strike ? m.strike.toLocaleString(undefined, { maximumFractionDigits: 4 }) : '—'}</td>
          <td>${fmt.mmss(m.secs_left)}</td>
          <td>${dc.p_used == null ? '—' : dc.p_used.toFixed(3)}</td>
          <td>${dc.p_market == null ? '—' : dc.p_market.toFixed(3)}</td>
          <td>${best ? (best.costs.net_edge >= 0 ? '+' : '') + best.costs.net_edge.toFixed(3) : '—'}</td>
          <td>${flowCell(dc)}</td>
          <td class="dim wrap">${fmtRegime(dc.regime)}</td>
          <td>${dc.reason ? `<span class="pending">${GATE_LABELS[dc.reason] || dc.reason}</span>`
            : (dc.side ? `<b>BUY ${dc.side}</b>` : '—')}</td>
          <td>${m.up_shares ? `<span class="side-tag" data-side="UP">UP</span> ${fmt.num(m.up_shares, 1)}`
            : m.down_shares ? `<span class="side-tag" data-side="DOWN">DOWN</span> ${fmt.num(m.down_shares, 1)}` : '—'}</td></tr>`; }).join('')
      + '</tbody>'
    : '<tbody><tr><td class="live-empty">No struck markets yet — needs 60s of price history first.</td></tr></tbody>';
}

/* Pair-lock (v2): every open cycle with its MATCHED pairs and UNMATCHED remainder side by side -- a locked P&L is
   only ever shown next to the matched quantity it belongs to; mark-to-market (executable bids) is kept apart from
   settlement P&L. */
function renderPairlock() {
  const pl = state.live.pairlock;
  const card = $('#pairlock-card');
  if (!pl || !(pl.version >= 2)) { card.hidden = true; return; }
  card.hidden = false;
  const s = pl.stats || {}, tot = pl.totals || {};
  const rc = pl.recovery || {}, lk = pl.profit_lock || {}, bl = pl.baseline || {};
  const n = (v, d = 2) => (v == null ? '—' : Number(v).toFixed(d));
  const sg = (v, d = 2) => (v == null ? '—' : (v >= 0 ? '+' : '') + Number(v).toFixed(d));
  $('#pairlock-sub').textContent = (pl.accounting_halt ? `ACCOUNTING HALT: ${pl.accounting_halt} · ` : '')
    + `${s.settled_cycles || 0} cycles settled · ${s.entry_fills || 0} entries filled · `
    + `${s.reentry_blocked || 0} re-entries blocked · ${s.pair_locked || 0} pair locks · `
    + `reconciliation ${s.recon_checked || 0} checked, ${s.recon_failures || 0} failed, max diff ${n(s.recon_max_diff, 6)}`;
  const kpis = [
    { l: 'Realised P&L', v: sg(tot.realised_pnl), n: 'settled cycles' },
    { l: 'No-hedge baseline', v: sg(bl.no_hedge_pnl), n: `first legs alone · hedging ${sg(bl.hedging_effect)}` },
    { l: 'Unrealised (MTM)', v: sg(tot.unrealised_mtm_pnl), n: 'open, at executable bids' },
    { l: 'Expected (open)', v: sg(tot.expected_pnl_open), n: 'model P(UP) x inventory - cost' },
    { l: 'Worst case (open)', v: sg(tot.worst_case_open), n: 'min(UP, DOWN) settlement' },
    { l: 'Fees / slippage', v: `${n(tot.fees)} / ${n(tot.slippage)}`, n: 'settled cycles' },
    { l: 'Capital deployed', v: n(tot.capital_deployed), n: `limit ${n(tot.capital_remaining_per_market, 0)} / market` },
  ];
  if (pl.version >= 3) {
    kpis.push(
      { l: 'Recovery hedges', v: `${rc.filled || 0} / ${rc.sent || 0}`,
        n: `filled / sent · ${rc.opportunities || 0} reversals, ${rc.candidates || 0} with an acceptable size, `
          + `${rc.rejected || 0} rejected · ${n(rc.qty, 1)} sh, partial ${rc.partial || 0}` },
      { l: 'Recovery effect', v: sg(rc.downside_reduction),
        n: `worst-case improvement · avg pair cost ${n(rc.avg_pair_cost, 3)} · settled ${rc.settled || 0} `
          + `markets ${sg(rc.settlement_pnl)} (leg ${sg(rc.leg_pnl)})` },
      { l: 'Profit locks', v: `${lk.filled || 0} / ${lk.sent || 0}`,
        n: `filled / sent · ${lk.opportunities || 0} opportunities, ${lk.rejected || 0} rejected · ${n(lk.qty, 1)} sh` },
      { l: 'Profit-lock effect', v: sg(lk.locked_profit),
        n: `locked on the pairs · settled ${lk.settled || 0} markets ${sg(lk.settlement_pnl)} (leg ${sg(lk.leg_pnl)})` });
  }
  $('#pairlock-kpis').innerHTML = kpis.map((k) => `
    <div class="kpi"><span class="kpi-label">${k.l}</span>
    <span class="kpi-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');
  const rows = pl.open || [];
  $('#pairlock-table').innerHTML = rows.length
    ? '<thead><tr><th>Market</th><th>State</th><th>UP qty</th><th>DOWN qty</th><th>Matched pairs</th>'
      + '<th>Unmatched UP</th><th>Unmatched DOWN</th><th>UP avg</th><th>DOWN avg</th><th>Pair cost</th>'
      + '<th>Pair edge (net)</th><th>Locked pair P&L</th><th>UP-res P&L</th><th>DOWN-res P&L</th>'
      + '<th>Worst case</th><th>MTM</th><th>Expected</th><th>Fees</th><th>Slip</th><th>Capital</th><th>Locked vs directional</th></tr></thead><tbody>'
      + rows.map((r) => `<tr>
          <td>${fmt.mkt(r.slug)}</td><td><b>${r.state}</b></td>
          <td>${n(r.up_qty, 1)}</td><td>${n(r.down_qty, 1)}</td><td><b>${n(r.matched_qty, 1)}</b></td>
          <td>${n(r.unmatched_up, 1)}</td><td>${n(r.unmatched_down, 1)}</td>
          <td>${n(r.up_avg_cost, 3)}</td><td>${n(r.down_avg_cost, 3)}</td><td>${n(r.pair_cost, 3)}</td>
          <td>${sg(r.pair_edge, 3)} (${sg(r.net_pair_edge, 3)})</td>
          <td>${r.matched_qty > 0 ? `${sg(r.locked_pair_pnl)} <span class="dim">on ${n(r.matched_qty, 1)} pairs</span>` : '—'}</td>
          <td>${sg(r.up_resolution_pnl)}</td><td>${sg(r.down_resolution_pnl)}</td><td>${sg(r.worst_case_pnl)}</td>
          <td>${sg(r.mark_to_market_pnl)}</td><td>${sg(r.expected_pnl)}</td>
          <td>${n(r.fees, 3)}</td><td>${sg(r.slippage, 3)}</td><td>${n(r.cash_spent)}</td>
          <td class="dim wrap">${r.locked_label || (r.matched_qty > 0 ? '' : 'all directional')}</td></tr>`).join('')
      + '</tbody>'
    : '<tbody><tr><td class="live-empty">No open pair-lock cycle.</td></tr></tbody>';
  const done = (pl.settled || []).slice().reverse().slice(0, 10);
  $('#pairlock-settled').innerHTML = done.length
    ? '<thead><tr><th>Settled market</th><th>Winner</th><th>Final state</th><th>UP / DOWN</th><th>Matched</th>'
      + '<th>Pair cost</th><th>P&L</th><th>No-hedge (first leg)</th><th>Recovery leg</th><th>Profit-lock leg</th>'
      + '<th>Pairs</th></tr></thead><tbody>'
      + done.map((r) => `<tr><td>${fmt.mkt(r.slug)}</td><td>${r.winner}</td><td>${r.state}</td>
          <td>${n(r.up_qty, 1)} / ${n(r.down_qty, 1)}</td><td>${n(r.matched_qty, 1)}</td><td>${n(r.pair_cost, 3)}</td>
          <td><b>${sg(r.pnl)}</b></td><td>${sg(r.prediction)}</td>
          <td>${r.recovery_leg == null ? sg(r.hedging) : sg(r.recovery_leg)}</td><td>${sg(r.profit_lock_leg)}</td>
          <td>${sg(r.pairs)}</td></tr>`).join('')
      + '</tbody>'
    : '';
}

function renderAgreement() {
  const ag = state.live.model_vs_market || {};
  const rows = Object.entries(ag).filter(([, v]) => v && v.n);
  $('#agreement-table').innerHTML = rows.length
    ? '<thead><tr><th scope="col">Asset</th><th scope="col">Bias</th>'
      + '<th scope="col">Mean |diff|</th><th scope="col">Sigma</th><th scope="col">Sources</th>'
      + '<th scope="col">Regime</th><th scope="col">n</th></tr></thead><tbody>'
      + rows.map(([a, v]) => `<tr><td>${a}</td>
          <td>${v.mean_bias >= 0 ? '+' : ''}${v.mean_bias.toFixed(3)}</td>
          <td>${v.mean_abs.toFixed(3)}</td>
          <td>${(state.live.sigma_bps_per_sec || {})[a] ?? '—'}</td>
          <td>${((state.live.spot_sources || {})[a] || {}).n ?? '—'}</td>
          <td class="dim wrap">${fmtRegime((state.live.regime || {})[a])}</td>
          <td>${fmt.num(v.n)}</td></tr>`).join('')
      + '</tbody>'
    : '<tbody><tr><td class="live-empty">Warming up.</td></tr></tbody>';

  const worst = rows.reduce((mx, [, v]) => Math.abs(v.mean_bias) > Math.abs(mx) ? v.mean_bias : mx, 0);
  $('#agreement-note').textContent = rows.length
    ? (Math.abs(worst) > 0.05
      ? `Our fair differs from the market's mid by ${worst.toFixed(3)} on average. A large `
        + 'one-directional gap against a liquid book is a bug in our model, not free money.'
      : 'Model and market agree closely. When mean |diff| exceeds |bias| the residual is '
        + 'scatter rather than a systematic error.')
    : '';
}

const TRADE_COLS = ['Time', 'Market', 'Side', 'Price', 'Shares', 'Cost', 'Our fair', 'Left', 'Result'];

/* Settlement polls the venue up to SETTLE_MAX_ATTEMPTS x SETTLE_RETRY_MS
   (5 x 30 s) after a window closes, so a fill is legitimately unresolved for
   up to ~150 s past its close before anything is wrong. */
const SETTLE_GRACE_MS = 180000;

/* A fill's own window close, derived from the point it was taken at. The
   ledger tail carries no market metadata, but every fill records how much of
   its window was left when it was struck, which is the same thing. */
function closeTsOf(f) {
  return f.ts + (f.secs_left || 0) * 1000;
}

function resultOf(f, s, now) {
  // a late resolution replaces the mark an abandoned window was booked at (its pnl is the
  // correction; window_pnl is the window's real result)
  if (s && s.resolved_late) {
    const w = Number(s.window_pnl || 0);
    return { text: `${w >= 0 ? 'won' : 'lost'} ${fmt.signed(w)} (late)`, won: w >= 0 };
  }
  // a settle row is authoritative whenever we have one
  if (s && s.unresolved) return { text: 'unresolved', won: null };
  // closed on the book before the oracle printed: a realised number, not a bet
  if (s && s.exited) return { text: `${s.pnl >= 0 ? 'banked' : 'cut'} ${fmt.signed(s.pnl)}`,
                              won: s.pnl >= 0 };
  if (s) return { text: s.pnl >= 0 ? `won ${fmt.signed(s.pnl)}` : `lost ${fmt.signed(s.pnl)}`,
                  won: s.pnl >= 0 };
  // no settle row: distinguish "still trading" from "closed, awaiting the
  // venue" from "closed long ago and never resolved" -- all three used to
  // render as "open", which is why resolved markets looked stuck
  const closed = closeTsOf(f);
  if (now < closed) return { text: 'open', won: null };
  if (now < closed + SETTLE_GRACE_MS) return { text: 'settling', won: null };
  return { text: 'unresolved', won: null };
}

function tradeRows() {
  const led = state.live.ledger || [];
  const settled = {};
  led.forEach((r) => { if (r.event === 'settle') settled[r.slug] = r; });
  const now = Date.now();
  return led.filter((r) => r.event === 'fill').slice().reverse().map((f) => {
    const s = settled[f.slug];
    const res = resultOf(f, s, now);
    return {
      slug: f.slug,
      sideRaw: f.side || '',
      ts: f.ts,
      time: fmt.clock(f.ts),
      market: fmt.mkt(f.slug),
      // an exit is a SELL of the same side we bought; say so rather than
      // showing a second "UP" row that looks like we doubled down
      side: f.action === 'SELL' ? `${f.side || '?'} exit` : (f.side || '—'),
      price: f.price.toFixed(3),
      shares: fmt.num(f.size, 1),
      cost: fmt.money(f.cost ?? f.price * f.size),
      fair: (f.p_used == null ? (f.fair ?? 0) : (f.side === 'UP' ? f.p_used : 1 - f.p_used)).toFixed(3),
      left: `${Math.round(f.secs_left || 0)}s`,
      result: res.text,
      won: res.won,
    };
  });
}

function renderTrades() {
  const rows = tradeRows();
  $('#trades-table').innerHTML = rows.length
    ? '<thead><tr>' + TRADE_COLS.map((h) => `<th scope="col">${h}</th>`).join('') + '</tr></thead><tbody>'
      + rows.map((r) => `<tr ${tradeRowAttrs(r.slug, r.sideRaw, r.ts)}>
          <td>${r.time}</td><td>${r.market}</td>
          <td><span class="side-tag" data-side="${r.side}">${r.side}</span></td>
          <td>${r.price}</td><td>${r.shares}</td><td>${r.cost}</td>
          <td>${r.fair}</td><td>${r.left}</td>
          <td class="${r.won === null ? '' : r.won ? 'pos' : 'neg'}">${r.result}</td></tr>`).join('')
      + '</tbody>'
    : '<tbody><tr><td class="live-empty">No trades yet.</td></tr></tbody>';

  const done = rows.filter((r) => r.won !== null);
  const st = state.live.stats || {};
  $('#trades-note').textContent = done.length
    ? `${done.length} settled fills · realised ${fmt.signed(st.realised_pnl || 0)}. `
      + 'Buying a favourite at price p only profits if the true win rate exceeds p.'
    : 'Nothing has settled yet.';
}

const GATE_LABELS = {
  OUTSIDE_TIME_WINDOW: 'Outside time window', CANNOT_LAND_IN_TIME: 'Cannot land in time',
  STALE_DATA: 'Stale data', DATA_INCONSISTENT: 'Sources disagree', NO_STRIKE: 'No strike yet',
  MODEL_NOT_READY: 'Model warming', MODEL_SANITY: 'Sanity halt', BAD_REGIME: 'Bad regime',
  NO_OFFER: 'No offer (one-sided book)', PRICE_BAND: 'Price outside band', LOW_LIQUIDITY: 'Low liquidity',
  HIGH_SLIPPAGE: 'High slippage', EDGE_TOO_SMALL: 'Edge too small', LOW_CONFIDENCE: 'Low confidence',
  ALREADY_POSITIONED: 'Already positioned', RISK_LIMIT: 'Risk limit', SIZE_ZERO: 'Size rounds to zero',
  ORDER_IN_FLIGHT: 'Order in flight', FOK_UNFILLABLE: 'FOK rejected at venue',
  MARKET_CLOSED: 'Landed after close', NO_LIQUIDITY: 'No liquidity at landing',
  REVALIDATE_HIGH_SLIPPAGE: 'Moved before send', REVALIDATE_EDGE_TOO_SMALL: 'Edge gone before send',
  outside_time_window: 'Outside time window',
  no_edge: 'No edge',
  price_outside_tradeable_band: 'Price outside band',
  fair_too_close_to_half: 'Fair too near 0.50',
  edge_inside_model_uncertainty: 'Edge inside model error',
  model_disagrees_with_market: 'Sanity halt',
  already_positioned: 'Already positioned',
  size_zero: 'Size rounds to zero',
  cannot_land_in_time: 'Cannot land in time',
  spot_too_stale: 'Spot too stale',
  book_too_stale: 'Book too stale',
  vol_not_ready: 'Vol not warmed',
  twap_not_ready: 'TWAP not warmed',
  no_spot: 'No spot',
};

function renderGates() {
  const host = $('#gate-chart');
  const rows = Object.entries(state.live.skips || {})
    .sort((a, b) => b[1] - a[1]).slice(0, 7);
  if (!rows.length) {
    host.innerHTML = '<p class="live-empty">No markets are being evaluated yet, '
      + 'so no rule has had anything to block.</p>';
    return;
  }

  const rowH = 30, m = { t: 6, r: 78, b: 6, l: 176 };
  const H = Math.max(rows.length * rowH + m.t + m.b, 110);
  const { svg, w } = mount(host, H);
  const pw = Math.max(w - m.l - m.r, 40);
  const max = Math.max(...rows.map((r) => r[1]), 1);

  rows.forEach(([k, v], i) => {
    const th = Math.min(20, rowH - 10);
    const y = m.t + i * rowH + (rowH - th) / 2;
    const len = Math.max((v / max) * pw, 1.5);

    const lab = el('text', { class: 'axis-text', x: m.l - 10, y: y + th / 2 + 4, 'text-anchor': 'end' });
    lab.textContent = GATE_LABELS[k] || k;
    svg.appendChild(lab);
    svg.appendChild(el('path', { d: barPath(m.l, y, len, th, 4), fill: css('--series-1') }));

    const vl = el('text', { class: 'value-label', x: m.l + len + 8, y: y + th / 2 + 4 });
    vl.textContent = fmt.num(v);
    svg.appendChild(vl);

    const hit = el('rect', { class: 'hit', x: m.l, y: m.t + i * rowH, width: pw + m.r, height: rowH });
    svg.appendChild(hit);
    hit.addEventListener('mousemove', (ev) => showTip(
      tipRows(GATE_LABELS[k] || k, [['Blocked', fmt.num(v)]]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });
}

function copyTrades() {
  const rows = tradeRows();
  const text = [TRADE_COLS.join(',')].concat(
    rows.map((r) => [r.time, r.market, r.side, r.price, r.shares, r.cost, r.fair, r.left, r.result]
      .map((c) => `"${String(c).replace(/"/g, '""')}"`).join(','))).join('\n');
  const btn = $('#copy-trades');
  navigator.clipboard?.writeText(text).then(
    () => { btn.textContent = 'Copied'; setTimeout(() => { btn.textContent = 'Copy as CSV'; }, 1400); },
    () => { btn.textContent = 'Copy failed'; setTimeout(() => { btn.textContent = 'Copy as CSV'; }, 1400); });
}

/* ═══════════════════ history across all runs (persisted) ═══════════════════ */

/* The live panels are per-process: a restart starts a fresh exchange at its
   --balance with an empty ledger, and stop() removes the state file. The
   trade log on disk is append-only and survives every restart, so this card
   is the continuous record -- shown whether or not a bot is running. */

function renderHistoryChart(curve) {
  const host = $('#history-chart');
  if (!curve.length) { host.innerHTML = ''; return; }
  const H = 150 * CHART_SCALE, m = { t: 30, r: 72, b: 30, l: 60 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const vals = curve.map((c) => c.cum).concat([0]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.1;
  lo -= pad; hi += pad;
  const X = (i) => m.l + (curve.length <= 1 ? pw / 2 : (i / (curve.length - 1)) * pw);
  const Y = (v) => m.t + ph - ((v - lo) / (hi - lo)) * ph;
  const color = css('--series-1');

  // round dollar gridlines, so the curve can be read without hovering
  const { ticks, decimals } = roundTicks(lo, hi, 4);
  ticks.forEach((v) => {
    if (Math.abs(v) < 1e-9) return;                   // zero gets its own, stronger line
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(v), y2: Y(v) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: Y(v) + 4, 'text-anchor': 'end' });
    lab.textContent = `${v < 0 ? '−' : ''}$${Math.abs(v).toFixed(decimals ? 2 : 0)}`;
    svg.appendChild(lab);
  });
  axisCaption(svg, m, 'Cumulative realised P&L ($), by settled trade');

  // the zero line is the one that matters here: above it the record is up
  svg.appendChild(el('line', { class: 'axis-line', x1: m.l, x2: m.l + pw, y1: Y(0), y2: Y(0) }));
  const z = el('text', { class: 'axis-text', x: m.l - 9, y: Y(0) + 4, 'text-anchor': 'end' });
  z.textContent = '$0';
  svg.appendChild(z);

  // when the record starts and where it is now, under the ends of the curve
  const first = curve[0], latest = curve[curve.length - 1];
  [[first, curve.length <= 1 ? 'middle' : 'start', X(0)],
   [latest, 'end', X(curve.length - 1)]].forEach(([c, anchor, x], i) => {
    if (i === 1 && curve.length <= 1) return;
    const lab = el('text', { class: 'axis-text', x, y: H - 10, 'text-anchor': anchor });
    lab.textContent = `${i ? 'latest' : 'first'} · ${fmt.stamp(c.ts)}`;
    svg.appendChild(lab);
  });

  const d = curve.map((c, i) => `${i ? 'L' : 'M'}${X(i)},${Y(c.cum)}`).join(' ');
  svg.appendChild(el('path', { class: 'series-line', d, stroke: color }));
  const last = curve[curve.length - 1];
  const cx = X(curve.length - 1), cy = Y(last.cum);
  svg.appendChild(el('circle', { cx, cy, r: 5, fill: color, stroke: css('--surface-1'), 'stroke-width': 2 }));
  const t = el('text', { class: 'value-label-strong', x: cx + 10, y: cy + 4 });
  t.textContent = fmt.signed(last.cum);
  svg.appendChild(t);

  curve.forEach((c, i) => {
    const hit = el('circle', { class: 'hit', cx: X(i), cy: Y(c.cum), r: 12 });
    svg.appendChild(hit);
    hit.addEventListener('mousemove', (ev) => showTip(tipRows(fmt.mkt(c.slug), [
      ['Settled', fmt.signed(c.pnl)],
      ['Cumulative', fmt.signed(c.cum)],
      ['When', fmt.clock(c.ts)],
    ]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });
}

//: paging state for the history table. Kept module-level (not in `state`)
//  because it is UI navigation, not data from the server -- pollHistory()
//  reads it to build the request and writes it back only when the server
//  had to clamp it (e.g. the operator was on a page that no longer exists).
let historyPage = 1;
const HISTORY_PAGE_SIZE = 25;

function renderHistoryPager(h) {
  const host = $('#history-pager');
  const totalPages = h.total_pages || 1;
  const page = h.page || 1;
  if ((h.total_rows || 0) <= HISTORY_PAGE_SIZE) { host.innerHTML = ''; return; }
  host.innerHTML = `
    <span class="pager-info">Page ${page} of ${totalPages} · ${fmt.num(h.total_rows || 0)} rows</span>
    <button class="btn btn-ghost btn-sm" id="history-prev" type="button" ${page <= 1 ? 'disabled' : ''}>Prev</button>
    <button class="btn btn-ghost btn-sm" id="history-next" type="button" ${page >= totalPages ? 'disabled' : ''}>Next</button>`;
  $('#history-prev').addEventListener('click', () => {
    historyPage = Math.max(1, page - 1);
    pollHistory();
  });
  $('#history-next').addEventListener('click', () => {
    historyPage = Math.min(totalPages, page + 1);
    pollHistory();
  });
}

/* One position is several events. This is the same data read as trades:
   when it was bought, when it was sold or settled, and what that cost and
   returned. The raw stream is one toggle away for anyone who wants it. */
function renderHistoryTrips(rows) {
  const t = $('#history-table');
  const head = '<thead><tr><th scope="col">Market</th><th scope="col">Side</th>'
    + '<th scope="col">Shares</th><th scope="col">Bought</th><th scope="col">Closed</th>'
    + '<th scope="col">Held</th><th scope="col">Result</th></tr></thead>';
  if (!rows.length) {
    t.innerHTML = head + '<tbody><tr><td colspan="7" class="dim">No trades yet.</td></tr></tbody>';
    return;
  }
  t.innerHTML = head + '<tbody>' + rows.map((r) => {
    const closedWord = r.status === 'open' ? 'still open'
      : r.status === 'sold' ? 'sold' : 'settled';
    const closedCell = r.closed_ts == null
      ? '<span class="dim">still open</span>'
      : `${fmt.clock(r.closed_ts)}<span class="sub">${closedWord}`
        + `${r.exit_price == null ? '' : ` @ ${fmt.cents(r.exit_price)}`}</span>`;
    const pnl = r.pnl;
    // a both-sides window reports its P&L once, on its last side's row
    const result = pnl == null
      ? (r.settled_ts != null ? '<span class="dim">in the window total</span>' : '<span class="dim">—</span>')
      : `<span class="${pnl >= 0 ? 'pos' : 'neg'}">${fmt.signed(pnl)}</span>`
        + `<span class="sub">fees ${fmt.money(r.fees || 0)}</span>`;
    return `<tr ${tradeRowAttrs(r.slug, r.side, r.bought_ts)}>
        <td>${fmt.mkt(r.slug)}${r.mode ? `<span class="sub">${r.mode}</span>` : ''}</td>
        <td><span class="side-tag" data-side="${r.side || ''}">${r.side || '—'}</span></td>
        <td>${fmt.num(r.shares, 2)}<span class="sub">${r.buys} fill${r.buys === 1 ? '' : 's'}</span></td>
        <td>${r.bought_ts == null ? '—' : fmt.clock(r.bought_ts)}
            <span class="sub">${r.entry_price == null ? '' : `@ ${fmt.cents(r.entry_price)}`}</span></td>
        <td>${closedCell}</td>
        <td>${r.hold_s == null ? '—' : fmt.dur(r.hold_s)}</td>
        <td>${result}</td>
      </tr>`;
  }).join('') + '</tbody>';
}

function renderHistoryTable(rows) {
  // rows arrive as one page, newest first, straight from the server --
  // no client-side reversing or slicing left to do.
  const t = $('#history-table');
  t.innerHTML = '<thead><tr><th scope="col">Time</th><th scope="col">Event</th>'
    + '<th scope="col">Market</th><th scope="col">Detail</th><th scope="col">Result</th></tr></thead><tbody>'
    + rows.map((r) => {
      if (r.event === 'fill') {
        // An entry and its exit are both "fill" rows for the same market and
        // side, so without naming the direction they read as the same trade
        // written twice. Older rows carry no action at all and are buys.
        const sold = String(r.action || '').toUpperCase() === 'SELL';
        return `<tr ${tradeRowAttrs(r.slug, r.side, r.ts)}><td>${fmt.clock(r.ts)}</td>`
          + `<td><span class="act ${sold ? 'act-sell' : 'act-buy'}">${sold ? 'sell' : 'buy'}</span></td>`
          + `<td>${fmt.mkt(r.slug)}</td>`
          + `<td><span class="side-tag" data-side="${r.side || ''}">${r.side || '—'}</span> `
          + `${fmt.num(r.size, 1)} sh @ ${Number(r.price).toFixed(3)}</td>`
          + `<td>${sold ? 'received' : 'cost'} ${fmt.money(Math.abs(r.cost ?? r.price * r.size))}</td></tr>`;
      }
      const pnl = Number(r.pnl || 0);
      // The venue outcome is authoritative. When Gamma had not published one
      // in time the bot settled on its own Binance-proxy TWAP; say so rather
      // than render a blank, so a basis disagreement is visible in the record.
      const venue = r.venue_up != null;
      const up = venue ? r.venue_up : r.ours_up;
      const who = up == null ? '' : (venue ? '' : ' (our TWAP)');
      // A position closed early never waits for a direction, so naming a
      // winner there would be inventing one. Say what actually happened.
      const how = r.resolved_late ? `${up ? 'UP' : 'DOWN'} won (resolved late)`
        : up != null ? `${up ? 'UP' : 'DOWN'} won${who}`
          : r.exited ? 'closed before resolution'
            : r.unresolved ? (r.corrected_late ? 'booked at the last mark; corrected when resolved'
                                               : 'no outcome yet: booked at the last mark')
              : 'outcome not recorded';
      const val = r.resolved_late
        ? `window ${fmt.signed(Number(r.window_pnl || 0))} (correction ${fmt.signed(pnl)})`
        : fmt.signed(pnl);
      const cls = (r.resolved_late ? Number(r.window_pnl || 0) : pnl) >= 0 ? 'pos' : 'neg';
      return `<tr ${tradeRowAttrs(r.slug, '', null)}><td>${fmt.clock(r.ts)}</td><td>settle</td><td>${fmt.mkt(r.slug)}</td>`
        + `<td>${how}</td>`
        + `<td class="${cls}">${val} · bal ${fmt.money(r.balance || 0)}</td></tr>`;
    }).join('') + '</tbody>';
}

/* ?view=events&money=live links the raw stream or one bot's half of it, the
   same way ?period= and ?scope= link an earnings view. */
const HIST = (() => {
  const q = new URLSearchParams(location.search);
  const view = q.get('view'), money = q.get('money');
  return {
    view: view === 'events' ? 'events' : 'trips',
    mode: ['all', 'paper', 'live'].includes(money) ? money : 'all',
  };
})();

/* Trade history, shown at once (2026-10-05): every page fetched is kept per bot / view / money filter / page and
   drawn immediately when it is asked for again, then refreshed; the neighbouring pages are fetched in the
   background, and page 1 is kept warm while another page of the dashboard is open. Only the newest request may
   draw, so a slow answer for the previous bot or page never overwrites the current one. */
const HIST_CACHE = new Map();
const HIST_CACHE_MAX = 60;
const HIST_FRESH_MS = 15000;
let histSeq = 0;
let histShown = '';

function histKey(bot, page) { return `${bot}|${HIST.view}|${HIST.mode}|${page}`; }

async function fetchHistory(bot, page, key) {
  const qs = `?page=${page}&page_size=${HISTORY_PAGE_SIZE}`
    + `&view=${HIST.view}&mode=${HIST.mode}&bot=${encodeURIComponent(bot)}`;
  const r = await authFetch(`/api/history${qs}`);
  const h = r && r.ok ? await r.json() : null;
  if (h) {
    HIST_CACHE.delete(key);
    HIST_CACHE.set(key, { h, at: Date.now() });
    if (HIST_CACHE.size > HIST_CACHE_MAX) HIST_CACHE.delete(HIST_CACHE.keys().next().value);
  }
  return h;
}

function warmHistory(bot, page) {
  const key = histKey(bot, page);
  const c = HIST_CACHE.get(key);
  if (!c || Date.now() - c.at > HIST_FRESH_MS) fetchHistory(bot, page, key).catch(() => { /* next tick */ });
}

async function pollHistory() {
  const bot = state.botId;
  const page = historyPage;
  const key = histKey(bot, page);
  if (state.page !== 'ledger') { warmHistory(bot, page); return; }
  const cached = HIST_CACHE.get(key);
  if (cached && histShown !== key) renderHistory(cached.h, key);
  const seq = ++histSeq;
  let h;
  try { h = await fetchHistory(bot, page, key); } catch { return; }
  if (seq !== histSeq || bot !== state.botId || state.page !== 'ledger') return;
  if (!h) return;
  renderHistory(h, key);
  const tp = h.total_pages || 1;
  [(h.page || 1) + 1, (h.page || 1) - 1].forEach((p) => { if (p >= 1 && p <= tp) warmHistory(bot, p); });
}

function renderHistory(h, key) {
  histShown = key;
  const card = $('#history-card');
  if (!h || (!h.settled && !h.fills)) { card.hidden = true; return; }
  card.hidden = false;
  // the server clamps an out-of-range page (e.g. after a log rotation);
  // follow it so Prev/Next keep working off the page that actually exists
  historyPage = h.page || 1;
  const pnl = h.realised_pnl || 0;
  const s = h.trips_summary || {};

  // the money filter only earns its place once two kinds of bot have written
  $('#history-mode-field').hidden = (h.modes || []).length < 2;

  $('#history-kpis').innerHTML = [
    { l: 'Realised PnL, all runs', v: fmt.signed(pnl), n: 'sum of every settlement on disk',
      cls: pnl >= 0 ? 'pos' : 'neg' },
    { l: 'Trades', v: fmt.num(s.trips || 0),
      n: `${fmt.num(h.events_total || 0)} events folded${s.open ? ` · ${s.open} open` : ''}` },
    { l: 'Won', v: s.win_rate == null ? '—' : fmt.pct(s.win_rate),
      n: `${fmt.num(s.wins || 0)}W / ${fmt.num(s.losses || 0)}L` },
    { l: 'Typical hold', v: s.median_hold_s == null ? '—' : fmt.dur(s.median_hold_s),
      n: `${fmt.num(s.sold_early || 0)} sold early · ${fmt.num(s.held_to_resolution || 0)} held` },
    { l: 'Fees paid', v: fmt.money(s.fees || 0), n: `${fmt.num(s.shares || 0, 2)} shares traded` },
  ].map((k) => `<div class="kpi"><span class="kpi-label">${k.l}</span>`
    + `<span class="kpi-value ${k.cls || ''}">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');

  renderHistoryChart(h.curve || []);
  if (h.view === 'events') renderHistoryTable(h.rows || []);
  else renderHistoryTrips(h.rows || []);
  renderHistoryPager(h);
}

function historySub(view) {
  return view === 'events'
    ? 'Every fill and settlement exactly as the bot wrote it, newest first. '
      + 'An entry and its exit are separate rows here.'
    : 'One row per position, with when it was bought and when it was sold or settled. '
      + 'A position is often entered in two fills and then exited, which is several '
      + 'events for one trade — those are folded together here.';
}

function wireHistory() {
  $$('#history-view .seg-btn').forEach((b) => b.classList.toggle('is-on', b.dataset.view === HIST.view));
  $('#history-mode').value = HIST.mode;
  $('#history-sub').textContent = historySub(HIST.view);
  $('#history-view').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-view]');
    if (!btn) return;
    HIST.view = btn.dataset.view;
    historyPage = 1;
    $$('#history-view .seg-btn').forEach((b) => b.classList.toggle('is-on', b === btn));
    $('#history-sub').textContent = historySub(HIST.view);
    pollHistory();
  });
  $('#history-mode').addEventListener('change', (ev) => {
    HIST.mode = ev.target.value;
    historyPage = 1;
    pollHistory();
  });
}

wireHistory();
pollHistory();
setInterval(pollHistory, 5000);
pollAccount();
setInterval(pollAccount, ACCOUNT_POLL_MS);

/* ═══════════════════════════════ wire up ════════════════════════════════ */

/* The chart refreshes ten times a second; the tables mostly do not change
   that often, and re-rendering a table wipes any text selection the user is
   in the middle of. So each table is rebuilt only when its own data moved. */
const sigs = {};
function changed(key, sig) {
  if (sigs[key] === sig) return false;
  sigs[key] = sig;
  return true;
}

function render(force = false) {
  if (!state.live) return;
  // Every panel below lives on the market page. Redrawing it ten times a
  // second while the operator is reading another tab costs the same CPU and
  // shows nobody anything, so the work simply does not happen.
  if (state.page !== 'market') return;
  const d = state.live;

  if (force) pickerSig = '';
  renderHero();
  renderPicker();
  renderPosition();
  if (!state.overlay && !$('#strike-panel').hidden) renderStrikeChart();   // the distance panel is hidden (the user, 2026-10-05)
  renderPriceChart();
  if (!$('#spot-panel').hidden) renderSpotChart();     // the user, 2026-10-05: the crypto price panel instead of the distance panel

  const mkSig = JSON.stringify(d.live_markets || []);
  if (force || changed('markets', mkSig)) renderMarkets();

  const agSig = JSON.stringify(d.model_vs_market || {});
  if (force || changed('agreement', agSig)) renderAgreement();

  const ledSig = (d.ledger || []).length + ':' + ((d.ledger || []).at(-1)?.ts ?? 0);
  if (force || changed('trades', ledSig)) renderTrades();

  const gateSig = JSON.stringify(d.skips || {});
  if (force || changed('gates', gateSig)) renderGates();

  const plSig = JSON.stringify(d.pairlock || null);
  if (force || changed('pairlock', plSig)) renderPairlock();

  renderOrderList(force);
}

let resizeTimer;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { render(true); refreshTrade(); }, 140);
});

$('#btn-start').addEventListener('click', () => control('start'));
$('#btn-stop').addEventListener('click', () => control('stop'));
$('#btn-restart').addEventListener('click', () => control('restart'));
$('#copy-trades').addEventListener('click', copyTrades);
$('#turns-toggle').checked = state.turns;
$('#turns-toggle').addEventListener('change', (ev) => {
  state.turns = ev.target.checked;
  try { localStorage.setItem('tpb-turns', state.turns ? '1' : '0'); } catch { /* private mode */ }
  render(true);
  if (SAVED.open) renderSavedChart(SAVED.open);
});
$('#overlay-toggle').addEventListener('change', (ev) => {
  state.overlay = ev.target.checked;
  try { localStorage.setItem('tpb-overlay', state.overlay ? '1' : '0'); } catch { /* private mode */ }
  applyOverlayLayout();
  render(true);
});
applyOverlayLayout();

initTheme();
initSession();
poll();
connectLive();                                 // push transport
// Fallback only: pollLive() returns immediately while the socket is open, so
// this costs one function call a tick until the socket is actually gone.
state.timer = setInterval(pollLive, 250);      // chart cadence (fallback)
state.botTimer = setInterval(pollBot, 3000);   // process status
$('#bot-select').addEventListener('change', (ev) => switchBot(ev.target.value));
renderBotPicker();
loadBots();
setInterval(loadBots, BOTS_POLL_MS);           // which bots exist and run

/* ══════════════════════════════════ earnings ══════════════════════════════
   Realised PnL per calendar period, from the append-only earnings log.

   Form: the job is polarity — did this day/week/month make money or lose it —
   so it is a DIVERGING column chart on a zero baseline, not a line and not a
   sequential ramp. Profit takes categorical slot 1 and loss slot 2 (the same
   two hues UP and DOWN use elsewhere); the pair validates at CVD dE 24.7 in
   light and 26.8 in dark, where green/red fails at 4.1 and is unreadable for
   the most common colour blindness. The hue is bound to the SIGN, so a filter
   that changes which periods are shown never repaints anything.

   Cumulative PnL is deliberately NOT overlaid: it is the same unit but a much
   larger magnitude, and sharing one axis would flatten every bar to nothing.
   It rides in the tooltip and in the card below instead.
   ========================================================================= */

const EARN_SPAN_HINT = { '5m': 'last 12 hours, one market per bar', hour: 'last 48 hours', day: 'last 30 days', week: 'last 26 weeks', month: 'last 12 months' };

function earnAxisMoney(v) {
  const a = Math.abs(v);
  const sign = v < 0 ? '-' : '';
  if (a >= 1000) {
    return sign + '$' + (a / 1000).toFixed(a >= 10000 ? 0 : 1).replace(/\.0$/, '') + 'k';
  }
  const s = a >= 10 ? a.toFixed(0) : a.toFixed(2).replace(/\.?0+$/, '');
  return sign + '$' + (s || '0');
}

/* Axis ticks land on round money — 0, $2.50, $5 — not on fractions of the
   data range. It also puts a gridline exactly on zero whenever the range
   crosses it, which is the one line a profit-and-loss chart is read against. */
function niceTicks(lo, hi, target = 5) {
  const span = (hi - lo) || 1;
  const mag = Math.pow(10, Math.floor(Math.log10(span / target)));
  const norm = span / target / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.floor(lo / step) * step;
  const end = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = start; v <= end + step * 1e-9; v += step) {
    ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  }
  return { ticks, lo: start, hi: end === start ? start + step : end };
}

/* a column with its far end rounded and its baseline end square */
function colPath(x, y, w, h, r, up) {
  const rr = Math.max(0, Math.min(r, w / 2, h));
  if (h <= 0.4) return `M${x},${y} h${w}`;              // a flat period still shows a tick
  return up
    ? `M${x},${y + h} V${y + rr} A${rr},${rr} 0 0 1 ${x + rr},${y}`
      + ` H${x + w - rr} A${rr},${rr} 0 0 1 ${x + w},${y + rr} V${y + h} Z`
    : `M${x},${y} H${x + w} V${y + h - rr} A${rr},${rr} 0 0 1 ${x + w - rr},${y + h}`
      + ` H${x + rr} A${rr},${rr} 0 0 1 ${x},${y + h - rr} Z`;
}

async function loadEarnings() {
  const { period, scope } = state.earn;
  try {
    const r = await fetch(`/api/earnings?period=${period}&scope=${scope}`);
    state.earn.data = await r.json();
  } catch {
    return;                                    // keep the last good view
  }
  renderEarnings();
}

function renderEarnings() {
  const d = state.earn.data;
  if (!d) return;
  const t = d.totals;

  const periodWord = { '5m': 'market', hour: 'hour', day: 'day', week: 'week', month: 'month' }[d.period];
  const kpis = [
    { l: `Earned (${EARN_SPAN_HINT[d.period]})`, v: fmt.signed(t.pnl),
      n: `${fmt.num(t.trades)} settled · ${t.periods_traded} ${periodWord}${t.periods_traded === 1 ? '' : 's'} traded` },
    { l: `Best ${periodWord}`, v: d.best ? fmt.signed(d.best.pnl) : '—',
      n: d.best ? d.best.label : 'nothing settled yet' },
    { l: `Worst ${periodWord}`, v: d.worst ? fmt.signed(d.worst.pnl) : '—',
      n: d.worst ? d.worst.label : 'nothing settled yet' },
    { l: 'Win rate', v: t.win_rate == null ? '—' : fmt.pct(t.win_rate),
      n: `${fmt.num(t.wins)}W / ${fmt.num(t.losses)}L` },
    { l: 'Per settled window', v: t.per_trade == null ? '—' : fmt.signed(t.per_trade),
      n: 'realised, after fees' },
    { l: 'All time', v: fmt.signed(t.all_time_pnl),
      n: `${fmt.num(t.all_time_trades)} windows on record` },
  ];
  $('#earnings-kpis').innerHTML = kpis.map((k) => `
    <div class="kpi"><span class="kpi-label">${k.l}</span>
    <span class="kpi-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');

  const pos = css('--series-1'), neg = css('--series-2');
  legend($('#earnings-legend'), [{ label: 'Profit', color: pos }, { label: 'Loss', color: neg }]);

  renderEarningsChart(d, pos, neg);
  renderBalanceChart(d, pos, neg);
  renderEarningsTable(d);

  const modes = (d.modes || []).join(', ');
  const scopeWord = { all: 'paper and real money together',
    paper: 'paper trading only', live: 'real money only' }[d.scope];
  $('#earnings-note').textContent = t.all_time_trades
    ? `Showing ${scopeWord}. Recorded from: ${modes || 'no runs yet'}. `
      + 'Every settled window is written to data/earnings.jsonl, which no restart clears.'
    : 'Nothing has settled yet. Each settled window appends one line to '
      + 'data/earnings.jsonl, and this card reads that file — so it keeps counting across restarts.';
}

function renderEarningsChart(d, pos, neg) {
  const host = $('#earnings-chart');
  const buckets = d.buckets || [];
  if (!buckets.length) { host.innerHTML = ''; return; }

  const H = 260 * CHART_SCALE, m = { t: 22, r: 16, b: 38, l: 60 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;

  const vals = buckets.map((b) => b.pnl);
  const rawHi = Math.max(0, ...vals), rawLo = Math.min(0, ...vals);
  // headroom for the direct labels that sit above and below the extreme bars
  const pad = ((rawHi - rawLo) || 2) * 0.12;
  const scale = niceTicks(rawLo - pad, rawHi + pad);
  const lo = scale.lo, hi = scale.hi;

  const band = pw / buckets.length;
  const barW = Math.min(24, Math.max(3, band - 2));     // the 2px surface gap
  const X = (i) => m.l + i * band + (band - barW) / 2;
  const Y = (v) => m.t + ph - ((v - lo) / (hi - lo)) * ph;
  const zero = Y(0);

  // gridlines: hairline, solid, recessive — they carry the values not labelled
  scale.ticks.forEach((v) => {
    const y = Y(v);
    if (v !== 0) {
      svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: y, y2: y }));
    }
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: y + 4, 'text-anchor': 'end' });
    lab.textContent = earnAxisMoney(v);
    svg.appendChild(lab);
  });
  // the baseline is the one line that matters: above it the period earned
  svg.appendChild(el('line', { class: 'axis-line', x1: m.l, x2: m.l + pw, y1: zero, y2: zero }));

  // x labels, thinned so they cannot collide, and the newest period always shown
  const every = Math.max(1, Math.ceil(buckets.length / 8));
  buckets.forEach((b, i) => {
    const last = i === buckets.length - 1;
    if (!last && (i % every || i > buckets.length - 1 - every / 2)) return;
    const lab = el('text', {
      class: 'axis-text', x: X(i) + barW / 2, y: H - 14,
      'text-anchor': last ? 'end' : 'middle',
    });
    lab.textContent = b.label;
    svg.appendChild(lab);
  });

  const labelled = new Set([
    d.best && d.best.key, d.worst && d.worst.key,
    buckets[buckets.length - 1].trades ? buckets[buckets.length - 1].key : null,
  ].filter(Boolean));

  buckets.forEach((b, i) => {
    const up = b.pnl >= 0;
    const h = Math.abs(Y(b.pnl) - zero);
    const y = up ? zero - h : zero;
    const colour = up ? pos : neg;
    if (b.trades) {
      svg.appendChild(el('path', { d: colPath(X(i), y, barW, h, 4, up), fill: colour }));
    }
    // direct labels, sparingly: the extremes and the current period only
    if (labelled.has(b.key) && b.trades) {
      const lab = el('text', {
        class: 'value-label-strong', x: X(i) + barW / 2,
        y: up ? y - 7 : y + h + 15, 'text-anchor': 'middle',
      });
      lab.textContent = fmt.signed(b.pnl);
      svg.appendChild(lab);
    }
    const hit = el('rect', { class: 'hit', x: m.l + i * band, y: m.t, width: band, height: ph });
    svg.appendChild(hit);
    hit.addEventListener('mousemove', (ev) => showTip(tipRows(b.label, [
      ['Realised', fmt.signed(b.pnl), b.trades ? colour : null],
      ['Running total', fmt.signed(b.cum)],
      ['Windows settled', fmt.num(b.trades)],
      ['Won / lost', `${b.wins} / ${b.losses}`],
    ]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });
}

/* The balance line: cumulative realised P&L at the END of each period (the earnings log's running total, so it
   starts from the first row ever logged, not from a deposit). A step line, because the balance only moves when a
   window settles; the segment takes the profit hue above zero and the loss hue below it, like the bars. */
function renderBalanceChart(d, pos, neg) {
  const host = $('#balance-chart');
  const buckets = (d.buckets || []).filter((b) => b.balance != null);
  const who = (d.balance_bots || []).map((b) => b.bot).join(', ');
  legend($('#balance-legend'), [{ label: who ? `Portfolio (${who})` : 'Portfolio', color: pos }]);
  const title = $('#balance-title');
  if (title) {
    title.textContent = d.balance_now == null
      ? 'Balance: no running bot in this scope to value, so there is nothing to plot'
      : `Balance · what the account is worth at the end of each period: now ${fmt.money(d.balance_now)}, walked back through the realised profit and loss`;
  }
  if (!buckets.length) { host.innerHTML = ''; return; }
  const H = 200 * CHART_SCALE, m = { t: 18, r: 16, b: 38, l: 60 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const opening = d.balance_opening == null ? buckets[0].balance : Number(d.balance_opening);
  const vals = [opening, ...buckets.map((b) => b.balance)];
  const rawHi = Math.max(0, ...vals), rawLo = Math.min(0, ...vals);
  const pad = ((rawHi - rawLo) || 2) * 0.1;
  const scale = niceTicks(rawLo - pad, rawHi + pad);
  const lo = scale.lo, hi = scale.hi;
  const band = pw / buckets.length;
  const X = (i) => m.l + (i + 1) * band;                 // the value at the END of bucket i
  const Y = (v) => m.t + ph - ((v - lo) / (hi - lo)) * ph;
  scale.ticks.forEach((v) => {
    const y = Y(v);
    svg.appendChild(el('line', { class: v === 0 ? 'axis-line' : 'grid-line', x1: m.l, x2: m.l + pw, y1: y, y2: y }));
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: y + 4, 'text-anchor': 'end' });
    lab.textContent = earnAxisMoney(v);
    svg.appendChild(lab);
  });
  const every = Math.max(1, Math.ceil(buckets.length / 8));
  buckets.forEach((b, i) => {
    const last = i === buckets.length - 1;
    if (!last && (i % every || i > buckets.length - 1 - every / 2)) return;
    const lab = el('text', { class: 'axis-text', x: X(i), y: H - 14, 'text-anchor': last ? 'end' : 'middle' });
    lab.textContent = b.label;
    svg.appendChild(lab);
  });
  // a step line: the balance moves only when a window settles; the segment takes the loss hue when the period lost
  let px = m.l, py = Y(opening), prev = opening;
  buckets.forEach((b, i) => {
    const x = X(i), y = Y(b.balance);
    svg.appendChild(el('path', { d: `M${px},${py} H${x} V${y}`, fill: 'none', stroke: b.balance >= prev ? pos : neg,
                                 'stroke-width': 2, 'stroke-linejoin': 'round' }));
    px = x; py = y; prev = b.balance;
  });
  const lastB = buckets[buckets.length - 1];
  svg.appendChild(el('circle', { cx: X(buckets.length - 1), cy: Y(lastB.balance), r: 3.5, fill: pos }));
  const endLab = el('text', { class: 'value-label-strong', x: X(buckets.length - 1) - 6, y: Y(lastB.balance) - 9, 'text-anchor': 'end' });
  endLab.textContent = fmt.money(lastB.balance);
  svg.appendChild(endLab);
  buckets.forEach((b, i) => {
    const hit = el('rect', { class: 'hit', x: m.l + i * band, y: m.t, width: band, height: ph });
    svg.appendChild(hit);
    hit.addEventListener('mousemove', (ev) => showTip(tipRows(b.label, [
      ['Balance at the end', fmt.money(b.balance)],
      ['Change in the period', b.trades ? fmt.signed(b.pnl) : 'no settlement', b.trades ? (b.pnl >= 0 ? pos : neg) : null],
      ['Windows settled', fmt.num(b.trades)],
    ]), ev));
    hit.addEventListener('mouseleave', hideTip);
  });
}

function renderEarningsTable(d) {
  const rows = (d.buckets || []).slice().reverse();
  const head = '<thead><tr><th>Period</th><th>Realised</th>'
    + '<th>Running total</th><th>Settled</th>'
    + '<th>Won</th><th>Lost</th></tr></thead>';
  const body = rows.map((b) => `<tr>
      <td>${b.label}</td>
      <td class="${b.pnl < 0 ? 'neg' : ''}">${b.trades ? fmt.signed(b.pnl) : '—'}</td>
      <td>${fmt.signed(b.cum)}</td>
      <td>${b.trades || '—'}</td>
      <td>${b.wins || '—'}</td>
      <td>${b.losses || '—'}</td>
    </tr>`).join('');
  $('#earnings-table').innerHTML = head + `<tbody>${body}</tbody>`;
}

function markSeg(sel, attr, value) {
  $$(`${sel} .seg-btn`).forEach((b) => b.classList.toggle('is-on', b.dataset[attr] === value));
}

function wireEarnings() {
  markSeg('#earn-period', 'period', state.earn.period);
  markSeg('#earn-scope', 'scope', state.earn.scope);
  $('#earn-period').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-period]');
    if (!btn) return;
    state.earn.period = btn.dataset.period;
    markSeg('#earn-period', 'period', state.earn.period);
    loadEarnings();
  });
  $('#earn-scope').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-scope]');
    if (!btn) return;
    state.earn.scope = btn.dataset.scope;
    markSeg('#earn-scope', 'scope', state.earn.scope);
    loadEarnings();
  });
}

/* ══════════════════════════════ the tuning panel ══════════════════════════
   Writes data/controls.json; a running bot re-reads it within ~2s and applies
   the whitelisted values to the very objects the strategy is using.

   A field left empty means "no override" — the bot keeps its own default, and
   the placeholder shows what that currently is. That distinction matters: an
   override equal to today's default is still an override, and would pin the
   value if the default ever changed.

   Nothing here can arm real money. Starting an armed bot stays a command-line
   act, so a stray click on a local web page can never begin spending.
   ========================================================================= */

const CTRL = { spec: [], startArgs: {}, stored: {}, edits: {}, kill: false, loaded: false };

const ctrlScope = () => $('#ctrl-scope').value;
const ctrlStored = () => CTRL.stored[ctrlScope()] || {};
const ctrlRunning = () => (state.live && state.live.controls && state.live.controls.values) || {};

async function loadControls() {
  try {
    const r = await fetch('/api/controls');
    const d = await r.json();
    CTRL.spec = d.spec || [];
    CTRL.startArgs = d.start_args || {};
    CTRL.stored = d.stored || {};
    CTRL.kill = !!d.kill_active;
    CTRL.killFile = d.kill_file || 'data/KILL';
    CTRL.loaded = true;
  } catch {
    return;
  }
  renderControls();
}

function ctrlShown(key) {
  if (key in CTRL.edits) return CTRL.edits[key];
  const stored = ctrlStored();
  return key in stored ? stored[key] : null;       // null = no override
}

function ctrlFmt(row, v) {
  if (v == null) return '—';
  if (row.kind === 'bool') return v ? 'on' : 'off';
  if (row.kind === 'list') return v === '' ? 'none' : String(v);
  return String(v);
}

const ctrlStrategy = () => (state.live && state.live.strategy) || '';

/* Why a value does nothing for the bot on screen, or '' when it matters. The
   engine's shared settings are mostly for the older strategies; the spot-lag
   ones exist only on a spot-lag bot, and the real-money envelope only on a
   real-money one. Greying them out beats letting someone tune a dead knob. */
/* Only the settings the selected bot reads are listed (the user, 2026-10-05); "Show all settings" lists every one,
   the unused greyed out, as before. Remembered per browser. With no bot running everything is shown. */
function ctrlShowAll() {
  try { return localStorage.getItem('tpb-ctrl-showall') === '1'; } catch { return false; }
}
function ctrlHidden(row, running) {
  return !ctrlShowAll() && Object.keys(running).length > 0 && !!ctrlUnused(row, running) && !(row.key in CTRL.edits);
}

function ctrlUnused(row, running) {
  if (!Object.keys(running).length) return '';
  const strat = ctrlStrategy();
  if (strat && (row.not_for || []).includes(strat)) return `The ${strat} strategy does not read this.`;
  if (!(row.key in running)) {
    return row.applies_to === 'live' ? 'Only a real-money bot has this.' : 'This bot does not have this setting.';
  }
  return '';
}

function ctrlRunTxt(row, running) {
  if (!Object.keys(running).length) return 'No bot running';
  return ctrlUnused(row, running) || `Running: <b>${ctrlFmt(row, running[row.key])}</b>`;
}

function ctrlInput(row, shown, live) {
  const attrs = `data-key="${row.key}" data-kind="${row.kind}" aria-label="${row.label}"`;
  if (row.kind === 'bool' || row.kind === 'choice') {
    // a select, not a checkbox, so an override can be taken away again
    const opts = row.kind === 'bool' ? ['on', 'off'] : (row.options || []);
    const cur = shown == null ? '' : (row.kind === 'bool' ? (shown ? 'on' : 'off') : String(shown));
    return `<select ${attrs}>
        <option value=""${cur === '' ? ' selected' : ''}>not overridden</option>
        ${opts.map((o) => `<option value="${o}"${cur === o ? ' selected' : ''}>${o}</option>`).join('')}
      </select>`;
  }
  if (row.kind === 'list') {
    const val = shown == null ? '' : (shown === '' ? 'none' : shown);
    const ph = live == null ? 'not overridden' : (live === '' ? 'none' : live);
    return `<input type="text" ${attrs} value="${val}" placeholder="${ph}" autocomplete="off" spellcheck="false">`;
  }
  return `<input type="number" ${attrs} min="${row.lo}" max="${row.hi}" step="${row.step}"
                 value="${shown == null ? '' : shown}" placeholder="${live == null ? 'default' : live}">`;
}

function renderControls() {
  if (!CTRL.loaded) return;
  const stored = ctrlStored();
  const running = ctrlRunning();
  const groups = [];
  CTRL.spec.forEach((row) => {
    let g = groups.find((x) => x.name === row.group);
    if (!g) { g = { name: row.group, rows: [], liveOnly: true }; groups.push(g); }
    g.rows.push(row);
    if (row.applies_to !== 'live') g.liveOnly = false;
  });

  const showAllBox = $('#ctrl-showall');
  if (showAllBox) showAllBox.checked = ctrlShowAll();
  const botName = ($('#bot-select') && $('#bot-select').selectedOptions[0] && $('#bot-select').selectedOptions[0].textContent) || 'this bot';
  $('#ctrl-groups').innerHTML = groups.map((g, gi) => {
    const n = g.rows.filter((r) => r.key in stored || r.key in CTRL.edits).length;
    const allUnused = g.rows.every((r) => ctrlUnused(r, running));
    const groupHidden = g.rows.every((r) => ctrlHidden(r, running));
    const fields = g.rows.map((row) => {
      const shown = ctrlShown(row.key);
      const dirty = row.key in CTRL.edits;
      const unused = ctrlUnused(row, running);
      return `<div class="ctrl-row${dirty ? ' is-dirty' : ''}${unused ? ' is-unused' : ''}" data-row="${row.key}"${ctrlHidden(row, running) ? ' hidden' : ''}>
          <div class="ctrl-row-head">
            <span class="ctrl-label">${row.label}</span>
            ${row.unit ? `<span class="ctrl-unit">${row.unit}</span>` : ''}
          </div>
          ${ctrlInput(row, shown, running[row.key])}
          <span class="ctrl-running">${ctrlRunTxt(row, running)}</span>
          <span class="ctrl-help">${row.help}</span>
        </div>`;
    }).join('');
    return `<details class="ctrl-group" data-group="${gi}"${gi === 0 ? ' open' : ''}${g.liveOnly ? ' data-live-only="1"' : ''}${groupHidden ? ' hidden' : ''}>
        <summary>${g.name}${n ? `<span class="ctrl-count">${n} set</span>` : ''}<span class="ctrl-unused-note"${allUnused ? '' : ' hidden'}>not used by this bot</span></summary>
        <div class="ctrl-grid">${fields}</div>
      </details>`;
  }).join('');
  const shownGroups = groups.filter((g) => !g.rows.every((r) => ctrlHidden(r, running))).length;
  if (shownGroups < groups.length) {
    $('#ctrl-groups').insertAdjacentHTML('beforeend',
      `<p class="ctrl-filter-note">${groups.length - shownGroups} setting group${groups.length - shownGroups === 1 ? '' : 's'} `
      + `not used by ${botName} ${groups.length - shownGroups === 1 ? 'is' : 'are'} hidden. Tick “Show all settings” to see them.</p>`);
  }

  const dirty = Object.keys(CTRL.edits).length;
  $('#ctrl-apply').disabled = !dirty;
  $('#ctrl-revert').disabled = !dirty;
  const killBtn = $('#ctrl-kill');
  killBtn.setAttribute('aria-pressed', CTRL.kill ? 'true' : 'false');
  killBtn.textContent = CTRL.kill ? 'Orders halted — resume' : 'Halt orders';

  const c = (state.live && state.live.controls) || null;
  const bits = [];
  if (CTRL.kill) bits.push(`Every order is being refused while ${CTRL.killFile} exists.`);
  if (c && c.applied_at) {
    bits.push(`The ${c.scope} bot last applied: ${c.applied.join('; ')}`
      + ` (${fmt.clock(c.applied_at * 1000)}).`);
  }
  if (c && c.errors && c.errors.length) bits.push(`Bot reported: ${c.errors.join('; ')}`);
  const r = c && c.restart_only;
  if (r) {
    bits.push(`Fixed until the bot restarts: ${r.mode}, strategy ${r.strategy}, assets ${r.assets || 'all listed'}, `
      + `${r.order_lanes} order lane${r.order_lanes === 1 ? '' : 's'}`
      + `${r.book_skip_assets ? `, book feed skips ${r.book_skip_assets}` : ''}.`);
  }
  bits.push('Bankroll, assets and spot exchanges are start-time arguments: '
    + 'change them in the header and press Restart. Removing an override returns that value '
    + 'to what the bot started with.');
  $('#ctrl-foot').textContent = bits.join(' ');
}

function ctrlStatus(msg, kind = 'ok') {
  const s = $('#ctrl-status');
  s.textContent = msg || '';
  s.dataset.kind = kind;
}

function wireControls() {
  $('#ctrl-groups').addEventListener('input', (ev) => {
    const input = ev.target.closest('[data-key]');
    if (!input) return;
    const key = input.dataset.key;
    const kind = input.dataset.kind;
    const raw = input.value.trim();
    if (raw === '') {
      delete CTRL.edits[key];                    // emptied: drop the override
    } else if (kind === 'bool') {
      CTRL.edits[key] = raw === 'on';
    } else if (kind === 'choice' || kind === 'list') {
      CTRL.edits[key] = raw;                     // "none" is an empty list, checked by the server
    } else {
      const n = Number(raw);
      if (Number.isFinite(n)) CTRL.edits[key] = n;
    }
    input.closest('.ctrl-row').classList.add('is-dirty');
    $('#ctrl-apply').disabled = false;
    $('#ctrl-revert').disabled = false;
    ctrlStatus('');
  });

  $('#ctrl-showall').addEventListener('change', (ev) => {
    try { localStorage.setItem('tpb-ctrl-showall', ev.target.checked ? '1' : '0'); } catch { /* private window */ }
    renderControls();
  });
  $('#ctrl-scope').addEventListener('change', () => {
    CTRL.edits = {};
    ctrlStatus('');
    renderControls();
  });

  $('#ctrl-revert').addEventListener('click', () => {
    CTRL.edits = {};
    ctrlStatus('');
    renderControls();
  });

  $('#ctrl-apply').addEventListener('click', async () => {
    const scope = ctrlScope();
    const values = { ...CTRL.edits };
    // an emptied field must be removed from the stored section, which the
    // server does by rewriting the scope with what is left
    const keep = { ...ctrlStored(), ...values };
    Object.keys(ctrlStored()).forEach((k) => {
      const input = $(`[data-key="${k}"]`);
      if (input && input.value.trim() === '') delete keep[k];
    });
    // raising a real-money limit is never one stray click: say what goes up, and ask
    if (scope !== 'paper') {
      const rows = Object.fromEntries(CTRL.spec.map((row) => [row.key, row]));
      const running = ctrlRunning();
      const stored = ctrlStored();
      const raised = Object.entries(values).filter(([k, v]) => {
        if (!rows[k] || !rows[k].ceiling) return false;
        const ref = running[k] != null ? running[k] : stored[k];
        return ref == null || Number(v) > Number(ref);
      });
      if (raised.length) {
        const lines = raised.map(([k, v]) => {
          const ref = running[k] != null ? running[k] : stored[k];
          return `• ${rows[k].label}: ${ref == null ? 'start value' : ref} → ${v}`;
        });
        if (!window.confirm(`This raises real-money limits:\n${lines.join('\n')}\n\nSave it?`)) {
          ctrlStatus('Not saved.', 'warn');
          return;
        }
      }
    }
    $('#ctrl-apply').disabled = true;
    try {
      const res = await post('/api/controls', { scope, values: keep, replace: true });
      CTRL.stored = res.stored || CTRL.stored;
      CTRL.kill = !!res.kill_active;
      CTRL.edits = {};
      const warn = (res.warnings || []).join(' ');
      const err = (res.errors || []).join(' ');
      if (err) ctrlStatus(err, 'error');
      else if (warn) ctrlStatus(`Saved, but check this: ${warn}`, 'warn');
      else ctrlStatus(`Saved. A running bot picks this up within a couple of seconds.`, 'ok');
      renderControls();
    } catch (e) {
      ctrlStatus('Could not reach the server: ' + e, 'error');
      $('#ctrl-apply').disabled = false;
    }
  });

  $('#ctrl-reset').addEventListener('click', async () => {
    const scope = ctrlScope();
    try {
      const res = await post('/api/controls', { scope, reset: true });
      CTRL.stored = res.stored || CTRL.stored;
      CTRL.edits = {};
      ctrlStatus('Overrides cleared. A bot already running keeps the values it '
        + 'has until you restart it.', 'warn');
      renderControls();
    } catch (e) {
      ctrlStatus('Could not reach the server: ' + e, 'error');
    }
  });

  $('#ctrl-kill').addEventListener('click', async () => {
    try {
      const res = await post('/api/kill', { active: !CTRL.kill });
      CTRL.kill = !!res.kill_active;
      ctrlStatus(CTRL.kill
        ? 'Halted. Every order is refused until you resume; positions already open are untouched.'
        : 'Resumed. Orders can be sent again.', CTRL.kill ? 'warn' : 'ok');
      renderControls();
    } catch (e) {
      ctrlStatus('Could not reach the server: ' + e, 'error');
    }
  });
}

/* The tuning panel shows each value the running bot is actually using. That
   arrives with the 200ms state tick, but rebuilding the form that often would
   steal focus mid-keystroke and wipe an unsaved edit, so only the running
   figures are patched in place. */
function refreshControlRunning() {
  const running = ctrlRunning();
  const rows = Object.fromEntries(CTRL.spec.map((row) => [row.key, row]));
  $$('.ctrl-row').forEach((rowEl) => {
    const row = rows[rowEl.dataset.row];
    if (!row) return;
    const span = rowEl.querySelector('.ctrl-running');
    if (span) span.innerHTML = ctrlRunTxt(row, running);
    rowEl.classList.toggle('is-unused', !!ctrlUnused(row, running));
    rowEl.hidden = ctrlHidden(row, running);
    const input = rowEl.querySelector('input');
    const v = running[row.key];
    if (input && v != null) {
      if (input.type === 'number') input.placeholder = String(v);
      if (input.type === 'text') input.placeholder = v === '' ? 'none' : String(v);
    }
  });
  $$('.ctrl-group').forEach((g) => {
    const keys = [...g.querySelectorAll('.ctrl-row')].map((el) => el.dataset.row);
    const note = g.querySelector('.ctrl-unused-note');
    if (note) note.hidden = !keys.every((k) => rows[k] && ctrlUnused(rows[k], running));
    g.hidden = keys.length > 0 && keys.every((k) => rows[k] && ctrlHidden(rows[k], running));
  });
  const groups = $$('.ctrl-group');
  const hiddenGroups = groups.filter((g) => g.hidden).length;
  const note = $('#ctrl-groups .ctrl-filter-note');
  const botName = ($('#bot-select') && $('#bot-select').selectedOptions[0] && $('#bot-select').selectedOptions[0].textContent) || 'this bot';
  const text = `${hiddenGroups} setting group${hiddenGroups === 1 ? '' : 's'} not used by ${botName} `
    + `${hiddenGroups === 1 ? 'is' : 'are'} hidden. Tick “Show all settings” to see them.`;
  if (hiddenGroups && note) note.textContent = text;
  else if (hiddenGroups && groups.length) $('#ctrl-groups').insertAdjacentHTML('beforeend', `<p class="ctrl-filter-note">${text}</p>`);
  else if (!hiddenGroups && note) note.remove();
}

wireEarnings();
wireControls();
wireBuyPanel();
loadEarnings();
loadControls();
// The earnings log only changes when a window settles, so a slow refresh is
// plenty; the controls file changes when another tab or a hand edit writes it.
setInterval(loadEarnings, 60000);
setInterval(refreshControlRunning, 2000);

/* ═══════════════════════════════ page router ══════════════════════════════
   One document, five pages, switched on the hash. Everything stays on one
   connection and one state file; what changes is which panels are in the DOM's
   way and, more importantly, which work runs at all.

   The market page redraws ten times a second. Leaving that running while you
   read the ledger would burn the same CPU for nothing, so the per-tick render
   and the two slow polls are gated on the page actually being open. Switching
   to a page refreshes it immediately, so nothing is ever stale on arrival.
   ========================================================================= */

const PAGES = ['market', 'earnings', 'ledger', 'saved', 'variables'];

function pageFromHash() {
  const h = (location.hash || '').replace(/^#/, '').split('?')[0];
  return PAGES.includes(h) ? h : 'market';
}

function showPage(name, { push = false } = {}) {
  state.page = PAGES.includes(name) ? name : 'market';
  $$('.page').forEach((p) => { p.hidden = p.dataset.page !== state.page; });
  $$('.tab').forEach((a) => {
    const on = a.dataset.page === state.page;
    a.classList.toggle('is-on', on);
    a.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if (push && location.hash !== `#${state.page}`) location.hash = state.page;
  try { localStorage.setItem('tpb-page', state.page); } catch { /* private mode */ }

  // arriving at a page refreshes it rather than waiting for its next tick
  if (state.page === 'market') render(true);
  if (state.page === 'earnings') loadEarnings();
  if (state.page === 'ledger') { pollHistory(); pollAccount(); }
  if (state.page === 'saved') loadSaved();
  if (state.page === 'variables') { loadControls(); refreshControlRunning(); }
  // ?window=<slug> deep-links one archived window
  const want = new URLSearchParams(location.search).get('window');
  if (state.page === 'saved' && want && (!SAVED.open || SAVED.open.slug !== want)) openSaved(want);
  scrollTo({ top: 0, behavior: 'instant' in document.documentElement.style ? 'instant' : 'auto' });
}

function wireRouter() {
  addEventListener('hashchange', () => showPage(pageFromHash()));
  const initial = location.hash
    ? pageFromHash()
    : (() => { try { return localStorage.getItem('tpb-page') || 'market'; } catch { return 'market'; } })();
  showPage(initial);
}

/* ═════════════════════════════ saved markets ══════════════════════════════
   The archive in data/charts: one JSON per closed window holding its whole
   200ms sample path, plus the PNG the dashboard rendered at the time.

   The list is served from a cached summary index, so paging through 876
   windows never touches the 175 MB of sample points. The PNG is the thumbnail
   because it already exists and costs nothing to show; opening a window
   fetches that one JSON and redraws it live, which a picture cannot do.
   ========================================================================= */

const SAVED = { data: null, page: 1, open: null, busy: false };

function savedQuery() {
  const p = new URLSearchParams({
    page: String(SAVED.page),
    asset: $('#saved-asset').value,
    outcome: $('#saved-outcome').value,
    traded: $('#saved-traded').value,
    sort: $('#saved-sort').value,
    q: $('#saved-q').value.trim(),
  });
  return p.toString();
}

async function loadSaved(resetPage = false) {
  if (resetPage) SAVED.page = 1;
  if (SAVED.busy) return;
  SAVED.busy = true;
  try {
    SAVED.data = await fetch(`/api/saved?${savedQuery()}`).then((r) => r.json());
  } catch {
    return;
  } finally {
    SAVED.busy = false;
  }
  renderSaved();
}

function renderSaved() {
  const d = SAVED.data;
  if (!d) return;
  SAVED.page = d.page || 1;
  const s = d.stats || {};

  const count = $('#tab-saved-count');
  if (count) count.textContent = fmt.num(s.windows || 0);

  $('#saved-kpis').innerHTML = [
    { l: 'Windows kept', v: fmt.num(s.windows || 0), n: `${(s.assets || []).length} assets` },
    { l: 'On disk', v: fmt.bytes(s.bytes || 0),
      n: `${fmt.num(s.images || 0)} with an image`
        + (state.saveCharts ? '' : ' · saving paused') },
    { l: 'We traded', v: fmt.num(s.traded || 0),
      n: s.windows ? `${fmt.pct((s.traded || 0) / s.windows)} of them` : '—' },
    { l: 'Realised on those', v: fmt.signed(s.pnl || 0),
      n: s.win_rate == null ? 'nothing settled' : `${s.wins}W / ${s.losses}L` },
    { l: 'Resolved Up', v: s.up_share == null ? '—' : fmt.pct(s.up_share),
      n: 'of decided windows' },
  ].map((k) => `<div class="kpi"><span class="kpi-label">${k.l}</span>`
    + `<span class="kpi-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');

  // the asset filter is built from what is actually on disk
  const sel = $('#saved-asset');
  const want = ['', ...(d.all_assets || [])];
  if (sel.options.length !== want.length) {
    const cur = sel.value;
    sel.innerHTML = want.map((a) => `<option value="${a}">${a || 'All'}</option>`).join('');
    sel.value = cur;
  }

  const rows = d.rows || [];
  $('#saved-gallery').innerHTML = rows.length ? rows.map((r) => {
    const outcome = r.outcome ? r.outcome.toUpperCase() : null;
    const badge = outcome
      ? `<span class="pill pill-${outcome === 'UP' ? 'up' : 'down'}">${outcome}</span>`
      : '<span class="pill">unresolved</span>';
    const pnl = r.traded
      ? `<span class="${r.pnl >= 0 ? 'pos' : 'neg'}">${fmt.signed(r.pnl)}</span>`
      : '<span class="dim">watched</span>';
    const thumb = r.has_png
      ? `<img loading="lazy" src="/charts/${r.slug}.png" alt="">`
      : '<div class="thumb-none">no image</div>';
    return `<button class="gcard" type="button" data-slug="${r.slug}">
        <div class="gcard-thumb">${thumb}</div>
        <div class="gcard-body">
          <div class="gcard-top"><strong>${r.asset}</strong>${badge}</div>
          <div class="gcard-when">${fmt.stamp(r.close_ts)}</div>
          <div class="gcard-foot">
            <span>${r.move_bps == null ? '—' : (r.move_bps >= 0 ? '+' : '') + fmt.num(r.move_bps, 0) + ' bps'}</span>
            ${pnl}
          </div>
        </div>
      </button>`;
  }).join('') : '<p class="empty-hint">No saved window matches these filters.</p>';

  const p = $('#saved-pager');
  p.innerHTML = `<span class="pager-info">${fmt.num(d.total_rows)} windows · page ${d.page} of ${d.total_pages}</span>
    <button class="btn btn-sm" type="button" id="saved-prev" ${d.page <= 1 ? 'disabled' : ''}>Previous</button>
    <button class="btn btn-sm" type="button" id="saved-next" ${d.page >= d.total_pages ? 'disabled' : ''}>Next</button>`;
  $('#saved-prev').onclick = () => { SAVED.page = Math.max(1, SAVED.page - 1); loadSaved(); };
  $('#saved-next').onclick = () => { SAVED.page = SAVED.page + 1; loadSaved(); };
}

async function openSaved(slug) {
  let doc;
  try {
    doc = await fetch(`/api/saved/${slug}`).then((r) => r.json());
  } catch {
    notice(`Could not read the saved window ${slug}`);
    return;
  }
  if (!doc || doc.error) { notice(`No saved window called ${slug}`); return; }
  SAVED.open = doc;
  // make the open window linkable without reloading the page
  try {
    const u = new URL(location.href);
    u.searchParams.set('window', slug);
    history.replaceState(null, '', u);
  } catch { /* older browsers: the view still works, the link just is not updated */ }
  const card = $('#saved-detail');
  card.hidden = false;

  const outcome = (doc.outcome || '').toUpperCase();
  $('#saved-detail-eyebrow').textContent = `${doc.asset || ''} · ${outcome || 'unresolved'}`
    + (doc.outcome_source ? ` · graded by the ${doc.outcome_source}` : '');
  $('#saved-detail-title').textContent = doc.question || fmt.mkt(doc.slug || slug);
  $('#saved-detail-sub').textContent =
    `${fmt.stamp(doc.open_ts)} → ${fmt.stamp(doc.close_ts)} · ${(doc.points || []).length} samples`;
  $('#saved-detail-json').href = `/api/saved/${slug}`;
  $('#saved-detail-json').setAttribute('download', `${slug}.json`);

  const fills = doc.fills || [];
  const shares = fills.reduce((a, f) => a + Math.abs(Number(f.size) || 0), 0);
  const fees = fills.reduce((a, f) => a + (Number(f.fee) || 0), 0);
  const pts = doc.points || [];
  const lastSpot = [...pts].reverse().find((p) => typeof p.spot === 'number');
  const move = (lastSpot && doc.strike) ? (lastSpot.spot - doc.strike) / doc.strike * 1e4 : null;
  $('#saved-detail-kpis').innerHTML = [
    { l: 'Strike', v: fmtSpot(doc.strike || 0), n: '60s TWAP at the open' },
    { l: 'Closed at', v: lastSpot ? fmtSpot(lastSpot.spot) : '—',
      n: move == null ? '' : `${move >= 0 ? '+' : ''}${fmt.num(move, 1)} bps from strike` },
    { l: 'Our position', v: fills.length ? `${fmt.num(shares, 2)} sh` : 'none',
      n: fills.length ? `${fills.length} fill${fills.length === 1 ? '' : 's'} · fees ${fmt.money(fees)}` : 'watched only' },
    { l: 'Realised', v: fills.length ? fmt.signed(doc.pnl || 0) : '—',
      n: fills.length ? 'after fees' : 'nothing at risk' },
  ].map((k) => `<div class="kpi"><span class="kpi-label">${k.l}</span>`
    + `<span class="kpi-value">${k.v}</span><span class="kpi-note">${k.n}</span></div>`).join('');

  renderSavedChart(doc);
  renderSavedFills(doc);
  $('#saved-detail-foot').textContent =
    `Fee schedule ${((doc.fee || {}).rate ?? 0) * 100}% · spot from ${(doc.exchanges || []).join(', ') || 'n/a'}`
    + ` · saved as data/charts/${slug}.json`;
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderSavedChart(doc) {
  const host = $('#saved-detail-chart');
  const pts = (doc.points || []).filter(inWindow);
  if (!pts.length) { host.innerHTML = '<p class="empty-hint">This window has no sample path.</p>'; return; }

  const H = 280 * CHART_SCALE, m = { t: 30, r: 54, b: 34, l: 52 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const { X } = fixedAxis(svg, m, pw, H);
  const Y = (v) => m.t + ph - v * ph;                   // probabilities: a fixed 0..1
  const up = css('--series-1'), down = css('--series-2');

  for (let k = 0; k <= 4; k++) {
    const v = k / 4, y = Y(v);
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: y, y2: y }));
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: y + 4, 'text-anchor': 'end' });
    lab.textContent = v.toFixed(2);
    svg.appendChild(lab);
  }
  axisCaption(svg, m, 'Share price ($) · band = bid to ask');
  spreadBand(svg, pts, X, Y, 'ub', 'ua', up);
  spreadBand(svg, pts, X, Y, 'db', 'da', down);

  [['up', up, 'Up'], ['down', down, 'Down']].forEach(([key, colour]) => {
    const d = pts.filter((p) => typeof p[key] === 'number')
      .map((p, i) => `${i ? 'L' : 'M'}${X(p)},${Y(p[key])}`).join(' ');
    if (d) svg.appendChild(el('path', { class: 'series-line', d, stroke: colour }));
  });

  (doc.fills || []).forEach((f) => {
    const left = typeof f.left === 'number' ? f.left
      : (doc.close_ts && f.ts ? (doc.close_ts - f.ts) / 1000 : null);
    if (left == null) return;
    const side = String(f.tag || f.side || '').toUpperCase().includes('DOWN') ? 'down' : 'up';
    const price = Number(f.price);
    if (!Number.isFinite(price)) return;
    const cx = X({ left }), cy = Y(price);
    svg.appendChild(el('circle', {
      cx, cy, r: 5, fill: side === 'up' ? up : down,
      stroke: css('--surface-1'), 'stroke-width': 2,
    }));
  });

  const turns = state.turns ? tokenTurns(pts) : [];
  const items = [{ label: 'Up', color: up }, { label: 'Down', color: down }];
  if (turns.length) turnsLegend(items, 'Up token');
  shocksLegend(items, doc.shocks);
  legend($('#saved-detail-legend'), items);

  pts.forEach((p) => {
    const hit = el('circle', { class: 'hit', cx: X(p), cy: m.t + ph / 2, r: 0 });
    svg.appendChild(hit);
  });
  const overlay = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(overlay);
  overlay.addEventListener('mousemove', (ev) => {
    const p = nearestByX(pts, ev, svg, m, pw);
    if (!p) return;
    showTip(tipRows(tipTime(p), [
      ['Up', p.up == null ? '—' : `${fmt.cents(p.up)} · ${bidAsk(p.ub, p.ua)}`, up],
      ['Down', p.down == null ? '—' : `${fmt.cents(p.down)} · ${bidAsk(p.db, p.da)}`, down],
      ['Spot', p.spot == null ? '—' : fmtSpot(p.spot)],
    ]), ev);
  });
  overlay.addEventListener('mouseleave', hideTip);
  // above the hover layer, so each marker keeps its own tooltip
  drawShocks(svg, doc.shocks || [], pts, X, (p) => Y(p.up));
  drawTurns(svg, turns, X, (p) => Y(p.up), 'Up token', (v) => v.toFixed(3),
            (l) => `${l >= 0 ? '+' : ''}${(l * 100).toFixed(1)}c`);
}

function renderSavedFills(doc) {
  const fills = doc.fills || [];
  const host = $('#saved-detail-fills');
  if (!fills.length) { host.innerHTML = ''; return; }
  host.innerHTML = '<table class="data-table"><thead><tr><th>Side</th><th>Shares</th>'
    + '<th>Price</th><th>Fee</th><th>Cost</th></tr></thead><tbody>'
    + fills.map((f) => `<tr>
        <td>${f.tag || f.side || '—'}</td>
        <td>${fmt.num(Number(f.size) || 0, 2)}</td>
        <td>${fmt.cents(Number(f.price) || 0)}</td>
        <td>${fmt.money(Number(f.fee) || 0)}</td>
        <td>${fmt.money((Number(f.price) || 0) * (Number(f.size) || 0))}</td>
      </tr>`).join('') + '</tbody></table>';
}

function wireSaved() {
  ['#saved-asset', '#saved-outcome', '#saved-traded', '#saved-sort'].forEach((sel) => {
    $(sel).addEventListener('change', () => loadSaved(true));
  });
  let typing;
  $('#saved-q').addEventListener('input', () => {
    clearTimeout(typing);
    typing = setTimeout(() => loadSaved(true), 250);
  });
  $('#saved-rescan').addEventListener('click', () => loadSaved());
  const toggle = $('#save-charts-toggle');
  toggle.checked = state.saveCharts;
  toggle.addEventListener('change', (ev) => {
    state.saveCharts = ev.target.checked;
    try { localStorage.setItem('tpb-save-charts', state.saveCharts ? '1' : '0'); }
    catch { /* private mode */ }
    notice(state.saveCharts
      ? 'Saving a chart image for each window as it closes.'
      : 'Not saving new chart images. Windows already saved are kept, and the '
        + 'bot still records every window to data/charts as JSON.', false);
    renderSaved();
  });
  $('#saved-gallery').addEventListener('click', (ev) => {
    const card = ev.target.closest('[data-slug]');
    if (card) openSaved(card.dataset.slug);
  });
  $('#saved-detail-close').addEventListener('click', () => {
    $('#saved-detail').hidden = true;
    SAVED.open = null;
    try {
      const u = new URL(location.href);
      u.searchParams.delete('window');
      history.replaceState(null, '', u);
    } catch { /* nothing to undo */ }
  });
}

/* The tab badge should say how many windows are kept before you have opened
   the page. One row is enough to learn the total, and the index is cached, so
   this costs a single cheap request on load. */
async function loadSavedCount() {
  try {
    const d = await fetch('/api/saved?page_size=1').then((r) => r.json());
    const el = $('#tab-saved-count');
    if (el && d && d.stats) el.textContent = fmt.num(d.stats.windows || 0);
  } catch { /* the badge simply stays empty */ }
}

/* ═══════════════════════════════ one trade ════════════════════════════════
   Click a trades or ledger row: the window that trade lived in, drawn with
   everything the bot did on it -- each order at its limit price when it was
   sent, the dashed line to where and when it filled, and the buy and sell
   fills from the trade log -- beside the order list itself.

   A window that is still running is re-read once a second, so an order joins
   the list when it is created and changes state as it fills or is killed. An
   archived window can no longer move; it is re-read slowly, and only for a
   late settlement, with the server sending no points ("keep yours").
   ========================================================================= */

const TRADE = {
  key: null,          // { slug, side, ts, bot } of the open dialog
  data: null,         // the last /api/trade answer, points accumulated
  timer: null,
  inFlight: false,
  seen: new Map(),    // order id -> outcome last drawn, to flash a row that changed
  sigs: {},
  hoverT: null,
  //: the chart's span: 'trade' fits the orders and fills, 'window' is all five
  //  minutes. A trade that lasted seven seconds is a sliver of a window.
  zoom: (() => { try { return localStorage.getItem('tpb-trade-zoom') === 'window' ? 'window' : 'trade'; } catch { return 'trade'; } })(),
};
const TRADE_LIVE_MS = 1000;
const TRADE_IDLE_MS = 15000;
const KIND_LABEL = {
  entry: 'entry', take_profit: 'take profit', stop_loss: 'stop loss',
  time_stop: 'time stop', momentum_reversal: 'reversal',
};
const kindLabel = (k) => KIND_LABEL[k] || String(k || '').toLowerCase().replace(/_/g, ' ');
const isFilled = (o) => o.outcome === 'filled' || o.outcome === 'partial';
const num3 = (v) => (typeof v === 'number' && Number.isFinite(v) ? v.toFixed(3) : '—');

/* A row that opens its trade. Only a real market slug is ever put into an
   attribute; the side and time are from the bot's own log. */
function tradeRowAttrs(slug, side, ts) {
  if (!slug) return '';
  return `class="row-link" tabindex="0" data-slug="${esc(slug)}" data-side="${esc(side || '')}"`
    + ` data-ts="${validTs(ts) ? Number(ts) : ''}" title="Open this trade"`;
}

function wireTradeRows(sel) {
  const host = $(sel);
  if (!host) return;
  const open = (tr) => openTrade({ slug: tr.dataset.slug, side: tr.dataset.side, ts: tr.dataset.ts });
  host.addEventListener('click', (ev) => {
    const tr = ev.target.closest('tr[data-slug]');
    // dragging across a cell to copy it is not a click on the row
    if (!tr || String(window.getSelection ? window.getSelection() : '').length) return;
    open(tr);
  });
  host.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    const tr = ev.target.closest('tr[data-slug]');
    if (!tr) return;
    ev.preventDefault();
    open(tr);
  });
}

function openTrade(spec) {
  if (!spec || !spec.slug) return;
  const dlg = $('#trade-dialog');
  const same = TRADE.key && TRADE.key.slug === spec.slug && TRADE.key.bot === state.botId;
  TRADE.key = { slug: spec.slug, side: spec.side || null,
                ts: validTs(spec.ts) ? Number(spec.ts) : null, bot: state.botId };
  if (!same) {
    TRADE.data = null;
    TRADE.seen = new Map();
    TRADE.sigs = {};
    TRADE.hoverT = null;
    $('#trade-eyebrow').textContent = 'Trade';
    $('#trade-title').textContent = fmt.mkt(spec.slug);
    $('#trade-sub').textContent = 'Loading…';
    ['#trade-stats', '#trade-chart', '#trade-legend', '#trade-readout', '#trade-orders',
     '#trade-orders-count', '#trade-foot'].forEach((s) => { $(s).innerHTML = ''; });
    $('#trade-live').hidden = true;
  }
  if (!dlg.open) {
    try { dlg.showModal(); } catch { dlg.setAttribute('open', ''); }
    // a modal dialog is drawn in the top layer, above any z-index: the shared
    // tooltip has to live inside it to be seen, and goes back on close
    dlg.appendChild(tip);
  }
  loadTrade();
}

function closeTrade() {
  const dlg = $('#trade-dialog');
  if (dlg.open) dlg.close();               // the close event does the rest
  else endTrade();
}

function endTrade() {
  clearTimeout(TRADE.timer);
  TRADE.timer = null;
  TRADE.key = null;
  hideTip();
  if (tip.parentNode !== document.body) document.body.appendChild(tip);
}

async function loadTrade() {
  clearTimeout(TRADE.timer);
  const key = TRADE.key;
  if (!key) return;
  if (TRADE.inFlight) { TRADE.timer = setTimeout(loadTrade, 250); return; }
  TRADE.inFlight = true;
  const held = TRADE.data;
  const p = new URLSearchParams({ bot: key.bot, slug: key.slug });
  if (held && held.source && held.points.length) {
    p.set('have', held.source);
    p.set('since', String(held.points[held.points.length - 1].t));
  }
  let d = null;
  try {
    const r = await authFetch('/api/trade?' + p.toString());
    d = r && r.ok ? await r.json() : null;
  } catch { d = null; } finally { TRADE.inFlight = false; }
  if (TRADE.key !== key) return;           // closed, or another row opened meanwhile
  if (d) {
    mergeTrade(d);
    renderTrade();
  } else if (!held) {
    $('#trade-sub').textContent = 'Could not load this trade.';
  }
  TRADE.timer = setTimeout(loadTrade, TRADE.data && TRADE.data.live ? TRADE_LIVE_MS : TRADE_IDLE_MS);
}

/* points: null means "unchanged, keep yours"; partial means "these follow yours" */
function mergeTrade(d) {
  const old = TRADE.data;
  let pts;
  if (d.points == null) pts = old ? old.points : [];
  else if (d.partial && old && old.source === d.source) pts = old.points.concat(d.points);
  else pts = d.points;
  TRADE.data = { ...d, points: pts || [] };
}

function refreshTrade() {
  if (!TRADE.data || !$('#trade-dialog').open) return;
  TRADE.sigs = {};
  renderTrade();
}

/* The round trip the clicked row belongs to: the same side, bought around the
   row's time. A window entered twice holds two; the other is still drawn. */
function pickTrip(d) {
  const trips = d.trips || [];
  const k = TRADE.key || {};
  const same = trips.filter((t) => !k.side || t.side === k.side);
  const pool = same.length ? same : trips;
  if (k.ts != null) {
    const hit = pool.find((t) => t.bought_ts != null && k.ts >= t.bought_ts - 2000
      && k.ts <= (t.closed_ts ?? Infinity) + 2000);
    if (hit) return hit;
  }
  return pool[pool.length - 1] || null;
}

function renderTrade() {
  const d = TRADE.data;
  if (!d) return;
  const trip = pickTrip(d);
  renderTradeHead(d, trip);
  const last = d.points.length ? d.points[d.points.length - 1].t : 0;
  const chartSig = [d.points.length, last, JSON.stringify(d.orders), (d.fills || []).length,
    d.settle ? d.settle.ts : '', d.outcome, trip ? trip.bought_ts : '', TRADE.zoom].join('|');
  if (TRADE.sigs.chart !== chartSig) { TRADE.sigs.chart = chartSig; renderTradeChart(d, trip); }
  const ordSig = JSON.stringify(d.orders);
  if (TRADE.sigs.orders !== ordSig) { TRADE.sigs.orders = ordSig; renderTradeOrders(d); }
}

function renderTradeHead(d, trip) {
  const side = (trip && trip.side) || (TRADE.key && TRADE.key.side) || '';
  const mode = (trip && trip.mode) || ((d.fills || [])[0] || {}).mode || '';
  $('#trade-eyebrow').textContent = ['Trade', d.bot !== 'main' ? d.bot : '', mode, `times ${TZ_LABEL}`]
    .filter(Boolean).join(' · ');
  const status = trip ? trip.status : (d.live ? 'watching' : 'no position');
  $('#trade-title').innerHTML = `${esc(d.asset)} ${fmt.hm(d.open_ts)}–${fmt.hm(d.close_ts)}`
    + (side ? ` <span class="side-tag" data-side="${esc(side)}">${esc(side)}</span>` : '')
    + ` <span class="status" data-status="${d.live ? 'live' : ''}">${esc(status)}</span>`;
  const bits = [`Window ${fmt.clock(d.open_ts)} → ${fmt.hms(d.close_ts)}`];
  if (d.strike) bits.push(`strike ${fmtSpot(d.strike)}`);
  if (d.outcome) bits.push(`${d.outcome} won`);
  else if (d.live && d.secs_left != null) bits.push(d.secs_left > 0 ? `${fmt.mmss(d.secs_left)} left` : 'awaiting settlement');
  $('#trade-sub').textContent = bits.join(' · ');
  $('#trade-live').hidden = !d.live;

  const orders = d.orders || [];
  const stat = (label, value, note = '', cls = '') =>
    `<div class="td-stat"><dt>${label}</dt><dd class="${cls}">${value}${note ? `<small>${note}</small>` : ''}</dd></div>`;
  const held = trip && trip.status === 'settled' && d.outcome ? (trip.side === d.outcome ? 1 : 0) : null;
  const exit = !trip ? '—'
    : trip.exit_price != null ? num3(trip.exit_price)
    : held != null ? `${held.toFixed(3)}` : 'open';
  const exitNote = !trip ? ''
    : trip.sold_ts != null ? fmt.short(trip.sold_ts)
    : held != null ? 'settled' : '';
  $('#trade-stats').innerHTML = [
    stat('Entry', trip && trip.entry_price != null ? num3(trip.entry_price) : '—',
         trip && trip.bought_ts != null ? fmt.short(trip.bought_ts) : ''),
    stat('Exit', exit, exitNote),
    stat('Shares', trip ? fmt.num(trip.shares, 2) : '—',
         trip ? `${trip.buys} buy${trip.buys === 1 ? '' : 's'} · ${trip.sells} sell${trip.sells === 1 ? '' : 's'}` : ''),
    stat('Held', trip && trip.hold_s != null ? fmt.dur(trip.hold_s) : '—'),
    stat('Fees', trip ? fmt.money(trip.fees || 0) : '—'),
    resultStat(d, trip),
    stat('Orders', fmt.num(orders.length),
         `${orders.filter(isFilled).length} filled · ${orders.filter((o) => !isFilled(o) && o.outcome !== 'sent' && o.outcome !== 'resting').length} not`),
    checkStat(d),
  ].join('');

  const src = d.source === 'archive' ? 'the archived recording in data/charts'
    : d.source === 'live' ? "the bot's live history" : null;
  $('#trade-foot').textContent = (src ? `Price path from ${src}. ` : 'No price path was recorded for this window. ')
    + 'Fills are from the trade log; orders from the forensics log and, while the window runs, '
    + "the bot's live state."
    + (d.live ? ' Updating every second.' : '');
}

/* The market's result: what the bot booked for the WHOLE window (every fill, both sides; the late correction
   folded in), not one side's trip -- a pair-lock market holds UP and DOWN at once. While the outcome is still
   unknown the figure is the mark at the last price, and says so. */
function resultStat(d, trip) {
  const r = d.result;
  const stat = (label, value, note = '', cls = '') =>
    `<div class="td-stat"><dt>${label}</dt><dd class="${cls}">${value}${note ? `<small>${note}</small>` : ''}</dd></div>`;
  if (!r || r.booked_pnl == null) {
    return stat('Result', trip && trip.pnl != null ? fmt.signed(trip.pnl) : '—',
      trip && trip.pnl != null && trip.cost ? fmt.pct(trip.pnl / trip.cost) : '',
      trip && trip.pnl != null ? (trip.pnl >= 0 ? 'pos' : 'neg') : '');
  }
  const pct = r.cost ? ` · ${fmt.pct(r.booked_pnl / r.cost)} of ${fmt.money(r.cost)} spent` : '';
  const legs = `UP ${fmt.num(r.shares_up, 2)} · DOWN ${fmt.num(r.shares_down, 2)} sh${r.sells ? ` · ${r.sells} sell${r.sells === 1 ? '' : 's'}` : ''}`;
  return stat('Result', fmt.signed(r.booked_pnl), (r.marked ? 'marked at the last price, outcome pending' : legs) + pct,
    r.booked_pnl >= 0 ? 'pos' : 'neg');
}

/* payout of the winning side + sells - every buy - every fee, from the fills alone, against the booked figure */
function checkStat(d) {
  const r = d.result;
  const stat = (label, value, note = '', cls = '') =>
    `<div class="td-stat"><dt>${label}</dt><dd class="${cls}">${value}${note ? `<small>${note}</small>` : ''}</dd></div>`;
  if (!r || r.check_pnl == null) return stat('Check', '—', r && r.marked ? 'outcome not known yet' : 'no fills');
  const parts = [`${r.outcome} paid ${fmt.money(r.payout)}`, r.proceeds ? `sells ${fmt.money(r.proceeds)}` : '',
    `cost ${fmt.money(r.cost)}`, `fees ${fmt.money(r.fees)}`].filter(Boolean).join(' · ');
  const verdict = r.agree == null ? '' : r.agree ? ' ✓ matches the booked result' : ` ✗ booked ${fmt.signed(r.booked_pnl)}`;
  return stat('Check', fmt.signed(r.check_pnl), parts + verdict, r.agree === false ? 'neg' : '');
}

/* A clock axis for any span, from a few seconds to the whole window: a label
   at the finest round step that leaves room for it, a gridline at each label
   (stronger on the minute). Seconds are shown until the step reaches one. */
function clockAxis(svg, m, pw, H, lo, hi, Xt) {
  const base = H - m.b;
  const span = Math.max(1, hi - lo);
  const step = [1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000]
    .find((st) => pw / (span / st) >= 72) || 120000;
  for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) {
    const x = Xt(t);
    const minute = t % 60000 === 0;
    if (t > lo && t < hi) {
      svg.appendChild(el('line', { class: minute ? 'grid-line' : 'grid-line-minor', x1: x, x2: x, y1: m.t, y2: base }));
    }
    svg.appendChild(el('line', { class: 'axis-line', x1: x, x2: x, y1: base, y2: base + 5 }));
    // a label hard against either end would run into its neighbour: tick only
    if ((x - m.l > 0.5 && x - m.l < 28) || (m.l + pw - x > 0.5 && m.l + pw - x < 28)) continue;
    const lab = el('text', { class: `axis-text${minute ? ' axis-text-major' : ''}`, x, y: base + 18,
      'text-anchor': x - m.l < 1 ? 'start' : m.l + pw - x < 1 ? 'end' : 'middle' });
    lab.textContent = step < 60000 ? fmt.hms(t) : fmt.hm(t);
    svg.appendChild(lab);
  }
  svg.appendChild(el('line', { class: 'axis-line', x1: m.l, x2: m.l + pw, y1: base, y2: base }));
}

/* The span the 'trade' zoom draws: the chosen round trip, the orders that led
   into it, and its exit, with room either side -- at least a minute, never
   outside the window. A window with no position fits every order on it. */
function tradeDomain(d, trip) {
  const open = d.open_ts, close = d.close_ts;
  if (TRADE.zoom === 'window') return [open, close];
  const orders = d.orders || [];
  const ts = [];
  const add = (t) => { if (validTs(t)) ts.push(Number(t)); };
  if (trip && trip.bought_ts != null) {
    const end = trip.closed_ts ?? (d.live && d.points.length ? d.points[d.points.length - 1].t : close);
    add(trip.bought_ts);
    add(Math.min(end, close));
    orders.filter((o) => o.side === trip.side && o.t >= trip.bought_ts - 30000 && o.t <= end + 5000)
      .forEach((o) => { add(o.t); add(o.t_done); });
  } else {
    orders.forEach((o) => { add(o.t); add(o.t_done); });
    (d.fills || []).forEach((f) => add(f.ts));
  }
  if (!ts.length) return [open, close];
  let lo = Math.min(...ts), hi = Math.max(...ts);
  const pad = Math.max(8000, (hi - lo) * 0.2);
  lo -= pad; hi += pad;
  if (hi - lo < 60000) { const mid = (lo + hi) / 2; lo = mid - 30000; hi = mid + 30000; }
  if (lo < open) { hi = Math.min(close, hi + (open - lo)); lo = open; }
  if (hi > close) { lo = Math.max(open, lo - (hi - close)); hi = close; }
  return [lo, hi];
}

function orderTip(o) {
  const exit = orderIsSell(o);
  const rows = [
    ['Sent', fmt.clockTenths(o.t)],
    ['Limit', num3(o.limit)],
    ['Shares', o.size == null ? '—' : fmt.num(o.size, 2)],
  ];
  if (isFilled(o)) rows.push(['Filled at', num3(o.price)]);
  if (o.t_done != null && o.t != null) rows.push([isFilled(o) ? 'Filled after' : 'Answered after', `${Math.round(o.t_done - o.t)} ms`]);
  if (o.reason) rows.push(['Why', esc(String(o.reason).replace(/_/g, ' '))]);
  rows.push(['Order', esc(o.id || '—')]);
  const verb = o.outcome === 'sent' ? 'working' : o.outcome;
  return tipRows(`${exit ? 'Sell' : 'Buy'} ${esc(o.side || '')} · ${esc(kindLabel(o.kind))} · ${esc(verb)}`, rows);
}

/* light the same order in every chart and list that shows it. The market
   chart redraws ten times a second, so the hovered id is kept and each redraw
   restores the halo. */
function hotOrder(id, on) {
  if (!id) return;
  ORDERS.hot = on ? id : (ORDERS.hot === id ? null : ORDERS.hot);
  const q = (window.CSS && CSS.escape) ? CSS.escape(id) : id;
  $$(`#trade-chart [data-oid="${q}"], #trade-orders tr[data-oid="${q}"], `
    + `#price-chart [data-oid="${q}"], #orders-table tr[data-oid="${q}"]`)
    .forEach((n) => n.classList.toggle('is-hot', on));
}

function tradeReadout(p) {
  const d = TRADE.data;
  const host = $('#trade-readout');
  if (!d) return;
  const pts = d.points || [];
  const q = p || pts[pts.length - 1];
  if (!q) { host.innerHTML = ''; return; }
  readout(host, p ? fmt.clockTenths(p.t) : (d.live ? 'now' : 'at close'), [
    { color: css('--series-1'), value: num3(q.up), label: `Up · ${bidAsk(q.ub, q.ua)}` },
    { color: css('--series-2'), value: num3(q.down), label: `Down · ${bidAsk(q.db, q.da)}` },
  ]);
}

function renderTradeChart(d, trip) {
  const host = $('#trade-chart');
  const close = d.close_ts;
  const [lo, hi] = tradeDomain(d, trip);
  const span = Math.max(1, hi - lo);
  const inSpan = (t) => validTs(t) && t >= lo - 1 && t <= hi + 1;
  // one sample either side of the span, so the lines run to its edges
  const all = d.points || [];
  const first = Math.max(0, all.findIndex((p) => p.t >= lo) - 1);
  let lastIdx = all.length - 1;
  while (lastIdx > 0 && all[lastIdx - 1].t > hi) lastIdx -= 1;
  const pts = all.slice(first, lastIdx + 1);
  const cUp = css('--series-1'), cDown = css('--series-2'), ring = css('--surface-1');
  const sideColor = (s) => (String(s || '').toUpperCase() === 'DOWN' ? cDown : cUp);
  const H = 400, m = { t: 30, r: 20, b: 34, l: 48 };
  const { svg, w } = mount(host, H);
  const pw = w - m.l - m.r, ph = H - m.t - m.b;
  const Xt = (t) => m.l + ((t - lo) / span) * pw;
  const Xp = (p) => Xt(p.t);
  const Y = (v) => m.t + ph - Math.max(0, Math.min(1, v)) * ph;

  [0, 0.25, 0.5, 0.75, 1].forEach((v) => {
    svg.appendChild(el('line', { class: 'grid-line', x1: m.l, x2: m.l + pw, y1: Y(v), y2: Y(v) }));
    const lab = el('text', { class: 'axis-text', x: m.l - 9, y: Y(v) + 4, 'text-anchor': 'end' });
    lab.textContent = v.toFixed(2);
    svg.appendChild(lab);
  });
  axisCaption(svg, m, `Share price ($) · band = bid to ask · ${fmt.clock(lo).slice(0, 10)} ${TZ_LABEL}`);
  clockAxis(svg, m, pw, H, lo, hi, Xt);

  // everything drawn as a span (bands, lines, holds) is clipped to the plot
  const clipId = `td-clip-${Math.random().toString(36).slice(2, 8)}`;
  const defs = el('defs');
  const clip = el('clipPath', { id: clipId });
  clip.appendChild(el('rect', { x: m.l, y: m.t - 6, width: pw, height: ph + 12 }));
  defs.appendChild(clip);
  svg.appendChild(defs);
  const plot = el('g', { 'clip-path': `url(#${clipId})` });
  svg.appendChild(plot);

  // how long each position was held, as a wash behind the prices
  (d.trips || []).forEach((tr) => {
    if (tr.bought_ts == null) return;
    const x1 = Xt(tr.bought_ts), x2 = Xt(tr.closed_ts ?? (d.live ? d.now : close));
    plot.appendChild(el('rect', { class: `hold-band${tr === trip ? ' is-on' : ''}`, x: x1, y: m.t,
      width: Math.max(2, x2 - x1), height: ph, fill: sideColor(tr.side) }));
  });

  if (pts.length) {
    spreadBand(plot, pts, Xp, Y, 'ub', 'ua', cUp);
    spreadBand(plot, pts, Xp, Y, 'db', 'da', cDown);
    [['up', cUp], ['down', cDown]].forEach(([key, color]) => {
      const run = pts.filter((p) => typeof p[key] === 'number');
      if (run.length < 2) return;
      plot.appendChild(el('path', { class: 'series-line', stroke: color,
        d: run.map((p, i) => `${i ? 'L' : 'M'}${Xp(p)},${Y(p[key])}`).join(' ') }));
      const lp = run[run.length - 1];
      if (d.live && inSpan(lp.t)) {
        svg.appendChild(el('circle', { cx: Xp(lp), cy: Y(lp[key]), r: 4, fill: color, stroke: ring, 'stroke-width': 2 }));
      }
    });
  } else {
    const t = el('text', { class: 'axis-text', x: m.l + pw / 2, y: m.t + ph / 2, 'text-anchor': 'middle' });
    t.textContent = 'No price path was recorded for this window';
    svg.appendChild(t);
  }

  // the hover layer sits UNDER the markers, so each marker keeps its own tooltip
  const hov = hoverLayer(svg, m, ph, [
    { color: cUp, y: (p) => (typeof p.up === 'number' ? Y(p.up) : null) },
    { color: cDown, y: (p) => (typeof p.down === 'number' ? Y(p.down) : null) },
  ]);
  const hit = el('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph });
  svg.appendChild(hit);
  const tAt = (ev) => lo + ((ev.clientX - svg.getBoundingClientRect().left - m.l) / (pw || 1)) * span;
  hit.addEventListener('mousemove', (ev) => {
    const p = nearestByT(pts, tAt(ev));
    if (!p) { TRADE.hoverT = null; hov.clear(); tradeReadout(null); hideTip(); return; }
    TRADE.hoverT = p.t;
    hov.show(Xp(p), p);
    tradeReadout(p);
    showTip(tipRows(fmt.clockTenths(p.t), [
      ['Up', `${num3(p.up)} · ${bidAsk(p.ub, p.ua)}`, cUp],
      ['Down', `${num3(p.down)} · ${bidAsk(p.db, p.da)}`, cDown],
      ['Left', fmt.mmss((close - p.t) / 1000)],
    ]), ev);
  });
  hit.addEventListener('mouseleave', () => { TRADE.hoverT = null; hov.clear(); tradeReadout(null); hideTip(); });

  // entry to exit: to the sell, or to the 1 / 0 the window paid at settlement
  (d.trips || []).forEach((tr) => {
    if (tr.bought_ts == null || tr.entry_price == null) return;
    let x2, y2, settled = false;
    if (tr.sold_ts != null && tr.exit_price != null) { x2 = Xt(tr.sold_ts); y2 = Y(tr.exit_price); }
    else if (tr.status === 'settled' && d.outcome) {
      x2 = Xt(close); y2 = Y(tr.side === d.outcome ? 1 : 0); settled = true;
    } else return;
    const color = sideColor(tr.side);
    plot.appendChild(el('line', { class: `trip-line${tr === trip ? ' is-on' : ''}`, x1: Xt(tr.bought_ts),
      y1: Y(tr.entry_price), x2, y2, stroke: color }));
    if (settled && inSpan(close)) {
      svg.appendChild(el('circle', { cx: x2, cy: y2, r: 5.5, fill: ring, stroke: color, 'stroke-width': 2 }));
      const lab = el('text', { class: 'value-label-strong', x: x2 - 9, y: y2 + (y2 < m.t + 14 ? 16 : -9),
        'text-anchor': 'end' });
      lab.textContent = `settled ${tr.side === d.outcome ? '1.00' : '0.00'}`;
      svg.appendChild(lab);
    }
  });

  // every order at its limit when it was sent, and the dashed line to its fill
  const orders = d.orders || [];
  orders.forEach((o) => {
    if (!inSpan(o.t) || o.limit == null) return;
    const color = sideColor(o.side);
    const cx = Xt(o.t), cy = Y(o.limit);
    const g = el('g', { class: `order-mark${ORDERS.hot === o.id ? ' is-hot' : ''}`, 'data-oid': o.id || '' });
    g.appendChild(el('circle', { class: 'mark-halo', cx, cy, r: 12, fill: color }));
    if (isFilled(o) && o.price != null && o.t_done != null) {
      g.appendChild(el('line', { x1: cx, y1: cy, x2: Xt(o.t_done), y2: Y(o.price), stroke: color,
        'stroke-width': 1.5, 'stroke-dasharray': '3 3' }));
    }
    if (o.outcome === 'sent') {
      g.appendChild(el('rect', { class: 'order-live', x: cx - 5, y: cy - 5, width: 10, height: 10,
        fill: ring, stroke: color, 'stroke-width': 2 }));
    } else if (isFilled(o)) {
      g.appendChild(el('rect', { x: cx - 4.5, y: cy - 4.5, width: 9, height: 9, fill: ring, stroke: color, 'stroke-width': 2 }));
    } else {
      g.appendChild(el('path', { d: `M${cx - 5},${cy - 5} L${cx + 5},${cy + 5} M${cx - 5},${cy + 5} L${cx + 5},${cy - 5}`,
        stroke: color, 'stroke-width': 2.25, fill: 'none' }));
    }
    const h = el('circle', { class: 'hit', cx, cy, r: 10 });
    g.appendChild(h);
    h.addEventListener('mousemove', (ev) => { hotOrder(o.id, true); showTip(orderTip(o), ev); });
    h.addEventListener('mouseleave', () => { hotOrder(o.id, false); hideTip(); });
    svg.appendChild(g);
  });

  // the fills: ▲ bought, ▼ sold. From the trade log; an order whose fill the
  // log does not have (an older run, a torn write) is drawn from the order
  const isSell = (f) => String(f.action || '').toUpperCase() === 'SELL';
  const fills = (d.fills || []).map((f) => ({ ts: f.ts, price: Number(f.price), side: f.side, sell: isSell(f),
    size: f.size, money: Math.abs(f.cost ?? f.price * f.size), fee: f.fee, kind: f.kind, ms: f.round_trip_ms }));
  orders.filter((o) => isFilled(o) && o.price != null && o.t_done != null).forEach((o) => {
    const exit = orderIsSell(o);
    if (fills.some((f) => f.side === o.side && f.sell === exit && Math.abs(f.ts - o.t_done) < 3000)) return;
    fills.push({ ts: o.t_done, price: o.price, side: o.side, sell: exit, size: o.filled ?? o.size,
      money: (o.filled ?? o.size ?? 0) * o.price, kind: o.kind, oid: o.id });
  });
  fills.forEach((f) => {
    if (!inSpan(f.ts) || !Number.isFinite(f.price)) return;
    const color = sideColor(f.side);
    const cx = Xt(f.ts), cy = Y(f.price), s = 7;
    const dpath = f.sell
      ? `M${cx},${cy + s} L${cx + s},${cy - s * 0.75} L${cx - s},${cy - s * 0.75} Z`
      : `M${cx},${cy - s} L${cx + s},${cy + s * 0.75} L${cx - s},${cy + s * 0.75} Z`;
    svg.appendChild(el('path', { d: dpath, fill: color, stroke: ring, 'stroke-width': 1.5 }));
    const h = el('circle', { class: 'hit', cx, cy, r: 11 });
    svg.appendChild(h);
    h.addEventListener('mousemove', (ev) => showTip(tipRows(
      `${f.sell ? 'Sold' : 'Bought'} ${esc(f.side || '')}${f.sell && f.kind ? ` · ${esc(kindLabel(String(f.kind).toLowerCase()))}` : ''}`, [
        ['Filled', fmt.clockTenths(f.ts)],
        ['Price', num3(f.price), color],
        ['Shares', f.size == null ? '—' : fmt.num(f.size, 2)],
        [f.sell ? 'Received' : 'Cost', fmt.money(f.money || 0)],
        ...(f.fee != null ? [['Fee', fmt.money(Number(f.fee) || 0)]] : []),
        ...(f.ms != null ? [['Round trip', `${Math.round(f.ms)} ms`]] : []),
        ['Left', fmt.mmss((close - f.ts) / 1000)],
      ]), ev));
    h.addEventListener('mouseleave', hideTip);
  });

  // a re-render under a still pointer keeps its crosshair
  if (TRADE.hoverT != null) {
    const p = nearestByT(pts, TRADE.hoverT);
    if (p) { hov.show(Xp(p), p); tradeReadout(p); } else tradeReadout(null);
  } else tradeReadout(null);

  const ink = css('--text-secondary');
  const items = [{ label: 'Up', color: cUp }, { label: 'Down', color: cDown }];
  if (fills.some((f) => !f.sell)) items.push({ label: 'buy fill', glyph: '▲', color: ink });
  if (fills.some((f) => f.sell)) items.push({ label: 'sell fill', glyph: '▼', color: ink });
  if (orders.length) items.push({ label: 'order sent, at its limit', glyph: '□', color: ink });
  if (orders.some((o) => !isFilled(o) && o.outcome !== 'sent')) items.push({ label: 'order not filled', glyph: '×', color: ink });
  if (orders.some(isFilled)) items.push({ label: 'order to its fill', glyph: '┄', color: ink });
  if ((d.trips || []).length) items.push({ label: 'position held', glyph: '▮', color: css('--axis') });
  legend($('#trade-legend'), items);
  const outside = orders.filter((o) => validTs(o.t) && !inSpan(o.t)).length;
  if (outside) {
    const note = document.createElement('span');
    note.className = 'legend-item';
    note.textContent = `${outside} more order${outside === 1 ? '' : 's'} outside this span — Whole window shows ${outside === 1 ? 'it' : 'them'}`;
    $('#trade-legend').appendChild(note);
  }
}

function renderTradeOrders(d) {
  const table = $('#trade-orders');
  const orders = (d.orders || []).slice().reverse();          // newest first, like a blotter
  const working = orders.filter((o) => o.outcome === 'sent').length;
  $('#trade-orders-count').textContent = orders.length
    ? `${orders.length} sent · ${orders.filter(isFilled).length} filled${working ? ` · ${working} working` : ''}`
    : '';
  const firstDraw = !TRADE.seen.size && !TRADE.sigs.ordersDrawn;
  TRADE.sigs.ordersDrawn = true;
  if (!orders.length) {
    table.innerHTML = '<tbody><tr><td class="live-empty">No orders recorded on this window.</td></tr></tbody>';
    return;
  }
  const head = `<thead><tr><th scope="col">Time (${TZ_LABEL})</th><th scope="col" class="l">Order</th>`
    + '<th scope="col">Limit</th><th scope="col">Size</th><th scope="col" class="l">Status</th>'
    + '<th scope="col">Took</th><th scope="col"></th></tr></thead>';
  const canSell = d.live && d.running && !d.outcome;
  const body = orders.map((o) => {
    const exit = orderIsSell(o);
    const sellBtn = canSell && !exit && isFilled(o) && o.side
      ? `<button type="button" class="btn btn-sm btn-sell" data-sell-slug="${esc(d.slug)}" data-sell-side="${esc(o.side)}"`
        + ` data-sell-shares="${o.filled != null && o.filled > 0 ? o.filled : (o.size || 0)}" data-sell-bot="${esc(d.bot || '')}"`
        + ` title="Sell these shares now at the bid (fill-and-kill)">Sell</button>` : '';
    const before = TRADE.seen.get(o.id);
    const flash = !firstDraw && before !== o.outcome;
    TRADE.seen.set(o.id, o.outcome);
    const st = o.outcome === 'sent' || o.outcome === 'resting' ? ['st-live', o.outcome === 'resting' ? 'resting' : 'working']
      : isFilled(o) ? ['st-filled', o.outcome === 'partial' ? `partial ${o.filled != null ? fmt.num(o.filled, 2) + ' of ' + fmt.num(o.size, 2) : ''}` : 'filled']
      : ['st-killed', o.outcome || '—'];
    const reason = o.reason ? String(o.reason).replace(/_/g, ' ') : '';
    const took = o.t_done != null && o.t != null ? `${Math.round(o.t_done - o.t)} ms` : '—';
    return `<tr data-oid="${esc(o.id || '')}"${flash ? ' class="is-new"' : ''}`
      + ` title="${esc(`${o.id || ''}${reason ? ` · ${reason}` : ''}`)}">`
      + `<td>${fmt.short(o.t)}</td>`
      + `<td class="l"><span class="act ${exit ? 'act-sell' : 'act-buy'}">${exit ? 'sell' : 'buy'}</span>`
      + ` <span class="side-tag side-mini" data-side="${esc(o.side || '')}">${esc(o.side || '—')}</span>`
      + `${exit ? ` <span class="ol-kind">${esc(kindLabel(o.kind))}</span>` : o.kind === 'pair' ? ' <span class="ol-kind">pair bid</span>' : ''}</td>`
      + `<td>${num3(o.limit)}</td>`
      + `<td>${o.size == null ? '—' : fmt.num(o.size, 2)}</td>`
      + `<td class="l"><span class="ostat ${st[0]}">${esc(st[1])}</span>`
      + (isFilled(o) ? ` <span class="ol-fill">@ ${num3(o.price)}</span>`
        : reason ? `<span class="ol-reason">${esc(reason)}</span>` : '')
      + `</td><td>${took}</td><td class="r">${sellBtn}</td></tr>`;
  }).join('');
  table.innerHTML = head + `<tbody>${body}</tbody>`;
}

/* The Buy controls (the user, 2026-10-05): buy N shares of UP or DOWN of the SELECTED market on the selected bot, at
   the ask, as a fill-and-kill. Written to the bot's command file like a sell. */
async function buyFromPanel() {
  const slug = state.selected;
  const side = ($('#buy-side .is-on') || {}).dataset ? $('#buy-side .is-on').dataset.side : null;
  const n = Number($('#buy-amount').value);
  const live = state.live ? pickerMarkets().some((m) => m.slug === slug) : false;
  if (!slug || !live) { notice('Pick a market that is still open on this bot.'); return; }
  if (!side) { notice('Pick UP or DOWN.'); return; }
  if (!(n >= 5)) { notice('At least 5 shares (the venue minimum).'); return; }
  if (!window.confirm(`Buy ${fmt.num(n, 2)} ${side} of ${slug} at the ask now?`)) return;
  const btn = $('#buy-send');
  btn.disabled = true;
  try {
    const r = await post('/api/buy', { bot: state.botId, slug, side, shares: n });
    if (r && r.error) notice(`Buy refused by the server for bot "${state.botId}": ${r.error}`);
    else notice(`Buy of ${fmt.num(n, 2)} ${side} sent to the bot; watch the order list.`, false);
  } catch (e) {
    notice(`Buy failed: ${e}`);
  }
  btn.disabled = false;
}
function wireBuyPanel() {
  const seg = $('#buy-side');
  if (!seg) return;
  seg.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-side]');
    if (!b) return;
    seg.querySelectorAll('[data-side]').forEach((x) => x.classList.toggle('is-on', x === b));
  });
  $('#buy-send').addEventListener('click', buyFromPanel);
  const all = $('#sell-all');
  if (all) all.addEventListener('click', sellAllFromPanel);
}

/* The Sell-all button (the user, 2026-10-07): every token the selected bot holds in the SELECTED market, both sides,
   sold at the bid as fill-and-kills. One sell command per side held, asking for more than any holding: the bot caps
   each sell at what it still holds, so the whole holding goes even if this page's numbers are a moment old. The
   second side is sent SELL_ALL_GAP_MS after the first, so the bot (which reads its command file every 2 s) sends the
   two sells in different passes and books the first before the second exists. */
const SELL_ALL_GAP_MS = 3500;
const SELL_ALL_SHARES = 10000;               // the server's ceiling for one sell; the bot sells what it holds
async function sellAllFromPanel() {
  const slug = state.selected;
  const btn = $('#sell-all');
  const m = state.live ? (state.live.live_markets || []).find((x) => x.slug === slug && x.secs_left > 0) : null;
  if (!slug || !m) { notice('Pick a market that is still open on this bot.'); return; }
  const held = [['UP', Number(m.up_shares) || 0], ['DOWN', Number(m.down_shares) || 0]].filter(([, q]) => q > 0);
  if (!held.length) { notice(`Bot "${state.botId}" holds no tokens in ${slug}.`); return; }
  const what = held.map(([s, q]) => `${fmt.num(q, 2)} ${s}`).join(' + ');
  if (!window.confirm(`Sell ALL tokens of ${slug} on bot "${state.botId}" at the bid now?\n${what}\n`
                      + 'Each side is sold for whatever the bot holds when the order goes out.')) return;
  const bot = state.botId;
  btn.disabled = true;
  btn.textContent = 'selling…';
  const sent = [], refused = [];
  for (let i = 0; i < held.length; i++) {
    const side = held[i][0];
    if (i > 0) await new Promise((res) => setTimeout(res, SELL_ALL_GAP_MS));
    try {
      const r = await post('/api/sell', { bot, slug, side, shares: SELL_ALL_SHARES });
      if (r && r.error) refused.push(`${side}: ${r.error}`); else sent.push(side);
    } catch (e) {
      refused.push(`${side}: ${e}`);
    }
  }
  if (refused.length) notice(`Sell all on bot "${bot}": ${sent.length ? `sent ${sent.join(' + ')}; ` : ''}refused ${refused.join('; ')}`);
  else notice(`Sell all of ${slug} sent to bot "${bot}" (${sent.join(' + ')}); watch the order list.`, false);
  btn.disabled = false;
  btn.textContent = 'Sell all tokens';
}

/* The Sell button (the user, 2026-10-05): sells the shares of that side the bot still holds in that market, at the
   bid, as a fill-and-kill. The request is written to the bot's command file; the bot acts within ~2 s and the sell
   appears in this list like any other order. */
async function sellFromRow(btn) {
  const { sellSlug: slug, sellSide: side, sellShares: shares, sellBot: bot } = btn.dataset;
  const n = Number(shares);
  if (!slug || !side || !(n > 0)) return;
  const ok = window.confirm(`Sell ${fmt.num(n, 2)} ${side} of ${slug} at the bid now?\nThe bot sells at most what it still holds.`);
  if (!ok) return;
  btn.disabled = true;
  btn.textContent = 'sending…';
  try {
    const r = await post('/api/sell', { bot: bot || state.botId, slug, side, shares: n });
    if (r && r.error) { notice(`Sell refused by the server for bot "${bot || state.botId}": ${r.error}`); btn.disabled = false; btn.textContent = 'Sell'; return; }
    notice(`Sell of ${fmt.num(n, 2)} ${side} sent to the bot; watch the order list.`, false);
    btn.textContent = 'sent';
  } catch (e) {
    notice(`Sell failed: ${e}`); btn.disabled = false; btn.textContent = 'Sell';
  }
}
document.addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-sell-slug]');
  if (btn) { ev.stopPropagation(); sellFromRow(btn); }
});

function wireTrade() {
  const dlg = $('#trade-dialog');
  dlg.addEventListener('close', endTrade);
  // a click on the backdrop lands on the dialog itself; the content is wrapped
  dlg.addEventListener('click', (ev) => { if (ev.target === dlg) closeTrade(); });
  $('#trade-close').addEventListener('click', closeTrade);
  const list = $('#trade-orders');
  list.addEventListener('mouseover', (ev) => {
    const tr = ev.target.closest('tr[data-oid]');
    if (tr) hotOrder(tr.dataset.oid, true);
  });
  list.addEventListener('mouseout', (ev) => {
    const tr = ev.target.closest('tr[data-oid]');
    if (tr && !tr.contains(ev.relatedTarget)) hotOrder(tr.dataset.oid, false);
  });
  ['#trades-table', '#history-table'].forEach(wireTradeRows);
  markSeg('#trade-zoom', 'zoom', TRADE.zoom);
  $('#trade-zoom').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-zoom]');
    if (!btn || btn.dataset.zoom === TRADE.zoom) return;
    TRADE.zoom = btn.dataset.zoom;
    try { localStorage.setItem('tpb-trade-zoom', TRADE.zoom); } catch { /* private mode */ }
    markSeg('#trade-zoom', 'zoom', TRADE.zoom);
    TRADE.hoverT = null;
    if (TRADE.data) renderTrade();
  });
}

/* the operator's clock, in the tab bar: every time on the page is on it */
function tickClock() {
  const c = $('#tz-clock');
  if (c) c.textContent = `${fmt.clock(Date.now())} ${TZ_LABEL}`;
}

/* ═══════════════════ the order list beside the live chart ═══════════════════
   Every order the bot sends, newest first, the moment it is created and again
   when the venue fills or kills it. Each live frame carries the orders of the
   markets still in the bot's live set; that set drops a window about four
   minutes after it closes, so the list keeps every order it has seen, and
   starts from the recent past in the forensics log rather than from empty.

   Rows are short on purpose: at 22 px a row, fifty orders sit beside the
   charts without scrolling. A row of a market on the chart selects it there;
   a row of a window that has closed opens that trade.
   ========================================================================= */

const ORDERS_KEEP = 400;
//: the live bot's book-age gate (spot_lag.max_quote_age_ms, set 2026-09-30): an age over it shows red
const BOOK_AGE_GATE_MS = 60;
//: re-read recent orders this often while the market page is open: a killed order's ages reach the
//  forensics log the moment it is killed, a filled one's when its position closes
const ORDERS_REFRESH_MS = 10000;
const ORDERS_SHOWN = 150;
// exit kinds in two letters; the row's tooltip spells them out
const KIND_SHORT = { take_profit: 'TP', stop_loss: 'SL', time_stop: 'TS', momentum_reversal: 'MR' };

/* Is this journaled order a SELL? Only by its kind: an exit kind (profit exit, recovery sell, ULR exits, a dashboard or
   website sale) sells; everything else buys -- the entry, a pair bid, and (2026-10-06, the user: "manual buy order is
   displayed as SELL") the dashboard's / website's manual buys, which carry a kind of their own. */
const BUY_KINDS = new Set(['entry', 'pair', 'manual_buy', 'web_buy', 'open', 'recovery', 'lock', 'late', 'add_on', 'hedge']);
function orderIsSell(o) {
  const k = String((o && o.kind) || 'entry').toLowerCase();
  if (BUY_KINDS.has(k) || k.endsWith('_buy')) return false;
  return true;
}

function absorbOrders(list) {
  (list || []).forEach((o) => {
    if (!o || !o.id) return;
    const next = { ...o, kind: String(o.kind || 'entry').toLowerCase() };
    const prev = ORDERS.map.get(o.id);
    if (!prev) { ORDERS.map.set(o.id, next); return; }
    // a finished order is never walked back to "working" by an older copy
    if (next.outcome === 'sent' && prev.outcome !== 'sent') return;
    const merged = { ...prev };
    Object.entries(next).forEach(([k, v]) => { if (v != null) merged[k] = v; });
    ORDERS.map.set(o.id, merged);
  });
  if (ORDERS.map.size > ORDERS_KEEP) {
    const keep = [...ORDERS.map.values()].sort((a, b) => (b.t || 0) - (a.t || 0)).slice(0, ORDERS_KEEP);
    ORDERS.map = new Map(keep.map((o) => [o.id, o]));
  }
}

function absorbOrderMarks(marks) {
  Object.entries(marks || {}).forEach(([slug, list]) => {
    absorbOrders((list || []).map((m) => ({ ...m, slug })));
  });
}

async function seedOrders() {
  const bot = state.botId;
  let d = null;
  try {
    const r = await authFetch(`/api/orders?bot=${encodeURIComponent(bot)}&limit=${ORDERS_SHOWN}`);
    d = r && r.ok ? await r.json() : null;
  } catch { d = null; }
  if (bot !== state.botId) return;              // switched while it was loading
  if (d) absorbOrders(d.orders);
  ORDERS.seeded = true;                          // even on failure: live orders still flash
  if (state.page === 'market') renderOrderList();         // redrawn only if something changed
}

function resetOrders() {
  const cached = ORDERS_CACHE.get(state.botId);
  ORDERS.map = cached ? new Map(cached) : new Map();
  ORDERS.drawn = new Map();
  ORDERS.seeded = false;
  ORDERS.primed = false;
  ORDERS.hot = null;
  resetFills();
  seedOrders();
}

/* "BTC 21:55": the asset and when its window opened, GMT+9 */
function orderMarket(slug) {
  const m = /^([a-z0-9]+)-updown-\d+m-(\d+)$/.exec(slug || '');
  return m ? `${m[1].toUpperCase()} ${fmt.hm(Number(m[2]) * 1000)}` : esc(slug || '—');
}

/* THE FILL LEDGER (2026-10-12, the user's layout): every fill of a market, oldest first, with what that
   fill and everything before it pay if Up wins and if Down wins. Read whole from /api/fills (the live push
   carries only the trade log's last 60 rows) and topped up from that push between reads. "All markets"
   shows the newest FILL_MARKETS markets, each from its first fill, so the running columns are exact.
   Taker or maker comes from the fee: the venue charges takers and not makers (server.py _FillsIndex). */
const FILLS_REFRESH_MS = 3000;
const FILL_MARKETS = 10;
const FILLS = { key: '', rows: [], outcomes: {}, t: 0, loading: false, drawn: new Set(), primed: false };

function resetFills() {
  FILLS.key = ''; FILLS.rows = []; FILLS.outcomes = {}; FILLS.t = 0; FILLS.drawn = new Set(); FILLS.primed = false;
}

function fillsKey() {
  return `${state.botId || ''}|${ORDERS.scope}|${ORDERS.scope === 'market' ? state.selected || '' : ''}`;
}

async function loadFills() {
  if (FILLS.loading) return;
  const key = fillsKey();
  const sel = state.selected;
  if (ORDERS.scope === 'market' && !sel) {          // nothing selected: nothing to ask for
    FILLS.key = key; FILLS.rows = []; FILLS.outcomes = {}; FILLS.t = Date.now();
    return;
  }
  FILLS.loading = true;
  let d = null;
  try {
    const q = `bot=${encodeURIComponent(state.botId || '')}`
      + (ORDERS.scope === 'market' ? `&slug=${encodeURIComponent(sel)}` : `&markets=${FILL_MARKETS}`);
    const r = await authFetch(`/api/fills?${q}`);
    d = r && r.ok ? await r.json() : null;
  } catch { d = null; }
  FILLS.loading = false;
  if (key !== fillsKey()) return;                   // switched while it loaded: the next render asks again
  if (FILLS.key !== key) { FILLS.drawn = new Set(); FILLS.primed = false; }
  if (d) { FILLS.rows = d.fills || []; FILLS.outcomes = d.outcomes || {}; FILLS.key = key; }
  FILLS.t = Date.now();                             // a failed read is retried after the same pause
  if (state.page === 'market') renderOrderList(true);
}

function fillId(f) {
  return `${Math.round(Number(f.ts) || 0)}|${f.slug}|${f.side}|${f.action}|${Number(f.size).toFixed(4)}`;
}

/* the live push's newest fills, in the API's shape */
function pushedFills() {
  return ((state.live && state.live.ledger) || [])
    .filter((r) => r && r.event === 'fill' && r.slug && Number(r.size) > 0 && Number.isFinite(Number(r.price)))
    .map((r) => {
      const fee = r.fee == null ? null : Number(r.fee);
      return {
        ts: Number(r.ts), slug: r.slug, side: String(r.side || '').toUpperCase(),
        action: String(r.action || '').toUpperCase() === 'SELL' ? 'SELL' : 'BUY',
        size: Number(r.size), price: Number(r.price), fee: fee || 0, kind: r.kind,
        liq: fee == null ? (r.pair ? 'maker' : 'taker') : (fee > 1e-9 ? 'taker' : 'maker'),
      };
    });
}

/* the API's rows plus any newer pushed fill: one of a market already shown, or of a market newer than
   everything the API returned (its first fills are in the push, so its running columns still start right) */
function currentFills() {
  if (FILLS.key !== fillsKey()) return null;
  const rows = FILLS.rows.slice();
  const have = new Set(rows.map(fillId));
  const slugs = new Set(rows.map((f) => f.slug));
  const newest = rows.reduce((m, f) => Math.max(m, Number(f.ts) || 0), 0);
  const sel = state.selected;
  pushedFills().forEach((f) => {
    if (have.has(fillId(f))) return;
    if (ORDERS.scope === 'market' ? f.slug !== sel : !(slugs.has(f.slug) || f.ts > newest)) return;
    rows.push(f);
    have.add(fillId(f));
  });
  rows.sort((a, b) => (a.ts || 0) - (b.ts || 0));
  return rows;
}

/* one market's fills, oldest first -> the ledger's numbers. A BUY of q at p with fee f pays q - qp - f if
   its side wins and -(qp + f) if not; a SELL is the mirror (qp - f, minus q if its side wins). */
function fillLedger(fills) {
  const held = { UP: 0, DOWN: 0 };
  let up = 0, dn = 0, notional = 0, fees = 0;
  const rows = fills.map((f) => {
    const q = Number(f.size), p = Number(f.price), fee = Number(f.fee) || 0, n = q * p;
    const side = f.side === 'DOWN' ? 'DOWN' : 'UP';
    const buy = f.action !== 'SELL';
    const cash = buy ? -(n + fee) : n - fee;
    const pay = buy ? q : -q;
    const thisUp = cash + (side === 'UP' ? pay : 0);
    const thisDn = cash + (side === 'DOWN' ? pay : 0);
    held[side] += buy ? q : -q;
    up += thisUp; dn += thisDn; notional += n; fees += fee;
    return { f, side, buy, n, fee, held: held[side], thisUp, thisDn, up, dn };
  });
  return { rows, notional, fees, up, dn };
}

const usd2 = (v) => '$' + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usdSigned = (v) => { const r = Math.round(v * 100) / 100; return (r < 0 ? '−' : '+') + usd2(r); };
const sh2 = (v) => Number(Math.abs(v) < 0.005 ? 0 : v)
  .toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const px34 = (p) => (Math.abs(p - Math.round(p * 1000) / 1000) < 1e-6 ? p.toFixed(3) : p.toFixed(4));
// win: true for the winning outcome's column (bold), false for the other, null while nobody leads
const pnlCell = (v, win = null) => {
  const r = Math.round(v * 100) / 100;
  const cls = [r > 0 ? 'pos' : r < 0 ? 'neg' : '', win === true ? 'fl-win' : win === false ? 'fl-lose' : ''].filter(Boolean).join(' ');
  return `<td${cls ? ` class="${cls}"` : ''}>${usdSigned(v)}</td>`;
};
const sideWord = (s) => (s === 'DOWN' ? 'Down' : 'Up');

/* The side whose columns are bold (2026-10-12, the user): the outcome that won once the market settled; before
   that, the side ahead now -- the higher token price in the market's latest price point. */
function winningSide(slug) {
  const won = FILLS.outcomes[slug];
  if (won) return { side: won, settled: true };
  const pts = ((state.live && state.live.price_history) || {})[slug];
  const last = pts && pts.length ? pts[pts.length - 1] : null;
  const up = last ? Number(last.up) : NaN;
  if (!Number.isFinite(up)) return null;
  const dn = Number.isFinite(Number(last.down)) ? Number(last.down) : 1 - up;
  return up === dn ? null : { side: up > dn ? 'UP' : 'DOWN', settled: false };
}
const winWord = (w) => (w ? `${sideWord(w.side)} ${w.settled ? 'won' : 'leading'}` : '');

function renderOrderList(force = false) {
  const table = $('#orders-table');
  if (!table) return;
  if (FILLS.key !== fillsKey() || Date.now() - FILLS.t > FILLS_REFRESH_MS) loadFills();
  const sel = state.selected;
  const fills = currentFills();

  // the orders behind them, for the footer: how many were sent, are still working, never filled
  let orders = [...ORDERS.map.values()];
  if (ORDERS.scope === 'market') orders = orders.filter((o) => o.slug === sel);
  else if (fills) { const shown = new Set(fills.map((f) => f.slug)); orders = orders.filter((o) => shown.has(o.slug)); }
  const working = orders.filter((o) => o.outcome === 'sent').length;
  const unfilled = orders.filter((o) => o.outcome !== 'sent' && !isFilled(o)).length;

  const shownSlugs = fills ? [...new Set(fills.map((f) => f.slug))] : [];
  const winners = Object.fromEntries(shownSlugs.map((s) => [s, winningSide(s)]));
  const sig = JSON.stringify([fillsKey(), fills ? fills.length : -1, fills && fills.length ? fillId(fills[fills.length - 1]) : '',
    FILLS.outcomes, orders.length, working, unfilled, shownSlugs.map((s) => winWord(winners[s]))]);
  if (!changed('orderlist', sig) && !force) return;

  const won = ORDERS.scope === 'market' ? winningSide(sel) : null;
  $('#orders-count').textContent = fills && fills.length
    ? `${fmt.num(fills.length)} fill${fills.length === 1 ? '' : 's'}`
      + (ORDERS.scope === 'all' ? ` · newest ${FILL_MARKETS} markets` : '')
      + (won ? ` · ${winWord(won)}` : '')
      + (orders.length ? ` · ${fmt.num(orders.length)} order${orders.length === 1 ? '' : 's'}`
        + (working ? `, ${working} working` : '') + (unfilled ? `, ${unfilled} not filled` : '') : '')
    : (working ? `${working} order${working === 1 ? '' : 's'} working` : '');
  if (!fills || !fills.length) {
    table.innerHTML = `<tbody><tr><td class="live-empty">${!fills ? 'Loading fills…'
      : ORDERS.scope === 'market' ? 'No fills on this market yet.' : 'No fills yet.'}</td></tr></tbody>`;
    return;
  }

  const scroller = table.parentElement;
  const keyChanged = table.dataset.key !== fillsKey();
  const atBottom = keyChanged || !scroller
    || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 24;

  const head = '<thead><tr>'
    + `<th scope="col" rowspan="2">Time (${TZ_LABEL})</th><th scope="col" rowspan="2" class="l">Side</th>`
    + '<th scope="col" rowspan="2" class="l">Outcome</th><th scope="col" rowspan="2">Shares</th>'
    + '<th scope="col" rowspan="2">Price</th><th scope="col" rowspan="2">Notional</th>'
    + '<th scope="col" rowspan="2" title="Shares of this outcome held after the fill">Held after</th>'
    + '<th scope="col" rowspan="2" title="Taker fee paid on this fill (makers pay none)">Fee</th>'
    + '<th scope="colgroup" colspan="2" class="fl-group">This fill</th>'
    + '<th scope="colgroup" colspan="2" class="fl-group">Overall after this fill</th></tr><tr>'
    + '<th scope="col">If Up wins</th><th scope="col">If Down wins</th>'
    + '<th scope="col">If Up wins</th><th scope="col">If Down wins</th></tr></thead>';

  const bySlug = new Map();
  fills.forEach((f) => { if (!bySlug.has(f.slug)) bySlug.set(f.slug, []); bySlug.get(f.slug).push(f); });
  const flashOn = FILLS.primed;
  let body = '';
  bySlug.forEach((list, slug) => {
    const L = fillLedger(list);
    const w = winners[slug];
    const upWin = w ? w.side === 'UP' : null, dnWin = w ? w.side === 'DOWN' : null;
    const selCls = slug === sel ? ' is-sel' : '';
    if (ORDERS.scope === 'all') {
      body += `<tr class="fl-market${selCls}" data-slug="${esc(slug)}" data-ts="${list[0].ts || ''}">`
        + `<td colspan="12" class="l">${orderMarket(slug)}${w ? ` · ${winWord(w)}` : ''}</td></tr>`;
    }
    L.rows.forEach((r) => {
      const f = r.f, id = fillId(f);
      const flash = flashOn && !FILLS.drawn.has(id);
      FILLS.drawn.add(id);
      const tip = `${fmt.clock(f.ts)} ${TZ_LABEL}${f.kind ? ` · ${kindLabel(String(f.kind).toLowerCase())}` : ''}`
        + `\nClick to ${state.live && pickerMarkets().some((m) => m.slug === slug) ? 'show it on the chart' : 'open this trade'}`;
      body += `<tr data-slug="${esc(slug)}" data-side="${esc(r.side)}" data-ts="${f.ts || ''}"`
        + `${flash || selCls ? ` class="${[flash ? 'is-new' : '', selCls.trim()].filter(Boolean).join(' ')}"` : ''} title="${esc(tip)}">`
        + `<td>${fmt.hms(f.ts)}</td>`
        + `<td class="l"><span class="act ${r.buy ? 'act-buy' : 'act-sell'}">${r.buy ? 'BUY' : 'SELL'}</span>`
        + ` <span class="fl-liq">${f.liq === 'maker' ? 'maker' : 'taker'}</span></td>`
        + `<td class="l"><span class="side-tag side-mini" data-side="${r.side}">${sideWord(r.side)}</span></td>`
        + `<td>${sh2(f.size)}</td><td>${px34(Number(f.price))}</td><td>${usd2(r.n)}</td>`
        + `<td>${sh2(r.held)}</td><td>${usd2(r.fee)}</td>`
        + pnlCell(r.thisUp, upWin) + pnlCell(r.thisDn, dnWin) + pnlCell(r.up, upWin) + pnlCell(r.dn, dnWin) + '</tr>';
    });
    body += `<tr class="fl-total${selCls}" data-slug="${esc(slug)}" data-ts="${list[list.length - 1].ts || ''}">`
      + `<td colspan="5" class="l">Total · ${fmt.num(list.length)} fill${list.length === 1 ? '' : 's'}</td>`
      + `<td>${usd2(L.notional)}</td><td></td><td>${usd2(L.fees)}</td>`
      + pnlCell(L.up, upWin) + pnlCell(L.dn, dnWin) + '<td></td><td></td></tr>';
  });
  table.innerHTML = head + `<tbody>${body}</tbody>`;
  table.dataset.key = fillsKey();
  if (scroller && atBottom) scroller.scrollTop = scroller.scrollHeight;
  FILLS.primed = true;
}

function wireOrderList() {
  const table = $('#orders-table');
  markSeg('#orders-scope', 'scope', ORDERS.scope);
  $('#orders-scope').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-scope]');
    if (!btn || btn.dataset.scope === ORDERS.scope) return;
    ORDERS.scope = btn.dataset.scope;
    try { localStorage.setItem('tpb-orders-scope', ORDERS.scope); } catch { /* private mode */ }
    markSeg('#orders-scope', 'scope', ORDERS.scope);
    renderOrderList(true);
  });
  table.addEventListener('click', (ev) => {
    const tr = ev.target.closest('tr[data-slug]');
    if (!tr || !tr.dataset.slug) return;
    const slug = tr.dataset.slug;
    if (state.live && pickerMarkets().some((m) => m.slug === slug)) {
      setSelected(slug);
      pollLive();
      render(true);
    } else {
      openTrade({ slug, side: tr.dataset.side, ts: tr.dataset.ts });
    }
  });
  table.addEventListener('mouseover', (ev) => {
    const tr = ev.target.closest('tr[data-oid]');
    if (tr) hotOrder(tr.dataset.oid, true);
  });
  table.addEventListener('mouseout', (ev) => {
    const tr = ev.target.closest('tr[data-oid]');
    if (tr && !tr.contains(ev.relatedTarget)) hotOrder(tr.dataset.oid, false);
  });
}

wireOrderList();
seedOrders();
setInterval(() => { if (state.page === 'market' && ORDERS.seeded) seedOrders(); }, ORDERS_REFRESH_MS);
wireTrade();
tickClock();
setInterval(tickClock, 1000);
wireSaved();
wireRouter();
if (state.page !== 'saved') setTimeout(loadSavedCount, 1200);
