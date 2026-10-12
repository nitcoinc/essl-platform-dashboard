(function () {
  "use strict";
  // Live hours on Today: every [data-timer-seconds][data-live="true"] element
  // counts up from its server value. Text only changes once a minute.
  // The progress ring on Today fills along with it.
  var rings = Array.prototype.slice.call(
    document.querySelectorAll('[data-ring-seconds][data-live="true"]')
  );
  var els = Array.prototype.slice.call(
    document.querySelectorAll('[data-timer-seconds][data-live="true"]')
  );
  if (!els.length) {
    return;
  }
  var startedAt = Date.now();
  var base = els.map(function (el) {
    return parseInt(el.dataset.timerSeconds, 10) || 0;
  });

  function format(totalSeconds) {
    var minutes = Math.floor(totalSeconds / 60);
    return Math.floor(minutes / 60) + "h " + String(minutes % 60).padStart(2, "0") + "m";
  }

  function render() {
    var elapsed = Math.floor((Date.now() - startedAt) / 1000);
    els.forEach(function (el, i) {
      var text = format(base[i] + elapsed);
      if (el.textContent !== text) {
        el.textContent = text;
      }
    });
    rings.forEach(function (ring) {
      var target = parseInt(ring.dataset.ringTarget, 10) || 1;
      var done = (parseInt(ring.dataset.ringSeconds, 10) || 0) + elapsed;
      ring.style.strokeDashoffset = String(100 - Math.min(100, Math.round((done * 100) / target)));
    });
  }

  render();
  setInterval(render, 5000);
})();
