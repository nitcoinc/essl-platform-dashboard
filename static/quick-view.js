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
  // Set by a strip arrow (U6): after the swap, focus the same arrow
  // ("earlier" or "later") so repeated paging keeps its place.
  var focusPage = null;

  function swapDay(target, source) {
    url = target;
    body.setAttribute("aria-busy", "true");
    window.htmx.ajax("GET", url, { source: source, target: "#quick-view-body", swap: "innerHTML" });
  }

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
    focusPage = null;
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
      focusPage = day.dataset.qvPage || null;
      focusDay = !focusPage;
      swapDay(day.dataset.qvDay, day);
      return;
    }
    // The dialog has no padding, so a hit on the dialog itself is the backdrop.
    if (event.target === dialog || event.target.closest("[data-qv-close]")) {
      dialog.close();
    } else if (event.target.closest("[data-qv-retry]")) {
      load();
    }
  });

  // Drawer date picker (U6): a plain GET to /quick would redirect to /day,
  // so build the fragment URL here (keeping from=) and swap in place.
  function pickDate(input) {
    var value = input.value;
    if (!value || !window.htmx || (input.min && value < input.min) || (input.max && value > input.max)) {
      return;
    }
    var target = new URL(input.dataset.qvDate, window.location.href);
    target.searchParams.set("date", value);
    focusPage = null;
    focusDay = true;
    swapDay(target.pathname + target.search, input);
  }

  dialog.addEventListener("change", function (event) {
    var input = event.target.closest ? event.target.closest("input[data-qv-date]") : null;
    if (input) {
      pickDate(input);
    }
  });

  dialog.addEventListener("submit", function (event) {
    var form = event.target.closest ? event.target.closest("form[data-qv-date-form]") : null;
    if (!form) {
      return;
    }
    event.preventDefault();
    var input = form.querySelector("input[data-qv-date]");
    if (input) {
      pickDate(input);
    }
  });

  dialog.addEventListener("close", function () {
    // Emptying the body removes #day-live, which stops timer.js.
    body.replaceChildren();
    body.removeAttribute("aria-busy");
    focusDay = false;
    focusPage = null;
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
    var arrow = focusPage ? body.querySelector('a[data-qv-page="' + focusPage + '"]') : null;
    // A disabled arrow (strip reached today or the first punch) falls back to the chip.
    var chip = focusDay || focusPage ? body.querySelector('[data-qv-day][aria-current="date"]') : null;
    focusDay = false;
    focusPage = null;
    if (heading) {
      dialog.removeAttribute("aria-label");
      dialog.setAttribute("aria-labelledby", "quick-view-title");
    }
    if (arrow) {
      arrow.focus();
    } else if (chip) {
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
