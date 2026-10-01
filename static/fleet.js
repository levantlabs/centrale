// static/fleet.js -- the one shared poller of the fleet snapshot (GET /api/fleet)
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
//
// task-187: the Views milestone's data layer. Views never fetch: they
// subscribe here and are handed every snapshot. There is ONE timer, and it
// only runs while somebody is subscribed, so a dashboard with every view
// hidden polls nothing.
(function (C) {
  "use strict";

  // The snapshot window views ask for: the Timeline's two hours (the
  // server's own default, spelled out so a change there is not silent).
  var WINDOW_SECONDS = 7200;
  var MIN_INTERVAL_MS = 2000;

  var subscribers = [];
  var timer = null;
  var inFlight = null;

  // The last good snapshot, or null before the first one. A failed poll
  // keeps it: a view draws stale data with `error` set rather than blank.
  C.fleetSnapshot = null;
  C.fleetError = null;

  function intervalMs() {
    var seconds = Number(C.refreshIntervalSeconds);
    return Math.max(MIN_INTERVAL_MS, (seconds > 0 ? seconds : 5) * 1000);
  }

  function notify() {
    subscribers.slice().forEach(function (fn) {
      try { fn(C.fleetSnapshot, C.fleetError); } catch (e) { /* one broken view must not starve the rest */ }
    });
  }

  function poll() {
    if (inFlight) return inFlight;
    inFlight = fetch("/api/fleet?window=" + WINDOW_SECONDS).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
        return data;
      });
    }).then(function (data) {
      C.fleetSnapshot = data;
      C.fleetError = null;
    }).catch(function (err) {
      C.fleetError = err.message || String(err);
    }).then(function () {
      inFlight = null;
      notify();
    });
    return inFlight;
  }

  function schedule() {
    if (timer !== null || !subscribers.length) return;
    timer = setTimeout(function () {
      timer = null;
      poll().then(schedule);
    }, intervalMs());
  }

  // Returns the unsubscribe function. A new subscriber is handed the
  // snapshot already held at once (so a view opened mid-session draws
  // immediately) and the first subscriber starts the timer and a first poll.
  function subscribe(fn) {
    subscribers.push(fn);
    if (C.fleetSnapshot || C.fleetError) {
      try { fn(C.fleetSnapshot, C.fleetError); } catch (e) { /* see notify */ }
    }
    if (subscribers.length === 1) poll().then(schedule);
    return function unsubscribe() {
      var i = subscribers.indexOf(fn);
      if (i !== -1) subscribers.splice(i, 1);
      if (!subscribers.length && timer !== null) { clearTimeout(timer); timer = null; }
    };
  }

  // ------------------------------------------------------------------
  // The shared segment model (task-189)
  // ------------------------------------------------------------------
  //
  // Fleet's sparkline and Timeline's rows are the same drawing at two
  // sizes, so the derivation lives here, in the data layer both views
  // already depend on, and neither view file needs the other. It is pure:
  // a snapshot in, per-agent segments out.
  //
  // A state lasts from its history row to the next one. `merged` and
  // `session ended` close the agent's last segment and open none (merged
  // also leaves a marker). A segment is OPEN (end null, drawn to "now")
  // only while the agent is a live tmux session: history is evidence of
  // past events, never proof that something is still running. The
  // snapshot carries only rows inside its window, so the state an agent
  // was in BEFORE the window opened is unknown and its first segment
  // starts at its first row (or at the window's start for a live agent
  // with no change in the window).

  var KINDS = {
    spawn: ["work", "starting"], working: ["work", "working"], waiting: ["wait", "waiting"],
    idle: ["idle", "idle"], finished: ["review", "ready to review"],
    "merge blocked": ["block", "merge blocked"], unknown: ["unknown", "unknown"]
  };

  function stateInfo(state) {
    var k = KINDS[state] || KINDS.unknown;
    return { kind: k[0], label: k[1] };
  }

  function duration(seconds) {
    var s = Math.max(0, Math.floor(seconds));
    if (s < 60) return s + "s";
    var m = Math.floor(s / 60);
    if (m < 60) return m + "m";
    return Math.floor(m / 60) + "h " + (m % 60) + "m";
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function clock(ts) {
    var d = new Date(ts * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function fleetSegments(snapshot) {
    var empty = { now: 0, start: 0, agents: [] };
    if (!snapshot) return empty;
    var now = Number(snapshot.timestamp) || 0;
    var start = now - (Number(snapshot.window) || WINDOW_SECONDS);
    var byKey = {};
    var order = [];
    function entry(project, taskId) {
      var key = project + "\u0000" + taskId;
      if (!byKey[key]) {
        byKey[key] = { project: project, taskId: taskId, agent: "unknown", live: null, rows: [], segments: [], merged: null };
        order.push(key);
      }
      return byKey[key];
    }
    (snapshot.agents || []).forEach(function (a) {
      var e = entry(a.project, a.taskId);
      e.live = a;
      if (a.agent) e.agent = a.agent;
    });
    (snapshot.history || []).forEach(function (r) {
      var e = entry(r.project, r.taskId);
      if (!e.live && r.agent) e.agent = r.agent;
      e.rows.push(r);
    });
    var agents = order.map(function (key) {
      var e = byKey[key];
      e.rows.sort(function (a, b) { return a.timestamp - b.timestamp; });
      var cur = null;
      function close(at) {
        if (!cur) return;
        if (at > start) e.segments.push({ kind: cur.kind, label: cur.label, start: Math.max(cur.start, start), end: at, open: false });
        cur = null;
      }
      e.rows.forEach(function (r) {
        close(r.timestamp);
        if (r.state === "merged") e.merged = r.timestamp;
        else if (r.state !== "session ended") {
          var info = stateInfo(r.state);
          cur = { kind: info.kind, label: info.label, start: r.timestamp };
        }
      });
      if (cur && e.live) {
        e.segments.push({ kind: cur.kind, label: cur.label, start: Math.max(cur.start, start), end: null, open: true });
      } else if (cur) {
        // Closed at its own start: a session that vanished unobserved.
        e.segments.push({ kind: cur.kind, label: cur.label, start: Math.max(cur.start, start), end: Math.max(cur.start, start), open: false });
      } else if (e.live && !e.rows.length) {
        var info0 = stateInfo(e.live.state);
        e.segments.push({ kind: info0.kind, label: info0.label, start: start, end: null, open: true });
      }
      var open = e.segments.length ? e.segments[e.segments.length - 1] : null;
      if (open && !open.open) open = null;
      e.state = open ? open.label : null;
      e.kind = open ? open.kind : "unknown";
      if (e.live && e.live.state && e.live.state !== "unknown") {
        var li = stateInfo(e.live.state);
        e.state = li.label; e.kind = li.kind;
      }
      e.since = e.live && typeof e.live.stateSince === "number" ? e.live.stateSince : (open ? open.start : null);
      return e;
    }).filter(function (e) { return e.live || e.segments.length || e.merged !== null && e.merged > start; });
    return { now: now, start: start, agents: agents };
  }

  // A task's title from the board data the page already holds, or "" when
  // the board has not loaded, the project errored or the task is unknown.
  function taskTitle(project, taskId) {
    var projects = (C.boardData && C.boardData.projects) || [];
    var want = String(taskId).toUpperCase();
    for (var i = 0; i < projects.length; i++) {
      if (projects[i].name !== project) continue;
      var tasks = projects[i].tasks || [];
      for (var j = 0; j < tasks.length; j++) {
        if (String(tasks[j].id).toUpperCase() === want) return tasks[j].title || "";
      }
    }
    return "";
  }

  C.fleetModel = { title: taskTitle, segments: fleetSegments, stateInfo: stateInfo, duration: duration, clock: clock };

  C.fleetData = { subscribe: subscribe, refresh: poll, subscriberCount: function () { return subscribers.length; } };
})(window.Centrale = window.Centrale || {});
