// static/view-timeline.js -- the Timeline view: two hours of state segments per agent
//
// A self-contained view (task-189): registered with C.views, fed by the
// shared snapshot and C.fleetModel. Another view reaches it only through
// C.views.open("timeline", {project, taskId}), which lands in focus().
// It scrolls forward because every snapshot carries a newer "now": the
// window is always [now - window, now], so segments drift left as the
// fleet poller delivers fresh snapshots.
(function (C) {
  "use strict";
  var M = C.fleetModel;
  var focusKey = null;
  var tip = null;

  function keyOf(project, taskId) { return project + "\u0000" + taskId; }

  function hue(project) { return "hsl(" + C.projectHue(project) + ", 62%, 52%)"; }
  function setVar(el, name, value) { if (el.style.setProperty) el.style.setProperty(name, value); else el.style[name] = value; }

  function pct(ts, model) { return 100 * (ts - model.start) / (model.now - model.start || 1); }

  function hideTip() { if (tip) tip.classList.remove("show"); }

  function showTip(target, ev) {
    if (!tip) return;
    C.clearChildren(tip);
    tip.appendChild(C.h("b", { text: target.task }));
    if (target.name) tip.appendChild(C.h("div", { className: "tl-tip-task", text: target.name }));
    tip.appendChild(C.h("div", { className: "tl-tip-state", text: target.title }));
    tip.appendChild(C.h("div", { className: "tl-tip-time", text: target.detail }));
    tip.classList.add("show");
    tip.style.left = Math.max(8, Math.min(ev.clientX + 14, (window.innerWidth || 800) - 220)) + "px";
    tip.style.top = (ev.clientY + 14) + "px";
  }

  function tipData(agent, seg, model) {
    var end = seg.end === null ? model.now : seg.end;
    return { task: agent.taskId + " · " + agent.agent, name: M.title(agent.project, agent.taskId), title: seg.label,
             detail: M.clock(seg.start) + " → " + (seg.end === null ? "now" : M.clock(seg.end)) + " · " + M.duration(end - seg.start) };
  }

  // Elements persist between polls (task-190): a segment keeps its element
  // so the growing one can shimmer and every one eases as the window scrolls.
  var rowsByKey = {};   // project+task -> {el, track, segs: {start: el}, merge}
  var groupEls = {};    // project -> label element
  var view = null;      // {root, body, overlay, now, legend}

  function placeSeg(el, agent, seg, model) {
    var end = seg.end === null ? model.now : seg.end;
    var left = Math.max(0, pct(seg.start, model));
    var width = Math.max(pct(end, model) - left, 0.5);
    var data = tipData(agent, seg, model);
    el.className = "tl-seg fleet-k-" + seg.kind + (seg.open ? " open" : "");
    el.setAttribute("data-state", seg.label);
    el.setAttribute("aria-label", data.title + ", " + data.detail);
    el.style.left = left + "%";
    el.style.width = Math.min(width, 100 - left) + "%";
    el.tipData = data;
  }

  function newSeg() {
    var el = C.h("span");
    el.addEventListener("mousemove", function (ev) { showTip(el.tipData, ev); });
    el.addEventListener("mouseleave", hideTip);
    return el;
  }

  function placeMerge(rec, agent, model) {
    var show = agent.merged !== null && agent.merged >= model.start;
    if (!show) { if (rec.merge && rec.merge.parentNode) rec.track.removeChild(rec.merge); return; }
    if (!rec.merge) {
      rec.merge = C.h("span", { className: "tl-merge" });
      rec.merge.addEventListener("mousemove", function (ev) { showTip(rec.merge.tipData, ev); });
      rec.merge.addEventListener("mouseleave", hideTip);
      if (primed) rec.merge.classList.add("enter");
    }
    rec.merge.tipData = { task: agent.taskId + " · " + agent.agent, name: M.title(agent.project, agent.taskId), title: "merged", detail: "at " + M.clock(agent.merged) };
    rec.merge.setAttribute("data-merged", agent.taskId);
    rec.merge.setAttribute("aria-label", "merged at " + M.clock(agent.merged));
    rec.merge.style.left = pct(agent.merged, model) + "%";
    if (!rec.merge.parentNode) rec.track.appendChild(rec.merge);
  }

  function axis(ax, model) {
    C.clearChildren(ax);
    var step = 900; // a label every quarter hour
    for (var t = Math.ceil(model.start / step) * step; t <= model.now; t += step) {
      var p = pct(t, model);
      if (p < 3 || p > 94) continue;
      var tick = C.h("span", { text: M.clock(t) });
      tick.style.left = p + "%";
      ax.appendChild(tick);
    }
  }

  function row(agent, model, animate) {
    var k = keyOf(agent.project, agent.taskId);
    var rec = rowsByKey[k];
    if (!rec) {
      rec = rowsByKey[k] = { segs: {}, merge: null };
      rec.track = C.h("div", { className: "tl-track" });
      rec.id = C.h("span", { className: "tl-label-id" });
      rec.agent = C.h("span", { className: "tl-label-agent" });
      rec.el = C.h("div", { className: "tl-row" + (animate ? " enter" : ""), children: [
        C.h("div", { className: "tl-label", children: [rec.id, rec.agent] }), rec.track] });
    }
    rec.el.setAttribute("data-task", agent.taskId);
    rec.el.setAttribute("data-project", agent.project);
    setVar(rec.el, "--p", hue(agent.project));
    rec.id.textContent = agent.taskId;
    rec.agent.textContent = agent.agent;
    var nodes = [];
    var seen = {};
    agent.segments.forEach(function (seg, i) {
      var sk = String(seg.start) + ":" + i;
      var el = rec.segs[sk] || (rec.segs[sk] = newSeg());
      placeSeg(el, agent, seg, model);
      seen[sk] = true;
      nodes.push(el);
    });
    Object.keys(rec.segs).forEach(function (sk) { if (!seen[sk]) delete rec.segs[sk]; });
    placeMerge(rec, agent, model);
    if (rec.merge && rec.merge.parentNode) nodes.push(rec.merge);
    C.syncChildren(rec.track, nodes);
    return rec;
  }

  function legend() {
    var items = [["work", "working"], ["wait", "waiting"], ["review", "ready to review"], ["idle", "idle"], ["block", "merge blocked"]];
    var box = C.h("div", { className: "tl-legend" });
    items.forEach(function (i) {
      box.appendChild(C.h("span", { children: [C.h("i", { className: "tl-seg fleet-k-" + i[0] }), C.h("span", { text: i[1] })] }));
    });
    box.appendChild(C.h("span", { children: [C.h("i", { className: "tl-merge-key" }), C.h("span", { text: "merged" })] }));
    return box;
  }

  // Rebuilt by every render: [{key, el}] for each agent row on screen.
  var rowIndex = [];
  var primed = false;

  function highlight() {
    rowIndex.forEach(function (r) {
      var on = focusKey !== null && r.key === focusKey;
      var had = r.el.classList.contains("hl");
      r.el.classList.toggle("hl", on);
      if (on && !had && r.el.scrollIntoView) r.el.scrollIntoView({ block: "nearest" });
    });
  }

  function render(host, snapshot, error) {
    if (!view || view.root.parentNode !== host) {
      C.clearChildren(host);
      view = { root: C.h("div", { className: "tl-root" }), body: C.h("div", { className: "tl" }) };
      view.overlay = C.h("div", { className: "tl-overlay", attrs: { "aria-hidden": "true" } });
      view.now = C.h("div", { className: "tl-now", attrs: { "aria-hidden": "true" } });
      view.nowLabel = C.h("span");
      view.now.appendChild(view.nowLabel);
      view.overlay.appendChild(view.now);
      view.card = C.h("div", { className: "tl-card", children: [C.h("div", { className: "tl-scroll", children: [view.body] })] });
      view.axis = C.h("div", { className: "tl-axis" });
      view.axisRow = C.h("div", { className: "tl-row", children: [C.h("div"), view.axis] });
      view.legend = legend();
      tip = C.h("div", { className: "tl-tip", attrs: { role: "tooltip" } });
      host.appendChild(view.root);
      rowsByKey = {}; groupEls = {}; primed = false;
    }
    rowIndex = [];
    var top = [];
    if (error) top.push(C.h("div", { className: "view-error", text: "Timeline could not be read: " + error }));
    if (!snapshot) {
      if (!error) top.push(C.h("div", { className: "fleet-empty", text: "Loading…" }));
      C.syncChildren(view.root, top);
      return;
    }
    var model = M.segments(snapshot);
    if (!model.agents.length) {
      top.push(C.h("div", { className: "fleet-empty", text: "No agent active in the last two hours" }));
      C.syncChildren(view.root, top);
      return;
    }
    var names = (snapshot.projects || []).map(function (p) { return p.name; });
    model.agents.forEach(function (a) { if (names.indexOf(a.project) === -1) names.push(a.project); });
    axis(view.axis, model);
    var nodes = [view.axisRow];
    var seen = {};
    names.forEach(function (name) {
      var rows = model.agents.filter(function (a) { return a.project === name; });
      if (!rows.length) return;
      if (!groupEls[name]) {
        groupEls[name] = C.h("div", { className: "tl-group", children: [C.h("i", { className: "tl-group-sq", attrs: { "aria-hidden": "true" } }), C.h("span", { text: name })] });
        setVar(groupEls[name], "--p", hue(name));
      }
      nodes.push(groupEls[name]);
      rows.forEach(function (a) {
        var rec = row(a, model, primed);
        seen[keyOf(a.project, a.taskId)] = true;
        rowIndex.push({ key: keyOf(a.project, a.taskId), el: rec.el });
        nodes.push(rec.el);
      });
    });
    Object.keys(rowsByKey).forEach(function (k) { if (!seen[k]) delete rowsByKey[k]; });
    view.nowLabel.textContent = "now " + M.clock(model.now);
    nodes.push(view.overlay);
    C.syncChildren(view.body, nodes);
    top.push(view.legend, view.card, tip);
    C.syncChildren(view.root, top);
    primed = true;
    highlight();
  }

  // Fleet hands over {project, taskId}: remember it, mark the row on the
  // next draw (the registry has normally drawn already) and bring it into view.
  function focus(arg) {
    focusKey = arg && arg.project && arg.taskId ? keyOf(arg.project, arg.taskId) : null;
    highlight();
  }

  C.views.register({ id: "timeline", label: "Timeline", render: render, focus: focus });
})(window.Centrale = window.Centrale || {});
