// static/pane.js -- the live tmux pane preview, its reply row, the theater, and drawer close
//
// One of the per-concern frontend files loaded in order by
// static/index.html (task-89); the seam between them is the single
// window.Centrale namespace object, taken here as C. See
// static/state.js for the two rules that seam follows.
(function (C) {
  "use strict";

  // ------------------------------------------------------------------
  // task-60: live read-only pane preview in the drawer
  // ------------------------------------------------------------------
  //
  // tmux stays the source of truth; this is a thin veneer over
  // GET /api/session-pane (one `tmux capture-pane` per request). The
  // whole thing is O(1) in agent count by construction: there is exactly
  // one poll loop, it exists only while a drawer is open for a task that
  // findLiveSession() says has a live session (and the feature is on),
  // and each tick fetches that ONE task's pane -- nothing here ever
  // iterates sessionsData to poll per session. Ticks self-reschedule
  // after each response (never overlapping), so a slow server can't
  // pile up requests.
  //
  // Rendering discipline (task-50): a tick writes only inside
  // #drawer-pane-area -- the <pre>'s text, the age label, an error line.
  // It never calls renderBoard()/renderAll()/renderDrawerSessionArea(),
  // and none of those ever touch #drawer-pane-area, so a pane refresh
  // can neither trigger a board rebuild nor reset a lane's scrollTop.
  var PANE_POLL_INTERVAL_MS = 2000;
  // task-114: the baseline above is what a session nobody is acting on
  // gets. Two faster cadences ride the SAME single timer -- see
  // paneTickDelayMs() -- because a fixed 2s is wrong at both ends: it
  // wastes captures on an idle pane, and it turns a reply into a dead
  // pause followed by a jump (the sent text and the agent's first
  // response arriving together on one tick).
  var PANE_POLL_ACTIVE_INTERVAL_MS = 1000; // the badge says the agent is working
  var PANE_POLL_BURST_INTERVAL_MS = 300;   // just after a reply was accepted
  var PANE_BURST_MS = 4000;                // how long that burst lasts before it decays back
  // Every tick fetches the server's full 200-line window (same single
  // tmux call as any smaller count); the drawer shows only the last
  // DRAWER_PANE_TAIL_LINES of it as a live tail, while the theater
  // renders the whole window -- instantly on open, from the payload
  // already in hand.
  //
  // task-152: 200 is a window the pane can actually fill. An agent TUI
  // owns tmux's alternate screen and keeps no scrollback, so a capture
  // can only ever return the pane's height -- which is why a spawned
  // session is created exactly this many rows tall
  // (spawn.SESSION_GEOMETRY = server.MAX_SESSION_PANE_LINES). Asking
  // for more than the pane is built for would be asking for lines that
  // can never arrive.
  var PANE_FETCH_LINES = 200;
  var DRAWER_PANE_TAIL_LINES = 40;
  var panePoll = {
    key: null,          // "project::taskId" being polled, or null when idle
    timer: null,        // pending setTimeout handle for the next tick
    seq: 0,             // bumped on every (re)start/stop; stale responses check it
    inFlight: false,
    receivedAt: null,   // Date.now() of the last successful capture (age label base)
    capturedAt: null,   // server's ISO capturedAt of that capture
    lastText: null,     // last rendered pane text (skip identical DOM writes)
    lastLines: null,    // full fetched window, so the theater/drawer can re-render at their own size without a fetch
    failed: false,      // last tick failed -> label the capture as stale
    burstUntil: 0       // task-114: Date.now() until which ticks run at the burst cadence (0 = not bursting)
  };

  function panePollKeyFor(drawer) {
    return drawer ? C.spawnKey(drawer.project, drawer.id) : null;
  }

  // ------------------------------------------------------------------
  // task-114: adaptive poll cadence
  // ------------------------------------------------------------------
  //
  // Still exactly ONE poller and one chained timer (the single-poller
  // contract): what follows changes the INTERVAL that timer is armed
  // with, and never adds a second timer or a parallel fetch path.
  //
  // Three cadences, most urgent first:
  //   - BURST, for a few seconds after a reply is accepted. tmux accepts
  //     the paste before the TUI has redrawn, so the immediate
  //     re-capture a send already triggers still shows the pre-send
  //     screen; at 2s the next one arrives carrying the user's text AND
  //     the agent's first response at once, which reads as a dead pause
  //     and then a jump. At ~300ms the text lands, then the agent
  //     begins, as one continuous motion.
  //   - ACTIVE, while the agent badge for this session says "working" --
  //     the pane is changing, so a second is worth it.
  //   - the BASELINE otherwise: an idle or waiting session that nobody
  //     is acting on does not need the attention.
  function paneTickDelayMs() {
    if (panePoll.burstUntil && Date.now() < panePoll.burstUntil) return PANE_POLL_BURST_INTERVAL_MS;
    // The same live session object the badge is rendered from (tasks.js);
    // read, never stored, so there is nothing here to keep in sync.
    var live = C.currentDrawer && C.findLiveSession(C.currentDrawer.project, C.currentDrawer.id);
    if (live && live.agentState === "working") return PANE_POLL_ACTIVE_INTERVAL_MS;
    return PANE_POLL_INTERVAL_MS;
  }

  // A cadence change re-arms the ONE pending timer at the new delay; it
  // never fetches (a tick already in flight has no timer to re-pace and
  // will schedule the next one itself when it lands).
  function repacePaneTimer() {
    if (panePoll.timer) scheduleNextPaneTick();
  }

  // Bounded by construction: the burst is an expiry, not a mode -- once
  // Date.now() passes it the very next reschedule is back at the
  // baseline, with nothing to cancel.
  function startPaneBurst() {
    if (!panePoll.key) return;
    panePoll.burstUntil = Date.now() + PANE_BURST_MS;
    repacePaneTimer();
  }

  // Ends the fast phase at once -- the theater closing, or the poll
  // stopping (drawer closed, session ended, tier turned off). A tick
  // already armed at the fast delay is re-paced rather than left to fire.
  function endPaneBurst() {
    if (!panePoll.burstUntil) return;
    panePoll.burstUntil = 0;
    repacePaneTimer();
  }

  // The single decision point: should the pane be polled right now, and
  // for which task? Idempotent and cheap -- safe to call from every
  // drawer open/close and every board/sessions render.
  function syncDrawerPanePolling() {
    var wanted = null;
    if (C.currentDrawer && C.sessionPreviewMode !== "off" && C.findLiveSession(C.currentDrawer.project, C.currentDrawer.id)) {
      wanted = panePollKeyFor(C.currentDrawer);
    }
    if (wanted === panePoll.key) { syncDrawerPaneReplyRow(); return; } // already polling the right task; only the reply tier may have changed
    stopDrawerPanePolling();
    if (!wanted) return;
    panePoll.key = wanted;
    panePoll.seq += 1;
    renderDrawerPaneSkeleton();
    fetchDrawerPane();
  }

  function stopDrawerPanePolling() {
    if (panePoll.timer) { clearTimeout(panePoll.timer); panePoll.timer = null; }
    panePoll.key = null;
    panePoll.seq += 1; // invalidates any response still in flight
    panePoll.inFlight = false;
    panePoll.receivedAt = null;
    panePoll.capturedAt = null;
    panePoll.lastText = null;
    panePoll.lastLines = null;
    panePoll.failed = false;
    panePoll.burstUntil = 0; // task-114: the timer is already cleared above, so there is nothing to re-pace
    closeTheater(); // task-73: the area goes home to the drawer before it is emptied
    C.clearChildren(C.byId("drawer-pane-area"));
    applyDrawerWidth(); // task-68: no pane section -> back to the summary width
  }

  // task-68: wide mode. The ONLY writer of #drawer's `wide` class and
  // the toggle's pressed state. Deliberately touches nothing else -- no
  // board render, no lane, no poll state -- so flipping the width is as
  // invisible to the board as a pane tick is (the drawer is
  // position:fixed, so the board never reflows under it either). Wide
  // applies only while the pane section (the toggle's home) is present:
  // the preference is remembered across sessions and reloads, but a
  // drawer with no live pane opens at the summary width.
  function applyDrawerWidth() {
    var wide = !!(C.drawerWide && panePoll.key && C.byId("drawer-pane-pre"));
    C.byId("drawer").classList.toggle("wide", wide);
    var btn = C.byId("drawer-pane-wide-toggle");
    if (btn) {
      btn.setAttribute("aria-pressed", C.drawerWide ? "true" : "false");
      btn.textContent = C.drawerWide ? "Narrow" : "Expand";
      btn.title = C.drawerWide
        ? "Back to the summary width"
        : "Widen the drawer so the full 80-column pane fits without horizontal scroll";
    }
  }

  function toggleDrawerWide() {
    C.drawerWide = !C.drawerWide;
    C.persistDrawerWide(C.drawerWide);
    applyDrawerWidth();
  }

  function renderDrawerPaneSkeleton() {
    var area = C.byId("drawer-pane-area");
    C.clearChildren(area);
    var header = C.h("div", { className: "drawer-pane-header" });
    var left = C.h("div", { className: "drawer-pane-header-left" });
    left.appendChild(C.h("h3", { text: "Live session pane", attrs: { id: "drawer-pane-title" } }));
    // task-73: shown only inside the theater, where the drawer header
    // that normally names the task is behind the backdrop.
    left.appendChild(C.h("span", {
      className: "drawer-pane-theater-task",
      text: C.currentDrawer ? C.currentDrawer.project + " / " + C.currentDrawer.id : ""
    }));
    header.appendChild(left);
    var right = C.h("div", { className: "drawer-pane-header-right" });
    right.appendChild(C.h("span", { className: "drawer-pane-age", attrs: { id: "drawer-pane-age" }, text: "capturing…" }));
    right.appendChild(C.h("button", {
      className: "btn btn-sm drawer-pane-wide-toggle",
      attrs: { id: "drawer-pane-wide-toggle", type: "button", "aria-pressed": "false" },
      onclick: toggleDrawerWide
    }));
    right.appendChild(C.h("button", {
      className: "btn btn-sm drawer-pane-theater-toggle",
      attrs: { id: "drawer-pane-theater-toggle", type: "button", "aria-pressed": "false" },
      onclick: toggleTheater
    }));
    header.appendChild(right);
    area.appendChild(header);
    var pre = C.h("pre", { className: "drawer-pane-pre empty", attrs: { id: "drawer-pane-pre" }, text: "Waiting for the first capture…" });
    area.appendChild(pre);
    area.appendChild(C.h("div", { className: "drawer-pane-error", attrs: { id: "drawer-pane-error" } }));
    applyDrawerWidth(); // task-68: the section exists now, so a stored wide preference applies
    applyTheaterControls(); // task-73: labels the fresh Maximize control (the theater itself is closed here: a skeleton only renders after stopDrawerPanePolling)
    syncDrawerPaneReplyRow(); // task-61: the reply row, when the tier allows it
  }

  // task-73: the session theater -- the READING/REPLYING tier above the
  // drawer's wide mode (task-68, the glance tier). The pane section is
  // shown in a large, centered in-page overlay built on the settings
  // modal's skeleton (fixed backdrop + fixed dialog layer with .open
  // classes, role=dialog/aria-modal, Esc through the one global keydown
  // handler, backdrop click) -- never a browser window or popup.
  //
  // Single poller, by construction: there is no theater copy of the
  // pane. Opening MOVES the drawer's #drawer-pane-area node (section
  // header, age label, <pre>, error line and -- when the tier has one --
  // the reply row) into #theater; closing moves the same node back to
  // its slot in the drawer. Every poll tick and every reply keeps
  // addressing the same element ids, so the poll cadence, the reply
  // gate and the tier rules are untouched and there is nothing to keep
  // in sync. The theater can only open while a live pane is being
  // polled, which is exactly when the tier isn't "off"; a "view" tier
  // pane has no reply row to move, so the theater has none either.
  //
  // Like applyDrawerWidth, open/close touch nothing but the overlay's
  // classes, the node's parent, the <pre>'s scroll offset and the
  // toggle -- no board render, no lane, no poll state. The drawer
  // underneath is left exactly as it was.
  //
  // task-114: the two surfaces do NOT share a scroll position. Opening
  // re-renders the same node from a 40-line tail to the full 200-line
  // window, so a pixel offset taken in one points somewhere unrelated
  // in the other -- which is why Maximize used to land in the middle of
  // nowhere. Maximize exists to read what the agent is doing NOW, so
  // the theater always opens at the bottom, on the newest output; and
  // closing always restores the DRAWER's own remembered offset rather
  // than mapping the theater's back through the same broken conversion.
  var theaterOpen = false;
  var theaterReturn = null; // what the drawer looked like when the theater opened: restored on close

  function paneScrollState(pre) {
    if (!pre) return null;
    // A node loses its scroll offset when it is re-parented; remember
    // it, and whether the sticky-bottom rule was following new output.
    return { top: pre.scrollTop, atBottom: (pre.scrollHeight - pre.scrollTop - pre.clientHeight) < 4 };
  }

  function restorePaneScroll(pre, state) {
    if (!pre || !state) return;
    pre.scrollTop = state.atBottom ? pre.scrollHeight : state.top;
  }

  // task-114: what Maximize opens on, unconditionally. Also re-arms the
  // sticky-bottom rule in renderDrawerPaneCapture, so the theater keeps
  // following new output until the reader scrolls up themselves.
  function scrollPaneToBottom(pre) {
    if (!pre) return;
    pre.scrollTop = pre.scrollHeight;
  }

  // The ONLY writer of the theater's open state in the DOM and of the
  // toggle's label/pressed state.
  function applyTheaterControls() {
    var theater = C.byId("theater");
    theater.classList.toggle("open", theaterOpen);
    theater.setAttribute("aria-hidden", theaterOpen ? "false" : "true");
    C.byId("theater-backdrop").classList.toggle("open", theaterOpen);
    var btn = C.byId("drawer-pane-theater-toggle");
    if (btn) {
      btn.setAttribute("aria-pressed", theaterOpen ? "true" : "false");
      btn.textContent = theaterOpen ? "Close" : "Maximize";
      btn.title = theaterOpen
        ? "Back to the drawer (Esc)"
        : "Read and reply in a large in-page view of the live pane";
    }
  }

  function openTheater() {
    if (theaterOpen) return;
    var area = C.byId("drawer-pane-area");
    var pre = C.byId("drawer-pane-pre");
    if (!panePoll.key || !pre) return; // no live pane being polled (incl. the "off" tier) -> nothing to show
    // The drawer's own reading position, remembered for close() and used
    // nowhere else. Its body offset goes with it: taking the pane footer
    // out of the drawer lets #drawer-body grow, which can clamp that
    // offset to 0 -- both are captured here so close() can put the
    // drawer back exactly as it was.
    var scroll = paneScrollState(pre);
    theaterReturn = { preScroll: scroll, bodyScrollTop: C.byId("drawer-body").scrollTop };
    C.byId("theater").appendChild(area);
    renderTheaterRail(); // task-93: the task rail, from data already in memory
    theaterOpen = true;
    applyTheaterControls();
    // Re-parenting dropped focus; put it on the (now "Close") control so
    // the next Esc closes the theater rather than nothing.
    var btn = C.byId("drawer-pane-theater-toggle");
    if (btn) btn.focus();
    // Show the full 200-line window immediately -- from the payload the
    // poller already fetched, NOT a new fetch (the single-poller
    // contract). lastText is cleared so the re-render isn't skipped as
    // an identical screen.
    if (panePoll.lastLines) {
      panePoll.lastText = null;
      renderDrawerPaneCapture(panePoll.lastLines);
    }
    // task-114: after the re-render, so it lands on the 200-line document
    // and not on the 40-line one it replaced.
    scrollPaneToBottom(pre);
  }

  function closeTheater() {
    if (!theaterOpen) return;
    var area = C.byId("drawer-pane-area");
    var pre = C.byId("drawer-pane-pre");
    // Where the drawer was when Maximize was clicked -- never where the
    // theater ended up. The theater opens at the bottom of a 200-line
    // document by design (task-114), so its offset carries no reading
    // position to bring back, and converting it into the 40-line tail
    // would be the same meaningless pixel mapping in reverse.
    var scroll = theaterReturn && theaterReturn.preScroll;
    endPaneBurst(); // task-114: leaving the theater ends the fast phase at once
    C.byId("drawer").insertBefore(area, C.byId("drawer-harvest-area"));
    removeTheaterRail(); // task-93
    theaterOpen = false;
    applyTheaterControls();
    // Back to the drawer's 40-line tail, from the cached window -- no
    // fetch, and before the scroll restore so it lands on final content.
    if (panePoll.lastLines) {
      panePoll.lastText = null;
      renderDrawerPaneCapture(panePoll.lastLines);
    }
    restorePaneScroll(pre, scroll);
    if (theaterReturn) C.byId("drawer-body").scrollTop = theaterReturn.bodyScrollTop;
    theaterReturn = null;
    var btn = C.byId("drawer-pane-theater-toggle");
    if (btn) btn.focus();
  }

  function toggleTheater() {
    if (theaterOpen) closeTheater(); else openTheater();
  }

  function scheduleNextPaneTick() {
    if (!panePoll.key) return;
    if (panePoll.timer) clearTimeout(panePoll.timer);
    panePoll.timer = setTimeout(function () {
      panePoll.timer = null;
      fetchDrawerPane();
    }, paneTickDelayMs()); // task-114: baseline, working, or burst -- one timer either way
  }

  function fetchDrawerPane() {
    if (!panePoll.key || panePoll.inFlight || !C.currentDrawer) return;
    var seq = panePoll.seq;
    var key = panePoll.key;
    // Always fetch the server's 200-line cap: it's the same single
    // capture call as 40 lines (the count is a display window, not a
    // cost knob), and having the full window on hand lets the theater
    // show a real read-back instantly on open -- from the last payload,
    // with NO extra fetch (see openTheater and the single-poller
    // contract). The drawer renders only the tail of it.
    var url = "/api/session-pane?project=" + encodeURIComponent(C.currentDrawer.project) +
      "&task=" + encodeURIComponent(C.currentDrawer.id) + "&lines=" + PANE_FETCH_LINES;
    panePoll.inFlight = true;
    fetch(url).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) {
          var err = new Error((data && data.error) || ("HTTP " + res.status));
          err.status = res.status;
          throw err;
        }
        return data;
      });
    }).then(function (data) {
      if (seq !== panePoll.seq || key !== panePoll.key) return; // poll was stopped/retargeted meanwhile
      panePoll.failed = false;
      panePoll.receivedAt = Date.now();
      panePoll.capturedAt = data && data.capturedAt ? data.capturedAt : null;
      renderDrawerPaneCapture((data && Array.isArray(data.lines)) ? data.lines : []);
      updateDrawerPaneAge();
    }).catch(function (err) {
      if (seq !== panePoll.seq || key !== panePoll.key) return;
      panePoll.failed = true;
      var errEl = C.byId("drawer-pane-error");
      if (err && err.status === 404) {
        // The session ended between polls. Leave the last capture up
        // (labeled stale) -- the next sessions refresh will stop this
        // poll via syncDrawerPanePolling() and clear the area.
        if (errEl) errEl.textContent = "Session is no longer live.";
      } else if (err && err.status === 403) {
        // Disabled server-side since we started: stop for good, the
        // next board refresh flips sessionPreviewMode to match.
        if (errEl) errEl.textContent = "Live pane disabled in Settings.";
        C.sessionPreviewMode = "off";
        stopDrawerPanePolling();
        return;
      } else if (errEl) {
        errEl.textContent = "Refresh failed: " + ((err && err.message) || err);
      }
      updateDrawerPaneAge();
    }).then(function () {
      if (seq !== panePoll.seq || key !== panePoll.key) return;
      panePoll.inFlight = false;
      scheduleNextPaneTick();
    });
  }

  // The pane grid is as wide as the tmux pane (typically 80 columns) but
  // the drawer is narrower, and the lines that actually overflow are
  // almost never content -- they're TUI chrome: full-width `────` rules,
  // `====`/`----` separators, progress-dot runs. Collapsing any run of
  // 40+ IDENTICAL decorative characters (box-drawing marks and the few
  // ASCII rule characters; never letters or digits) to 40 keeps those
  // lines inside the drawer without touching real output -- a truncated
  // rule conveys exactly what the full-width one did. Genuinely wide
  // content still overflows into the <pre>'s own horizontal scroll.
  var PANE_DECOR_RUN_RE = /([─━═╌┄┈╍┅┉▔▁▀▄█\-=_·.*#~])\1{39,}/g;

  function clampPaneLine(line) {
    return line.replace(PANE_DECOR_RUN_RE, function (run, ch) {
      return new Array(41).join(ch);
    });
  }

  function renderDrawerPaneCapture(lines) {
    var pre = C.byId("drawer-pane-pre");
    var errEl = C.byId("drawer-pane-error");
    if (!pre) return;
    if (errEl) errEl.textContent = "";
    // The full fetched window is kept so open/closeTheater can re-render
    // at the other surface's size without a fetch; the drawer displays
    // only the tail of it.
    panePoll.lastLines = lines;
    var shown = theaterOpen ? lines : lines.slice(-DRAWER_PANE_TAIL_LINES);
    var text = shown.map(clampPaneLine).join("\n");
    if (text === panePoll.lastText) return; // identical screen: leave the DOM alone
    // Sticky bottom: keep following the newest output unless the user
    // has scrolled up to read something earlier.
    var wasAtBottom = (pre.scrollHeight - pre.scrollTop - pre.clientHeight) < 4;
    panePoll.lastText = text;
    if (!lines.length) {
      pre.className = "drawer-pane-pre empty";
      pre.textContent = "(pane is blank)";
    } else {
      pre.className = "drawer-pane-pre";
      pre.textContent = text;
    }
    if (wasAtBottom) pre.scrollTop = pre.scrollHeight;
  }

  function updateDrawerPaneAge() {
    if (!panePoll.key) return;
    updateDrawerPaneReplyGate(); // task-61: the send gate ages on the same clock as the label
    var el = C.byId("drawer-pane-age");
    if (!el) return;
    if (panePoll.receivedAt === null) {
      el.textContent = panePoll.failed ? "no capture yet" : "capturing…";
      el.className = "drawer-pane-age" + (panePoll.failed ? " stale" : "");
      return;
    }
    var ageSec = Math.max(0, Math.round((Date.now() - panePoll.receivedAt) / 1000));
    var when = "";
    if (panePoll.capturedAt) {
      var d = new Date(panePoll.capturedAt);
      if (!isNaN(d.getTime())) when = "captured " + d.toLocaleTimeString() + " · ";
    }
    var stale = panePoll.failed || ageSec > 10;
    el.textContent = when + (ageSec === 0 ? "just now" : ageSec + "s ago") + (stale ? " (stale)" : "");
    el.className = "drawer-pane-age" + (stale ? " stale" : "");
  }

  // ------------------------------------------------------------------
  // task-61: reply to a waiting agent from the drawer (tmux bracketed paste)
  // ------------------------------------------------------------------
  //
  // A single-line input plus Send and the two session keys (task-135:
  // Esc and Enter), living INSIDE #drawer-pane-area under the <pre> and
  // written only by this code (same task-50 discipline as the pane: never
  // a board re-render). It exists only while the pane poll is running AND
  // sessionPreviewMode is "interact" -- the "view" tier keeps the
  // read-only pane and drops the row; the server refuses
  // POST /api/session-input in both other tiers.
  //
  // The keys do not editorialise. Enter confirms whatever the pane has
  // focused, which on some dialogs is a default nobody would choose blind
  // -- so the terminal view directly above these buttons IS the preflight,
  // and there is deliberately no confirm step, warning, or capture read
  // layered on top of the click.
  //
  // Staleness honesty: replies land in a TUI seen through a polled capture,
  // so a prompt can change between the capture and the keystroke. That
  // is mitigated, not solved: sending is blocked (here AND with a 409
  // server-side) unless the last capture is recent, the capture age sits
  // right next to the input, and a successful send forces an immediate
  // re-capture so the effect is visible at once.
  var REPLY_MAX_CAPTURE_AGE_SEC = 10; // mirrors SESSION_INPUT_MAX_CAPTURE_AGE_SECONDS in server.py
  var REPLY_MESSAGE_TTL_MS = 6000;
  // task-135: restored after task-77 removed all three quick keys, minus
  // the "y" it removed for reasons that still hold. A pane parked on a
  // startup modal is unreachable by the text box -- the line lands where
  // there is no prompt and its trailing Enter answers the focused option
  // -- so without these two the only way out is attaching to tmux.
  var REPLY_KEYS = [
    { key: "Escape", label: "Esc", title: "Press Escape in the session (dismiss / cancel)" },
    { key: "Enter", label: "Enter", title: "Press Enter in the session (accept whatever it has focused)" }
  ];
  var paneReply = {
    inFlight: false,
    message: null,        // last send result shown below the input
    messageKind: null,    // "success" | "error"
    messageUntil: 0       // Date.now() after which the message clears
  };

  function replyTierEnabled() { return C.sessionPreviewMode === "interact"; }

  // Add or remove the row to match the tier; idempotent. Called from the
  // pane skeleton and from syncDrawerPanePolling when the poll target is
  // unchanged but Settings may have flipped the tier mid-drawer.
  function syncDrawerPaneReplyRow() {
    if (!panePoll.key) return; // no pane -> no row (the area is empty anyway)
    var row = C.byId("drawer-pane-reply");
    if (!replyTierEnabled()) {
      if (row) row.parentNode.removeChild(row);
      return;
    }
    if (row) return;
    var area = C.byId("drawer-pane-area");
    if (!area || !C.byId("drawer-pane-pre")) return;
    area.appendChild(renderDrawerPaneReplyRow());
    updateDrawerPaneReplyGate();
  }

  function renderDrawerPaneReplyRow() {
    var row = C.h("div", { className: "drawer-pane-reply", attrs: { id: "drawer-pane-reply" } });

    var form = C.h("div", { className: "drawer-pane-reply-form" });
    var input = document.createElement("input");
    input.type = "text";
    input.id = "drawer-pane-reply-input";
    input.className = "settings-input drawer-pane-reply-input";
    input.placeholder = "Type a one-line reply; Enter sends it";
    input.maxLength = 1000; // MAX_SESSION_INPUT_TEXT_CHARS
    input.autocomplete = "off";
    input.spellcheck = false;
    input.setAttribute("aria-label", "Reply to the agent");
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        sendDrawerPaneReply({ text: input.value });
      } else if (e.key === "Escape") {
        // The field owns Escape while focused (like the search box): clear
        // a draft, or drop focus -- never close the drawer under the user.
        e.stopPropagation();
        if (input.value) input.value = ""; else input.blur();
      }
    });
    form.appendChild(input);
    form.appendChild(C.h("button", {
      className: "btn btn-sm btn-primary", text: "Send",
      attrs: { id: "drawer-pane-reply-send", type: "button" },
      title: "Type this line into the session, followed by Enter",
      onclick: function () { sendDrawerPaneReply({ text: input.value }); }
    }));
    // Beside Send, not on a row of their own: they answer the same pane
    // as the text box and share its gate, its status line and its note.
    REPLY_KEYS.forEach(function (k) {
      form.appendChild(C.h("button", {
        className: "btn btn-sm drawer-pane-reply-key", text: k.label, title: k.title,
        attrs: { type: "button", "data-key": k.key },
        onclick: function () { sendDrawerPaneReply({ key: k.key }); }
      }));
    });
    row.appendChild(form);

    row.appendChild(C.h("span", {
      className: "drawer-pane-reply-status",
      attrs: { id: "drawer-pane-reply-status" }
    }));

    row.appendChild(C.h("div", {
      className: "drawer-pane-reply-note",
      text: "Replies are pasted into the tmux session above and submitted with Enter; Esc and Enter go in as those keys. The pane is a polled capture, so the prompt may have moved on since it was taken -- check the capture age before you answer."
    }));
    return row;
  }

  // Why sending is blocked right now, or null when it may go ahead. The
  // server enforces the same rules (404 session gone, 409 stale capture).
  function drawerPaneReplyBlockReason() {
    if (!panePoll.key) return "no live session";
    if (panePoll.receivedAt === null) return "waiting for the first capture";
    if (panePoll.failed) return "session is not live (last capture failed)";
    var ageSec = Math.max(0, Math.round((Date.now() - panePoll.receivedAt) / 1000));
    if (ageSec > REPLY_MAX_CAPTURE_AGE_SEC) return "capture is " + ageSec + "s old";
    return null;
  }

  function setDrawerPaneReplyMessage(message, kind) {
    paneReply.message = message;
    paneReply.messageKind = kind;
    paneReply.messageUntil = message ? Date.now() + REPLY_MESSAGE_TTL_MS : 0;
    updateDrawerPaneReplyGate();
  }

  function updateDrawerPaneReplyGate() {
    var row = C.byId("drawer-pane-reply");
    if (!row) return;
    var reason = drawerPaneReplyBlockReason();
    var disabled = !!reason || paneReply.inFlight;
    row.querySelectorAll("input, button").forEach(function (el) { el.disabled = disabled; });
    if (paneReply.message && Date.now() > paneReply.messageUntil) {
      paneReply.message = null;
      paneReply.messageKind = null;
    }
    var status = C.byId("drawer-pane-reply-status");
    if (!status) return;
    // Priority: in flight > blocked (a live gate always beats a transient
    // result message, so a session that just vanished is never hidden
    // behind a stale success message for six seconds) > last result > plain age.
    if (paneReply.inFlight) {
      status.textContent = "sending…";
      status.className = "drawer-pane-reply-status";
    } else if (reason) {
      status.textContent = "sending blocked: " + reason;
      status.className = "drawer-pane-reply-status stale";
    } else if (paneReply.message) {
      status.textContent = paneReply.message;
      status.className = "drawer-pane-reply-status " + paneReply.messageKind;
    } else {
      var ageSec = Math.max(0, Math.round((Date.now() - panePoll.receivedAt) / 1000));
      status.textContent = "capture " + (ageSec === 0 ? "just now" : ageSec + "s old");
      status.className = "drawer-pane-reply-status";
    }
  }

  // Pull the next capture forward so a reply's effect shows up at once
  // rather than up to 2s later. Never overlaps an in-flight tick.
  function refreshDrawerPaneNow() {
    if (!panePoll.key || panePoll.inFlight) return;
    if (panePoll.timer) { clearTimeout(panePoll.timer); panePoll.timer = null; }
    fetchDrawerPane();
  }

  // What the status line calls a sent payload: the key's own label, or
  // the text quoted so a reply of "Enter" can't read as the key.
  function describeReplyPayload(payload) {
    if (payload.key !== undefined) return payload.key === "Escape" ? "Esc" : payload.key;
    return JSON.stringify(payload.text);
  }

  // `payload` is exactly one of { text: "<one line>" } or { key: "Escape" |
  // "Enter" } -- the same exactly-one-of the server validates.
  function sendDrawerPaneReply(payload) {
    if (!C.currentDrawer || !panePoll.key || paneReply.inFlight || !replyTierEnabled()) return;
    var reason = drawerPaneReplyBlockReason();
    if (reason) { setDrawerPaneReplyMessage("not sent: " + reason, "error"); return; }
    if (payload.text !== undefined) {
      if (!payload.text.trim()) { setDrawerPaneReplyMessage("type a reply first", "error"); return; }
      if (/[\r\n]/.test(payload.text)) { setDrawerPaneReplyMessage("single-line replies only", "error"); return; }
    }
    // task-99: identity goes in the JSON body, not the query string --
    // the server no longer reads ?project=&task= here, so that a bare
    // cross-origin form POST cannot reach this endpoint fully populated.
    var body = { project: C.currentDrawer.project, taskId: C.currentDrawer.id };
    if (payload.key !== undefined) body.key = payload.key; else body.text = payload.text;
    paneReply.inFlight = true;
    updateDrawerPaneReplyGate();
    fetch("/api/session-input", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) {
          var err = new Error((data && data.error) || ("HTTP " + res.status));
          err.status = res.status;
          throw err;
        }
        return data;
      });
    }).then(function () {
      paneReply.inFlight = false;
      if (!C.byId("drawer-pane-reply")) return; // drawer moved on while sending
      var input = C.byId("drawer-pane-reply-input");
      if (payload.text !== undefined && input) input.value = "";
      setDrawerPaneReplyMessage("sent " + describeReplyPayload(payload), "success");
      // task-114: the capture below is pulled forward the instant tmux
      // accepted the paste, which is BEFORE the TUI has redrawn -- so it
      // still shows the pre-send screen. The burst makes the few seconds
      // after it run at ~300ms, so the sent line appearing and the agent
      // starting to answer read as one motion instead of one jump.
      startPaneBurst();
      refreshDrawerPaneNow();
      if (input && payload.text !== undefined && !input.disabled) input.focus();
    }).catch(function (err) {
      paneReply.inFlight = false;
      if (err && err.status === 403) {
        // Disabled server-side since we rendered: drop to the read-only
        // tier locally (the next board refresh confirms the real mode).
        C.sessionPreviewMode = "view";
        syncDrawerPaneReplyRow();
        C.showToast("Replying from the drawer is disabled in Settings.", "error");
        return;
      }
      var msg;
      if (err && err.status === 409) msg = "not sent: the capture went stale -- wait for a fresh one";
      else if (err && err.status === 404) msg = "not sent: the session is no longer live";
      else msg = "not sent: " + ((err && err.message) || err);
      setDrawerPaneReplyMessage(msg, "error");
      refreshDrawerPaneNow();
    });
  }

  function closeDrawer() {
    C.currentDrawer = null;
    C.drawerRequestSeq++;
    syncDrawerPanePolling(); // no drawer -> stops the pane poll and empties its area
    C.byId("drawer").classList.remove("open");
    C.byId("drawer").setAttribute("aria-hidden", "true");
    C.byId("drawer-backdrop").classList.remove("open");
  }

  // ------------------------------------------------------------------
  // task-93: the theater's task rail
  // ------------------------------------------------------------------
  //
  // The theater is a modal, so opening it puts the drawer -- and with it
  // the description and the acceptance criteria -- behind the backdrop,
  // at exactly the moment they matter most: while reading what the agent
  // is doing, or answering a question it stopped to ask. The rail is a
  // second column carrying that ticket detail, read-only, beside the
  // terminal.
  //
  // Deliberately additive and self-contained, so it can be backed out
  // wholesale (see the task's implementation notes): everything below,
  // its own CSS block in styles.css, its own localStorage key in
  // state.js, the detail stash in drawer.js, and exactly two call lines
  // in openTheater/closeTheater. Nothing in the pane rendering path it
  // sits next to was rewritten.
  //
  // No fetch of its own, ever: the rail renders from what is already in
  // memory -- currentDrawer.summary (the board task), currentDrawer.detail
  // (the drawer's /api/task response) and currentDrawer.branchTask (the
  // same response's branch-side copy, which is what makes the ticks move
  // as the agent works). The single-poller contract is untouched by
  // construction: no timer, no fetch, no poll state is named here.
  //
  // No lifecycle actions either: the theater reads and replies, the
  // drawer acts. Duplicating Spawn/Merge/End across both surfaces is how
  // the two would drift apart.
  var THEATER_RAIL_MIN_VIEWPORT_PX = 1000; // below this the pane would be squeezed under 80 columns

  // Too narrow to show both columns -- the rail collapses itself here
  // regardless of the stored preference, and says so on the control.
  function railViewportTooNarrow() {
    return window.innerWidth < THEATER_RAIL_MIN_VIEWPORT_PX;
  }

  function railIsCollapsed() {
    return !!C.theaterRailCollapsed || railViewportTooNarrow();
  }

  // The ONLY writer of the theater's rail classes and of the rail
  // toggle's label/pressed/disabled state. Like applyDrawerWidth, it
  // touches nothing else.
  function applyTheaterRail() {
    var theater = C.byId("theater");
    var hasRail = !!C.byId("theater-rail");
    var collapsed = railIsCollapsed();
    // Both classes exist only while the rail does, so a closed theater --
    // and a theater with the rail code removed -- carries neither.
    theater.classList.toggle("has-rail", hasRail);
    theater.classList.toggle("rail-collapsed", hasRail && collapsed);
    var btn = C.byId("theater-rail-toggle");
    if (!btn) return;
    var forced = railViewportTooNarrow();
    btn.setAttribute("aria-pressed", collapsed ? "false" : "true");
    btn.disabled = forced;
    btn.textContent = collapsed ? "Show task" : "Hide task";
    btn.title = forced
      ? "The window is too narrow for the task rail (needs " + THEATER_RAIL_MIN_VIEWPORT_PX +
        "px) -- the pane keeps the full width"
      : (collapsed
        ? "Show this task's description and acceptance criteria beside the pane"
        : "Hide the task rail and give the pane the full width");
  }

  function toggleTheaterRail() {
    if (railViewportTooNarrow()) return; // the control is disabled here anyway
    C.theaterRailCollapsed = !C.theaterRailCollapsed;
    C.persistTheaterRailCollapsed(C.theaterRailCollapsed);
    applyTheaterRail();
  }

  // A muted "Status: X" style chip, next to the ones the drawer already
  // uses for labels and milestones.
  function railChip(text, className) {
    return C.h("span", { className: className || "label-chip", text: text });
  }

  function railSection(heading, tag) {
    var section = C.h("div", { className: "theater-rail-section" });
    var head = C.h("h4", { className: "theater-rail-h", text: heading });
    if (tag) head.appendChild(C.h("span", { className: "theater-rail-source", text: tag }));
    section.appendChild(head);
    return section;
  }

  function railAcItem(item) {
    var row = C.h("label", { className: "ac-item" + (item && item.checked ? " checked" : "") });
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!(item && item.checked);
    cb.disabled = true;
    row.appendChild(cb);
    row.appendChild(C.h("span", { text: (item && item.text) || "" }));
    return row;
  }

  // Everything inside the rail, rebuilt from scratch. Cheap, and it means
  // the late-arriving /api/task detail (see refreshTheaterRail) needs no
  // partial-update path of its own.
  function renderTheaterRailBody(rail) {
    C.clearChildren(rail);
    var drawer = C.currentDrawer || {};
    var summary = drawer.summary || {};
    var detail = drawer.detail || null;
    var branch = drawer.branchTask || null;
    var task = detail || summary;

    var head = C.h("div", { className: "theater-rail-head" });
    head.appendChild(C.h("div", {
      className: "theater-rail-id",
      text: (drawer.project || "") + " / " + (drawer.id || "")
    }));
    head.appendChild(C.h("div", { className: "theater-rail-title", text: task.title || summary.title || "(untitled)" }));

    var chips = C.h("div", { className: "theater-rail-chips" });
    chips.appendChild(railChip("Status: " + (summary.status || task.status || "unknown")));
    // The branch can be ahead of main on status too (the agent moves the
    // task to Done on its own branch first) -- say so rather than showing
    // main's status as if it were the whole truth.
    if (branch && (branch.status || "") && (branch.status || "") !== (summary.status || task.status || "")) {
      chips.appendChild(railChip("branch: " + branch.status, "label-chip theater-rail-branch-chip"));
    }
    // task-91: the chip carries the milestone's TITLE, resolved through
    // the project's board data -- `task` here is usually the /api/task
    // detail, which carries the id and no title. The summary fallback is
    // for the window before that detail lands.
    var milestone = C.milestoneChipLabel(drawer.project, task)
      || C.milestoneChipLabel(drawer.project, summary);
    if (milestone) chips.appendChild(railChip(milestone, "milestone-chip"));
    (task.labels || summary.labels || []).forEach(function (lbl) { chips.appendChild(railChip(lbl)); });
    head.appendChild(chips);
    rail.appendChild(head);

    var descSection = railSection("Description");
    if (task.description && String(task.description).trim()) {
      descSection.appendChild(C.h("div", { className: "drawer-text theater-rail-text", text: task.description }));
    } else {
      descSection.appendChild(C.h("div", {
        className: "drawer-empty",
        text: detail ? "No description." : "Loading task details\u2026"
      }));
    }
    rail.appendChild(descSection);

    // The whole point of the rail during a spawn: the ticks the agent is
    // committing on its own branch, not main's stale copy. Fall back to
    // main's when there is no branch to read.
    var branchAc = (branch && branch.acceptanceCriteria) || [];
    var fromBranch = branchAc.length > 0;
    var ac = fromBranch ? branchAc : (task.acceptanceCriteria || []);
    var acSection = railSection("Acceptance Criteria", fromBranch ? "agent branch" : "main");
    acSection.appendChild(C.h("div", {
      className: "theater-rail-hint",
      text: fromBranch
        ? "As committed on task/" + String(drawer.id || "").toLowerCase() + ", unmerged -- these ticks move as the agent works."
        : (detail ? "As committed on the project's own branch." : "Loading task details\u2026")
    }));
    if (ac.length === 0) {
      acSection.appendChild(C.h("div", { className: "drawer-empty", text: detail ? "None." : "" }));
    } else {
      ac.forEach(function (item) { acSection.appendChild(railAcItem(item)); });
    }
    rail.appendChild(acSection);

    rail.appendChild(C.h("div", {
      className: "theater-rail-foot",
      text: "Read-only. Spawn, Merge, Resume and End session stay in the task drawer."
    }));
  }

  // Build the rail and its control. Called from openTheater only, once
  // the pane section is already inside #theater, so the rail simply goes
  // in ahead of it as the first column.
  function renderTheaterRail() {
    var theater = C.byId("theater");
    if (!theater || C.byId("theater-rail")) return;
    var rail = C.h("aside", {
      className: "theater-rail",
      attrs: { id: "theater-rail", "aria-label": "Task detail" }
    });
    renderTheaterRailBody(rail);
    theater.insertBefore(rail, theater.firstChild);
    // The control sits in the pane section's header, in the slot the
    // (theater-hidden) Expand/Narrow toggle occupies in the drawer --
    // the closest existing precedent for this kind of view switch.
    var slot = C.byId("drawer-pane-theater-toggle");
    if (slot && slot.parentNode) {
      slot.parentNode.insertBefore(C.h("button", {
        className: "btn btn-sm theater-rail-toggle",
        attrs: { id: "theater-rail-toggle", type: "button", "aria-pressed": "true" },
        onclick: toggleTheaterRail
      }), slot);
    }
    applyTheaterRail();
  }

  // The exact inverse: both nodes go away, and so do both classes.
  function removeTheaterRail() {
    var rail = C.byId("theater-rail");
    if (rail && rail.parentNode) rail.parentNode.removeChild(rail);
    var btn = C.byId("theater-rail-toggle");
    if (btn && btn.parentNode) btn.parentNode.removeChild(btn);
    applyTheaterRail();
  }

  // The drawer's /api/task response usually lands long before anyone
  // clicks Maximize, but not always -- this lets drawer.js hand the
  // detail over when it arrives. A no-op with no rail on screen.
  function refreshTheaterRail() {
    var rail = C.byId("theater-rail");
    if (!rail) return;
    renderTheaterRailBody(rail);
  }

  // Resizing across the threshold re-decides the automatic collapse (and
  // relabels the control). Idle whenever the rail isn't on screen.
  window.addEventListener("resize", function () {
    if (C.byId("theater-rail")) applyTheaterRail();
  });

  // Third arg is the open task's id: the drawer lands on that task's
  // detail view on the Backlog.md board rather than the board root
  // (task-92). The project chips in board.js pass no id, so a
  // project-level open still opens the root.
  C.byId("drawer-open-board").addEventListener("click", function (e) {
    if (C.currentDrawer) C.openProjectBoard(C.currentDrawer.project, e.currentTarget, C.currentDrawer.id);
  });

  C.byId("drawer-close").addEventListener("click", closeDrawer);
  C.byId("drawer-backdrop").addEventListener("click", closeDrawer);
  C.byId("theater-backdrop").addEventListener("click", closeTheater); // task-73
  // One Escape closes ONE layer, outermost first: settings, then the
  // session theater (task-73), then the drawer -- never two at once.
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (C.settingsOpen) C.closeSettingsModal();
    else if (theaterOpen) closeTheater();
    else if (C.currentDrawer) closeDrawer();
  });

  // ------------------------------------------------------------------
  // Seam: what the other files reach for through window.Centrale.
  // ------------------------------------------------------------------

  C.refreshTheaterRail = refreshTheaterRail;
  C.syncDrawerPanePolling = syncDrawerPanePolling;
  C.updateDrawerPaneAge = updateDrawerPaneAge;
})(window.Centrale = window.Centrale || {});
