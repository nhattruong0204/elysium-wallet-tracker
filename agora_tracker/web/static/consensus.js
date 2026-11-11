// Consensus settings: remembered per browser. A bare /consensus visit restores the last
// applied settings; "Reset" forgets them. Storage may be unavailable: then nothing is kept.
(function () {
  'use strict';
  var KEY = 'consensus-settings-v1';
  var form = document.getElementById('cons-set');
  if (!form) return;
  var qs = window.location.search;
  function store(v) { try { if (v === null) localStorage.removeItem(KEY); else localStorage.setItem(KEY, v); } catch (e) { /* private mode */ } }
  function load() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  var isReset = qs.indexOf('reset=1') !== -1;
  if (isReset) store(null);
  if (!qs) {
    var saved = load();
    if (saved && /^[a-z0-9_=&.%-]+$/i.test(saved)) { window.location.replace('/consensus?' + saved); return; }
  } else if (!isReset) {
    store(qs.slice(1));
  }
  form.querySelectorAll('select, input[type=checkbox]').forEach(function (el) {
    el.addEventListener('change', function () { form.submit(); });
  });
  // Owner-only shared settings, saved to Postgres. Never persist the admin key anywhere.
  var save = document.getElementById('cons-tg-save');
  var key = document.getElementById('cons-tg-key');
  var enabled = document.getElementById('cons-tg-enabled');
  var status = document.getElementById('cons-tg-status');
  var version = null;
  function notify(s) { status.textContent = s; }
  fetch('/api/v1/consensus/settings', {credentials: 'omit', cache: 'no-store'})
    .then(function (r) { if (!r.ok) throw new Error('unavailable'); return r.json(); })
    .then(function (d) {
      version = d.version;
      enabled.checked = d.enabled;
      notify('Shared bot settings: ' + (d.enabled ? 'ON' : 'OFF')
             + ' · version ' + d.version
             + '. Change view filters above, then save to publish them.');
    }).catch(function () {
      notify('Server-side settings unavailable (not configured or service offline).');
      save.disabled = true;
    });
  save.addEventListener('click', function () {
    if (version === null || !key.value) { notify('Enter owner key to save.'); return; }
    var params = new URLSearchParams(new FormData(form));
    var data = {
      version: version, enabled: enabled.checked,
      traders: Number(params.get('traders')),
      window: Number(params.get('window')),
      min_net: Number(params.get('min_net')),
      range: params.get('range'),
      launchpad: params.get('launchpad') || '',
      independent: params.get('independent') === '1'
    };
    save.disabled = true;
    notify('Saving shared bot settings…');
    fetch('/api/v1/consensus/settings', {
      method: 'POST', credentials: 'omit', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key.value},
      body: JSON.stringify(data)
    }).then(function (r) {
      if (!r.ok) throw new Error('Save failed (HTTP ' + r.status + ')');
      return r.json();
    }).then(function (d) {
      version = d.version;
      notify('Saved. Bot will use version ' + d.version + ' on the next poll ('
             + (d.enabled ? 'enabled' : 'disabled') + ').');
    }).catch(function (e) { notify(e.message + '; no changes confirmed.'); })
      .finally(function () { key.value = ''; save.disabled = false; });
  });
})();
