// static/shell.js -- the header/search/keyboard wiring, the theme toggle, the sidebar collapse and the footer's version credit
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // Header control wiring
  // ------------------------------------------------------------------

  C.byId("search-input").addEventListener("input", function (e) {
    C.searchText = e.target.value || "";
    C.renderBoard();
  });

  function clearSearch(keepFocus) {
    var input = C.byId("search-input");
    input.value = "";
    C.searchText = "";
    C.renderBoard();
    if (keepFocus) input.focus();
  }

  C.byId("search-clear").addEventListener("click", function () {
    clearSearch(true);
  });

  C.byId("projects-reset-all").addEventListener("click", function () {
    C.activeProjects = new Set(C.knownProjects);
    C.renderAll();
  });

  // Escape while the field is focused clears it (if there's a query) or
  // blurs it (if already empty), and never falls through to the global
  // Escape handler below (e.g. closing an open drawer) -- the field owns
  // Escape while it has focus.
  C.byId("search-input").addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    e.stopPropagation();
    if (this.value) {
      clearSearch(true);
    } else {
      this.blur();
    }
  });

  function isMacPlatform() {
    try {
      return /Mac|iPhone|iPod|iPad/.test(navigator.platform || navigator.userAgent || "");
    } catch (e) {
      return false;
    }
  }

  C.byId("search-kbd-hint").textContent = isMacPlatform() ? "⌘K" : "Ctrl K";

  // Cmd+K (Mac) / Ctrl+K (elsewhere) focuses the search field from
  // anywhere on the page, matching the hint chip shown inside it. The
  // field is hidden while the sidebar is collapsed to a rail, so expand
  // it first in that case (applySidebarCollapsed/sidebarCollapsed are
  // defined below; safe to reference here since this only runs on a
  // later user keypress, after the whole script has finished loading).
  document.addEventListener("keydown", function (e) {
    var mod = isMacPlatform() ? e.metaKey : e.ctrlKey;
    if (!mod || (e.key !== "k" && e.key !== "K")) return;
    e.preventDefault();
    if (sidebarCollapsed) {
      sidebarCollapsed = false;
      applySidebarCollapsed();
      C.persistSidebarCollapsed(sidebarCollapsed);
    }
    var input = C.byId("search-input");
    input.focus();
    input.select();
  });

  // Restored value applied before the first render so there's no flash
  // of the (unchecked) default -- the checkbox has no `checked` attribute
  // in the HTML itself, since that default only applies pre-restore.
  C.byId("ready-toggle").checked = C.readyOnly;

  C.byId("ready-toggle").addEventListener("change", function (e) {
    C.readyOnly = !!e.target.checked;
    C.persistReadyOnly(C.readyOnly);
    C.renderBoard();
  });

  // task-86: the dropdown's own options are (re)built by
  // renderMilestoneFilter on every changed refresh; changing the
  // SELECTION only re-filters the lanes, on the same direct renderBoard()
  // path the ready toggle and search box take -- never through the
  // task-50 skip check, whose payload comparison covers fetched data only.
  C.byId("milestone-filter").addEventListener("change", function (e) {
    C.milestoneFilter = e.target.value || "";
    C.persistMilestoneFilter(C.milestoneFilter);
    C.renderBoard();
  });

  C.byId("refresh-btn").addEventListener("click", function () {
    C.doRefresh(true);
  });

  C.byId("tmux-hint-dismiss").addEventListener("click", C.dismissTmuxHint);

  C.byId("harvest-all-btn").addEventListener("click", C.harvestAllReady);

  // ------------------------------------------------------------------
  // Theme toggle
  // ------------------------------------------------------------------

  function updateThemeToggleButton() {
    var btn = C.byId("theme-toggle");
    var dark = C.isDarkTheme();
    btn.textContent = dark ? "☀" : "☽"; // sun (switch to light) / moon (switch to dark)
    btn.title = dark ? "Switch to light theme" : "Switch to dark theme";
  }

  C.byId("theme-toggle").addEventListener("click", function () {
    window.CentraleTheme.set(C.isDarkTheme() ? "light" : "dark");
    updateThemeToggleButton();
    C.renderAll(); // project-chip colors are computed inline and theme-dependent
  });

  // If the user hasn't made an explicit choice, react live to the OS
  // theme changing while the tab is open (CSS follows automatically via
  // prefers-color-scheme; this just keeps the toggle icon and the
  // inline-styled project chips in sync with it).
  try {
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
        if (window.CentraleTheme.hasExplicitChoice()) return;
        updateThemeToggleButton();
        C.renderAll();
      });
    }
  } catch (e) { /* matchMedia change listener unsupported: system changes need a reload */ }

  updateThemeToggleButton();

  // ------------------------------------------------------------------
  // Sidebar collapse
  // ------------------------------------------------------------------

  // An explicit stored choice (from the toggle button, or the Ctrl/Cmd+K
  // auto-expand) always wins. Only with no stored choice does a narrow
  // viewport's heuristic apply: those get a fixed-position, off-canvas-
  // style sidebar (see the max-width: 880px rule), so start collapsed
  // there so the board is visible on first paint rather than covered by
  // an open sidebar.
  var sidebarCollapsed = false;
  var storedSidebarCollapsed = C.readStoredSidebarCollapsed();
  if (storedSidebarCollapsed !== null) {
    sidebarCollapsed = storedSidebarCollapsed;
  } else {
    try {
      sidebarCollapsed = !!(window.matchMedia && window.matchMedia("(max-width: 880px)").matches);
    } catch (e) { /* matchMedia unavailable: default to expanded */ }
  }

  function applySidebarCollapsed() {
    C.byId("sidebar").classList.toggle("collapsed", sidebarCollapsed);
    var btn = C.byId("sidebar-toggle");
    btn.title = sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar";
    btn.setAttribute("aria-label", btn.title);
  }
  applySidebarCollapsed();

  C.byId("sidebar-toggle").addEventListener("click", function () {
    sidebarCollapsed = !sidebarCollapsed;
    applySidebarCollapsed();
    C.persistSidebarCollapsed(sidebarCollapsed);
  });

  // ------------------------------------------------------------------
  // The version credit in the sidebar footer (task-107)
  // ------------------------------------------------------------------
  //
  // The server resolves this ONCE, at startup, and reports the same
  // string on every board load -- so what the footer shows is the build
  // the SERVING PROCESS booted from, not what the checkout on disk has
  // become since. That is the whole point: a value re-derived per
  // request would cheerfully name code this process has never run,
  // which is exactly the failure decision-2 records.
  //
  // Called after each board load rather than once at boot because the
  // first load is what supplies the value; it settles on the first one
  // and every later call is a no-op.
  function renderVersion() {
    var el = C.byId("version");
    if (!el) return;
    var v = C.boardData && C.boardData.version;
    if (typeof v !== "string" || !v || el.textContent === v) return;
    el.textContent = v;
    el.title = "Centrale " + v + " \u2014 the build this server process started from";
  }

  // ------------------------------------------------------------------
  // The stale-process banner (task-128)
  // ------------------------------------------------------------------
  //
  // The other half of the credit above: the server compares the commit
  // it loaded with HEAD of its own checkout on every board request and
  // reports `codeDrift` -- null while they agree, otherwise the two
  // commits and how many commits the process is behind. The frontend
  // computes nothing of its own here and never remembers a previous
  // answer: it shows exactly what the last board response said, and a
  // null (the normal case), an absent field (an older server) or a
  // failed load all leave the banner as the response left it. There is
  // no dismiss and no restart button -- a restart mid-merge is unsafe,
  // so the banner says what to do and leaves the doing to the operator.
  function renderCodeDrift() {
    var banner = C.byId("code-drift-banner");
    if (!banner) return;
    var drift = C.boardData && C.boardData.codeDrift;
    if (!drift || typeof drift !== "object" || !drift.loaded || !drift.current) {
      banner.hidden = true;
      return;
    }
    var text = C.byId("code-drift-text");
    C.clearChildren(text);
    text.appendChild(document.createTextNode(
      "The code on disk has changed since this server process started: it loaded "
    ));
    text.appendChild(C.h("code", { text: String(drift.loaded) }));
    text.appendChild(document.createTextNode(", the checkout is now at "));
    text.appendChild(C.h("code", { text: String(drift.current) }));
    var n = drift.commitsBehind;
    if (typeof n === "number" && n >= 1) {
      text.appendChild(document.createTextNode(
        " (" + n + " commit" + (n === 1 ? "" : "s") + " later)"
      ));
    }
    text.appendChild(document.createTextNode(
      ". Restart Centrale to load it \u2014 after checking no merge is in flight."
    ));
    banner.hidden = false;
  }

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.renderVersion = renderVersion;
  C.renderCodeDrift = renderCodeDrift;
})(window.Centrale = window.Centrale || {});
