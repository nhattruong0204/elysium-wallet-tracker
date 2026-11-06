// Activity monitor: visibility-aware incremental polling with scroll preservation.
// One request in flight at a time, backoff on failure, nothing runs while the tab is hidden
// (or at a slow rate), and the page is never reloaded: new rows are fetched as JSON from
// /api/v1/activity?since_id=N and inserted by DOM calls (text nodes only, no innerHTML).
(function () {
  'use strict';
  var root = document.getElementById('activity');
  if (!root) return;

  var $ = function (id) { return document.getElementById(id); };
  var body = $('act-body'), table = $('act-table');
  var cfg = {
    api: root.getAttribute('data-api'),
    live: root.getAttribute('data-live') === '1',
    poll: Math.max(5, parseInt(root.getAttribute('data-poll'), 10) || 20) * 1000,
    hiddenPoll: (parseInt(root.getAttribute('data-hidden-poll'), 10) || 0) * 1000,
    sort: root.getAttribute('data-sort')
  };
  var state = {
    head: parseInt(root.getAttribute('data-head-id'), 10) || 0,
    next: root.getAttribute('data-next') || '',
    seen: Object.create(null), pending: [], timer: null, inflight: null, errors: 0,
    auto: true, lastOk: null, local: false, fresh: null
  };
  var MAX_ROWS = 300, ACTIONS = {
    buy: 'Buy', sell: 'Sell', swap: 'Swap', transfer_in: 'Transfer in',
    transfer_out: 'Transfer out', other: 'Multi / other'
  };

  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }

  var originalTitle = document.title, unread = Object.create(null), notified = Object.create(null);
  var audio = null, sound = false, lastTone = 0;
  function paintUnread() {
    var n = Object.keys(unread).length, button = $('act-unread');
    document.title = n ? '(' + n + ') ' + originalTitle : originalTitle;
    if (button) { button.hidden = !n; button.textContent = n + ' new'; }
  }
  function acknowledge() { unread = Object.create(null); paintUnread(); }
  function viewingNewest() { return !document.hidden && document.hasFocus() && atTop(); }
  function tone(items) {
    if (!sound || !audio || audio.state !== 'running' || Date.now() - lastTone < 5000) return;
    // A synchronous shared lease suppresses duplicate tones in other Activity tabs.
    var previous;
    try { previous = JSON.parse(store('act.lastTone') || '{}'); } catch (e) { previous = {}; }
    if (previous.at && Date.now() - previous.at < 5000) return;
    store('act.lastTone', JSON.stringify({ at: Date.now() }));
    try {
      var osc = audio.createOscillator(), gain = audio.createGain();
      osc.frequency.value = 660; gain.gain.setValueAtTime(0.04, audio.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + 0.15);
      osc.connect(gain); gain.connect(audio.destination); osc.start(); osc.stop(audio.currentTime + 0.15);
      lastTone = Date.now();
    } catch (e) { setText('act-audio-status', 'Sound unavailable'); }
  }
  var soundButton = $('act-sound');
  if (soundButton) soundButton.addEventListener('click', function () {
    if (sound) { sound = false; soundButton.textContent = 'Sound: OFF'; soundButton.setAttribute('aria-pressed', 'false'); return; }
    try {
      var Context = window.AudioContext || window.webkitAudioContext;
      audio = audio || new Context();
      Promise.resolve(audio.resume()).then(function () {
        sound = audio.state === 'running';
        soundButton.textContent = sound ? 'Sound: ON' : 'Enable sound';
        soundButton.setAttribute('aria-pressed', String(sound));
        setText('act-audio-status', sound ? '' : 'Click to enable sound; browser blocked audio');
      }).catch(function () { setText('act-audio-status', 'Sound blocked; click to try again'); });
    } catch (e) { setText('act-audio-status', 'Sound unavailable in this browser'); }
  });

  var fmtTip = null;                       // same tooltip app.js puts on server-rendered times
  try { fmtTip = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }); } catch (e) {}

  // ---- rendering (mirrors the act_row macro in _macros.html) -------------------------------
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function short(a, h, t) { h = h || 6; t = t || 4; return !a ? '–' : (a.length <= h + t + 2 ? a : a.slice(0, h) + '…' + a.slice(-t)); }
  function hype(v, places) {
    var f = Number(v);
    if (f !== 0 && Math.abs(f) < Math.pow(10, -places)) return f.toExponential(2).replace(/e([+-])(\d)$/, 'e$10$2');
    return f.toLocaleString('en-US', { minimumFractionDigits: places, maximumFractionDigits: places });
  }
  function utcText(iso) { return iso.replace('T', ' ').replace('Z', '') + ' UTC'; }
  function safeHttps(u) { return typeof u === 'string' && /^https:\/\//i.test(u) ? u : null; }
  function isAddr(a) { return /^0x[0-9a-f]{40}$/.test(a); }

  function renderRow(it) {
    var tr = el('tr');
    tr.setAttribute('data-id', String(it.id));
    tr.setAttribute('data-ts', String(Math.floor(Date.parse(it.block_time) / 1000)));
    tr.setAttribute('data-kind', it.kind);
    var td = el('td', 'nw'), t = el('time', null, utcText(it.block_time));
    t.setAttribute('datetime', it.block_time); t.setAttribute('data-ts', '');
    if (fmtTip) { var d0 = new Date(it.block_time); if (!isNaN(d0)) t.title = 'Your time: ' + fmtTip.format(d0); }
    var txh = safeHttps(it.tx_url);
    if (txh) {
      var tl = el('a', 'txl'); tl.setAttribute('href', txh); tl.setAttribute('target', '_blank');
      tl.setAttribute('rel', 'noopener noreferrer'); tl.setAttribute('title', 'Open transaction ' + it.tx_hash);
      tl.appendChild(t); td.appendChild(tl);
    } else td.appendChild(t);
    tr.appendChild(td);

    td = el('td'); var sp = el('span', 'addr'), a = el('a', null, short(it.wallet));
    if (isAddr(it.wallet)) a.setAttribute('href', '/wallets/' + it.wallet);
    a.setAttribute('title', it.wallet);
    var cp = document.createElement('button'); cp.type = 'button'; cp.className = 'copy'; cp.textContent = 'copy'; cp.setAttribute('data-copy', it.wallet); cp.setAttribute('aria-label', 'Copy address');
    sp.appendChild(a); sp.appendChild(cp); td.appendChild(sp); tr.appendChild(td);

    td = el('td'); a = el('a', 'tok'); var b = el('b', null, it.symbol || short(it.token));
    if (isAddr(it.token)) a.setAttribute('href', '/tokens/' + it.token);
    a.setAttribute('title', it.token); a.appendChild(b); td.appendChild(a); tr.appendChild(td);

    td = el('td');
    if (it.launchpad_label) {
      var lp = safeHttps(it.launchpad_url), label = el(lp ? 'a' : 'span', 'lp', it.launchpad_label + (lp ? ' ↗' : ''));
      if (lp) { label.href = lp; label.target = '_blank'; label.rel = 'noopener noreferrer'; }
      td.appendChild(label);
    }
    else { var nl = el('span', 'faint', '–'); nl.setAttribute('title', 'Agora has no launchpad profile for this token'); td.appendChild(nl); }
    tr.appendChild(td);

    td = el('td'); td.appendChild(el('span', 'act act-' + it.action, ACTIONS[it.action] || it.action)); tr.appendChild(td);

    td = el('td', 'r');
    if (it.amount_hype !== null && it.amount_hype !== undefined) td.textContent = hype(it.amount_hype, 3);
    else {
      var tf = it.kind === 'TRANSFER_IN' || it.kind === 'TRANSFER_OUT';
      var f = el('span', 'faint', tf ? 'no HYPE leg' : 'unknown');
      f.setAttribute('title', tf ? 'A transfer moves tokens without a HYPE payment: it is not a buy or a sell.' : 'The HYPE amount could not be determined.');
      td.appendChild(f);
    }
    tr.appendChild(td);

    td = el('td', 'r');
    if (it.market_cap_text) {
      var mc = el('span', null, it.market_cap_text);
      mc.setAttribute('title', (it.market_cap_source === 'snapshot' ? 'Market cap at this block: nearest stored Agora snapshot' : "Market cap at this block: this trade's price × supply") + ', in USD at the HYPE rate of that time');
      if (it.market_cap_source === 'trade_price') mc.appendChild(el('sup', 'faint', 'p'));
      td.appendChild(mc);
    } else { var nm = el('span', 'faint', '–'); nm.setAttribute('title', 'No market data near this block'); td.appendChild(nm); }
    tr.appendChild(td);

    td = el('td');
    td.appendChild(el('span', 'badge ' + (it.basis === 'verified' ? 'ok' : it.basis === 'estimated' ? 'est' : ''), it.basis === 'verified' ? 'verified' : it.basis === 'estimated' ? 'estimated' : 'unknown'));
    tr.appendChild(td);

    td = el('td', 'r mono', Number(it.block_number).toLocaleString('en-US')); tr.appendChild(td);

    applyLocal(tr);
    return tr;
  }

  // ---- time zone toggle (display only; storage and queries stay UTC) -------------------------
  var fmtLocal = null;
  try { fmtLocal = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZoneName: 'short' }); } catch (e) {}
  function applyLocal(scope) {
    scope.querySelectorAll('time[data-ts]').forEach(function (t) {
      var iso = t.getAttribute('datetime');
      if (state.local && fmtLocal) {
        var d = new Date(iso);
        if (!isNaN(d)) t.textContent = fmtLocal.format(d);
      } else t.textContent = utcText(iso);
    });
  }

  // ---- row bookkeeping --------------------------------------------------------------------------
  function indexExisting() {
    if (!body) return;
    body.querySelectorAll('tr[data-id]').forEach(function (r) { state.seen[r.getAttribute('data-id')] = true; });
  }
  function rowKey(tr) { return [parseInt(tr.getAttribute('data-ts'), 10), parseInt(tr.getAttribute('data-id'), 10)]; }
  function newer(a, b) { return a[0] > b[0] || (a[0] === b[0] && a[1] > b[1]); }
  function insertSorted(tr) {              // newest first: place before the first older row
    var rows = body.children, k = rowKey(tr), i;
    for (i = 0; i < rows.length; i++) { if (newer(k, rowKey(rows[i]))) break; }
    body.insertBefore(tr, rows[i] || null);
    tr.classList.add('fresh-row');
    setTimeout(function () { tr.classList.remove('fresh-row'); }, 2500);
  }
  function showTable() {
    var empty = $('act-empty'); if (empty) empty.hidden = true;
    if (table && table.parentNode.hidden) table.parentNode.hidden = false;
  }
  function insertItems(items) {
    var n = 0;
    items.slice().sort(function (a, b) { return Date.parse(a.block_time) - Date.parse(b.block_time) || a.id - b.id; })
      .forEach(function (it) {
        if (state.seen[it.id]) return;
        state.seen[it.id] = true; showTable(); insertSorted(renderRow(it)); n++;
      });
    return n;
  }
  function atTop() { return (window.pageYOffset || document.documentElement.scrollTop || 0) < 120; }

  // ---- status line -------------------------------------------------------------------------------
  function ago(iso) {
    if (!iso) return null;
    var s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
    return s < 90 ? s + 's ago' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 172800 ? (s / 3600).toFixed(1) + ' h ago' : (s / 86400).toFixed(1) + ' d ago';
  }
  function setText(id, t) { var n = $(id); if (n && n.textContent !== t) n.textContent = t; }
  function tickClock() {
    var hb = $('act-hb'), last = $('act-last');
    if (hb && hb.getAttribute('data-at')) setText('act-hb', ago(hb.getAttribute('data-at')));
    if (last && last.getAttribute('data-at')) setText('act-last', ago(last.getAttribute('data-at')));
    if (state.lastOk) setText('act-refreshed-b', ago(new Date(state.lastOk).toISOString()));
  }
  function setFresh(f) {
    if (!f) return;
    state.fresh = f;
    var dot = $('act-dot'); if (dot) dot.className = 'sdot ' + f.scanner_status;
    setText('act-scanner', 'Scanner: ' + f.scanner_status);
    setText('act-note', f.scanner_note || '');
    if (f.last_indexed_block !== null && f.last_indexed_block !== undefined) setText('act-block', Number(f.last_indexed_block).toLocaleString('en-US'));
    var hb = $('act-hb'); if (hb) hb.setAttribute('data-at', f.indexer_heartbeat_at || '');
    var last = $('act-last'); if (last) { last.setAttribute('data-at', f.last_observed_transaction_at || ''); if (!f.last_observed_transaction_at) last.textContent = 'none yet'; }
    var stale = $('act-stale');
    if (f.scanner_status === 'stale' || f.scanner_status === 'unknown') {
      if (!stale) {
        stale = el('div', 'banner bad'); stale.id = 'act-stale';
        stale.appendChild(el('b', null, 'The scanner is not reporting. '));
        stale.appendChild(document.createTextNode((f.scanner_note || '') + '. New activity will not appear until it recovers.'));
        root.insertBefore(stale, $('act-new').nextSibling);
      }
    } else if (stale) stale.parentNode.removeChild(stale);
    tickClock();
  }
  function setError(msg) {
    var e = $('act-err'); if (!e) return;
    e.hidden = !msg; e.textContent = msg || '';
  }
  function setPendingBanner() {
    var n = state.pending.length, box = $('act-new');
    if (!box) return;
    box.hidden = n === 0;
    setText('act-new-n', String(n) + (state.overflow ? '+' : ''));
    setText('act-new-t', n === 1 && !state.overflow ? 'new activity' : 'new activities');
  }
  function flushPending() {
    if (!state.pending.length) return;
    if (body.children.length + state.pending.length > MAX_ROWS) {      // keep the DOM bounded
      var b = $('act-new'); b.hidden = false;
      b.textContent = '';
      var a = el('a', null, 'Reload to see the newest activity'); a.setAttribute('href', location.pathname + location.search);
      b.appendChild(a); return;
    }
    insertItems(state.pending); state.pending = []; state.overflow = false; setPendingBanner(); acknowledge();
  }

  // ---- polling -------------------------------------------------------------------------------------
  function qsWithout(skip) {
    var u = new URLSearchParams(location.search);
    skip.forEach(function (k) { u.delete(k); });
    return u;
  }
  function interval() {
    var base = document.hidden ? cfg.hiddenPoll : cfg.poll;
    if (!base) return 0;
    if (state.errors) base = Math.min(300000, base * Math.pow(2, Math.min(state.errors, 5)));
    return base * (0.85 + Math.random() * 0.3);
  }
  function schedule(delay) {
    clearTimeout(state.timer); state.timer = null;
    if (!cfg.live || !state.auto) return;
    var d = delay === undefined ? interval() : delay;
    if (d === 0 && document.hidden && !cfg.hiddenPoll) return;         // hidden and paused
    state.timer = setTimeout(poll, d);
  }
  function poll() {
    state.timer = null;
    if (state.inflight || !state.auto) return;                        // never overlap
    if (document.hidden && !cfg.hiddenPoll) return;
    var u = qsWithout(['cursor', 'dir', 'since_id', 'sort']);
    u.set('since_id', String(state.head));
    var ctl = new AbortController(), to = setTimeout(function () { ctl.abort(); }, 10000);
    state.inflight = ctl;
    fetch(cfg.api + '?' + u.toString(), { signal: ctl.signal, headers: { Accept: 'application/json' }, credentials: 'omit' })
      .then(function (r) {
        if (r.status === 429) {
          var ra = parseInt(r.headers.get('Retry-After'), 10) || 10;
          var e = new Error('rate limited'); e.retry = ra * 1000; throw e;
        }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (j) {
        if (j.meta && j.meta.stale) return;
        state.errors = 0; state.lastOk = Date.now(); setError('');
        setFresh(j.freshness);
        if (typeof j.head_id === 'number' && j.head_id > state.head) state.head = j.head_id;
        if (!state.auto) return;
        var items = (j.items || []).filter(function (it) {
          if (state.seen[it.id] || notified[it.id]) return false;
          notified[it.id] = true; return true;
        });
        if (items.length) {
          items.forEach(function (it) { unread[it.id] = true; }); paintUnread(); tone(items);
        }
        if (items.length) {
          if (viewingNewest() && !state.pending.length) { insertItems(items); acknowledge(); }
          else {
            items.forEach(function (it) { if (!state.pending.some(function (p) { return p.id === it.id; })) state.pending.push(it); });
            setPendingBanner();
          }
        }
        if (j.overflow) { state.overflow = true; setPendingBanner(); }
        var rf = $('act-refreshed'); if (rf) rf.innerHTML = 'Browser refreshed <b id="act-refreshed-b">just now</b>';
      })
      .catch(function (e) {
        if (e && e.retry) { setError('Rate limited, pausing for ' + Math.round(e.retry / 1000) + 's'); state.errors = Math.max(state.errors, 1); schedule(e.retry); return 'scheduled'; }
        state.errors++;
        setError('Connection problem' + (e && e.name === 'AbortError' ? ' (timeout)' : '') + ': retrying with backoff');
      })
      .then(function (flag) {
        clearTimeout(to); state.inflight = null;
        if (flag !== 'scheduled') schedule();
      });
  }

  // ---- controls ---------------------------------------------------------------------------------------
  var autoBtn = $('act-auto');
  function paintAuto() {
    if (!autoBtn) return;
    autoBtn.setAttribute('aria-pressed', state.auto ? 'true' : 'false');
    autoBtn.innerHTML = 'Auto-refresh: <b>' + (state.auto ? 'ON' : 'OFF') + '</b>';
  }
  if (autoBtn) autoBtn.addEventListener('click', function () {
    state.auto = !state.auto; store('act.auto', state.auto ? '1' : '0'); paintAuto();
    if (state.auto) { poll(); } else { clearTimeout(state.timer); state.timer = null; if (state.inflight) state.inflight.abort(); }
  });
  var unreadButton = $('act-unread');
  if (unreadButton) unreadButton.addEventListener('click', function () {
    flushPending(); window.scrollTo(0, 0);
    if (!state.pending.length) acknowledge();
  });
  var show = $('act-show');
  if (show) show.addEventListener('click', function () {
    flushPending(); window.scrollTo(0, 0);
  });
  window.addEventListener('scroll', (function () {
    var t = null;
    return function () {
      if (t) return;
      t = setTimeout(function () { t = null; if (viewingNewest() && state.auto && state.pending.length) flushPending(); }, 150);
    };
  })(), { passive: true });
  document.addEventListener('visibilitychange', function () {
    if (!cfg.live) return;
    if (document.hidden) { clearTimeout(state.timer); schedule(); }
    else if (state.auto) { var due = !state.lastOk || Date.now() - state.lastOk > cfg.poll; schedule(due ? 0 : undefined); }
  });
  window.addEventListener('pagehide', function () {
    clearTimeout(state.timer); if (state.inflight) state.inflight.abort();
  });

  var loc = $('act-local');
  if (loc) {
    state.local = store('act.local') === '1'; loc.checked = state.local; applyLocal(root);
    loc.addEventListener('change', function () { state.local = loc.checked; store('act.local', loc.checked ? '1' : '0'); applyLocal(root); });
  }

  // "Load older": append the next keyset page in place instead of navigating
  var older = $('act-older'), olderLink = $('act-older-link');
  if (older && state.next) {
    older.hidden = false;
    older.addEventListener('click', function () {
      if (older.disabled) return;
      older.disabled = true;
      var u = qsWithout(['cursor', 'dir', 'since_id']);
      u.set('cursor', state.next); u.set('dir', 'next');
      fetch(cfg.api + '?' + u.toString(), { headers: { Accept: 'application/json' }, credentials: 'omit' })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function (j) {
          (j.items || []).forEach(function (it) {
            if (state.seen[it.id]) return; state.seen[it.id] = true; showTable(); body.appendChild(renderRow(it));
          });
          state.next = (j.page && j.page.next_cursor) || '';
          if (!state.next) { older.hidden = true; if (olderLink) olderLink.outerHTML = '<span class="dis">Older →</span>'; }
          setError('');
        })
        .catch(function () { setError('Could not load older activity; try again'); })
        .then(function () { older.disabled = false; });
    });
  }

  window.ActivityMonitor = { renderRow: renderRow, state: state, poll: poll };
  indexExisting();
  state.auto = store('act.auto') !== '0';
  paintAuto();
  state.lastOk = Date.now();
  setInterval(tickClock, 1000);
  schedule();
})();
