(function () {
  "use strict";
  // Today's live region (U11). The server renders #day-live with:
  //   data-base-seconds    effective seconds at the server instant
  //   data-target-seconds  daily target in seconds
  //   data-live            "true" while inside (the numbers tick)
  //   data-state           inside | met | out
  //   data-server-epoch-ms the instant base seconds was computed
  //   data-updated         that instant as HH:MM:SS (IST)
  //   data-poll            fragment URL; present only on Today
  // Values are derived, never counted: base + (now + offset - anchor).
  var POLL_MS = 30000;
  var JITTER_MS = 5000;
  var offset = null; // server clock minus browser clock, measured once
  var tickTimer = null;
  var pollTimer = null;
  var inflight = false;
  var failures = 0;
  var before = null;

  function region() {
    return document.getElementById("day-live");
  }

  function num(el, name) {
    return parseInt(el.dataset[name], 10) || 0;
  }

  function anchor(r) {
    return num(r, "serverEpochMs");
  }

  function elapsed(r) {
    if (r.dataset.live !== "true" || offset === null) {
      return 0;
    }
    return Math.max(0, Math.floor((Date.now() + offset - anchor(r)) / 1000));
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function clock(s) {
    return Math.floor(s / 3600) + ":" + pad(Math.floor((s % 3600) / 60)) + ":" + pad(s % 60);
  }

  function hours(s) {
    var m = Math.floor(s / 60);
    return Math.floor(m / 60) + "h " + pad(m % 60) + "m";
  }

  function short(m) {
    return m < 60 ? m + "m" : Math.floor(m / 60) + "h " + pad(m % 60) + "m";
  }

  // Mirrors day_story.left_text: "left" rounds up, "over" rounds down.
  function leftText(done, target) {
    if (done >= target) {
      return "+" + short(Math.floor((done - target) / 60)) + " over";
    }
    return short(Math.ceil((target - done) / 60)) + " left";
  }

  function setText(el, text) {
    if (el.textContent !== text) {
      el.textContent = text;
    }
  }

  function render() {
    var r = region();
    if (!r) {
      return;
    }
    var e = elapsed(r);
    var done = num(r, "baseSeconds") + e;
    var target = num(r, "targetSeconds");
    r.querySelectorAll("[data-tick]").forEach(function (el) {
      setText(el, clock(num(el, "tick") + e));
    });
    r.querySelectorAll("[data-tick-hours]").forEach(function (el) {
      setText(el, hours(num(el, "tickHours") + e));
    });
    r.querySelectorAll("[data-left]").forEach(function (el) {
      setText(el, leftText(done, target));
    });
    r.querySelectorAll("[data-ring-seconds]").forEach(function (ring) {
      var pct = target > 0 ? Math.min(100, Math.round((done * 100) / target)) : 100;
      ring.style.strokeDashoffset = String(100 - pct);
    });
  }

  // Tick on each second boundary of the server clock; setTimeout, not rAF,
  // so a throttled tab simply catches up on the next tick.
  function tick() {
    clearTimeout(tickTimer);
    var r = region();
    if (!r || r.dataset.live !== "true" || failures >= 2) {
      return;
    }
    render();
    var phase = (Date.now() + offset - anchor(r)) % 1000;
    tickTimer = setTimeout(tick, 1000 - phase + 5);
  }

  function announce(text) {
    var status = document.getElementById("live-status");
    if (status && text) {
      status.textContent = text;
    }
  }

  function showFailure() {
    var r = region();
    if (!r) {
      return;
    }
    var updated = r.dataset.updated || "";
    r.querySelectorAll("[data-sync-line]").forEach(function (el) {
      el.hidden = false;
      el.textContent = failures >= 2 ? "Last known · updated " + updated + " · retrying" : "Updated " + updated + " · retrying";
    });
    if (failures >= 2) {
      clearTimeout(tickTimer);
      r.querySelectorAll("[data-tick]").forEach(function (el) {
        el.classList.add("is-stale");
      });
    }
  }

  function refresh() {
    var r = region();
    if (!r || !r.dataset.poll || !window.htmx || inflight) {
      return;
    }
    inflight = true;
    window.htmx.ajax("GET", r.dataset.poll, { target: "#day-live", swap: "outerHTML" });
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    var r = region();
    if (!r || !r.dataset.poll) {
      return;
    }
    pollTimer = setTimeout(function () {
      if (!document.hidden) {
        refresh();
      }
      schedulePoll();
    }, POLL_MS + Math.random() * JITTER_MS);
  }

  function isOurs(event) {
    var t = event.detail && (event.detail.target || event.detail.elt);
    return t && t.id === "day-live";
  }

  function detached(event) {
    var t = event.detail && event.detail.target;
    return !t || !t.isConnected;
  }

  // (Re)start for whatever #day-live is on the page now: the Day page at
  // load, or a region that arrived inside another swap (the quick view).
  function init() {
    clearTimeout(tickTimer);
    clearTimeout(pollTimer);
    failures = 0;
    inflight = false;
    before = null;
    var r = region();
    if (!r) {
      return;
    }
    offset = anchor(r) - Date.now();
    tick();
    schedulePoll();
  }

  document.body.addEventListener("htmx:beforeSwap", function (event) {
    if (!isOurs(event)) {
      return;
    }
    if (detached(event)) {
      // The region left (drawer closed) while this poll was in flight.
      event.detail.shouldSwap = false;
      before = null;
      return;
    }
    var r = region();
    before = r ? { state: r.dataset.state, base: num(r, "baseSeconds") } : null;
  });

  document.body.addEventListener("htmx:afterSettle", function () {
    var r = region();
    // outerHTML replaced the old node, so match by the recorded state.
    if (!r || !before) {
      return;
    }
    failures = 0;
    var target = num(r, "targetSeconds");
    var base = num(r, "baseSeconds");
    if (r.dataset.state !== before.state) {
      announce(r.dataset.state === "inside" ? "Inside" : "Out");
    }
    // Only a server value confirms the target: an unseen exit could take
    // back a tick-only crossing.
    if (before.base < target && base >= target) {
      announce("Target reached");
    }
    before = null;
    tick();
  });

  document.body.addEventListener("htmx:afterSwap", function (event) {
    // Our own outerHTML poll is handled in afterSettle; any other swap that
    // brought a #day-live in starts the timers afresh for it.
    if (isOurs(event)) {
      return;
    }
    var t = event.detail && event.detail.target;
    var r = region();
    if (r && t && t.contains(r)) {
      init();
    }
  });

  document.body.addEventListener("htmx:afterRequest", function (event) {
    if (!isOurs(event)) {
      return;
    }
    if (detached(event)) {
      return;
    }
    inflight = false;
    if (!event.detail.successful) {
      failures += 1;
      showFailure();
    }
  });

  document.body.addEventListener("htmx:sendError", function (event) {
    if (isOurs(event)) {
      inflight = false;
    }
  });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      return;
    }
    tick();
    refresh();
    schedulePoll();
  });

  init();
})();
