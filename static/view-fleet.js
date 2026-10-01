// static/view-fleet.js -- the Fleet view: one card per project (capacity ring,
// live agents). The fleet-at-a-glance bar and the Activity feed are shared
// pieces around every view (task-194), not drawn here.
//
// A self-contained view (task-189): it registers itself with C.views and
// reads everything from the shared snapshot and C.fleetModel; it names no
// other view except to ask the registry to open the Timeline, which the
// registry answers with false when that view is absent or hidden.
(function (C) {
  "use strict";
  var M = C.fleetModel;

  function capacity(project) {
    var max = project.maxAgents;
    if (typeof max === "number" && max > 0) {
      return { text: project.agentCount + " / " + max + " agents", full: project.agentCount >= max, pct: Math.min(100, Math.round(100 * project.agentCount / max)) };
    }
    return { text: project.agentCount + " agents · no limit", full: false, pct: null };
  }

  // Everything below keeps its elements between polls (task-190): a row is
  // updated in place so the state dot keeps breathing, the meter eases to its
  // new size and a state change can flash once, none of which survives a
  // host that is emptied and rebuilt. Rebuilt only when the registry has
  // cleared the host under us.
  var root = null;
    var clockBase = null; // {now, at}: the snapshot's time and the browser time it arrived, so the rows count up between polls
  var ticking = false;
  var cards = {};     // project name -> {el, name, cap, ring, rows: {taskId: row}}
  var primed = false; // false until a first draw, so the first paint doesn't animate every row in
  var quiet = null;

  function openTask(agent) {
    var task = C.findTask(agent.project, agent.taskId);
    if (!task) {
      C.showToast(agent.taskId + " is not on the board right now; refresh the board and try again.", "error");
      return;
    }
    C.openDrawer({ name: agent.project }, task);
  }

  // A click on a button inside the row is that button's own business.
  function onOwnControl(ev, row) {
    var node = ev && ev.target;
    while (node && node !== row) {
      if (node.tagName && /^(BUTTON|A|INPUT|SELECT|TEXTAREA)$/i.test(node.tagName) && String(node.className).indexOf("fleet-agent-open") === -1) return true;
      node = node.parentNode;
    }
    return false;
  }

  function sinceText(agent, now) {
    return agent.since === null ? "\u2014" : M.duration(now - agent.since);
  }

  // Seconds of browser time since the snapshot was taken, added to the
  // snapshot's own clock: the duration keeps counting between polls (task-193)
  // and lands on the server's number again when the next poll arrives.
  function liveNow() {
    return clockBase.now + (Date.now() - clockBase.at) / 1000;
  }

  function tick() {
    if (!root || !root.parentNode || !clockBase) { ticking = false; return; }
    var now = liveNow();
    Object.keys(cards).forEach(function (k) {
      var rows = cards[k].rows;
      Object.keys(rows).forEach(function (t) { rows[t].since.textContent = sinceText(rows[t].agent, now); });
    });
    setTimeout(tick, 1000);
  }

  function startTicking() {
    if (ticking) return;
    ticking = true;
    setTimeout(tick, 1000);
  }

  function setVar(el, name, value) {
    if (el.style.setProperty) el.style.setProperty(name, value); else el.style[name] = value;
  }

  function paintSpark(spark, agent, model) {
    var span = model.now - model.start || 1;
    agent.segments.forEach(function (seg, i) {
      var end = seg.end === null ? model.now : seg.end;
      var left = Math.max(0, 100 * (seg.start - model.start) / span);
      var width = Math.min(Math.max(100 * (end - seg.start) / span, 0.6), 100 - left);
      var bit = spark.childNodes[i];
      if (!bit) { bit = C.h("span"); spark.appendChild(bit); }
      bit.className = "fleet-spark-seg fleet-k-" + seg.kind + (seg.end === null ? " open" : "");
      bit.style.left = left + "%";
      bit.style.width = width + "%";
    });
    while (spark.childNodes.length > agent.segments.length) spark.removeChild(spark.childNodes[spark.childNodes.length - 1]);
  }

  function makeRow(agent) {
    var row = { agent: agent };
    row.dot = C.h("span", { className: "fleet-dot" });
    row.id = C.h("span", { className: "fleet-agent-id" });
    row.title = C.h("span", { className: "fleet-agent-title" });
    row.state = C.h("span", { className: "fleet-agent-state" });
    row.since = C.h("span", { className: "fleet-agent-since" });
    row.spark = C.h("span", { className: "fleet-spark", attrs: { "aria-hidden": "true" } });
    row.name = C.h("span", { className: "fleet-agent-name" });
    // The keyboard target is a real button; the whole row is also clickable
    // for the mouse. Timeline is the secondary control, and asks the
    // registry rather than assuming the Timeline exists.
    row.open = C.h("button", { className: "fleet-agent-open", attrs: { type: "button" },
      children: [row.id, row.title] });
    row.tl = C.h("button", { className: "fleet-agent-tl", text: "Timeline", attrs: { type: "button" },
      onclick: function (ev) {
        if (ev && ev.stopPropagation) ev.stopPropagation();
        if (C.views && C.views.open) C.views.open("timeline", { project: row.agent.project, taskId: row.agent.taskId });
      } });
    row.foot = C.h("span", { className: "fleet-agent-foot", children: [row.spark, row.name, row.since] });
    row.el = C.h("div", { className: "fleet-agent",
      children: [row.dot, C.h("span", { className: "fleet-agent-main", children: [row.open] }), row.state, row.tl, row.foot] });
    row.el.addEventListener("click", function (ev) {
      if (onOwnControl(ev, row.el)) return;
      var sel = window.getSelection && window.getSelection();
      if (sel && String(sel).length) return;   // the user was selecting text, not clicking
      openTask(row.agent);
    });
    row.kind = null;
    row.stateText = null;
    return row;
  }

  function paintRow(row, agent, model, animate) {
    var title = M.title(agent.project, agent.taskId);
    var since = sinceText(agent, model.now);
    var state = agent.state || "unknown";
    row.agent = agent;
    row.el.setAttribute("data-task", agent.taskId);
    row.el.setAttribute("data-project", agent.project);
    row.open.setAttribute("aria-label", agent.taskId + (title ? " " + title : "") + ", " + state + " for " + since + ". Open task");
    row.tl.setAttribute("aria-label", "Show " + agent.taskId + " on the Timeline");
    var changed = row.kind !== null && (row.kind !== agent.kind || row.stateText !== state);
    if (row.kind !== agent.kind) {
      if (row.kind) row.el.classList.remove("fleet-k-" + row.kind);
      row.el.classList.add("fleet-k-" + agent.kind);
    }
    row.kind = agent.kind;
    row.stateText = state;
    if (animate) row.el.classList.add("enter");
    if (changed) C.replayClass(row.el, "flash");
    row.id.textContent = agent.taskId;
    // No title known: the id stands alone rather than leaving an empty span.
    if (title) {
      row.title.textContent = title;
      row.title.title = title;
      if (!row.title.parentNode) row.open.appendChild(row.title);
    } else if (row.title.parentNode) {
      row.open.removeChild(row.title);
    }
    row.state.textContent = state;
    row.since.textContent = since;
    row.name.textContent = agent.agent;
    paintSpark(row.spark, agent, model);
  }

  function makeCard(project) {
    var card = { rows: {} };
    var hue = C.projectHue(project.name);
    card.tile = C.h("span", { className: "fleet-tile", text: String(project.name).charAt(0).toUpperCase(), attrs: { "aria-hidden": "true" } });
    card.name = C.h("span", { className: "fleet-card-name", text: project.name });
    card.cap = C.h("span", { className: "fleet-capacity", attrs: { "data-capacity": project.name } });
    card.count = C.h("span", { className: "fleet-ring-count" });
    card.ring = C.h("span", { className: "fleet-ring", children: [card.count] });
    card.head = C.h("div", { className: "fleet-card-head", children: [card.tile,
      C.h("span", { className: "fleet-card-id", children: [card.name, card.cap] }), card.ring] });
    card.el = C.h("section", { className: "fleet-card", attrs: { "data-project": project.name } });
    setVar(card.el, "--p", "hsl(" + hue + ", 62%, 52%)");
    return card;
  }

  function paintCard(card, project, model, animate) {
    var cap = capacity(project);
    card.cap.className = "fleet-capacity" + (cap.full ? " full" : "");
    card.cap.textContent = cap.text;
    card.ring.className = "fleet-ring" + (cap.pct === null ? " nolimit" : "") + (cap.full ? " full" : "");
    card.ring.setAttribute("role", "img");
    card.ring.setAttribute("aria-label", cap.pct === null ? project.agentCount + " agents, no limit set" : project.agentCount + " of " + project.maxAgents + " agent slots used");
    if (cap.pct !== null) setVar(card.ring, "--ring", cap.pct + "%");
    card.count.textContent = cap.pct === null ? String(project.agentCount) : project.agentCount + "/" + project.maxAgents;
    var live = model.agents.filter(function (a) { return a.project === project.name && a.live; });
    var nodes = [card.head];
    var seen = {};
    live.forEach(function (a) {
      var row = card.rows[a.taskId];
      var fresh = !row;
      if (fresh) row = card.rows[a.taskId] = makeRow(a);
      paintRow(row, a, model, fresh && animate);
      seen[a.taskId] = true;
      nodes.push(row.el);
    });
    Object.keys(card.rows).forEach(function (k) { if (!seen[k]) delete card.rows[k]; });
    C.syncChildren(card.el, nodes);
  }

  function render(host, snapshot, error) {
    if (!root || root.parentNode !== host) {
      C.clearChildren(host);
      root = C.h("div", { className: "fleet-root" });
      host.appendChild(root);
      cards = {};
      quiet = null;
      primed = false;
    }
    var top = [];
    if (error) top.push(C.h("div", { className: "view-error", text: "Fleet could not be read: " + error }));
    if (!snapshot) {
      if (!error) top.push(C.h("div", { className: "fleet-empty", text: "Loading…" }));
      C.syncChildren(root, top);
      return;
    }
    var model = M.segments(snapshot);
    clockBase = { now: model.now, at: Date.now() };
    startTicking();
    var projects = snapshot.projects || [];
    if (!projects.length) {
      top.push(C.h("div", { className: "fleet-empty", text: "No projects configured" }));
      C.syncChildren(root, top);
      return;
    }
    // A project with nothing running gets a mention, not a card.
    function running(p) { return model.agents.some(function (a) { return a.project === p.name && a.live; }); }
    var busy = projects.filter(running);
    var idle = projects.filter(function (p) { return !running(p); });
    var grid = root.grid || (root.grid = C.h("div", { className: "fleet-grid" }));
    var mainNodes = [];
    var nodes = [];
    var seen = {};
    busy.forEach(function (p) {
      if (!cards[p.name]) cards[p.name] = makeCard(p);
      paintCard(cards[p.name], p, model, primed);
      seen[p.name] = true;
      nodes.push(cards[p.name].el);
    });
    Object.keys(cards).forEach(function (k) { if (!seen[k]) delete cards[k]; });
    C.syncChildren(grid, nodes);
    if (busy.length) mainNodes.push(grid);
    if (idle.length) {
      if (!quiet) quiet = C.h("div", { className: "fleet-quiet", attrs: { "data-idle-projects": "1" } });
      quiet.textContent = "No agents: " + idle.map(function (p) { return p.name; }).join(", ");
      mainNodes.push(quiet);
    }
    C.syncChildren(root, top.concat(mainNodes));
    primed = true;
  }

  C.views.register({ id: "fleet", label: "Fleet", render: render });
})(window.Centrale = window.Centrale || {});
