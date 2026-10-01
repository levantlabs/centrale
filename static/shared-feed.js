// static/shared-feed.js -- the Activity feed beside every view
//
// task-194: like the summary bar, the mockup shows the feed next to every
// tab, so it is its own piece in the "side" slot (stacking below the view at
// narrow widths). snapshot.history, newest first, one line per event.
// Deleting this file and its script tag removes it and nothing else.
(function (C) {
  "use strict";
  var M = C.fleetModel;
  var FEED_CAP = 30;
  var LABELS = { spawn: "spawned", "session ended": "session ended" };
  var feed = null;      // {el, list, empty, items: {key: li}}
  var primed = false;   // false until a first draw, so the first paint doesn't animate every line in

  function make() {
    var f = { items: {} };
    f.list = C.h("ol", { className: "fleet-feed-list" });
    f.empty = C.h("p", { className: "fleet-feed-empty", text: "Nothing has happened in this window yet." });
    f.el = C.h("section", { className: "fleet-feed", attrs: { "aria-label": "Live activity" },
      children: [C.h("h2", { text: "Activity" }), f.list] });
    return f;
  }

  function kindOf(state) {
    if (state === "merged") return "merged";
    if (state === "session ended") return "idle";
    return M.stateInfo(state).kind;
  }

  function render(host, snapshot) {
    if (!feed || feed.el.parentNode !== host) {
      C.clearChildren(host);
      feed = make();
      host.appendChild(feed.el);
      primed = false;
    }
    if (!snapshot) return;
    var rows = (snapshot.history || []).slice().sort(function (a, b) { return b.timestamp - a.timestamp; }).slice(0, FEED_CAP);
    var nodes = [];
    var seen = {};
    rows.forEach(function (r) {
      var key = r.timestamp + "|" + r.project + "|" + r.taskId + "|" + r.state;
      var li = feed.items[key];
      if (!li) {
        li = feed.items[key] = C.h("li", { className: "fleet-feed-item fleet-k-" + kindOf(r.state) + (primed ? " new" : ""),
          attrs: { "data-task": r.taskId, "data-project": r.project },
          children: [C.h("span", { className: "fleet-feed-time", text: M.clock(r.timestamp) }),
            C.h("span", { className: "fleet-dot", attrs: { "aria-hidden": "true" } }),
            C.h("span", { className: "fleet-feed-text", children: [C.h("b", { text: r.taskId }),
              C.h("span", { text: " " + (LABELS[r.state] || r.state) }),
              C.h("span", { className: "fleet-feed-project", text: " · " + r.project })] })] });
      }
      seen[key] = true;
      nodes.push(li);
    });
    Object.keys(feed.items).forEach(function (k) { if (!seen[k]) delete feed.items[k]; });
    C.syncChildren(feed.list, nodes.length ? nodes : [feed.empty]);
    primed = true;
  }

  C.views.registerShared({ id: "feed", slot: "side", render: render });
})(window.Centrale = window.Centrale || {});
