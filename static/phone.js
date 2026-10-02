// static/phone.js -- the read-only status page (task-212)
//
// Loaded only by static/phone.html, which status_page.py serves on its own
// opt-in listener -- never by the dashboard's index.html. It reuses dom.js
// (C.h, C.syncChildren, C.projectHue) and fleet.js's pure model
// (C.fleetModel), and draws with the Fleet view's own classes from
// styles.css, so it looks like the Fleet view at phone width. It polls
// GET /api/status, the only data that listener serves, carrying the key
// from its own URL. It has no controls: there is nothing to act on.
(function (C) {
  "use strict";
  var M = C.fleetModel;
  var KEY = null;
  try { KEY = new URLSearchParams(window.location.search).get("key"); } catch (e) { KEY = null; }
  var MIN_INTERVAL_S = 5;
  var FEED_CAP = 30;
  var NEEDS_CAP = 6;
  var KINDS = [["work", "working"], ["wait", "waiting"], ["idle", "idle"], ["review", "ready to review"], ["block", "merge blocked"], ["unknown", "unknown"]];
  var FEED_LABELS = { spawn: "spawned", "session ended": "session ended" };
  var NEEDS_LABELS = { permission: "permission dialog", parked: "parked", idle: "idle, no report",
    merge: "merge blocked", message: "message not delivered", owner: "owner question" };

  var snapshot = null;   // the last good snapshot; kept when a poll fails
  var clockBase = null;  // {now, at}: snapshot time and the browser time it arrived
  var sinceCells = [];   // [{el, since}] counted up between polls
  var timer = null;

  function statusUrl() {
    return "/api/status" + (KEY ? "?key=" + encodeURIComponent(KEY) : "");
  }

  function liveNow() {
    return clockBase ? clockBase.now + (Date.now() - clockBase.at) / 1000 : 0;
  }

  function ago(since, now) {
    return typeof since === "number" ? M.duration(now - since) : "—";
  }

  function trackSince(el, since) {
    sinceCells.push({ el: el, since: since });
    el.textContent = ago(since, liveNow());
    return el;
  }

  function setVar(el, name, value) {
    if (el.style.setProperty) el.style.setProperty(name, value); else el.style[name] = value;
  }

  // -- summary bar: the dashboard's fleet-at-a-glance bar, same markup --
  function pulse(model, snap) {
    var live = model.agents.filter(function (a) { return a.live; });
    var n = {};
    KINDS.forEach(function (k) { n[k[0]] = 0; });
    live.forEach(function (a) { n[n[a.kind] === undefined ? "unknown" : a.kind]++; });
    var merges = (snap.history || []).filter(function (r) { return r.state === "merged"; }).length;
    var stack = C.h("span", { className: "fleet-stack", attrs: { "aria-hidden": "true" } });
    var legend = C.h("span", { className: "fleet-legend" });
    KINDS.forEach(function (k) {
      var seg = C.h("span", { className: "fleet-stack-seg fleet-k-" + k[0] });
      seg.style.width = (live.length ? 100 * n[k[0]] / live.length : 0) + "%";
      stack.appendChild(seg);
      legend.appendChild(C.h("span", { className: "fleet-legend-item fleet-k-" + k[0], attrs: { "data-kind": k[0] },
        children: [C.h("i"), C.h("b", { text: String(n[k[0]]) }), C.h("span", { text: " " + k[1] })] }));
    });
    return C.h("section", { className: "fleet-pulse", attrs: { "aria-label": "Fleet at a glance" }, children: [
      C.h("span", { className: "fleet-pulse-stat", children: [C.h("b", { className: "fleet-pulse-num", text: String(live.length), attrs: { "data-pulse": "live" } }), C.h("span", { text: " live agents" })] }),
      C.h("span", { className: "fleet-pulse-stat", children: [C.h("b", { className: "fleet-pulse-num", text: String(merges), attrs: { "data-pulse": "merges" } }), C.h("span", { text: " merges in window" })] }),
      C.h("span", { className: "fleet-pulse-states", children: [stack, legend] })] });
  }

  // -- Needs you: a count and a short list, no content --
  function needs(snap) {
    var data = snap.needsYou || { count: 0, items: [], unavailable: 0 };
    var count = Number(data.count) || 0;
    var head = C.h("div", { className: "phone-needs-head", children: [
      C.h("h2", { text: "Needs you" }),
      C.h("b", { className: "phone-needs-count" + (count ? " some" : ""), text: String(count), attrs: { "data-needs-count": String(count) } })] });
    var nodes = [head];
    var items = (data.items || []).slice(0, NEEDS_CAP);
    if (items.length) {
      nodes.push(C.h("ol", { className: "phone-needs-list", children: items.map(function (i) {
        return C.h("li", { className: "phone-needs-item", attrs: { "data-kind": i.kind }, children: [
          C.h("span", { className: "phone-needs-kind", text: NEEDS_LABELS[i.kind] || i.kind }),
          C.h("span", { className: "phone-needs-task", children: [C.h("b", { text: i.taskId }),
            C.h("span", { className: "fleet-feed-project", text: " · " + i.project + " · " + i.agent })] }),
          trackSince(C.h("span", { className: "fleet-agent-since" }), i.since)] });
      }) }));
      if (count > items.length) nodes.push(C.h("p", { className: "phone-note", text: "and " + (count - items.length) + " more on the dashboard" }));
    } else {
      nodes.push(C.h("p", { className: "phone-note", text: "Nothing is waiting on you." }));
    }
    if (data.unavailable) {
      nodes.push(C.h("p", { className: "phone-note phone-warn", text: data.unavailable + " check" + (data.unavailable === 1 ? "" : "s") +
        " could not run, so this count may be incomplete. Open the dashboard for details." }));
    }
    return C.h("section", { className: "fleet-pulse phone-needs", attrs: { "aria-label": "Needs you" }, children: nodes });
  }

  // -- one card per busy project: capacity ring and agent rows --
  function card(project, agents) {
    var max = project.maxAgents;
    var limited = typeof max === "number" && max > 0;
    var full = limited && project.agentCount >= max;
    var ring = C.h("span", { className: "fleet-ring" + (limited ? "" : " nolimit") + (full ? " full" : ""),
      attrs: { role: "img", "aria-label": limited ? project.agentCount + " of " + max + " agent slots used" : project.agentCount + " agents, no limit set" },
      children: [C.h("span", { className: "fleet-ring-count", text: limited ? project.agentCount + "/" + max : String(project.agentCount) })] });
    if (limited) setVar(ring, "--ring", Math.min(100, Math.round(100 * project.agentCount / max)) + "%");
    var head = C.h("div", { className: "fleet-card-head", children: [
      C.h("span", { className: "fleet-tile", text: String(project.name).charAt(0).toUpperCase(), attrs: { "aria-hidden": "true" } }),
      C.h("span", { className: "fleet-card-id", children: [C.h("span", { className: "fleet-card-name", text: project.name }),
        C.h("span", { className: "fleet-capacity" + (full ? " full" : ""), text: limited ? project.agentCount + " / " + max + " agents" : project.agentCount + " agents · no limit" })] }),
      ring] });
    var el = C.h("section", { className: "fleet-card", attrs: { "data-project": project.name }, children: [head].concat(agents.map(row)) });
    setVar(el, "--p", "hsl(" + C.projectHue(project.name) + ", 62%, 52%)");
    return el;
  }

  function row(agent) {
    var state = (agent.state || "unknown") + (agent.live && agent.live.parked ? " · parked" : "");
    return C.h("div", { className: "fleet-agent fleet-k-" + agent.kind, attrs: { "data-task": agent.taskId, "data-project": agent.project }, children: [
      C.h("span", { className: "fleet-dot" }),
      C.h("span", { className: "fleet-agent-main", children: [C.h("span", { className: "fleet-agent-id", text: agent.taskId })] }),
      C.h("span", { className: "fleet-agent-state", text: state }),
      C.h("span", { className: "fleet-agent-foot", children: [C.h("span", { className: "fleet-agent-name", text: agent.agent }),
        trackSince(C.h("span", { className: "fleet-agent-since" }), agent.since)] })] });
  }

  function cards(model, snap) {
    var projects = snap.projects || [];
    if (!projects.length) return [C.h("div", { className: "fleet-empty", text: "No projects configured" })];
    var nodes = [];
    var idle = [];
    projects.forEach(function (p) {
      var live = model.agents.filter(function (a) { return a.project === p.name && a.live; });
      if (live.length) nodes.push(card(p, live)); else idle.push(p.name);
    });
    if (idle.length) nodes.push(C.h("div", { className: "fleet-quiet", text: "No agents: " + idle.join(", ") }));
    return nodes;
  }

  // -- Activity: the dashboard's feed, newest first --
  function feed(snap) {
    var rows = (snap.history || []).slice().sort(function (a, b) { return b.timestamp - a.timestamp; }).slice(0, FEED_CAP);
    var list = C.h("ol", { className: "fleet-feed-list" });
    rows.forEach(function (r) {
      var kind = r.state === "merged" ? "merged" : r.state === "session ended" ? "idle" : M.stateInfo(r.state).kind;
      list.appendChild(C.h("li", { className: "fleet-feed-item fleet-k-" + kind, attrs: { "data-task": r.taskId, "data-project": r.project }, children: [
        C.h("span", { className: "fleet-feed-time", text: M.clock(r.timestamp) }),
        C.h("span", { className: "fleet-dot", attrs: { "aria-hidden": "true" } }),
        C.h("span", { className: "fleet-feed-text", children: [C.h("b", { text: r.taskId }),
          C.h("span", { text: " " + (FEED_LABELS[r.state] || r.state) }),
          C.h("span", { className: "fleet-feed-project", text: " · " + r.project })] })] }));
    });
    var nodes = [C.h("h2", { text: "Activity" })];
    if (snap.historyUnavailable) nodes.push(C.h("p", { className: "phone-note phone-warn", text: "The activity history could not be read in full." }));
    nodes.push(rows.length ? list : C.h("p", { className: "fleet-feed-empty", text: "Nothing has happened in this window yet." }));
    return C.h("section", { className: "fleet-feed", attrs: { "aria-label": "Live activity" }, children: nodes });
  }

  function render(error) {
    var errEl = C.byId("phone-error");
    errEl.hidden = !error;
    errEl.textContent = error ? (snapshot ? "Could not refresh (" + error + "). Showing what was true at " + M.clock(snapshot.timestamp) + "." : "Could not load the status: " + error) : "";
    if (!snapshot) {
      if (error) C.byId("phone-updated").textContent = "Not loaded";
      return;
    }
    sinceCells = [];
    var model = M.segments(snapshot);
    C.syncChildren(C.byId("phone-pulse"), [pulse(model, snapshot)]);
    C.syncChildren(C.byId("phone-needs"), [needs(snapshot)]);
    C.syncChildren(C.byId("phone-cards"), cards(model, snapshot));
    C.syncChildren(C.byId("phone-feed"), [feed(snapshot)]);
    C.byId("phone-updated").textContent = "Updated " + M.clock(snapshot.timestamp);
  }

  function intervalMs() {
    var s = snapshot && Number(snapshot.refreshIntervalSeconds);
    return Math.max(MIN_INTERVAL_S, s > 0 ? s : 10) * 1000;
  }

  function schedule() {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(poll, intervalMs());
  }

  function poll() {
    timer = null;
    return fetch(statusUrl(), { cache: "no-store", credentials: "omit" }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) throw new Error(res.status === 404 ? "the link's key was not accepted" : (data && data.error) || ("HTTP " + res.status));
        return data;
      });
    }).then(function (data) {
      snapshot = data;
      clockBase = { now: Number(data.timestamp) || 0, at: Date.now() };
      render(null);
    }).catch(function (err) {
      render(err.message || String(err));
    }).then(schedule);
  }

  function tick() {
    var now = liveNow();
    sinceCells.forEach(function (c) { c.el.textContent = ago(c.since, now); });
  }

  setInterval(tick, 1000);
  // A phone puts a background tab to sleep; coming back should not wait
  // a whole interval to show what changed meanwhile.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") poll();
  });

  C.phone = { poll: poll, render: render };
  poll();
})(window.Centrale = window.Centrale || {});
