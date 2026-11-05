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
  if (qs.indexOf('reset=1') !== -1) { store(null); return; }
  if (!qs) {
    var saved = load();
    if (saved && /^[a-z0-9_=&.%-]+$/i.test(saved)) { window.location.replace('/consensus?' + saved); return; }
  } else {
    store(qs.slice(1));
  }
  form.querySelectorAll('select, input[type=checkbox]').forEach(function (el) {
    el.addEventListener('change', function () { form.submit(); });
  });
})();
