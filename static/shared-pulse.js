// static/shared-pulse.js -- the fleet-at-a-glance bar above every view
//
// task-194: in the mockup this bar sits over every tab, so it is its own
// piece (not Fleet's): it registers with C.views.registerShared in the
// "top" slot and reads the shared snapshot and C.fleetModel. Deleting this
// file and its script tag removes the bar and nothing else.
(function (C) {
  "use strict";
  var M = C.fleetModel;
  var KINDS = [["work", "working"], ["wait", "waiting"], ["idle", "idle"], ["review", "ready to review"], ["block", "merge blocked"], ["unknown", "unknown"]];
  var bar = null;   // {el, live, merges, stack, segs, counts, legend}

  function make() {
    var p = { segs: {}, counts: {} };
    p.live = C.h("b", { className: "fleet-pulse-num", attrs: { "data-pulse": "live" } });
    p.merges = C.h("b", { className: "fleet-pulse-num", attrs: { "data-pulse": "merges" } });
    p.stack = C.h("span", { className: "fleet-stack", attrs: { "aria-hidden": "true" } });
    p.legend = C.h("span", { className: "fleet-legend" });
    KINDS.forEach(function (k) {
      p.segs[k[0]] = C.h("span", { className: "fleet-stack-seg fleet-k-" + k[0] });
      p.stack.appendChild(p.segs[k[0]]);
      p.counts[k[0]] = C.h("b");
      p.legend.appendChild(C.h("span", { className: "fleet-legend-item fleet-k-" + k[0], attrs: { "data-kind": k[0] },
        children: [C.h("i"), p.counts[k[0]], C.h("span", { text: " " + k[1] })] }));
    });
    p.el = C.h("section", { className: "fleet-pulse", attrs: { "aria-label": "Fleet at a glance" },
      children: [
        C.h("span", { className: "fleet-pulse-stat", children: [p.live, C.h("span", { text: " live agents" })] }),
        C.h("span", { className: "fleet-pulse-stat", children: [p.merges, C.h("span", { text: " merges in window" })] }),
        C.h("span", { className: "fleet-pulse-states", children: [p.stack, p.legend] })] });
    return p;
  }

  function render(host, snapshot) {
    if (!bar || bar.el.parentNode !== host) {
      C.clearChildren(host);
      bar = make();
      host.appendChild(bar.el);
    }
    if (!snapshot) return;   // nothing yet: the bar keeps what it last showed
    var model = M.segments(snapshot);
    var live = model.agents.filter(function (a) { return a.live; });
    var n = {};
    KINDS.forEach(function (k) { n[k[0]] = 0; });
    live.forEach(function (a) { n[n[a.kind] === undefined ? "unknown" : a.kind]++; });
    bar.live.textContent = String(live.length);
    bar.merges.textContent = String((snapshot.history || []).filter(function (r) { return r.state === "merged"; }).length);
    KINDS.forEach(function (k) {
      bar.counts[k[0]].textContent = String(n[k[0]]);
      bar.segs[k[0]].style.width = (live.length ? 100 * n[k[0]] / live.length : 0) + "%";
    });
  }

  C.views.registerShared({ id: "pulse", slot: "top", render: render });
})(window.Centrale = window.Centrale || {});
