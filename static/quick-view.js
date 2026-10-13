(function () {
  "use strict";
  // Quick-view drawer (U6). A plain primary click on a[data-quick-view]
  // opens the dialog and loads the fragment; any modified click falls
  // through to the link's href (new tab, new window, download).
  var dialog = document.getElementById("quick-view");
  var body = document.getElementById("quick-view-body");
  if (!dialog || !body) {
    return;
  }
  var opener = null;
  var url = null;
  // Set by a day-strip click (U8): after the swap, focus the selected chip
  // instead of the heading. Opener and its from= query stay untouched.
  var focusDay = false;

  function fill(id) {
    body.replaceChildren(document.getElementById(id).content.cloneNode(true));
  }

  function labelStatic() {
    dialog.removeAttribute("aria-labelledby");
    dialog.setAttribute("aria-label", "Person quick view");
  }

  function focusClose() {
    var close = body.querySelector("[data-qv-close]");
    if (close) {
      close.focus();
    }
  }

  function load() {
    labelStatic();
    fill("quick-view-loading");
    body.setAttribute("aria-busy", "true");
    focusClose();
    window.htmx.ajax("GET", url, { source: opener, target: "#quick-view-body", swap: "innerHTML" });
  }

  function showError(missing) {
    focusDay = false;
    body.removeAttribute("aria-busy");
    labelStatic();
    fill(missing ? "quick-view-missing" : "quick-view-error");
    var full = body.querySelector("[data-qv-full]");
    if (full && opener) {
      full.href = opener.href;
    }
    focusClose();
  }

  function ours(event) {
    return event.detail && event.detail.target === body && dialog.open;
  }

  document.body.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
      return;
    }
    var link = event.target.closest ? event.target.closest("a[data-quick-view]") : null;
    if (!link || !window.htmx) {
      return;
    }
    event.preventDefault();
    opener = link;
    url = link.dataset.quickView;
    fill("quick-view-loading");
    dialog.showModal();
    load();
  });

  dialog.addEventListener("click", function (event) {
    var day = event.target.closest ? event.target.closest("a[data-qv-day]") : null;
    if (day) {
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || !window.htmx) {
        return;
      }
      event.preventDefault();
      url = day.dataset.qvDay;
      focusDay = true;
      body.setAttribute("aria-busy", "true");
      window.htmx.ajax("GET", url, { source: day, target: "#quick-view-body", swap: "innerHTML" });
      return;
    }
    // The dialog has no padding, so a hit on the dialog itself is the backdrop.
    if (event.target === dialog || event.target.closest("[data-qv-close]")) {
      dialog.close();
    } else if (event.target.closest("[data-qv-retry]")) {
      load();
    }
  });

  dialog.addEventListener("close", function () {
    // Emptying the body removes #day-live, which stops timer.js.
    body.replaceChildren();
    body.removeAttribute("aria-busy");
    focusDay = false;
    labelStatic();
    var back = opener;
    opener = null;
    // Safari does not focus links on click, so return focus explicitly.
    if (back && back.isConnected) {
      back.focus();
    }
  });

  document.body.addEventListener("htmx:afterSwap", function (event) {
    if (!ours(event)) {
      return;
    }
    body.removeAttribute("aria-busy");
    var heading = document.getElementById("quick-view-title");
    var chip = focusDay ? body.querySelector('[data-qv-day][aria-current="date"]') : null;
    focusDay = false;
    if (heading) {
      dialog.removeAttribute("aria-label");
      dialog.setAttribute("aria-labelledby", "quick-view-title");
    }
    if (chip) {
      chip.focus();
    } else if (heading) {
      heading.focus();
    }
  });

  document.body.addEventListener("htmx:responseError", function (event) {
    if (ours(event)) {
      showError(event.detail.xhr && event.detail.xhr.status === 404);
    }
  });

  document.body.addEventListener("htmx:sendError", function (event) {
    if (ours(event)) {
      showError(false);
    }
  });
})();
