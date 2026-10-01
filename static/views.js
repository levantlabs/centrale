// static/views.js -- the view registry, the view tab strip and Settings' Views checklist
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
//
// task-187: the Views milestone's foundation. A view is its own
// static/view-*.js file that calls C.views.register({id, label, render})
// and nothing else in the tree names it: the tab strip, the Settings
// checklist and cross-view links are all built from what registered, so
// deleting a view file and its script tag (or unticking it in Settings)
// leaves every other view and this file untouched.
//
// render(host, snapshot, error) is called with the view's own empty-able
// container and the latest /api/fleet snapshot (static/fleet.js); the view
// owns everything inside `host`; the host arrives empty when the view is
// first shown. A view
// may also define focus(arg), called by C.views.open(id, arg) after the
// tab is shown -- how "open this agent on the Timeline" is carried. And
// badge(snapshot), returning a number, string or null, puts a live value on
// the view's tab (e.g. "Needs you 3") even while another view is showing.
//
// task-194: SHARED pieces sit around every view (not the Board): a piece
// calls C.views.registerShared({id, slot: "top" | "side", render}) from its own
// static/shared-*.js and nothing else names it, so deleting that script
// tag removes it the way deleting a view file removes a view. render(host,
// snapshot, error) gets a host element the registry created for that piece
// and keeps between views, so the piece's motion survives tab switches.
//
// task-190: a view keeps its elements across renders (C.syncChildren) rather
// than clearing the host, so its CSS motion survives each poll; the host is
// cleared only when the active view changes. The tab strip is one persistent
// segmented control whose highlight slides to the selected tab.
(function (C) {
  "use strict";

  var registry = [];              // registration order is tab order
  var hidden = C.readStoredHiddenViews();
  var activeId = null;            // null: the Board itself
  var unsubscribe = null;
  var badgeUnsubscribe = null;    // the registry's own subscription, for tab badges
  var badges = {};                // view id -> what its badge(snapshot) last returned
  var shared = [];                // {id, slot, render, el}: pieces drawn around every view

  function find(id) {
    for (var i = 0; i < registry.length; i++) if (registry[i].id === id) return registry[i];
    return null;
  }

  function isHidden(id) { return hidden.indexOf(id) !== -1; }

  function visibleViews() {
    return registry.filter(function (v) { return !isHidden(v.id); });
  }

  // The views a link may target: loaded and not switched off.
  function isAvailable(id) {
    return !!find(id) && !isHidden(id);
  }

  // ------------------------------------------------------------------
  // Showing the active view (or the Board)
  // ------------------------------------------------------------------

  function slotHost(slot) { return C.byId(slot === "side" ? "view-side" : "view-top"); }

  function drawShared(snapshot, error) {
    shared.forEach(function (piece) {
      try {
        piece.render(piece.el, snapshot, error);
      } catch (e) {
        C.clearChildren(piece.el);
        piece.el.appendChild(C.h("div", { className: "view-error", text: "The " + piece.id + " panel failed to draw: " + (e.message || e) }));
      }
    });
  }

  function drawActive(snapshot, error) {
    var view = activeId === null ? null : find(activeId);
    if (!view) return;
    drawShared(snapshot, error);
    var host = C.byId("view-host");
    try {
      view.render(host, snapshot, error);
    } catch (e) {
      C.clearChildren(host);
      host.appendChild(C.h("div", { className: "view-error", text: "The " + view.label + " view failed to draw: " + (e.message || e) }));
    }
  }

  function applyActive() {
    // A hidden or vanished active view falls back to the Board, never to
    // a blank page.
    if (activeId !== null && !isAvailable(activeId)) activeId = null;
    var onBoard = activeId === null;
    C.byId("board-wrap").hidden = !onBoard;
    C.byId("view-host").hidden = onBoard;
    C.byId("view-stage").hidden = onBoard;
    if (unsubscribe) { unsubscribe(); unsubscribe = null; }
    C.clearChildren(C.byId("view-host"));
    if (!onBoard) unsubscribe = C.fleetData.subscribe(drawActive);
  }

  // ------------------------------------------------------------------
  // Tab badges: a live value on a tab while the view is not on screen
  // ------------------------------------------------------------------

  function updateBadges(snapshot) {
    badges = {};
    visibleViews().forEach(function (v) {
      if (typeof v.badge !== "function") return;
      try {
        var value = v.badge(snapshot);
        if (value !== null && value !== undefined && value !== "") badges[v.id] = String(value);
      } catch (e) { /* a broken badge shows nothing rather than breaking the strip */ }
    });
    renderTabs();
  }

  // The registry subscribes for itself only while some visible view wants a
  // badge, so with none the poller still has no subscriber and stops.
  function syncBadgeSubscription() {
    var wanted = visibleViews().some(function (v) { return typeof v.badge === "function"; });
    if (wanted && !badgeUnsubscribe) {
      badgeUnsubscribe = C.fleetData.subscribe(updateBadges);
    } else if (!wanted && badgeUnsubscribe) {
      badgeUnsubscribe(); badgeUnsubscribe = null;
      badges = {};
    }
  }

  // ------------------------------------------------------------------
  // The tab strip
  // ------------------------------------------------------------------

  var tabEls = {};                // view id ("" for the Board) -> its persistent button
  var indicator = null;           // the highlight that slides under the selected tab

  function tabFor(id) {
    var key = id === null ? "" : id;
    if (tabEls[key]) return tabEls[key];
    var el = C.h("button", {
      className: "view-tab",
      attrs: { type: "button", "data-view": key },
      onclick: function () { show(id); }
    });
    el.appendChild(C.h("span", { className: "view-tab-label" }));
    tabEls[key] = el;
    return el;
  }

  // The badge is its own element so a changing count never rebuilds the
  // button (which would drop the slide and the focus ring).
  function paintTab(el, id, label) {
    var selected = activeId === id;
    var badge = id !== null && badges[id] !== undefined ? badges[id] : null;
    el.className = "view-tab" + (selected ? " active" : "");
    el.setAttribute("aria-current", selected ? "page" : "false");
    el.childNodes[0].textContent = label;
    var chip = el.childNodes[1] || null;
    if (badge === null) {
      if (chip) el.removeChild(chip);
    } else if (!chip) {
      el.appendChild(C.h("span", { className: "view-tab-badge", text: badge }));
    } else if (chip.textContent !== badge) {
      chip.textContent = badge;
      C.replayClass(chip, "bump");
    }
    el.setAttribute("aria-label", badge === null ? label : label + ", " + badge);
  }

  function moveIndicator() {
    var active = tabEls[activeId === null ? "" : activeId];
    if (!indicator || !active || !active.parentNode || !active.offsetWidth) return;
    indicator.style.left = active.offsetLeft + "px";
    indicator.style.width = active.offsetWidth + "px";
    // The first placement is a jump, not a slide in from nothing.
    if (!indicator.classList.contains("ready")) {
      if (window.requestAnimationFrame) window.requestAnimationFrame(function () { indicator.classList.add("ready"); });
      else indicator.classList.add("ready");
    }
  }

  function renderTabs() {
    var nav = C.byId("view-tabs");
    var views = visibleViews();
    // Nothing to switch to: no strip at all, not a lone "Board" tab.
    nav.hidden = views.length === 0;
    if (!views.length) { C.clearChildren(nav); indicator = null; tabEls = {}; return; }
    if (!indicator) indicator = C.h("span", { className: "view-tab-ind", attrs: { "aria-hidden": "true" } });
    var nodes = [indicator];
    var board = tabFor(null);
    paintTab(board, null, "Board");
    nodes.push(board);
    var live = { "": true };
    views.forEach(function (v) {
      var el = tabFor(v.id);
      paintTab(el, v.id, v.label);
      nodes.push(el);
      live[v.id] = true;
    });
    Object.keys(tabEls).forEach(function (k) { if (!live[k]) delete tabEls[k]; });
    C.syncChildren(nav, nodes);
    moveIndicator();
  }

  if (window.addEventListener) {
    window.addEventListener("resize", moveIndicator);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(moveIndicator);
  }

  function show(id, arg) {
    activeId = id;
    applyActive();
    renderTabs();
    var view = activeId === null ? null : find(activeId);
    if (view && typeof view.focus === "function") {
      try { view.focus(arg); } catch (e) { /* a focus hint must not break navigation */ }
    }
  }

  // ------------------------------------------------------------------
  // Settings: the Views checklist
  // ------------------------------------------------------------------

  function renderChecklist() {
    var section = C.byId("settings-views-section");
    var list = C.byId("settings-views-list");
    C.clearChildren(list);
    // Nothing registered: nothing to choose, so no empty section.
    section.hidden = registry.length === 0;
    registry.forEach(function (v) {
      var box = C.h("input", { attrs: { type: "checkbox", "data-view": v.id } });
      box.checked = !isHidden(v.id);
      box.addEventListener("change", function () { setHidden(v.id, !box.checked); });
      list.appendChild(C.h("label", { className: "settings-view-option", children: [box, C.h("span", { text: " " + v.label })] }));
    });
  }

  function setHidden(id, makeHidden) {
    hidden = hidden.filter(function (x) { return x !== id; });
    if (makeHidden) hidden.push(id);
    C.persistHiddenViews(hidden);
    // Immediately, no reload: the tab goes (or returns) and, if it was the
    // one on screen, the Board takes over.
    applyActive();
    syncBadgeSubscription();
    renderTabs();
  }

  // ------------------------------------------------------------------
  // The registry's public face
  // ------------------------------------------------------------------

  function registerShared(spec) {
    if (!spec || typeof spec.id !== "string" || !spec.id || typeof spec.render !== "function" ||
        (spec.slot !== "top" && spec.slot !== "side")) {
      throw new Error("a shared piece needs an id, a slot of top or side and a render function");
    }
    for (var i = 0; i < shared.length; i++) if (shared[i].id === spec.id) return false;
    var piece = { id: spec.id, slot: spec.slot, render: spec.render,
      el: C.h("div", { className: "view-shared view-shared-" + spec.slot, attrs: { "data-shared": spec.id } }) };
    shared.push(piece);
    slotHost(piece.slot).appendChild(piece.el);
    // The side column exists only while something lives in it.
    C.byId("view-side").hidden = !shared.some(function (p) { return p.slot === "side"; });
    if (activeId !== null) drawShared(C.fleetSnapshot, C.fleetError);
    return true;
  }

  function register(spec) {
    if (!spec || typeof spec.id !== "string" || !spec.id || typeof spec.label !== "string" || typeof spec.render !== "function") {
      throw new Error("a view needs an id, a label and a render function");
    }
    if (find(spec.id)) return false;
    registry.push(spec);
    syncBadgeSubscription();
    renderTabs();
    renderChecklist();
    return true;
  }

  // Opens `id` (optionally handing it `arg`). The link-following path
  // every cross-view jump uses: false, and nothing changes, when the
  // target was never loaded or is switched off.
  function open(id, arg) {
    if (!isAvailable(id)) return false;
    show(id, arg);
    return true;
  }

  C.views = { register: register, registerShared: registerShared, open: open, isAvailable: isAvailable, current: function () { return activeId; } };

  C.byId("view-side").hidden = true;
  renderTabs();
  renderChecklist();
})(window.Centrale = window.Centrale || {});
