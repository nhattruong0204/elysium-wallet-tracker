// Minimal progressive enhancement: copy buttons, local time on hover, custom-range form.
(function () {
  'use strict';
  document.addEventListener('click', function (e) {
    var b = e.target.closest('button.copy');
    if (!b) return;
    var text = b.getAttribute('data-copy');
    var done = function () { var t = b.textContent; b.textContent = 'copied'; setTimeout(function () { b.textContent = t; }, 1200); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () {});
    } else {
      var ta = document.createElement('textarea'); ta.value = text; ta.setAttribute('readonly', '');
      ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch (err) {} document.body.removeChild(ta);
    }
  });
  // Show each <time data-ts> in the visitor's zone as a tooltip; the visible text stays UTC.
  try {
    var fmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    document.querySelectorAll('time[data-ts]').forEach(function (t) {
      var d = new Date(t.getAttribute('datetime'));
      if (!isNaN(d)) t.title = 'Your time: ' + fmt.format(d);
    });
  } catch (err) {}
  // Custom range: prefill with the current window.
  var f = document.getElementById('custom-form');
  if (f) f.addEventListener('submit', function () { f.querySelector('[name=range]').value = 'custom'; });
})();
